"""`deploy/ci/agent_flow.py` and the `deploy` job's step 7c: the Linux agent
built from this checkout enrols over :443, connects over :8443 with mutual
TLS, verifies and runs a signed `hello`, and the audit chain still verifies
(design 2026-11-23-vps-y-marca-novieri §4, §5; plan 2026-11-23, Task 10).

The live run is CI's. These hold what it asserts, what it may print, and
that the job runs it the way an endpoint would: the agent's own binary, the
name it dials, the token in a 0600 file and never in an argv.
"""

import ast
import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy/ci/agent_flow.py"
CI = REPO / ".github/workflows/ci.yml"
OAUTH_STEP = "the platform's own OAuth server and MCP door"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_ci_agent_flow", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


agents = _load()


def _steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = yaml.safe_load(CI.read_text())["jobs"]["deploy"]["steps"]
    return steps


def _run(name: str) -> str:
    for step in _steps():
        if str(step.get("name", "")).startswith(name):
            lines = step["run"].splitlines()
            return "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    raise AssertionError(f"the deploy job has no step named {name!r}...")


def test_it_imports_only_the_standard_library_and_its_sibling() -> None:
    """CI runs it with the runner's bare `python3`, next to `mcp_flow.py`."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(SCRIPT.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    assert names <= set(sys.stdlib_module_names) | {"__future__", "mcp_flow"}, names


def test_every_promised_section_is_marked_where_it_is_proven() -> None:
    """A section in `PHASES` is reached only by a `done` call naming it, and
    every `done` names a promised section."""
    marked = {
        node.args[0].value
        for node in ast.walk(ast.parse(SCRIPT.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "done"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    promised = {section for sections in agents.PHASES.values() for section in sections}
    assert marked == promised


def test_a_phase_that_comes_back_without_its_sections_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "owner.json"
    state.write_text(json.dumps({"email": "e", "password": "p", "recovery_codes": ["a", "b"]}))
    monkeypatch.setattr(agents.AgentFlow, "token", lambda self, token_file: None)
    rc = agents.main(
        [
            "--base",
            "https://rmm.example.com",
            "--state-file",
            str(state),
            "--phase",
            "token",
            "--token-file",
            str(tmp_path / "t"),
        ]
    )
    assert rc == 1
    assert "never reached" in capsys.readouterr().out


def test_the_token_is_written_once_0600_and_never_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "enroll-token"
    agents.write_secret(str(path), "tok-123")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert path.read_text() == "tok-123"
    with pytest.raises(FileExistsError):
        agents.write_secret(str(path), "again")
    assert capsys.readouterr().out == ""


class _Answer:
    def __init__(self, status: int, body: Any, path: str = "/x") -> None:
        self.status, self._body, self.path = status, body, path

    def json(self) -> Any:
        return self._body


class _Cookies:
    values = {agents.SESSION_COOKIE: "session"}


class _FakeClient:
    """Answers the token phase's calls; the token it mints is a marker that
    must reach the file and nothing else."""

    cookies = _Cookies()
    TOKEN = "SECRET-ENROLL-TOKEN-0123456789"

    def request(self, method: str, path: str, **kwargs: Any) -> _Answer:
        if path == "/api/v1/auth/login":
            return _Answer(200, {"state": "factor_required"}, path)
        if path == "/api/v1/auth/factor/recovery":
            return _Answer(200, {"state": "signed_in"}, path)
        if path == "/api/v1/clients":
            return _Answer(200, [{"id": "c2", "kind": "internal"}], path)
        if path == "/api/v1/clients/c2/sites":
            return _Answer(200, [{"id": "s1", "name": "Principal"}], path)
        if path == "/api/v1/enrollment-tokens":
            return _Answer(201, {"token": self.TOKEN, "max_uses": 1, "site_id": "s1"}, path)
        raise AssertionError(f"unexpected call {method} {path}")


def test_the_token_phase_never_prints_the_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    owner = {"email": "e", "password": "p", "recovery_codes": ["a", "b"]}
    flow = agents.AgentFlow(_FakeClient(), owner)
    path = tmp_path / "enroll-token"
    flow.token(str(path))
    assert path.read_text() == _FakeClient.TOKEN
    seen = capsys.readouterr()
    assert _FakeClient.TOKEN not in seen.out + seen.err
    assert "a one-use enrollment token, written and not printed" in flow.reached


def test_an_owner_without_two_recovery_codes_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "owner.json"
    state.write_text(json.dumps({"email": "e", "password": "p", "recovery_codes": ["only"]}))
    with pytest.raises(agents.Failed):
        agents.read_owner(str(state))


def test_the_site_is_the_internal_clients_principal() -> None:
    clients = [
        {"id": "c1", "kind": "customer"},
        {"id": "c2", "kind": "internal"},
        {"id": "c3", "kind": "unassigned"},
    ]
    sites = {"c2": [{"id": "s-other", "name": "Annex"}, {"id": "s-main", "name": "Principal"}]}
    assert agents.principal_site(clients, sites.__getitem__) == "s-main"
    with pytest.raises(agents.Failed):
        agents.principal_site([{"id": "c1", "kind": "customer"}], sites.__getitem__)
    with pytest.raises(agents.Failed):
        agents.principal_site([{"id": "c2", "kind": "internal"}], lambda _cid: [])


def test_the_audit_trail_is_found_in_order_and_only_in_order() -> None:
    trail = list(agents.AGENT_TRAIL)
    assert agents.trail_in_order(["auth.login", *trail, "auth.logout"])
    assert agents.trail_in_order([trail[0], "x", trail[1], trail[2], "y", trail[3], trail[4]])
    assert not agents.trail_in_order(trail[:-1])
    assert not agents.trail_in_order([trail[1], trail[0], *trail[2:]])
    assert agents.AGENT_TRAIL == (
        "enrollment.token_created",
        "enrollment.device_enrolled",
        "gateway.connected",
        "jobs.dispatched",
        "jobs.succeeded",
    )


def test_the_trail_is_this_device_and_this_job_not_any_with_the_same_name() -> None:
    """The gateway sends the device an `inventory.collect` of its own on
    connect, so a name-only check is satisfied by a job that is not the
    hello. The trail is the hello's `jobs.*` events (target_id the job's) and
    this device's enrolment and connection (target_id the device's)."""

    def event(n: int, action: str, target: str) -> dict[str, str]:
        return {
            "id": f"06b04ea5-2723-7825-8000-00000000000{n}",
            "action": action,
            "target_id": target,
        }

    events = [
        event(1, "enrollment.token_created", "token-1"),
        event(2, "enrollment.device_enrolled", "dev-1"),
        event(3, "gateway.connected", "dev-1"),
        event(4, "jobs.dispatched", "job-inventory"),
        event(5, "jobs.succeeded", "job-inventory"),
        event(6, "jobs.dispatched", "job-hello"),
    ]
    assert not agents.trail_in_order(agents.trail_for(events, "dev-1", "job-hello"))
    assert agents.trail_in_order(agents.trail_for(events, "dev-1", "job-inventory"))
    events.append(event(7, "jobs.succeeded", "job-hello"))
    assert agents.trail_in_order(agents.trail_for(events, "dev-1", "job-hello"))
    assert not agents.trail_in_order(agents.trail_for(events, "dev-2", "job-hello"))


def test_the_chain_is_read_in_its_own_order_not_by_the_second() -> None:
    """The first live run (2026-11-23) failed here: `ts` is whole seconds, and
    the audit API answers newest first, so a hello dispatched and answered in
    the same second read answered first. The chain's order is its ids'."""
    same_second = "2026-11-24T01:37:22Z"
    newest_first = [
        {
            "id": "06b04ea5-2724-7000-8000-000000000002",
            "ts": same_second,
            "action": "jobs.succeeded",
        },
        {
            "id": "06b04ea5-2723-7825-8000-000000000001",
            "ts": same_second,
            "action": "jobs.dispatched",
        },
    ]
    assert agents.chain_order(newest_first) == ["jobs.dispatched", "jobs.succeeded"]


def test_the_deploy_job_builds_the_agent_it_enrols() -> None:
    names = [str(step.get("name", step.get("uses", ""))) for step in _steps()]
    build = names.index("the Linux agent, built from this checkout")
    assert names[build - 1] == "actions/setup-go@v5"
    assert build < next(i for i, n in enumerate(names) if n.startswith(OAUTH_STEP))
    assert _run("the Linux agent, built") == (
        "CGO_ENABLED=0 go build -trimpath -o dist/novieri-agent ./cmd/agent"
    )


CHECK = (
    '| ./scripts/first-boot-check.sh --tls-cafile "$pebble_root" --connect 127.0.0.1 '
    "--wait-job 180"
)
REFUSED = (
    "agents --phase refused --hostname ci-agent --container ci-agent "
    '--pins-file "$agent_data/pins.json"'
)
#: The device first-boot-check.sh enrolled, by the id its 11c line names:
#: a host that ran it before keeps the earlier agent's device, offline, under
#: the same hostname (coordinator, 2026-11-27).
DEVICE_ID = (
    """device_id="$(sed -n 's/^  ok      step 11c: the agent enrolled as device """
    """\\([0-9a-f-]*\\), .*/\\1/p' "$RUNNER_TEMP/check.log")\""""
)
JOB = 'agents --phase job --hostname "$(hostname)" --device-id "$device_id"'


