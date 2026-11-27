"""The CI's live check of an endpoint: the Linux agent built from this
checkout enrols over the stack's front door, connects to the gateway on :8443
with mutual TLS, verifies and runs a signed `hello`, and the audit chain still
verifies (design 2026-11-23-vps-y-marca-novieri §4 and §5; plan 2026-11-23,
Task 10). And the negative half of invariant 1, end to end (coordinator's
ruling Q3, plan 2026-11-27, Task 5): the same binary, made to pin a
job-signing key the server does not hold, refuses a signed `hello` with
`bad_signature` and never runs it.

It drives only the console's own API, signed in as the owner `mcp_flow.py`
created (its `--state-file`: the address, the password and three recovery
codes, mode 0600): one recovery code per phase, because a sign-in spends a
second factor and a TOTP code may not be replayed inside its window. The
agents themselves are started by the `deploy` job between the phases.

    python3 ci/agent_flow.py --base https://rmm.novieri.com \\
        --resolve rmm.novieri.com:127.0.0.1 --cafile "$pebble_root" \\
        --state-file "$RUNNER_TEMP/ci-owner.json" \\
        --phase token --token-file "$RUNNER_TEMP/enroll-token" \\
        --token-file "$RUNNER_TEMP/agent-data/enroll-token"
    python3 ci/agent_flow.py ... --phase job --hostname "$(hostname)" \\
        --device-id "$device_id"
    python3 ci/agent_flow.py ... --phase refused --hostname ci-agent \\
        --container ci-agent --pins-file "$RUNNER_TEMP/agent-data/pins.json"

`token`: for each `--token-file`, a one-use, one-hour enrollment token for the
organization's own client's Principal site, written to a NEW file, 0600
(`O_EXCL`), and never printed. `job`: the device the agent enrolled, online --
the one `--device-id` names, which first-boot-check.sh's step 11c line gives,
because a host that ran that script before keeps the earlier agent's device
under the same hostname; a
`hello` dispatched to it, signed by the server and answered by the agent with
its greeting; and the audit chain verified, with the enrolment, the dispatch
and the result in it. `refused`: the device online; its agent stopped, the
job-signing key it pinned at enrollment (`pins.json`) replaced by one nobody
holds the private half of, and started again; a `hello` the server signs as
always, refused with `bad_signature` and no greeting; and the chain verified,
with the dispatch and the refusal in it and no success. It prints what held,
never a token, a password or a code.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_flow import (  # noqa: E402
    SESSION_COOKIE,
    Client,
    Failed,
    Unreachable,
    check,
    ok,
    parse_resolve,
    pin_resolution,
)

#: What each phase has to have proven before it may say "every assertion
#: held"; `main` fails a phase that came back with any of these unmarked
#: (the same rule as `mcp_flow.PHASES`). `backend/tests/test_ci_agent_flow.py`
#: holds the lists and every mark.
PHASES: dict[str, tuple[str, ...]] = {
    "token": (
        "signed in with a recovery code",
        "the Principal site of the organization's own client",
        "a one-use enrollment token, written and not printed",
    ),
    "job": (
        "signed in with a recovery code",
        "the agent's device, online",
        "a server-signed hello, answered by the agent",
        "the audit chain verifies, with the agent in it",
    ),
    "refused": (
        "signed in with a recovery code",
        "the agent's device, online",
        "the agent's pinned job-signing key, one the server does not hold",
        "a server-signed hello, refused with bad_signature and never run",
        "the audit chain verifies, with the refusal in it",
    ),
}
#: The audit actions the job phase must find, in this order, after the token.
AGENT_TRAIL = (
    "enrollment.token_created",
    "enrollment.device_enrolled",
    "gateway.connected",
    "jobs.dispatched",
    "jobs.succeeded",
)
#: The refused phase's, for its device and its hello.
REFUSAL_TRAIL = (
    "enrollment.device_enrolled",
    "gateway.connected",
    "jobs.dispatched",
    "jobs.refused",
)
TERMINAL = frozenset({"succeeded", "failed", "expired", "refused", "cancelled"})
#: SubjectPublicKeyInfo for Ed25519 (RFC 8410), before the 32 key bytes.
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


def write_secret(path: str, value: str) -> None:
    """A NEW file, 0600 from its creation (`O_EXCL`); nothing printed."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)


