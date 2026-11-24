"""`deploy/ci/agent_flow.py` and the `deploy` job's step 7c: the Linux agent
built from this checkout enrols over :443, connects over :8443 with mutual
TLS, verifies and runs a signed `hello`, and the audit chain still verifies
(design 2026-11-23-vps-y-marca-novieri §4, §5; plan 2026-11-23, Task 10).

The live run is CI's. These hold what it asserts, what it may print, and
that the job runs it the way an endpoint would: the agent's own binary, the
name it dials, the token in a 0600 file and never in an argv.
"""

import ast
import importlib.util
import json
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


def test_the_agent_enrols_after_the_claim_and_before_the_last_checks() -> None:
    run = _run(OAUTH_STEP)
    order = [
        run.index("flow --phase open"),
        run.index("docker compose stop api"),
        run.index('/tmp/issue-gateway-cert.py rmm.novieri.com <<<"$gw_token"'),
        run.index("docker compose up -d --wait gateway"),
        run.index("agents --phase token"),
        run.index("docker run -d --name ci-agent"),
        run.index("agents --phase job --hostname ci-agent"),
        run.index('test ! -e "$agent_data/enroll-token"'),
        run.index('if grep -qi keycloak <<<"$running"'),
    ]
    assert order == sorted(order), order


def test_the_agent_dials_the_public_name_and_trusts_only_pebbles_root() -> None:
    """An endpoint's view: rmm.novieri.com is traefik on the stack's network,
    both :443 and :8443 go through it, and the CA bundle the agent sees is
    the root that signed :443 -- no -k, no skip-verify, no other trust."""
    run = _run(OAUTH_STEP)
    start = run.index("docker run -d --name ci-agent")
    command = run[start : run.index("busybox:1.36.1 /usr/local/bin/novieri-agent", start)]
    assert "--network it-suite_default" in command
    assert "--add-host rmm.novieri.com:172.19.240.250" in command
    assert '-v "$pebble_root:/etc/ssl/certs/ca-certificates.crt:ro"' in command
    assert "-e ITS_AGENT_ENROLL_URL=https://rmm.novieri.com" in command
    assert "ITS_AGENT_ENROLL_TOKEN" not in run, "the token travels in a 0600 file, never in env"
    assert "--privileged" not in command and "-p " not in command, "the agent listens on nothing"


def test_the_gateway_issuer_token_is_masked_and_travels_on_stdin() -> None:
    run = _run(OAUTH_STEP)
    assert run.index('echo "::add-mask::$gw_token"') < run.index('<<<"$gw_token"')
    for line in run.splitlines():
        if "$gw_token" in line:
            assert line.strip().startswith('echo "::add-mask::') or '<<<"$gw_token"' in line, line


def test_what_the_agent_held_goes_however_the_step_ends() -> None:
    run = _run(OAUTH_STEP)
    # The one place the whole trap is asserted (test_ci_mcp_flow.py holds only
    # its first half): the container goes before the directory it writes to.
    assert (
        """trap 'rm -f "$init_json" "$RUNNER_TEMP/setup-code" "$RUNNER_TEMP/ci-owner.json"; """
        """docker rm -f ci-agent >/dev/null 2>&1 || true; """
        """rm -rf "$RUNNER_TEMP/agent-data"' EXIT""" in run
    )
    line = next(x.strip() for x in run.splitlines() if "agents --phase job" in x)
    assert line.endswith(
        "|| { docker logs ci-agent; docker compose logs api gateway traefik; exit 1; }"
    )