def test_the_agent_enrols_after_the_claim_and_before_the_last_checks() -> None:
    """first-boot-check.sh is the endpoint since 2026-11-27 (design
    2026-11-27-despliegue-automatizado §5): it starts once the token exists,
    agent_flow.py runs the job once the script says the agent connected, and
    the job waits for the script to finish before its own last checks."""
    run = _run(OAUTH_STEP)
    order = [
        run.index("flow --phase open"),
        run.index("docker compose stop api"),
        run.index('agents --phase token --token-file "$RUNNER_TEMP/enroll-token"'),
        run.index(CHECK),
        run.index("grep -q '^  ok      step 11c:' \"$RUNNER_TEMP/check.log\""),
        run.index(DEVICE_ID),
        run.index(JOB),
        run.index('wait "$check"'),
        run.index("grep -qx 'First boot is complete.' \"$RUNNER_TEMP/check.log\""),
        run.index("docker run -d --name ci-agent"),
        run.index(REFUSED),
        run.index("grep -E 'msg=\"job refused\".*job_type=hello.*reason=bad_signature'"),
        run.index('if grep -qi keycloak <<<"$running"'),
    ]
    assert order == sorted(order), order


def test_the_agent_dials_the_public_name_and_trusts_only_pebbles_root() -> None:
    """An endpoint's view: rmm.novieri.com at 127.0.0.1, where traefik
    publishes both :443 and :8443, and the CA bundle the agent sees is the root
    that signed :443 -- no -k, no skip-verify, no other trust. The script turns
    the two flags into the agent's `--add-host` and its only CA file
    (`test_first_boot_check_script.py` runs it); the token reaches it on
    stdin, never in its environment."""
    run = _run(OAUTH_STEP)
    assert CHECK in run
    assert '{ cat "$RUNNER_TEMP/enroll-token"; echo; } \\' in run
    assert "ITS_AGENT_ENROLL_TOKEN" not in run, "the token travels in a 0600 file, never in env"
    script = (REPO / "deploy/scripts/first-boot-check.sh").read_text()
    start = script.index("  run_args=(-d --name smoke-agent --network host")
    command = script[start : script.index("busybox:1.36.1 /usr/local/bin/novieri-agent", start)]
    bundle = "/etc/ssl/certs/ca-certificates.crt"
    assert f'-v "${{tls_cafile:-{bundle}}}:{bundle}:ro"' in command
    assert '-e "ITS_AGENT_ENROLL_URL=https://$host"' in command
    assert '[ -z "$connect" ] || run_args+=(--add-host "$host:$connect")' in command
    assert "ITS_AGENT_ENROLL_TOKEN" not in script
    assert "--privileged" not in command and "-p " not in command, "the agent listens on nothing"