def distrusted(pins: dict[str, Any], raw: bytes | None = None) -> dict[str, Any]:
    """`pins` with `job_signing_public_key_pem` replaced by an Ed25519 key
    nobody holds the private half of: 32 random bytes in the RFC 8410
    wrapper. The agent parses it as it parses the real one
    (`agent/internal/enroll/enroll.go`, `parseEd25519PublicKey`), and no
    signature the server makes verifies against it."""
    key = raw if raw is not None else os.urandom(32)
    check(len(key) == 32, "an Ed25519 public key is 32 bytes")
    body = base64.b64encode(ED25519_SPKI_PREFIX + key).decode("ascii")
    pem = "-----BEGIN PUBLIC KEY-----\n" + body + "\n-----END PUBLIC KEY-----\n"
    check(pem != pins.get("job_signing_public_key_pem"), "the new key is the old one")
    return {**pins, "job_signing_public_key_pem": pem}


def run_docker(*args: str) -> str:
    """One `docker` command on the runner; its output, both streams."""
    done = subprocess.run(["docker", *args], capture_output=True, text=True, check=True)
    return done.stdout + done.stderr


def read_owner(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        owner = json.load(handle)
    check(
        isinstance(owner, dict)
        and isinstance(owner.get("recovery_codes"), list)
        and len(owner["recovery_codes"]) >= 2,
        "the owner's state file holds no two recovery codes",
    )
    return owner


def principal_site(
    clients: list[dict[str, Any]], sites_of: Callable[[str], list[dict[str, Any]]]
) -> str:
    """The organization's own client (`kind` internal) and its site named
    Principal (`app/services/clients.py`, `PRINCIPAL_SITE`)."""
    internal = [c for c in clients if c.get("kind") == "internal"]
    check(len(internal) == 1, f"{len(internal)} internal clients, not one")
    sites = [s for s in sites_of(internal[0]["id"]) if s.get("name") == "Principal"]
    check(len(sites) == 1, "the internal client has no site named Principal")
    return str(sites[0]["id"])


def chain_order(events: list[dict[str, Any]]) -> list[str]:
    """The actions oldest first, in the chain's own order: by `id`, a UUIDv7
    (`docs/07-backup-restore.md`, "`ORDER BY id` is the correct ordering").
    Not by `ts`, which is whole seconds: a job dispatched and answered in the
    same second would read answered first (measured on 2026-11-23)."""
    return [event["action"] for event in sorted(events, key=lambda event: str(event["id"]))]


def trail_for(events: list[dict[str, Any]], device_id: str, job_id: str) -> list[str]:
    """The chain's actions, oldest first, that belong to THIS agent and THIS
    hello: the device's enrolment and connection (`target_id` the device's) and
    the hello's `jobs.*` events (`target_id` the job's). The gateway sends the
    device an `inventory.collect` of its own on connect, so a check on names
    alone is satisfied by a job that is not the hello."""
    target = {
        "enrollment.device_enrolled": device_id,
        "gateway.connected": device_id,
        "jobs.dispatched": job_id,
        "jobs.succeeded": job_id,
    }
    return [
        event["action"]
        for event in sorted(events, key=lambda event: str(event["id"]))
        if event["action"] == "enrollment.token_created"
        or target.get(event["action"]) == event.get("target_id")
        or (event["action"] == "jobs.refused" and event.get("target_id") == job_id)
    ]


def trail_in_order(actions: list[str], expected: tuple[str, ...] = AGENT_TRAIL) -> bool:
    """`expected` appears in `actions` as a subsequence, oldest first."""
    rest = iter(actions)
    return all(any(action == want for action in rest) for want in expected)


class AgentFlow:
    def __init__(
        self,
        client: Client,
        owner: dict[str, Any],
        docker: Callable[..., str] = run_docker,
    ) -> None:
        self.http = client
        self.owner = owner
        self.docker = docker
        self.reached: set[str] = set()

    def done(self, section: str) -> None:
        self.reached.add(section)

    def get(self, path: str) -> Any:
        answer = self.http.request("GET", path)
        check(answer.status == 200, f"GET {answer.path} answered {answer.status}")
        return answer.json()

    def sign_in(self, code_index: int) -> None:
        login = self.http.request(
            "POST",
            "/api/v1/auth/login",
            body={"email": self.owner["email"], "password": self.owner["password"]},
            console=True,
        )
        check(login.status == 200, f"the sign-in answered {login.status}")
        check(login.json().get("state") == "factor_required", "the sign-in asked for no factor")
        factor = self.http.request(
            "POST",
            "/api/v1/auth/factor/recovery",
            body={"code": self.owner["recovery_codes"][code_index]},
            console=True,
        )
        check(factor.status == 200, f"the recovery code answered {factor.status}")
        check(factor.json().get("state") == "signed_in", "the recovery code did not sign in")
        check(SESSION_COOKIE in self.http.cookies.values, "no session cookie after the factor")
        ok("signed in: password and a recovery code")
        self.done("signed in with a recovery code")

    def token(self, *token_files: str) -> None:
        """One one-use token per file: one sign-in, however many agents."""
        check(len(token_files) >= 1, "no --token-file to write a token to")
        self.sign_in(0)
        site = principal_site(
            self.get("/api/v1/clients"), lambda cid: self.get(f"/api/v1/clients/{cid}/sites")
        )
        ok("the site: the internal client's Principal")
        self.done("the Principal site of the organization's own client")
        for token_file in token_files:
            made = self.http.request(
                "POST",
                "/api/v1/enrollment-tokens",
                body={"name": "ci agent", "expires_in_hours": 1, "max_uses": 1, "site_id": site},
                console=True,
            )
            check(made.status == 201, f"POST /api/v1/enrollment-tokens answered {made.status}")
            body = made.json()
            check(
                body.get("max_uses") == 1 and body.get("site_id") == site, "not the token asked for"
            )
            write_secret(token_file, body["token"])
            ok("an enrollment token: one use, one hour, written 0600 and not printed")
        self.done("a one-use enrollment token, written and not printed")

    def device(self, hostname: str, wait_s: float, device_id: str | None = None) -> str:
        """The device named HOSTNAME -- the one of DEVICE_ID, when given --
        once it is online. Without an id, a name two devices share is no
        device: the first of them may be an earlier agent's."""
        deadline = time.monotonic() + wait_s
        seen = "no device"
        while True:
            items = self.get("/api/v1/devices?limit=200")["items"]
            mine = [d for d in items if d.get("hostname") == hostname]
            if device_id is not None:
                mine = [d for d in mine if str(d.get("id")) == device_id]
            check(len(mine) <= 1, f"{len(mine)} devices are named {hostname}, and no id was given")
            if mine:
                seen = f"agent_status={mine[0].get('agent_status')}"
                if mine[0].get("agent_status") == "online":
                    return str(mine[0]["id"])
            if time.monotonic() > deadline:
                which = f"{hostname} {device_id}" if device_id else hostname
                raise Failed(f"no online device {which} after {wait_s:.0f} s ({seen})")
            time.sleep(2)

    def distrust(self, container: str, pins_file: str, wait_s: float) -> None:
        """Stop the agent, replace the key it pinned, start it, and wait for it
        to connect again (one more `gateway connected` in its log)."""
        before = self.docker("logs", container).count("gateway connected")
        self.docker("stop", container)
        with open(pins_file, encoding="utf-8") as handle:
            pins = json.load(handle)
        with open(pins_file, "w", encoding="utf-8") as handle:
            json.dump(distrusted(pins), handle)
        self.docker("start", container)
        deadline = time.monotonic() + wait_s
        while self.docker("logs", container).count("gateway connected") <= before:
            if time.monotonic() > deadline:
                raise Failed(f"{container} did not connect again in {wait_s:.0f} s")
            time.sleep(2)
        ok("the agent pins a job-signing key the server does not hold, and is connected again")
        self.done("the agent's pinned job-signing key, one the server does not hold")

    def job(self, hostname: str, device_id: str, wait_s: float) -> None:
        self.sign_in(1)
        device_id = self.device(hostname, wait_s, device_id)
        ok(f"the device {hostname} enrolled and is online")
        self.done("the agent's device, online")

        message = "novieri-ci-" + secrets.token_hex(4)
        made = self.http.request(
            "POST",
            "/api/v1/jobs",
            body={"type": "hello", "device_id": device_id, "params": {"message": message}},
            console=True,
        )
        check(made.status == 201, f"POST /api/v1/jobs answered {made.status}")
        job = made.json()
        check(job.get("signed") is True, "the job carries no server signature")
        deadline = time.monotonic() + wait_s
        while job.get("status") not in TERMINAL:
            if time.monotonic() > deadline:
                raise Failed(f"the hello job is still {job.get('status')} after {wait_s:.0f} s")
            time.sleep(1)
            job = self.get(f"/api/v1/jobs/{job['id']}")
        check(job["status"] == "succeeded", f"the hello job ended {job['status']}")
        check(job.get("result") == {"greeting": message}, "the hello result is not its greeting")
        ok("hello: server-signed, answered by the agent with its greeting")
        self.done("a server-signed hello, answered by the agent")

        verified = self.get("/api/v1/audit/verify")
        check(
            verified.get("ok") is True and verified.get("broken_at") is None, "the chain is broken"
        )
        events = self.get("/api/v1/audit?limit=200")["items"]
        check(
            trail_in_order(trail_for(events, device_id, str(job["id"]))),
            f"the audit log lacks, for this device and this hello, in order: "
            f"{', '.join(AGENT_TRAIL)}",
        )
        ok(f"the audit chain verifies over {verified['count']} events, the agent's included")
        self.done("the audit chain verifies, with the agent in it")

    def refused(self, hostname: str, container: str, pins_file: str, wait_s: float) -> None:
        check(
            len(self.owner["recovery_codes"]) >= 3,
            "the owner's state file holds no third recovery code for this phase",
        )
        self.sign_in(2)
        device_id = self.device(hostname, wait_s)
        ok(f"the device {hostname} enrolled and is online")
        self.done("the agent's device, online")
        self.distrust(container, pins_file, wait_s)

        message = "novieri-ci-refused-" + secrets.token_hex(4)
        made = self.http.request(
            "POST",
            "/api/v1/jobs",
            body={"type": "hello", "device_id": device_id, "params": {"message": message}},
            console=True,
        )
        check(made.status == 201, f"POST /api/v1/jobs answered {made.status}")
        job = made.json()
        check(job.get("signed") is True, "the job carries no server signature")
        deadline = time.monotonic() + wait_s
        while job.get("status") not in TERMINAL:
            if time.monotonic() > deadline:
                raise Failed(f"the hello job is still {job.get('status')} after {wait_s:.0f} s")
            time.sleep(1)
            job = self.get(f"/api/v1/jobs/{job['id']}")
        check(job["status"] == "refused", f"the hello it does not trust ended {job['status']}")
        check(
            job.get("result") == {"refused": "bad_signature"},
            f"refused, but not for its signature: {job.get('result')}",
        )
        # In its result: every answer about a job echoes its params, the
        # greeting it was sent included (`JobOut.params`), run or not.
        check(
            message not in json.dumps(job.get("result")),
            "the refused hello's greeting came back: it ran",
        )
        ok("hello: server-signed, refused by the agent with bad_signature, never run")
        self.done("a server-signed hello, refused with bad_signature and never run")

        verified = self.get("/api/v1/audit/verify")
        check(
            verified.get("ok") is True and verified.get("broken_at") is None, "the chain is broken"
        )
        events = self.get("/api/v1/audit?limit=200")["items"]
        check(
            trail_in_order(trail_for(events, device_id, str(job["id"])), REFUSAL_TRAIL),
            f"the audit log lacks, for this device and this hello, in order: "
            f"{', '.join(REFUSAL_TRAIL)}",
        )
        check(
            not any(
                e["action"] == "jobs.succeeded" and e.get("target_id") == str(job["id"])
                for e in events
            ),
            "the audit log says the refused hello succeeded",
        )
        ok(f"the audit chain verifies over {verified['count']} events, the refusal included")
        self.done("the audit chain verifies, with the refusal in it")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="the public entrypoint")
    parser.add_argument("--cafile", help="the root the entrypoint's certificate chains to")
    parser.add_argument(
        "--resolve", type=parse_resolve, action="append", default=[], help="NAME:ADDRESS"
    )
    parser.add_argument("--state-file", required=True, help="the owner's sign-in (mcp_flow.py)")
    parser.add_argument("--phase", choices=tuple(PHASES), required=True)
    parser.add_argument(
        "--token-file", action="append", default=[], help="phase token: where a token goes"
    )
    parser.add_argument("--hostname", help="phases job and refused: the agent's hostname")
    parser.add_argument("--device-id", help="phase job: the device first-boot-check.sh enrolled")
    parser.add_argument("--container", help="phase refused: the agent's container")
    parser.add_argument("--pins-file", help="phase refused: the agent's pins.json")
    parser.add_argument("--wait-s", type=float, default=120.0)
    args = parser.parse_args(argv)
    if args.phase == "token" and not args.token_file:
        parser.error("--phase token needs --token-file")
    if args.phase == "job" and not (args.hostname and args.device_id):
        parser.error("--phase job needs --hostname and --device-id")
    if args.phase == "refused" and not (args.hostname and args.container and args.pins_file):
        parser.error("--phase refused needs --hostname, --container and --pins-file")
    pin_resolution(dict(args.resolve))
    try:
        flow = AgentFlow(Client(args.base, args.cafile), read_owner(args.state_file))
        if args.phase == "token":
            flow.token(*args.token_file)
        elif args.phase == "job":
            flow.job(args.hostname, args.device_id, args.wait_s)
        else:
            flow.refused(args.hostname, args.container, args.pins_file, args.wait_s)
    except Failed as exc:
        print(f"FAIL  {exc}")
        return 1
    except Unreachable as exc:
        print(f"UNREACHABLE  {exc}", file=sys.stderr)
        return 2
    missing = [section for section in PHASES[args.phase] if section not in flow.reached]
    if missing:
        print(f"FAIL  phase {args.phase} never reached: {'; '.join(missing)}")
        return 1
    print(f"agent_flow: phase {args.phase}: every assertion held")
    return 0


if __name__ == "__main__":
    sys.exit(main())
