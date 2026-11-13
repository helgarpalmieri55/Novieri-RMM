"""The OAuth server's pure pieces (autenticación propia plan 2026-11-06, Task 1).

What is pinned here, each with its source:

- the issuer is the console's https address and nothing else (RFC 8414 §2,
  RFC 9207 §2), and its endpoints hang off it;
- the registration switch reads `ITS_MCP_REGISTRATION=open` exactly;
- an error body is RFC 6749 §5.2's, ASCII, and never cached (§5.1);
- a redirect is ASCII (RFC 3986 §2), https or http to the machine itself
  (MCP [S30]), without a fragment (RFC 6749 §3.1.2) or a response parameter
  in its query (OAuth 2.1-17 §2.3), and matches exactly but for a loopback
  port (RFC 8252 §7.3; OAuth 2.1-17 §4.1.1);
- the answer adds to the redirect's own query without re-encoding it;
- PKCE is S256, with RFC 7636 Appendix B's own vector.
"""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.core.auth.oauth import errors, pkce, policy, redirects
from app.core.auth.oauth.errors import OAuthError, Problem


def _settings(url: str | None, registration: str = "") -> SimpleNamespace:
    return SimpleNamespace(public_console_url=url, mcp_registration=registration)


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://itms.example.com", "https://itms.example.com"),
        ("https://itms.example.com/", "https://itms.example.com"),
        ("  https://itms.example.com  ", "https://itms.example.com"),
        ("https://itms.example.com:8443", "https://itms.example.com:8443"),
        ("http://itms.example.com", None),
        ("https://itms.example.com/console", None),
        ("https://itms.example.com/?x=1", None),
        ("https://user@itms.example.com", None),
        ("https://itms.example.com:99999", None),
        ("", None),
        (None, None),
    ],
)
def test_the_issuer_is_the_consoles_https_address(url: str | None, expected: str | None) -> None:
    assert policy.issuer(_settings(url)) == expected


def test_the_endpoints_hang_off_the_issuer() -> None:
    settings = _settings("https://itms.example.com/")
    assert policy.endpoint(settings, policy.TOKEN_PATH) == "https://itms.example.com/oauth/token"
    assert policy.endpoint(settings, policy.METADATA_PATH) == (
        "https://itms.example.com/.well-known/oauth-authorization-server"
    )
    assert policy.endpoint(_settings(None), policy.TOKEN_PATH) is None


@pytest.mark.parametrize(
    ("value", "open_"),
    [("open", True), (" open ", True), ("OPEN", False), ("yes", False), ("", False)],
)
def test_registration_opens_only_on_the_exact_word(value: str, open_: bool) -> None:
    assert policy.registration_open(_settings("https://x.example", value)) is open_


def test_an_ipv6_address_is_counted_by_its_64() -> None:
    import ipaddress

    assert policy.network_of(ipaddress.ip_address("203.0.113.9")) == "ip:203.0.113.9"
    assert policy.network_of(ipaddress.ip_address("2001:db8:1:2::5")) == "net:2001:db8:1:2::/64"
    assert policy.network_of(ipaddress.ip_address("2001:db8:1:2:ffff::1")) == (
        "net:2001:db8:1:2::/64"
    )
    assert policy.network_of(None) == "ip:unknown"


def test_a_display_name_keeps_text_and_nothing_else() -> None:
    assert policy.display_name("  Cla\u0000ude\n  Desktop\t ") == "Claude Desktop"
    assert policy.display_name("\u0007\u0008") is None
    assert len(policy.display_name("x" * 500) or "") == policy.MAX_CLIENT_NAME


def test_the_numbers_are_the_designs() -> None:
    """§5.2: a code of 60 s; §5.3: an access token of 10 min, a refresh family
    and (§5.4) a registration that die after 30 days unused; B3-02: at most 200
    live registrations, 20 an hour from one network."""
    assert policy.CODE_TTL_S == 60
    assert policy.ACCESS_TOKEN_TTL_S == 600
    assert policy.REFRESH_IDLE.days == 30
    assert policy.CLIENT_IDLE.days == 30
    assert policy.SCOPE == "mcp"
    assert policy.MAX_REGISTRATIONS == 200
    assert policy.REGISTRATIONS_PER_HOUR == 20
    assert policy.IPV6_NETWORK_BITS == 64


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