def test_the_gateway_issuer_token_is_first_boots_and_travels_on_stdin() -> None:
    """The gateway's certificate is first-boot.sh's step 6 since 2026-11-27:
    the gateway-issuer token goes from its 0600 file to issue-gateway-cert.py
    on stdin, as the runbook's step 6 sends it, and the job holds none."""
    assert "gw_token" not in _run(OAUTH_STEP)
    script = (REPO / "deploy/scripts/first-boot.sh").read_text()
    assert 'if ! sudo cat "$token_file" | docker compose run --rm --no-deps -T \\' in script
    line = next(x for x in script.splitlines() if "/tmp/issue-gateway-cert.py \"$host\"" in x)
    assert line.strip() == '--entrypoint python gateway /tmp/issue-gateway-cert.py "$host"; then'


def test_what_the_agent_held_goes_however_the_step_ends() -> None:
    run = _run(OAUTH_STEP)
    # The one place the whole trap is asserted (test_ci_mcp_flow.py holds only
    # its first half): the container goes before the directory it writes to.
    assert (
        """trap 'rm -f "$RUNNER_TEMP/setup-code" "$RUNNER_TEMP/ci-owner.json" """
        """"$RUNNER_TEMP/enroll-token" "$RUNNER_TEMP/check.log"; """
        """docker rm -f smoke-agent ci-agent >/dev/null 2>&1 || true; """
        """rm -rf /tmp/smoke-agent "$RUNNER_TEMP/agent-data"' EXIT""" in run
    )
    line = next(x.strip() for x in run.splitlines() if "agents --phase job" in x)
    assert line.endswith(
        '|| { kill "$check" 2>/dev/null || true; wait "$check" || true; '
        'cat "$RUNNER_TEMP/check.log"; docker compose logs api gateway traefik; exit 1; }'
    )


