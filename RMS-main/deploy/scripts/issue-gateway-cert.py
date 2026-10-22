#!/usr/bin/env python3
"""Issue the gateway's TLS server certificate from the INTERNAL CA, and export
the client CA root the gateway verifies agent certificates against.

Nothing else in the repository did this. `gateway.crt` appeared only in
deploy/README.md, deploy/docker-compose.yml and docs/07-backup-restore.md --
named as a thing that must exist, with no tool to produce it. Until this
script existed, first boot dead-ended: without the certificate the gateway
exits at startup with

    ctx.load_cert_chain(certfile, keyfile)
    FileNotFoundError: [Errno 2] No such file or directory

and with no gateway there is no mTLS listener, so no agent can ever connect.

RUN IT THROUGH THE `gateway` SERVICE -- the only container that mounts BOTH
named volumes this writes to (its-gateway-tls and its-ca). It PROMPTS for the
token, with echo off:

    cd deploy
    docker compose run --rm --no-deps \
      -v "$PWD/scripts/issue-gateway-cert.py:/tmp/issue-gateway-cert.py:ro" \
      --entrypoint python gateway /tmp/issue-gateway-cert.py itms.toeshee.com
    docker compose restart gateway

Unattended (the GO-LIVE.md §5 timer), feed it on STDIN from a 0600 file:

    docker compose run --rm --no-deps -T ... \
      /tmp/issue-gateway-cert.py itms.toeshee.com < /etc/it-suite/gateway-issuer.token

THE TOKEN IS NEVER AN ARGUMENT AND NEVER INTERPOLATED INTO A COMMAND LINE, AND
THAT IS WHAT THIS PARAGRAPH IS ABOUT. The documented invocation used to be
(quoted so it is recognisable, and carrying the exemption marker that tells
`backend/tests/test_deploy_runbook_secrets.py` it is a counter-example rather
than an instruction):

    -e BAO_TOKEN="$(grep '^OPENBAO_TOKEN=' .env | cut -d= -f2-)"      [removed]

which expands the token into the argv of `docker compose`, where ANY user on
the host can read it out of `ps` for as long as the container runs, and where a
shell with history enabled records the command that produced it. That is a
credential leak in the runbook of a script whose own next paragraph is about not
leaking key material to shell history. `BAO_TOKEN` is still read from the
environment when it is already set (systemd `EnvironmentFile=`, a CI secret) --
an environment variable is not argv -- but nothing in the docs constructs it.

USE THE `gateway-issuer` TOKEN, NOT THE ROOT TOKEN. `deploy/openbao/its-app.hcl`
exists precisely so the application does not run as root, and the same reasoning
applies to a certificate issuer: this needs exactly ONE capability,
`update` on `pki/issue/gateway`. `deploy/openbao/gateway-issuer.hcl` grants that
and nothing else, and deploy/README.md §3.4 mints a token for it. Handing this
script the root token instead would mean the credential that can re-key
`transit/job-signing` -- the key authorising every command sent to every
endpoint in the fleet -- is the credential sitting in a file on the host for a
monthly systemd timer to read.

WHY A SCRIPT AND NOT A `bao` ONE-LINER. The private key must never touch the
operator's shell history, a host file, or a `docker cp` staging path
(invariant #3), and the `openbao` container mounts neither volume. This runs
inside the backend image, so the key is generated in the vault, streamed over
the compose network, and written straight to the volume the gateway reads at
mode 0600 -- and nowhere else.

WHAT IT WRITES
  /var/lib/its/tls/gateway.crt   leaf + issuing CA   (ITS_GATEWAY_TLS_CERT)
  /var/lib/its/tls/gateway.key   mode 0600           (ITS_GATEWAY_TLS_KEY)
  /var/lib/its/ca/root.crt       agent client CA     (ITS_GATEWAY_CLIENT_CA)

THE HOSTNAME MATTERS. Pass the exact host agents dial in ITS_GATEWAY_URL
(`wss://<host>:8443/agent`). The agent verifies the gateway certificate
against the CA it pinned at enrollment AND against that hostname; a
certificate issued for the wrong name is a fleet that cannot connect. This is
NOT your public certificate -- :8443 is TCP passthrough and the agent trusts
only the internal CA. The public certificate belongs on :443
(deploy/traefik/dynamic.yml).

RENEWAL -- 365 days, and it does not renew itself. Agent certificates rotate
automatically at two-thirds of life (docs/modules/agent/cert-renewal.md); this
one does not, and a 365-day certificate with no renewal path is exactly the
defect OQ-1 closed for agents. Re-running this script IS the renewal (it
overwrites both files), so put it on a timer well inside the window -- see
"Renewing the gateway certificate" in deploy/README.md, which installs a
monthly systemd timer. Verify what is actually being served with:

    openssl s_client -connect itms.toeshee.com:8443 2>/dev/null \
      | openssl x509 -noout -subject -dates
"""

import getpass
import os
import sys
from pathlib import Path

TLS_DIR = Path("/var/lib/its/tls")
CA_DIR = Path("/var/lib/its/ca")
# 365 days, matching app/core/ca.py's AGENT_CERT_TTL_DAYS and its
# LocalCA.issue_server_cert default -- one expiry cadence for the fleet.
TTL_DAYS = 365


