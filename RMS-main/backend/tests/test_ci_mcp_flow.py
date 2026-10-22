"""The pure parts of `deploy/keycloak/ci_mcp_flow.py`, the live CI check of the
MCP sign-in path. The live run is CI's (`deploy` job); these hold the helpers
that decide what it asserts and what it is allowed to print."""

import ast
import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy/keycloak/ci_mcp_flow.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_ci_mcp_flow", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


flow = _load()


def _jwt(claims: dict) -> str:
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


def test_it_imports_nothing_outside_the_standard_library() -> None:
    """CI runs it with the runner's bare `python3`: no venv, no pip."""
    tree = ast.parse(SCRIPT.read_text())
    names = set()
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


def test_claims_are_decoded_whatever_the_padding() -> None:
    for org in ("a", "ab", "abc"):  # three payload lengths mod 4
        assert flow.jwt_claims(_jwt({"org": org}))["org"] == org
    with pytest.raises(flow.Failed):
        flow.jwt_claims("not-a-jwt")


def test_audiences_accepts_a_string_or_a_list() -> None:
    assert flow.audiences({"aud": "x"}) == ["x"]
    assert flow.audiences({"aud": ["x", "y"]}) == ["x", "y"]
    assert flow.audiences({}) == []


def test_the_audience_must_be_exactly_the_resource() -> None:
    resource = "https://itms.example.test/mcp"
    assert flow.exact_audience({"aud": resource}, resource)
    assert flow.exact_audience({"aud": [resource]}, resource)
    for wrong in ([resource, "account"], [resource, "it-suite"], [], ["https://other/mcp"]):
        assert not flow.exact_audience({"aud": wrong}, resource), wrong
    assert not flow.exact_audience({}, resource)


def test_only_non_secret_claim_values_are_printed() -> None:
    claims = {
        "aud": "https://itms.example.test/mcp",
        "org": "acme",
        "sub": "5f1c-person-id",
        "sid": "session-handle",
        "email": "person@example.test",
        "nonce": "n-0S6_WzA2Mj",
    }
    text = flow.describe_claims(claims)
    assert '"https://itms.example.test/mcp"' in text and '"acme"' in text
    for hidden in ("5f1c-person-id", "session-handle", "person@example.test", "n-0S6_WzA2Mj"):
        assert hidden not in text
    # ...but the NAMES are there, which is what the service-account check reads.
    for name in ("sub", "sid", "email", "nonce"):
        assert name in text.split()


def test_a_callback_is_matched_exactly_not_by_prefix() -> None:
    redirect = "http://127.0.0.1:33418/callback"
    assert flow.callback_params(f"{redirect}?code=c&state=s", redirect) == {
        "code": "c",
        "state": "s",
    }
    for elsewhere in (
        "http://127.0.0.1:33418/callback.evil?code=c",
        "http://127.0.0.1:33419/callback?code=c",
        "https://127.0.0.1:33418/callback?code=c",
    ):
        assert not flow.is_callback(elsewhere, redirect)
        with pytest.raises(flow.Failed):
            flow.callback_params(elsewhere, redirect)


LOGIN_PAGE = """<html><head><title>Sign in to ITMS</title></head><body>
<form id="kc-form-login" method="post"
  action="https://localhost/realms/it-suite/login-actions/authenticate?session_code=a&amp;\
execution=b&amp;client_id=c">
  <input id="username" name="username" type="text" value="">
  <input id="password" name="password" type="password">
  <input type="hidden" id="id-hidden-input" name="credentialId">
  <input type="checkbox" name="rememberMe">
  <button name="login" type="submit" value="Sign in">Sign in</button>
</form></body></html>"""

CONSENT_PAGE = """<form class="form-actions" method="POST"
  action="/realms/it-suite/login-actions/consent?x=1">
  <input type="hidden" name="code" value="consent-code">
  <input name="accept" id="kc-login" type="submit" value="Yes">
  <input name="cancel" id="kc-cancel" type="submit" value="No">
</form>"""