def test_the_job_goes_to_the_device_first_boot_check_names() -> None:
    """The id is read out of the script's own 11c line, as the script
    prints it (its source, rendered), by the job's own sed."""
    run = _run(OAUTH_STEP)
    assert DEVICE_ID in run and JOB in run
    script = (REPO / "deploy/scripts/first-boot-check.sh").read_text()
    said = next(x for x in script.splitlines() if x.startswith('ok "step 11c: '))
    uuid = "06b04ea5-2723-7825-8000-0000000000aa"
    line = "  ok      " + said[len('ok "') : -1].replace("$device_id", uuid)
    expression = DEVICE_ID.split("sed -n '")[1].split("' ")[0]
    found = subprocess.run(
        ["sed", "-n", expression], input=f"  note    x\n{line}\n", capture_output=True, text=True
    )
    assert found.stdout == uuid + "\n", (line, found.stdout)


def test_the_deploy_job_waits_for_a_healthy_worker() -> None:
    """Finding F7 of the first-boot rehearsal of 2026-11-24 (plan 2026-11-23,
    Task 12): an image built from backend/pyproject.toml without Celery's
    `redis` extra had no `packaging`, and `worker` restart-looped while every
    check of this job stayed green -- the job never started it. Now it waits
    for the worker's own health check (its heartbeat in Redis) on the image
    the job built, after the agent's checks, and shows its log if it fails."""
    run = _run(OAUTH_STEP)
    line = next((x.strip() for x in run.splitlines() if "--wait-timeout 180 worker" in x), "")
    assert line, "the deploy job never waits for the worker"
    assert line == (
        "docker compose up -d --wait --wait-timeout 180 worker "
        "|| { docker compose logs worker; exit 1; }"
    )
    done = run.index("grep -qx 'First boot is complete.' \"$RUNNER_TEMP/check.log\"")
    assert done < run.index(line)
    assert run.index(line) < run.index('if grep -qi keycloak <<<"$running"')