def _write(cert_pem: str, key_pem: str, ca_pem: str, hostname: str) -> None:
    TLS_DIR.mkdir(parents=True, exist_ok=True)
    CA_DIR.mkdir(parents=True, exist_ok=True)

    (TLS_DIR / "gateway.crt").write_text(cert_pem.strip() + "\n")

    key_path = TLS_DIR / "gateway.key"
    key_path.touch(mode=0o600)
    key_path.write_text(key_pem.strip() + "\n")
    key_path.chmod(0o600)

    (CA_DIR / "root.crt").write_text(ca_pem.strip() + "\n")

    print(f"gateway.crt  {TLS_DIR / 'gateway.crt'}  (CN={hostname}, {TTL_DAYS}d)")
    print(f"gateway.key  {key_path}  (mode 0600)")
    print(f"root.crt     {CA_DIR / 'root.crt'}  (agent client CA)")


def _from_openbao(hostname: str, addr: str, token: str) -> int:
    import httpx

    with httpx.Client(timeout=15) as client:
        issued = client.post(
            f"{addr}/v1/pki/issue/gateway",
            headers={"X-Vault-Token": token},
            json={
                "common_name": hostname,
                "alt_names": hostname,
                "ttl": f"{TTL_DAYS * 24}h",
            },
        )
        if issued.status_code == 403:
            print(
                f"403 from {addr}/v1/pki/issue/gateway -- that token cannot issue.\n"
                "Give the `gateway-issuer` token (deploy/README.md step 3.4), not\n"
                "ITS_OPENBAO_TOKEN: the runtime token carries the least-privilege\n"
                "`its-app` policy, which deliberately excludes pki/issue/gateway.",
                file=sys.stderr,
            )
            return 1
        if issued.status_code == 404:
            print(
                "404 -- the `gateway` PKI role does not exist. Run\n"
                "  docker compose up openbao-init\n"
                "first; deploy/openbao/init.sh creates it.",
                file=sys.stderr,
            )
            return 1
        if issued.status_code >= 400:
            print(f"{issued.status_code} from OpenBao: {issued.text}", file=sys.stderr)
            return 1
        data = issued.json()["data"]

        # pki/ca/pem is unauthenticated by design -- a CA certificate is public.
        ca_pem = client.get(f"{addr}/v1/pki/ca/pem").text

    chain = [data["certificate"], *(data.get("ca_chain") or [data["issuing_ca"]])]
    _write("\n".join(c.strip() for c in chain), data["private_key"], ca_pem, hostname)
    print(f"serial       {data['serial_number']}")
    return 0


def _from_local_ca(hostname: str) -> int:
    """ITS_CA_BACKEND=local (development). The same CA object the API uses."""
    import asyncio

    from app.core.ca import LocalCA
    from app.core.config import Settings

    settings = Settings()  # type: ignore[call-arg]
    ca = LocalCA(settings.ca_dir, allow_generate=settings.environment != "production")
    key_pem, cert_pem = ca.issue_server_cert([hostname], ttl_days=TTL_DAYS)
    _write(cert_pem, key_pem, asyncio.run(ca.ca_bundle_pem()), hostname)
    return 0


def _resolve_token() -> str:
    """The issuing token, from anywhere EXCEPT a command line.

    Three sources, in order, and the order is the point:

    1. `BAO_TOKEN` in the environment -- for systemd `EnvironmentFile=` and CI
       secrets. An environment variable is not argv: `/proc/<pid>/environ` is
       readable only by the process owner and root, while `/proc/<pid>/cmdline`
       is world-readable, which is why `ps` can show you every argument of every
       process on the box and not one variable.
    2. STDIN, when it is not a terminal -- `... < /etc/it-suite/gateway-issuer.token`.
       One line, stripped.
    3. A no-echo prompt, when stdin IS a terminal. The interactive runbook path:
       the operator pastes the token, it is not echoed, and no shell records it.

    **`ITS_OPENBAO_TOKEN` IS DELIBERATELY NOT A FALLBACK.** It is set inside the
    `gateway` container, so reading it would make this script "work" by silently
    presenting the runtime application token -- whose `its-app` policy excludes
    `pki/issue/gateway` on purpose -- and the operator would get a 403 they had
    not asked for, from a token they had not chosen, having supplied nothing.
    """
    token = os.environ.get("BAO_TOKEN", "").strip()
    if token:
        return token
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    return getpass.getpass("OpenBao gateway-issuer token: ").strip()


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    hostname = sys.argv[1]

    backend = os.environ.get("ITS_CA_BACKEND", "openbao").strip().lower()
    if backend == "local":
        return _from_local_ca(hostname)

    addr = os.environ.get("ITS_OPENBAO_ADDR", "http://openbao:8200").rstrip("/")
    token = _resolve_token()
    if not token:
        print(
            "no token. Set BAO_TOKEN in the environment, redirect a 0600 token "
            "file to stdin, or run this on a terminal and paste it at the "
            "prompt. Do NOT pass it as an argument: argv is world-readable "
            "through `ps`. See deploy/README.md step 3.4 for minting the "
            "`gateway-issuer` token.",
            file=sys.stderr,
        )
        return 2
    return _from_openbao(hostname, addr, token)


if __name__ == "__main__":
    raise SystemExit(main())
