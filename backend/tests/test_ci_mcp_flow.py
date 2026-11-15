"""The pure parts of `deploy/ci/mcp_flow.py`, the live CI check of the
platform's own OAuth server and its MCP door (own authentication phase 4,
plan 2026-11-15, Task 7). The live run is CI's (`deploy` job); these hold the
helpers that decide what it asserts and what it is allowed to print.

Until phase 4 this file tested `deploy/keycloak/ci_mcp_flow.py`, which drove
Keycloak's registration and sign-in; that script went with Keycloak.
"""

import ast
import base64
import hashlib
import hmac
import importlib.util
import json
import re
import sys
from email.message import Message
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy/ci/mcp_flow.py"
CI = REPO / ".github/workflows/ci.yml"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_ci_mcp_flow", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


flow = _load()


def _part(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _jwt(claims: dict[str, Any], header: dict[str, Any] | None = None) -> str:
    return f"{_part(header or {'alg': 'ES256', 'typ': 'at+jwt'})}.{_part(claims)}.c2ln"


def test_it_imports_nothing_outside_the_standard_library() -> None:
    """CI runs it with the runner's bare `python3`: no venv, no pip."""
    tree = ast.parse(SCRIPT.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    assert names <= set(sys.stdlib_module_names) | {"__future__"}, names


def test_the_pkce_pair_is_s256_of_a_valid_verifier() -> None:
    verifier, challenge = flow.pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert set(verifier) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected.rstrip(b"=").decode()
    assert "=" not in challenge
    assert flow.pkce_pair()[0] != verifier


def test_a_token_is_read_whatever_the_padding_and_refused_when_it_is_not_a_jwt() -> None:
    for org in ("a", "ab", "abc"):  # three payload lengths mod 4
        header, claims = flow.jwt_parts(_jwt({"org": org}))
        assert claims["org"] == org and header["alg"] == "ES256"
    for bad in ("not-a-jwt", "a.b", "e30.bm90LWpzb24.c2ln"):
        with pytest.raises(flow.Failed):
            flow.jwt_parts(bad)


def test_the_audience_must_be_exactly_the_resource_as_a_string() -> None:
    """Design §5.3 and the api's verifier: a string, never a list holding it."""
    resource = "https://localhost/mcp"
    assert flow.exact_audience({"aud": resource}, resource)
    for wrong in ([resource], [resource, "it-suite"], "it-suite", None, "https://other/mcp"):
        assert not flow.exact_audience({"aud": wrong}, resource), wrong
    # `audiences()` went in fix round 1 (review minor 8): nothing called it.
    assert not hasattr(flow, "audiences")


def test_totp_matches_rfc_6238s_own_vectors() -> None:
    """RFC 6238 Appendix B, SHA-1, eight digits: the ASCII secret
    "12345678901234567890", base32-encoded as an otpauth URI carries it."""
    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    assert flow.totp(secret, 59, digits=8) == "94287082"
    assert flow.totp(secret, 1111111109, digits=8) == "07081804"
    assert flow.totp(secret, 1234567890, digits=8) == "89005924"
    # RFC 4226 Appendix D: the same key at counter 1, six digits.
    assert flow.totp(secret, 30) == "287082"


def test_the_totp_secret_comes_out_of_the_otpauth_uri() -> None:
    uri = "otpauth://totp/ITMS:owner%40ci.invalid?secret=JBSWY3DPEHPK3PXP&issuer=ITMS"
    assert flow.totp_secret(uri) == "JBSWY3DPEHPK3PXP"
    for bad in ("https://totp/x?secret=A", "otpauth://hotp/x?secret=A", "otpauth://totp/x"):
        with pytest.raises(flow.Failed):
            flow.totp_secret(bad)


def test_the_challenge_is_read_into_its_parameters() -> None:
    header = 'Bearer resource_metadata="https://h/.well-known/x", error="invalid_token"'
    assert flow.challenge_params(header) == {
        "resource_metadata": "https://h/.well-known/x",
        "error": "invalid_token",
    }
    with pytest.raises(flow.Failed):
        flow.challenge_params('Basic realm="x"')


def test_the_authorization_request_is_an_mcp_clients() -> None:
    query = dict(
        pair.split("=", 1)
        for pair in flow.authorize_query("itms_x", "c" * 43, "st", "https://h/mcp").split("&")
    )
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"
    assert query["resource"] == "https%3A%2F%2Fh%2Fmcp"
    assert query["redirect_uri"] == "http%3A%2F%2F127.0.0.1%3A33418%2Fcallback"


def test_a_callback_is_matched_exactly_not_by_prefix() -> None:
    good = flow.REDIRECT_URI + "?code=c&state=s&iss=https%3A%2F%2Fh"
    assert flow.callback_params(good) == {"code": "c", "state": "s", "iss": "https://h"}
    for bad in (
        "http://127.0.0.1:33418/callback.evil?code=c",
        "http://127.0.0.1:33419/callback?code=c",
        "https://127.0.0.1:33418/callback?code=c",
        "http://localhost:33418/callback?code=c",
    ):
        with pytest.raises(flow.Failed):
            flow.callback_params(bad)


def test_a_tampered_token_changes_only_its_signature() -> None:
    token = _jwt({"sub": "x"})
    changed = flow.tampered(token)
    assert changed != token
    assert changed.split(".")[:2] == token.split(".")[:2]


def test_the_foreign_token_is_shaped_like_keycloaks_and_signed_by_nobody_known() -> None:
    token = flow.foreign_token("https://localhost", "someone")
    header, claims = flow.jwt_parts(token)
    assert header["alg"] == "HS256"
    assert claims["iss"] == "https://localhost/realms/it-suite"
    assert (claims["aud"], claims["azp"], claims["typ"]) == ("it-suite", "it-suite-web", "Bearer")
    assert not flow.exact_audience(claims, "https://localhost/mcp")
    # A fresh random key each time: two tokens never share a signature.
    assert token.split(".")[2] != flow.foreign_token("https://localhost", "someone").split(".")[2]
    signing_input = ".".join(token.split(".")[:2]).encode()
    guessed = hmac.new(b"", signing_input, hashlib.sha256).digest()
    assert flow._b64url(guessed) != token.split(".")[2]


def test_only_non_secret_claim_values_are_printed() -> None:
    claims = {"aud": "https://h/mcp", "sub": "u-1", "jti": "j", "fam": "f", "scope": "mcp"}
    shown = flow.describe_claims(claims)
    assert 'aud="https://h/mcp"' in shown and 'scope="mcp"' in shown
    for secret_value in ("u-1", '"j"', '"f"'):
        assert secret_value not in shown


def test_no_secret_reaches_a_printed_line() -> None:
    """Nothing the script prints -- `print`, `ok`, or a `check` message that
    becomes the FAIL line -- names a token, a code, a password or a secret."""
    secret_names = {
        "password",
        "secret",
        "setup_code",
        "code",
        "verifier",
        "access",
        "refresh",
        "token",
    }
    tree = ast.parse(SCRIPT.read_text())
    leaks = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {"print", "ok", "check"}:
            continue
        printed = node.args[1:] if node.func.id == "check" else node.args
        for arg in printed:
            for inner in ast.walk(arg):
                name = (
                    inner.id
                    if isinstance(inner, ast.Name)
                    else inner.attr
                    if isinstance(inner, ast.Attribute)
                    else None
                )
                if name in secret_names:
                    leaks.append(f"line {node.lineno}: {name}")
    assert leaks == [], leaks


def test_the_cookie_store_keeps_the_session_and_forgets_it_when_cleared() -> None:
    set_it = Message()
    set_it["Set-Cookie"] = "__Host-itms_session=abc; Path=/; Secure; HttpOnly; SameSite=Strict"
    clear = Message()
    clear["Set-Cookie"] = '__Host-itms_session=""; Max-Age=0; Path=/; Secure; HttpOnly'
    jar = flow.Cookies()
    jar.take(set_it)
    assert jar.header() == "__Host-itms_session=abc"
    jar.take(clear)
    assert jar.header() is None


def test_it_speaks_the_mcp_revision_the_backend_tests_speak() -> None:
    from mcp.types import LATEST_PROTOCOL_VERSION

    assert flow.PROTOCOL_VERSION == LATEST_PROTOCOL_VERSION


def test_the_former_keycloak_paths_are_all_checked() -> None:
    """The closed phase checks each path Keycloak's router answered; an
    emptied list would make that check pass without checking anything."""
    paths = flow.FORMER_KEYCLOAK_PATHS
    assert len(paths) >= 6 and len(set(paths)) == len(paths), paths
    for prefix in ("/realms/", "/resources/", "/js/", "/admin/"):
        assert any(path.startswith(prefix) for path in paths), prefix


#: The paths the committed router model sends nowhere near an identity
#: provider (Task 6's `tests/test_public_surface.py::FORMER_IDENTITY_PATHS`,
#: whose docstring says CI sends "the same paths" through the real Traefik).
ROUTER_MODEL_PATHS = (
    "/realms/it-suite/.well-known/openid-configuration",
    "/realms/it-suite/protocol/openid-connect/auth",
    "/realms/it-suite/protocol/openid-connect/token",
    "/realms/it-suite/clients-registrations/openid-connect",
    "/realms/it-suite/clients-registrations/default",
    "/realms/it-suite/account",
    "/resources/abc/login/itms/css/itms.css",
    "/js/keycloak.js",
    "/admin/master/console/",
)


def test_the_live_check_sends_the_paths_the_router_model_checks() -> None:
    """The same nine, in the same order, as the router model's. Where that
    test file defines its tuple, it is read from it, so the two cannot drift
    apart after Tasks 6 and 7 meet."""
    assert flow.FORMER_KEYCLOAK_PATHS == ROUTER_MODEL_PATHS
    surface = REPO / "backend/tests/test_public_surface.py"
    for node in ast.walk(ast.parse(surface.read_text())):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "FORMER_IDENTITY_PATHS"
            for target in node.targets
        ):
            assert ast.literal_eval(node.value) == flow.FORMER_KEYCLOAK_PATHS


def _deploy_step(name: str) -> str:
    """The `run` of the deploy job's step whose name starts with NAME, read
    as YAML and without its comment lines: a commented-out command is not one
    the job runs."""
    steps = yaml.safe_load(CI.read_text())["jobs"]["deploy"]["steps"]
    for step in steps:
        if str(step.get("name", "")).startswith(name):
            lines = step["run"].splitlines()
            return "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    raise AssertionError(f"the deploy job has no step named {name!r}...")


OAUTH_STEP = "the platform's own OAuth server and MCP door"


def test_ci_runs_both_phases_through_traefik_on_a_real_openbao() -> None:
    """The deploy job runs the closed phase before the claim and the open
    phase after the api restarted with the opt-in, against the stack's own
    OpenBao; and it names Keycloak only to assert that nothing of it is
    defined or ran (design §10, falsifier 11)."""
    run = _deploy_step(OAUTH_STEP)
    assert "python3 ci/mcp_flow.py" in run
    closed = run.index("flow --phase closed")
    opened = run.index("flow --phase open")
    restart = run.index('echo "ITS_MCP_REGISTRATION=open" >> .env')
    assert closed < restart < opened, "closed, then the opt-in and a restart, then open"
    assert "bao operator init -key-shares=1 -key-threshold=1 -format=json" in run
    assert "docker compose run --rm openbao-init" in run
    named = [line.strip() for line in run.splitlines() if re.search("keycloak", line, re.I)]
    assert len(named) == 2, named
    assert all(line.startswith("if grep -qi keycloak <<<") for line in named), named


#: Lines that hold a vault secret without an argv: an assignment, and bash's
#: own printf and echo, which run in the shell and start no process.
SHELL_BUILTINS = ("unseal=", "root=", "printf '%s' ", 'echo "OPENBAO_TOKEN=')


def test_no_secret_of_the_vault_travels_in_an_argv() -> None:
    """The unseal key and the root token reach `bao` on stdin, never as an
    argument a process listing on a shared runner would show; the files that
    held them go however the step ends."""
    run = _deploy_step(OAUTH_STEP)
    for secret in ('"$unseal"', '"$root"', "$root"):
        argv = [
            line.strip()
            for line in run.splitlines()
            if secret in line
            and "<<<" not in line
            and "::add-mask::" not in line
            and not line.strip().startswith(SHELL_BUILTINS)
        ]
        assert argv == [], argv
    assert """trap 'rm -f "$init_json" "$RUNNER_TEMP/setup-code"' EXIT""" in run


def test_ci_restores_the_old_identity_providers_database_from_a_pre_upgrade_backup() -> None:
    """Operator decision K1 (2026-11-15), live: a nightly backup leaves the
    `keycloak` database out, the pre-upgrade one keeps it, and a forced
    restore of that one brings it back."""
    run = _deploy_step("the old identity provider's database")
    nightly = run.index('./scripts/backup.sh "$nightly"')
    assert 'test ! -e "$nightly/keycloak.dump"' in run
    pre = run.index('ITS_BACKUP_OLD_IDENTITY_DB=1 ./scripts/backup.sh "$pre_upgrade"')
    restore = run.index('ITS_RESTORE_FORCE=1 ./scripts/restore.sh "$pre_upgrade"')
    assert nightly < pre < restore
    assert 'test -s "$pre_upgrade/keycloak.dump"' in run
    assert run.index("DROP DATABASE keycloak WITH (FORCE)") < restore
    assert "realm_probe" in run[restore:]


def test_the_k1_backups_are_this_runs_own_and_go_with_the_step() -> None:
    """Fix round 1 (review minors 2 and 3). A fixed `/tmp/ci-nightly` on a
    reused runner could hold last run's `keycloak.dump` and answer for a
    backup that never wrote one; each directory comes from `mktemp -d` under
    the runner's temp, and the trap removes them however the step ends. And
    the restore is proven by a row this run wrote, not by a table's name."""
    run = _deploy_step("the old identity provider's database")
    assert "/tmp/" not in run
    for name in ("nightly", "pre_upgrade"):
        assert f'{name}="$(mktemp -d "$RUNNER_TEMP/' in run, name
    assert """trap 'rm -rf "$nightly" "$pre_upgrade" "$restore_log"' EXIT""" in run
    assert run.index("trap ") < run.index("./scripts/backup.sh")
    restore = run.index('ITS_RESTORE_FORCE=1 ./scripts/restore.sh "$pre_upgrade"')
    wrote = run.index("INSERT INTO realm_probe VALUES ('$marker')")
    assert wrote < run.index('./scripts/backup.sh "$nightly"')
    assert "SELECT x FROM realm_probe" in run[restore:]
    assert '= "$marker" ]' in run[restore:]


def test_the_deploy_job_keeps_the_name_branch_protection_may_require() -> None:
    """Fix round 1 (review minor 12): a required status check is matched by
    the job's name, and a renamed job leaves the PR waiting on the old one."""
    job = yaml.safe_load(CI.read_text())["jobs"]["deploy"]
    assert job["name"] == "deploy (compose first boot · app_user privileges)"


def test_openbao_is_waited_for_until_it_answers_not_initialised() -> None:
    """Fix round 1 (review minor 4): `bao status` exits 2 for any sealed
    vault, initialised or not; `operator init -status` exits 2 only for one
    that is not initialised, which is the fact `operator init` needs."""
    run = _deploy_step(OAUTH_STEP)
    waited = run.index("docker compose exec -T openbao bao operator init -status")
    assert waited < run.index("bao operator init -key-shares=1")
    assert "bao status" not in run


def test_the_api_is_waited_for_until_it_answers_itself() -> None:
    """The deploy job's first run with phase 4 (run 43746102957) failed with
    "/mcp without a token answered 405": the wait passed while the api was
    still starting. Traefik leaves out a container whose health check has not
    passed yet, so the console's catch-all router answered the metadata path
    with its HTML page and a 200, and the flow's POST /mcp reached nginx. The
    wait takes only the api's own JSON for that path -- and so does the wait
    after the api is recreated for the opt-in."""
    run = _deploy_step(OAUTH_STEP)
    start = run.index("wait_for_api() {")
    wait = run[start : run.index("\n}\n", start)]
    assert "https://localhost/.well-known/oauth-authorization-server" in wait
    assert "application/json" in wait
    loop = wait[wait.index("for _ in"):]
    metadata = loop.index("oauth-authorization-server")
    assert "application/json" in loop[metadata:], "checked on the metadata answer itself"
    assert run.count("wait_for_api\n") >= 2, "both waits: the first start and the recreate"


def test_a_failed_phase_leaves_the_stacks_logs() -> None:
    """Fix round 1 (review minor 5). The masks are in place before the first
    call that can print them (the install code is the last secret read)."""
    run = _deploy_step(OAUTH_STEP)
    logs = "|| { docker compose logs api web traefik; exit 1; }"
    for phase in ("closed", "open"):
        line = next(x.strip() for x in run.splitlines() if f"flow --phase {phase}" in x)
        assert line.endswith(logs), line
    assert run.index('echo "::add-mask::$(<"$RUNNER_TEMP/setup-code")"') < run.index(
        "flow --phase open"
    )


def test_the_files_that_hold_a_secret_are_the_runners_alone() -> None:
    """The throwaway .env holds the vault's root token and the its-app token
    from the moment they are written: it is 0600 first, as the runbook's step 1
    makes it. And the install code is masked before anything can print it --
    the api logs it once when it is created (`app/core/setup_code.py`)."""
    run = _deploy_step(OAUTH_STEP)
    assert "umask 077" in run
    assert run.index("chmod 600 .env") < run.index('echo "OPENBAO_TOKEN=$root"')
    masked = run.index('echo "::add-mask::$(<"$RUNNER_TEMP/setup-code")"')
    copied = run.index('cat /var/lib/its/secrets/setup-code > "$RUNNER_TEMP/setup-code"')
    assert copied < masked < run.index("flow --phase open")


# --------------------------------------------------------------------------- #
# A phase that checked nothing is not a pass (own authentication phase 4,
# Task 7). CI reads only the exit code, so a phase that came back early -- a
# stray `return`, a call lost to an edit, an emptied list -- has to exit 1
# rather than print "every assertion held" over the sections it never ran.
# --------------------------------------------------------------------------- #


def test_each_phase_promises_what_the_deploy_job_is_there_to_prove() -> None:
    """The closed phase: discovery, registration refused, the old paths. The
    open phase: the AI app's whole path -- dynamic registration, a sign-in
    and consent with a fresh factor, the code exchange with PKCE S256, `/mcp`,
    the refresh and its reuse ending the grant -- and a token shaped like
    Keycloak's refused at `/mcp` and at the console, and one `/mcp` has just
    accepted refused at the console too (1c(a), fix round 1)."""
    assert flow.PHASES["closed"] == (
        "discovery",
        "registration refused",
        "the former Keycloak paths fall to the console",
    )
    assert flow.PHASES["open"] == (
        "signed in with a fresh factor",
        "MCP turned on",
        "discovery",
        "dynamic registration",
        "consent",
        "code exchange with PKCE S256",
        "/mcp",
        "a tampered token refused at /mcp",
        "a Keycloak-shaped token refused at /mcp",
        "a Keycloak-shaped token refused at the console",
        "an MCP access token refused at the console",
        "refresh",
        "refresh reuse ends the grant",
        "code reuse ends the grant",
        "revocation ends the grant",
        "signing out everywhere ends the grant",
    )


def test_every_promised_section_is_marked_where_it_is_proven() -> None:
    """Each section a phase promises is marked by a `self.done(...)` in the
    script, and nothing is marked that no phase promises: a section with no
    mark could never be reached, and a mark no phase reads proves nothing."""
    tree = ast.parse(SCRIPT.read_text())
    marked = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "done"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert marked == set(flow.PHASES["closed"]) | set(flow.PHASES["open"])


@pytest.mark.parametrize("phase", ["closed", "open"])
def test_a_phase_that_comes_back_without_its_sections_fails(
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    code = tmp_path / "setup-code"
    code.write_text("not-a-real-code\n")
    argv = ["--base", "https://localhost", "--phase", phase]
    if phase == "open":
        argv += ["--setup-code-file", str(code)]
    sections = flow.PHASES[phase]

    def nothing(self: object, *args: object) -> None:
        return None

    monkeypatch.setattr(flow.Flow, phase, nothing)
    assert flow.main(argv) == 1
    out = capsys.readouterr().out
    assert out.startswith(f"FAIL  phase {phase} never reached: "), out
    assert "every assertion held" not in out
    assert all(section in out for section in sections), out

    def all_but_the_last(self: object, *args: object) -> None:
        for section in sections[:-1]:
            self.done(section)  # type: ignore[attr-defined]

    monkeypatch.setattr(flow.Flow, phase, all_but_the_last)
    assert flow.main(argv) == 1
    assert capsys.readouterr().out == f"FAIL  phase {phase} never reached: {sections[-1]}\n"

    def every_one(self: object, *args: object) -> None:
        for section in sections:
            self.done(section)  # type: ignore[attr-defined]

    monkeypatch.setattr(flow.Flow, phase, every_one)
    assert flow.main(argv) == 0
    assert capsys.readouterr().out == f"mcp_flow: phase {phase}: every assertion held\n"


def test_the_closed_phase_refuses_an_empty_list_of_former_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checked before the first request: an emptied list fails at once rather
    than passing a loop of zero checks."""
    monkeypatch.setattr(flow, "FORMER_KEYCLOAK_PATHS", ())
    with pytest.raises(flow.Failed, match="no former Keycloak path"):
        flow.Flow(flow.Client("https://localhost", None)).closed()


def test_the_open_phase_will_not_start_without_the_install_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What the Keycloak-era test of the same purpose held
    (`test_the_open_run_requires_everything_it_signs_in_with`): a run that
    cannot sign in stops at its arguments, before any request."""
    with pytest.raises(SystemExit) as stopped:
        flow.main(["--base", "https://localhost", "--phase", "open"])
    assert stopped.value.code == 2
    assert "--phase open needs --setup-code-file" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        flow.main(["--base", "https://localhost"])


# --------------------------------------------------------------------------- #
# Fix round 1. A grant is shown working before it is shown ended (review
# IMPORTANT-1), an ended grant's refresh token is dead too (design §10,
# falsifier 6), and what the flow accepts as an answer is read, not grepped.
# --------------------------------------------------------------------------- #


def _calls_in_open() -> list[tuple[int, str, str]]:
    """`(line, method, first argument as source)` of every `self.<method>(...)`
    call in `Flow.open`, in source order."""
    tree = ast.parse(SCRIPT.read_text())
    flow_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Flow")
    method = next(n for n in flow_class.body if isinstance(n, ast.FunctionDef) and n.name == "open")
    calls = [
        (node.lineno, node.func.attr, ast.unparse(node.args[0]))
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and node.args
    ]
    return sorted(calls)


def _lines(calls: list[tuple[int, str, str]], method: str, arg: str) -> list[int]:
    return [line for line, name, first in calls if name == method and first == arg]


#: Each section that ends a grant, and the grant (`Flow.open`'s name) it ends.
ENDED = {
    "refresh reuse ends the grant": "rotated",
    "code reuse ends the grant": "second",
    "revocation ends the grant": "third",
    "signing out everywhere ends the grant": "fourth",
}


def test_the_grants_the_open_phase_ends_are_these_four() -> None:
    refused = {
        first.removesuffix(".access")
        for _line, name, first in _calls_in_open()
        if name == "refused_at_mcp" and first.endswith(".access")
    }
    assert refused == set(ENDED.values())


@pytest.mark.parametrize("section", list(ENDED))
def test_a_grant_is_shown_working_before_it_is_shown_ended(section: str) -> None:
    """A refusal at `/mcp` proves a grant ended only if the same token was
    accepted there first: a token that never worked is refused with nothing
    having ended it, and an RFC 7009 revocation answers 200 for a token that
    was never valid (§2.2). A control placed after the act that ends the
    grant would fail live, not pass, so the order checked here is enough."""
    grant, calls = ENDED[section], _calls_in_open()
    accepted = _lines(calls, "reaches_mcp", grant)
    refused = _lines(calls, "refused_at_mcp", f"{grant}.access")
    marked = _lines(calls, "done", repr(section))
    assert accepted and refused and marked, (accepted, refused, marked)
    assert accepted[0] < refused[0] < marked[0]


#: The sections after which an ended grant's refresh token is presented.
FAMILY = {"refresh reuse ends the grant": "rotated", "revocation ends the grant": "third"}


@pytest.mark.parametrize("section", list(FAMILY))
def test_an_ended_grants_refresh_token_is_dead_too(section: str) -> None:
    """Design §10 falsifier 6, "revoca su familia": after a reuse, the
    refresh token the rotation issued is refused; after a revocation, the
    token the app revoked."""
    grant, calls = FAMILY[section], _calls_in_open()
    refused = _lines(calls, "refused_at_mcp", f"{grant}.access")
    dead = _lines(calls, "refused_refresh", grant)
    marked = _lines(calls, "done", repr(section))
    assert refused and dead and marked, (refused, dead, marked)
    assert refused[0] < dead[0] < marked[0]


def _response(status: int, body: object, media: str = "application/json") -> Any:
    headers = Message()
    headers["Content-Type"] = media
    return flow.Response("/x", status, headers, json.dumps(body).encode())


class _Answers:
    """Stands in for `Client`: every request gets the next answer."""

    def __init__(self, *answers: Any) -> None:
        self.base = "https://localhost"
        self.cookies = flow.Cookies()
        self.answers = list(answers)

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        return self.answers.pop(0)


GRANT = flow.Grant("itms_x", "not-an-access-token", "not-a-refresh-token")
INITIALIZED: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "protocolVersion": flow.PROTOCOL_VERSION,
        "capabilities": {},
        "serverInfo": {"name": "itms", "version": "1"},
    },
}