# --------------------------------------------------------------------------- #
# The negative half of invariant 1, end to end (coordinator's ruling Q3,
# plan 2026-11-27, Task 5): an agent made to pin a key the server does not
# hold refuses the signed hello with bad_signature and runs nothing.
# --------------------------------------------------------------------------- #


def test_the_negative_case_runs_on_its_own_agent_as_an_endpoint_would() -> None:
    """Its own agent, enrolled with the second token: first-boot-check.sh
    retired its one at 11f. It dials rmm.novieri.com as traefik's address on
    the stack's network and trusts Pebble's root alone; the token is in its
    0600 file, never in its environment; it listens on nothing."""
    run = _run(OAUTH_STEP)
    assert (
        'agents --phase token --token-file "$RUNNER_TEMP/enroll-token" '
        '--token-file "$agent_data/enroll-token"'
    ) in run
    assert 'echo "::add-mask::$(<"$agent_data/enroll-token")"' in run
    start = run.index("docker run -d --name ci-agent")
    command = run[start : run.index("busybox:1.36.1 /usr/local/bin/novieri-agent", start)]
    assert "--network it-suite_default" in command
    assert "--add-host rmm.novieri.com:172.19.240.250" in command
    assert '-v "$pebble_root:/etc/ssl/certs/ca-certificates.crt:ro"' in command
    assert '-v "$agent_data:/var/lib/novieri-agent"' in command
    assert "ITS_AGENT_ENROLL_TOKEN" not in command
    assert "--privileged" not in command and "-p " not in command
    assert run.index('test ! -e "$agent_data/enroll-token"') < run.index(REFUSED)
    line = next(x.strip() for x in run.splitlines() if x.strip().startswith(REFUSED))
    assert line.endswith(
        "|| { docker logs ci-agent; docker compose logs api gateway traefik; exit 1; }"
    )


def test_the_owner_keeps_a_third_recovery_code_for_it() -> None:
    """One recovery code per sign-in: token, job and refused."""
    source = (REPO / "deploy/ci/mcp_flow.py").read_text()
    assert "self.remember_owner(email, password, list(codes)[:3])" in source


class _TokenClient(_FakeClient):
    """Mints a different token each time, and counts the sign-ins."""

    def __init__(self) -> None:
        self.minted = 0
        self.logins = 0

    def request(self, method: str, path: str, **kwargs: Any) -> _Answer:
        if path == "/api/v1/auth/login":
            self.logins += 1
        if path == "/api/v1/enrollment-tokens":
            self.minted += 1
            body = {"token": f"TOKEN-{self.minted}", "max_uses": 1, "site_id": "s1"}
            return _Answer(201, body, path)
        return super().request(method, path, **kwargs)


def test_one_sign_in_mints_one_one_use_token_per_file(tmp_path: Path) -> None:
    client = _TokenClient()
    owner = {"email": "e", "password": "p", "recovery_codes": ["a", "b", "c"]}
    flow = agents.AgentFlow(client, owner)
    first, second = tmp_path / "one", tmp_path / "two"
    flow.token(str(first), str(second))
    assert (first.read_text(), second.read_text()) == ("TOKEN-1", "TOKEN-2")
    assert client.logins == 1 and client.minted == 2
    for path in (first, second):
        assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_the_distrusted_pin_is_an_ed25519_key_nobody_signs_with() -> None:
    """RFC 8410's SubjectPublicKeyInfo around 32 bytes: the agent parses it as
    it parses the real one (x509.ParsePKIXPublicKey, then the Ed25519 type
    check), and no signature verifies against it. The rest of pins.json stays."""
    pins = {"ca_bundle_pem": "CA", "job_signing_public_key_pem": "OLD"}
    new = agents.distrusted(pins, raw=bytes(range(32)))
    assert new["ca_bundle_pem"] == "CA"
    pem = new["job_signing_public_key_pem"].splitlines()
    assert pem[0] == "-----BEGIN PUBLIC KEY-----" and pem[-1] == "-----END PUBLIC KEY-----"
    der = base64.b64decode("".join(pem[1:-1]))
    assert der == bytes.fromhex("302a300506032b6570032100") + bytes(range(32))
    assert agents.distrusted(pins) != agents.distrusted(pins), "a fresh key each time"
    same = {"job_signing_public_key_pem": new["job_signing_public_key_pem"]}
    with pytest.raises(agents.Failed):
        agents.distrusted(same, raw=bytes(range(32)))