def test_the_login_form_is_read_with_its_action_unescaped() -> None:
    (form,) = flow.parse_forms(LOGIN_PAGE)
    assert form.method == "post"
    assert form.action.endswith("session_code=a&execution=b&client_id=c")
    assert form.fields == {"username": "", "password": "", "credentialId": ""}
    assert form.submits == {"login": "Sign in"}
    assert flow.page_title(LOGIN_PAGE) == "Sign in to ITMS"


def test_the_consent_form_keeps_accept_and_cancel_apart() -> None:
    (form,) = flow.parse_forms(CONSENT_PAGE)
    assert form.fields == {"code": "consent-code"}
    assert form.submits == {"accept": "Yes", "cancel": "No"}


def test_the_open_run_requires_everything_it_signs_in_with() -> None:
    with pytest.raises(SystemExit):
        flow.parse_args(["--base", "https://localhost", "--resource", "https://h/mcp"])
    closed = flow.parse_args(["--base", "https://localhost/", "--expect-registration-closed"])
    assert closed.base == "https://localhost"


def test_the_console_token_must_have_the_shape_every_console_door_requires() -> None:
    """I-4. The api admits a console token only with `azp == "it-suite-web"`
    and `typ == "Bearer"` (`app/core/security.py`), so the live flow proves
    the real sign-in produces exactly that, not only the audience."""
    resource = "https://itms.example.test/mcp"
    good = {"aud": ["it-suite"], "azp": "it-suite-web", "typ": "Bearer"}
    assert flow.check_console_token(good, resource) == ["it-suite"]

    for wrong, says in (
        ({**good, "azp": "someone-else"}, "azp"),
        ({k: v for k, v in good.items() if k != "azp"}, "azp"),
        ({**good, "typ": "ID"}, "typ"),
        ({k: v for k, v in good.items() if k != "typ"}, "typ"),
        ({**good, "aud": ["account"]}, "aud"),
        ({**good, "aud": ["it-suite", resource]}, "MCP resource"),
    ):
        with pytest.raises(flow.Failed, match=says):
            flow.check_console_token(wrong, resource)


def test_ci_proves_the_url_alone_does_not_open_registration() -> None:
    """Operator decision, 2026-10-15: the live step runs keycloak-init with the
    console URL and WITHOUT the opt-in, and proves registration closed against
    Keycloak, before it opts in and proves it open."""
    ci = (SCRIPT.parents[2] / ".github/workflows/ci.yml").read_text()
    step = ci[ci.index("- name: MCP — live registration") :]
    step = step[: step.index("- name: teardown")]

    url = step.index("export ITS_PUBLIC_CONSOLE_URL=")
    opt_in = step.index("export ITS_MCP_REGISTRATION=open")
    assert url < opt_in
    between = step[url:opt_in]
    assert "flow --expect-registration-closed" in between
    assert "until ITS_MCP_REGISTRATION=open" in between
    assert "'registration:removed:trusted-hosts'" in step[opt_in:]


def test_the_consent_page_is_read_for_the_mcp_line_in_each_language() -> None:
    """The live flow proves Keycloak renders the scope's `${itmsMcpConsent}`
    from the itms theme's bundle, in the page's language -- not the raw key,
    which is what an unresolved message shows."""
    en = (
        "<li><span>Act as you in ITMS: everything your role allows, including running "
        "actions on managed computers</span></li>"
    )
    es = (
        "<li><span>Actuar como t&uacute; en ITMS: todo lo que permite tu rol, incluido "
        "ejecutar acciones en equipos gestionados</span></li>"
    )
    assert flow.consent_page_says(en, "en")
    assert flow.consent_page_says(es, "es")
    assert not flow.consent_page_says(es, "en")
    assert not flow.consent_page_says("<li><span>itmsMcpConsent</span></li>", "en")