def test_mcp_is_reached_only_by_initializes_own_reply() -> None:
    """Review minor 10: the JSON-RPC reply to this request, with initialize's
    result -- not any body that happens to contain the word `protocolVersion`.

    The revision is the one asked for or an earlier one the server supports
    (MCP: "Otherwise, the server MUST respond with another protocol version it
    supports"). Live, the api answers `2025-11-25` to `2026-07-28`: the SDK
    (mcp 2.3.0) negotiates `initialize` among its handshake revisions, whose
    latest is 2025-11-25, and 2026-07-28 has no `initialize` of its own."""
    for revision in (flow.PROTOCOL_VERSION, "2025-11-25"):
        answer = {**INITIALIZED, "result": {**INITIALIZED["result"], "protocolVersion": revision}}
        reached = flow.Flow(_Answers(_response(200, answer)))
        reached.reaches_mcp(GRANT, "a token")
        assert "/mcp" in reached.reached
    result = INITIALIZED["result"]
    for wrong in (
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad protocolVersion"}},
        {"jsonrpc": "2.0", "id": 2, "result": result},
        {"jsonrpc": "1.0", "id": 1, "result": result},
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "id": 1, "result": {**result, "protocolVersion": "2099-01-01"}},
        {"jsonrpc": "2.0", "id": 1, "result": {**result, "protocolVersion": "latest"}},
        {"jsonrpc": "2.0", "id": 1, "result": {**result, "serverInfo": {}}},
        {"jsonrpc": "2.0", "id": 1, "result": {**result, "capabilities": None}},
        [INITIALIZED],
    ):
        with pytest.raises(flow.Failed):
            flow.Flow(_Answers(_response(200, wrong))).reaches_mcp(GRANT, "a token")
    with pytest.raises(flow.Failed):
        streamed = _response(200, INITIALIZED, "text/event-stream")
        flow.Flow(_Answers(streamed)).reaches_mcp(GRANT, "a token")
    with pytest.raises(flow.Failed):
        flow.Flow(_Answers(_response(401, INITIALIZED))).reaches_mcp(GRANT, "a token")


def test_the_console_refuses_with_its_own_sign_in_refusal() -> None:
    """Review minor 1: the console door's 401 is `auth_required`, the one the
    console sends to the sign-in page -- not any 401."""
    flow.Flow(
        _Answers(_response(401, {"detail": "d", "code": "auth_required"}))
    ).refused_at_the_console("a-token", "a token")
    for status, body in (
        (401, {"detail": "d"}),
        (401, {"detail": "d", "code": "csrf"}),
        (200, {"email": "owner@ci.invalid"}),
    ):
        with pytest.raises(flow.Failed):
            flow.Flow(_Answers(_response(status, body))).refused_at_the_console("t", "a token")


def test_a_dead_refresh_token_is_refused_as_invalid_grant() -> None:
    dead = {"error": "invalid_grant", "error_description": "the refresh token is not valid"}
    flow.Flow(_Answers(_response(400, dead))).refused_refresh(GRANT, "a dead refresh token")
    for status, body in (
        (200, {"access_token": "x", "refresh_token": "y"}),
        (400, {"error": "invalid_request", "error_description": "x"}),
        (401, dead),
    ):
        with pytest.raises(flow.Failed):
            flow.Flow(_Answers(_response(status, body))).refused_refresh(GRANT, "a token")