class _RefusedClient(_FakeClient):
    """The refused phase's calls: ci-agent online, a hello that ends STATUS
    with RESULT ("GREETING": the greeting it was sent), and an audit log with
    its dispatch and its ending. Each job answer carries the job's `params`, as
    the api's `JobOut` does: the greeting it was sent is in every answer, run
    or not (the rehearsal of 2026-11-27, Step 7, first green run)."""

    def __init__(self, status: str, result: Any) -> None:
        self.status, self.result = status, result
        self.sent: dict[str, Any] = {}

    def request(self, method: str, path: str, **kwargs: Any) -> _Answer:
        if path == "/api/v1/devices?limit=200":
            device = {"id": "dev-1", "hostname": "ci-agent", "agent_status": "online"}
            return _Answer(200, {"items": [device]}, path)
        if path == "/api/v1/jobs" and method == "POST":
            self.sent = kwargs["body"]
            queued = {"id": "job-1", "signed": True, "status": "queued"}
            return _Answer(201, {**queued, "params": self.sent["params"]}, path)
        if path == "/api/v1/jobs/job-1":
            result = self.result
            if result == "GREETING":
                result = {"greeting": self.sent["params"]["message"]}
            body = {
                "id": "job-1",
                "signed": True,
                "status": self.status,
                "params": self.sent["params"],
                "result": result,
            }
            return _Answer(200, body, path)
        if path == "/api/v1/audit/verify":
            return _Answer(200, {"ok": True, "broken_at": None, "count": 9}, path)
        if path == "/api/v1/audit?limit=200":
            ending = "jobs.refused" if self.status == "refused" else "jobs.succeeded"
            trail = [
                ("enrollment.token_created", "token-1"),
                ("enrollment.device_enrolled", "dev-1"),
                ("gateway.connected", "dev-1"),
                ("jobs.dispatched", "job-1"),
                (ending, "job-1"),
            ]
            items = [
                {"id": f"06b04ea5-2723-7825-8000-00000000000{n}", "action": a, "target_id": t}
                for n, (a, t) in enumerate(trail, 1)
            ]
            return _Answer(200, {"items": items}, path)
        return super().request(method, path, **kwargs)


class _FakeDocker:
    """`docker logs` counts one `gateway connected` per start."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.starts = 1

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        if args[0] == "start":
            self.starts += 1
        if args[0] == "logs":
            return 'level=INFO msg="gateway connected"\n' * self.starts
        return ""


THREE = {"email": "e", "password": "p", "recovery_codes": ["a", "b", "c"]}


def _refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: _RefusedClient) -> Any:
    monkeypatch.setattr(agents.time, "sleep", lambda _s: None)
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"ca_bundle_pem": "CA", "job_signing_public_key_pem": "OLD"}))
    docker = _FakeDocker()
    flow = agents.AgentFlow(client, THREE, docker=docker)
    flow.refused("ci-agent", "ci-agent", str(pins), 5.0)
    return flow, docker, json.loads(pins.read_text())


def test_a_hello_refused_with_bad_signature_and_not_run_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow, docker, pins = _refused(
        tmp_path, monkeypatch, _RefusedClient("refused", {"refused": "bad_signature"})
    )
    assert flow.reached == set(agents.PHASES["refused"])
    assert [call[0] for call in docker.calls] == ["logs", "stop", "start", "logs"]
    assert pins["ca_bundle_pem"] == "CA" and pins["job_signing_public_key_pem"] != "OLD"


def test_a_hello_the_agent_ran_fails_the_negative_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The falsifier: an agent that stopped verifying runs it, and answers."""
    with pytest.raises(agents.Failed, match="the hello it does not trust ended succeeded"):
        _refused(tmp_path, monkeypatch, _RefusedClient("succeeded", "GREETING"))