def test_every_problem_is_ascii_as_rfc_6749_allows() -> None:
    problems = [v for v in vars(errors).values() if isinstance(v, Problem)]
    assert len(problems) >= 24
    for problem in problems:
        assert errors.ALLOWED.fullmatch(problem.error), problem
        assert errors.ALLOWED.fullmatch(problem.description), problem
        assert 400 <= problem.status < 600


def test_a_problem_outside_the_character_set_is_refused_at_construction() -> None:
    with pytest.raises(ValueError):
        OAuthError(Problem("invalid_request", 'say "no"'))
    with pytest.raises(ValueError):
        OAuthError(Problem("invalid_request", "código"))


async def test_the_handler_answers_the_rfc_body_and_is_never_cached() -> None:
    app = FastAPI()
    app.add_exception_handler(OAuthError, errors.oauth_error_handler)

    @app.post("/boom")
    async def boom() -> None:
        raise OAuthError(errors.CLIENT_AUTH, headers={"WWW-Authenticate": 'Basic realm="itms"'})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://x.example"
    ) as client:
        resp = await client.post("/boom")
    assert resp.status_code == 401
    assert resp.json() == {
        "error": "invalid_client",
        "error_description": errors.CLIENT_AUTH.description,
    }
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["pragma"] == "no-cache"
    assert resp.headers["www-authenticate"] == 'Basic realm="itms"'


def test_no_store_answers_carry_both_headers() -> None:
    resp = errors.no_store({"a": 1}, status_code=201)
    assert resp.status_code == 201
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["pragma"] == "no-cache"


# --------------------------------------------------------------------------- #
# redirects
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "uri",
    [
        "https://claude.ai/api/mcp/auth_callback",
        "http://127.0.0.1:33418/callback",
        "http://[::1]/callback",
        "http://localhost:1/callback",
        "https://app.example/cb?tenant=a",
        "https://xn--bcher-kva.example/cb",
    ],
)
def test_an_https_or_loopback_redirect_registers(uri: str) -> None:
    assert redirects.check_registered(uri) == uri


@pytest.mark.parametrize(
    ("uri", "reason"),
    [
        ("http://evil.example/cb", "scheme"),
        ("myapp://cb", "scheme"),
        ("javascript:alert(1)", "scheme"),
        ("https://claude.ai/cb#x", "fragment"),
        ("https://u:p@claude.ai/cb", "credentials"),
        ("https://*.claude.ai/cb", "malformed"),
        ("https://claude.ai/ cb", "malformed"),
        ("https://claude.ai:99999/cb", "malformed"),
        ("https://claude.ai/" + "a" * 2000, "malformed"),
        # A Cyrillic "с" (U+0441): it looks like claude.ai and is not (RFC 3986 §2).
        ("https://сlaude.ai/api/mcp/auth_callback", "malformed"),
        ("https://bücher.example/cb", "malformed"),
        ("https://app.example/cb?code=evil", "query"),
        ("https://app.example/cb?a=1&state=x", "query"),
        ("https://app.example/cb?iss=https://evil.example", "query"),
        ("https://app.example/cb?error=x", "query"),
        (5, "malformed"),
        ("", "malformed"),
    ],
)
def test_any_other_redirect_is_refused_with_its_reason(uri: object, reason: str) -> None:
    with pytest.raises(redirects.RedirectRejected) as refused:
        redirects.check_registered(uri)
    assert refused.value.reason == reason


def test_a_redirect_matches_exactly() -> None:
    registered = ["https://claude.ai/api/mcp/auth_callback"]
    assert redirects.matches(registered, "https://claude.ai/api/mcp/auth_callback")
    assert not redirects.matches(registered, "https://claude.ai/api/mcp/auth_callback/")
    assert not redirects.matches(registered, "https://claude.ai:444/api/mcp/auth_callback")
    assert not redirects.matches(registered, "https://CLAUDE.ai/api/mcp/auth_callback")


def test_a_loopback_redirect_matches_on_any_port_and_nothing_else() -> None:
    assert redirects.matches(["http://127.0.0.1:33418/callback"], "http://127.0.0.1:51004/callback")
    assert redirects.matches(["http://localhost/callback"], "http://localhost:51004/callback")
    assert redirects.matches(["http://[::1]/callback"], "http://[::1]:7/callback")
    assert not redirects.matches(["http://localhost/callback"], "http://127.0.0.1:51004/callback")
    assert not redirects.matches(["http://127.0.0.1:1/callback"], "http://127.0.0.1:2/other")
    assert not redirects.matches(["http://127.0.0.1:1/cb"], "http://127.0.0.1:2/cb?x=1")
    assert not redirects.matches(["http://127.0.0.1:1/cb"], "http://u@127.0.0.1:2/cb")
    # Only the port is normalised: the scheme and host are compared as written.
    assert not redirects.matches(["http://127.0.0.1/cb"], "HTTP://127.0.0.1:5/cb")
    assert not redirects.matches(["http://localhost/cb"], "http://LOCALHOST:5/cb")


def test_the_answer_adds_to_the_redirects_own_query_as_written() -> None:
    added = redirects.with_params("https://h.example/cb?x=1", {"code": "c d", "iss": "https://i"})
    assert added == "https://h.example/cb?x=1&code=c+d&iss=https%3A%2F%2Fi"
    # Retained, not re-encoded (OAuth 2.1-17 §2.3).
    kept = redirects.with_params("https://h.example/cb?a=b%20c&flag", {"code": "x"})
    assert kept == "https://h.example/cb?a=b%20c&flag&code=x"
    assert redirects.with_params("https://h.example/cb", {"code": "x"}) == (
        "https://h.example/cb?code=x"
    )


def test_the_host_shown_to_the_person() -> None:
    assert redirects.host_of("https://claude.ai/api/mcp/auth_callback") == "claude.ai"
    assert redirects.host_of("http://[::1]:5/cb") == "::1"
    assert redirects.is_loopback("http://localhost:7/cb")
    assert not redirects.is_loopback("https://localhost/cb")


@pytest.mark.parametrize(
    ("uri", "here"),
    [
        ("http://localhost:7/cb", True),
        ("https://localhost/cb", True),
        ("https://127.0.0.1/cb", True),
        ("http://[::1]/cb", True),
        # Any loopback address, and `localhost` with its root dot (re-review
        # f3-1, Minor 7): https to any host registers, so these can.
        ("https://127.0.0.2/cb", True),
        ("https://localhost./cb", True),
        ("https://claude.ai/cb", False),
        ("https://localhost.claude.ai/cb", False),
    ],
)
def test_an_answer_that_stays_on_this_computer_is_told_by_its_host(uri: str, here: bool) -> None:
    """MCP: "SHOULD display additional warnings for localhost-only redirect URIs"
    [S30], whatever the scheme, however the machine is spelled."""
    assert redirects.stays_on_this_computer(uri) is here


# --------------------------------------------------------------------------- #
# pkce
# --------------------------------------------------------------------------- #

#: RFC 7636 Appendix B.
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_s256_is_rfc_7636s_own_example() -> None:
    assert pkce.s256(VERIFIER) == CHALLENGE
    assert pkce.verify(VERIFIER, CHALLENGE)


def test_a_wrong_or_malformed_verifier_never_verifies() -> None:
    assert not pkce.verify(VERIFIER[:-1] + "Y", CHALLENGE)
    assert not pkce.verify("x" * 42, pkce.s256("x" * 42))  # under 43 characters
    assert not pkce.verify("x" * 129, pkce.s256("x" * 129))
    assert not pkce.verify("x" * 43 + "!", CHALLENGE)


def test_a_challenge_is_43_base64url_characters() -> None:
    assert pkce.valid_challenge(CHALLENGE)
    assert not pkce.valid_challenge(CHALLENGE + "=")
    assert not pkce.valid_challenge(CHALLENGE[:-1])
    assert not pkce.valid_challenge("plain-text-is-not-a-challenge-of-any-kind!!")