def test_a_refusal_for_another_reason_fails_the_negative_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(agents.Failed, match="refused, but not for its signature"):
        _refused(tmp_path, monkeypatch, _RefusedClient("refused", {"refused": "expired"}))


def test_the_negative_case_needs_its_own_recovery_code(tmp_path: Path) -> None:
    owner = {"email": "e", "password": "p", "recovery_codes": ["a", "b"]}
    flow = agents.AgentFlow(_RefusedClient("refused", {}), owner, docker=_FakeDocker())
    with pytest.raises(agents.Failed, match="no third recovery code"):
        flow.refused("ci-agent", "ci-agent", str(tmp_path / "pins.json"), 1.0)


# --------------------------------------------------------------------------- #
# The good hello goes to the device first-boot-check.sh enrolled, by its id
# (coordinator, 2026-11-27): a host that ran the script before keeps the
# earlier agent's device under the same hostname, and the first device of a
# name is not this run's.
# --------------------------------------------------------------------------- #


class _JobClient(_RefusedClient):
    """Two devices named `vm`: an earlier run's, listed first, and this
    run's. The hello succeeds with its greeting, and the trail is dev-2's."""

    def __init__(self, earlier_status: str = "offline") -> None:
        super().__init__("succeeded", "GREETING")
        self.earlier_status = earlier_status

    def request(self, method: str, path: str, **kwargs: Any) -> _Answer:
        if path == "/api/v1/devices?limit=200":
            devices = [
                {"id": "dev-1", "hostname": "vm", "agent_status": self.earlier_status},
                {"id": "dev-2", "hostname": "vm", "agent_status": "online"},
            ]
            return _Answer(200, {"items": devices}, path)
        if path == "/api/v1/audit?limit=200":
            trail = [
                ("enrollment.token_created", "token-2"),
                ("enrollment.device_enrolled", "dev-2"),
                ("gateway.connected", "dev-2"),
                ("jobs.dispatched", "job-1"),
                ("jobs.succeeded", "job-1"),
            ]
            items = [
                {"id": f"06b04ea5-2723-7825-8000-00000000000{n}", "action": a, "target_id": t}
                for n, (a, t) in enumerate(trail, 1)
            ]
            return _Answer(200, {"items": items}, path)
        return super().request(method, path, **kwargs)


@pytest.mark.parametrize("earlier_status", ["offline", "online"])
def test_the_hello_goes_to_the_device_of_the_id_not_the_first_of_its_name(
    monkeypatch: pytest.MonkeyPatch, earlier_status: str
) -> None:
    monkeypatch.setattr(agents.time, "sleep", lambda _s: None)
    client = _JobClient(earlier_status)
    flow = agents.AgentFlow(client, THREE)
    flow.job("vm", "dev-2", 5.0)
    assert client.sent["device_id"] == "dev-2"
    assert flow.reached == set(agents.PHASES["job"])


def test_a_device_of_another_hostname_is_not_the_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents.time, "sleep", lambda _s: None)
    flow = agents.AgentFlow(_JobClient(), THREE)
    with pytest.raises(agents.Failed, match="no online device other-host dev-2"):
        flow.device("other-host", 0.0, "dev-2")


def test_a_name_two_devices_share_is_no_device_without_an_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agents.time, "sleep", lambda _s: None)
    flow = agents.AgentFlow(_JobClient("online"), THREE)
    with pytest.raises(agents.Failed, match="2 devices are named vm"):
        flow.device("vm", 5.0)


def test_the_job_phase_needs_the_device_id(tmp_path: Path) -> None:
    state = tmp_path / "owner.json"
    state.write_text(json.dumps(THREE))
    base = ["--base", "https://rmm.example.com", "--state-file", str(state), "--phase", "job"]
    with pytest.raises(SystemExit):
        agents.main([*base, "--hostname", "vm"])
    with pytest.raises(SystemExit):
        agents.main([*base, "--hostname", "vm", "--device-id", ""])