# --------------------------------------------------------------------------- #
# Fix round 1 (review of be9e584)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example\\.claude.ai/cb",
        "https://claude%2Eai/cb",
        "https://claude.ai/cb%zz",
        "https://127.1/cb",
        "https://2130706433/cb",
        "https://0x7f.0.0.1/cb",
        "https://010.0.0.1/cb",
        "https://256.1.1.1/cb",
        'https://a"b.example/cb',
        "https://a{b.example/cb",
        "https://a|b.example/cb",
        "https://-a.example/cb",
        "https://a_b.example/cb",
        "https://a..example/cb",
        "https://claude.ai/c`b",
        "https://claude.ai/c<b",
        "https://[::1%25eth0]/cb",
        "https://" + "a" * 64 + ".example/cb",
    ],
)
def test_a_host_the_browser_would_read_differently_is_refused(uri: str) -> None:
    with pytest.raises(redirects.RedirectRejected) as refused:
        redirects.check_registered(uri)
    assert refused.value.reason == "malformed"


@pytest.mark.parametrize(
    "uri",
    [
        "https://xn--bcher-kva.example/cb",
        "https://a-b.example1.com/cb?x=%20y",
        "https://203.0.113.9/cb",
        "https://[2001:db8::1]:8443/cb",
        "http://127.0.0.1:5/cb",
        "http://[::1]:5/cb",
        "http://localhost:5/cb",
    ],
)
def test_ordinary_and_loopback_hosts_still_register(uri: str) -> None:
    assert redirects.check_registered(uri) == uri


def test_valid_host_is_public() -> None:
    assert redirects.valid_host("claude.ai")
    assert redirects.valid_host("::1")
    assert redirects.valid_host("127.0.0.1")
    assert not redirects.valid_host("127.1")
    assert not redirects.valid_host("")
    assert not redirects.valid_host("a b")
    assert not redirects.valid_host("a\\b")
    assert not redirects.valid_host("a" * 254)


def test_the_displayed_host_is_one_the_browser_agrees_with() -> None:
    assert redirects.host_of("https://evil.example\\.claude.ai/cb") == ""


async def test_error_headers_cannot_unset_no_store() -> None:
    exc = OAuthError(
        errors.CLIENT_AUTH,
        headers={"cache-control": "public", "PRAGMA": "x", "WWW-Authenticate": "Basic"},
    )
    resp = await errors.oauth_error_handler(None, exc)  # type: ignore[arg-type]
    assert resp.headers.getlist("cache-control") == ["no-store"]
    assert resp.headers.getlist("pragma") == ["no-cache"]
    assert resp.headers["www-authenticate"] == "Basic"


@pytest.mark.parametrize(
    ("uri", "here"),
    [
        ("https://[::ffff:127.0.0.1]/cb", True),
        ("https://app.localhost/cb", True),
        ("https://localhost.example/cb", False),
        ("https://[::ffff:8.8.8.8]/cb", False),
    ],
)
def test_more_ways_to_spell_this_computer(uri: str, here: bool) -> None:
    assert redirects.stays_on_this_computer(uri) is here


@pytest.mark.parametrize(
    "url",
    [
        "https://itms.example.com#",
        "https://itms.example.com?",
        "https://itms.example.com:",
        "https://itms .example.com",
        "https://itms.example.com\\x",
        "https://127.1",
    ],
)
def test_an_issuer_with_leftovers_or_a_bad_host_is_none(url: str) -> None:
    assert policy.issuer(_settings(url)) is None


def test_an_ipv4_mapped_address_counts_as_its_ipv4() -> None:
    import ipaddress

    assert policy.network_of(ipaddress.ip_address("::ffff:1.2.3.4")) == "ip:1.2.3.4"


def test_a_128_character_verifier_and_its_punctuation_verify() -> None:
    for verifier in ("a" * 128, "a.b~c-d_e" + "x" * 40):
        assert pkce.verify(verifier, pkce.s256(verifier))


async def test_create_app_registers_the_oauth_error_handler(api_env) -> None:  # type: ignore[no-untyped-def]
    assert api_env.app.exception_handlers[OAuthError] is errors.oauth_error_handler
