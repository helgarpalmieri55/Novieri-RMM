"""A write on the console's session cookie comes from the console (P2 design §6.4).

FALSIFIER 7 of design §10: "Una escritura sin la cabecera propia, o con
`Origin: null`, se rechaza." The middleware is `app/core/auth/csrf.py`; each
test names the condition it protects. The write used throughout is `PATCH
/api/v1/me` (any signed-in person may make it), and every refusal is checked
to have changed nothing.
"""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.csrf import console_host, console_origin
from app.models import User
from tests.mcp_support import marked_client
from tests.session_support import CONSOLE, console_headers, cookie_header, sign_in, use_console


@pytest.fixture()
async def signed(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    use_console(monkeypatch, api_env)
    org_id = await api_env.create_org("csrf")
    sub = await api_env.create_user(org_id, "read-only")
    return await sign_in(api_env, org_id, sub)


async def _display_name(env: Any, user_id: Any) -> str:
    async with AsyncSession(env.admin_engine) as s:
        name: str = (
            await s.execute(select(User.display_name).where(User.id == user_id))
        ).scalar_one()
        return name


async def _patch(env: Any, headers: dict[str, str]) -> Any:
    return await env.client.patch("/api/v1/me", json={"display_name": "Changed"}, headers=headers)


async def test_the_consoles_own_write_passes(api_env: Any, signed: Any) -> None:
    response = await _patch(api_env, signed.headers)
    assert response.status_code == 200, response.text
    assert await _display_name(api_env, signed.user_id) == "Changed"


async def test_a_write_without_the_header_is_refused(api_env: Any, signed: Any) -> None:
    """Protects the `X-ITMS-Request` check: a cross-site page cannot add it
    without a preflight this API never answers [S63]."""
    headers = {"Cookie": cookie_header(signed.token), "Origin": CONSOLE}
    response = await _patch(api_env, headers)
    assert response.status_code == 403
    assert response.json()["code"] == "csrf"
    assert await _display_name(api_env, signed.user_id) == ""


@pytest.mark.parametrize(
    "origin",
    [None, "null", "https://evil.example", "http://itms.test", "https://itms.test.evil.example"],
    ids=["absent", "null", "foreign", "other-scheme", "suffix"],
)
async def test_a_write_from_any_other_origin_is_refused(
    api_env: Any, signed: Any, origin: str | None
) -> None:
    """Protects `origin_allowed`: absent and `null` are no evidence of the same
    origin [S63], and the comparison is exact, scheme and host."""
    headers = {"Cookie": cookie_header(signed.token), "X-ITMS-Request": "1"}
    if origin is not None:
        headers["Origin"] = origin
    response = await _patch(api_env, headers)
    assert response.status_code == 403
    assert response.json()["code"] == "csrf"
    assert await _display_name(api_env, signed.user_id) == ""


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none"])
async def test_a_browser_that_says_cross_site_is_refused(
    api_env: Any, signed: Any, site: str
) -> None:
    """Protects the `Sec-Fetch-Site` check: only `same-origin` passes."""
    response = await _patch(api_env, {**signed.headers, "Sec-Fetch-Site": site})
    assert response.status_code == 403
    assert response.json()["code"] == "csrf"


async def test_same_origin_fetch_metadata_passes(api_env: Any, signed: Any) -> None:
    response = await _patch(api_env, {**signed.headers, "Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 200, response.text


async def test_a_read_needs_neither(api_env: Any, signed: Any) -> None:
    response = await api_env.client.get(
        "/api/v1/me", headers={"Cookie": cookie_header(signed.token)}
    )
    assert response.status_code == 200, response.text


async def test_without_a_console_url_every_cookie_write_is_refused(
    api_env: Any, signed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `ITS_PUBLIC_CONSOLE_URL`, no console origin to match: fail closed."""
    monkeypatch.setattr(api_env.app.state.settings, "public_console_url", None)
    response = await _patch(api_env, signed.headers)
    assert response.status_code == 403
    assert response.json()["code"] == "csrf"


async def test_the_sign_in_routes_are_held_to_it_without_a_cookie(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Login CSRF: `/api/v1/auth/` is checked with no session cookie at all.
    With the console's headers the request goes on to the router (whatever the
    sign-in route then answers, Task 7, it is not this refusal)."""
    use_console(monkeypatch, api_env)
    body = {"email": "someone@example.com", "password": "x" * 12}
    refused = await api_env.client.post("/api/v1/auth/login", json=body)
    assert refused.status_code == 403 and refused.json()["code"] == "csrf"
    passed = await api_env.client.post("/api/v1/auth/login", json=body, headers=console_headers())
    assert passed.status_code != 403 or passed.json().get("code") != "csrf"


async def test_a_bearer_request_is_not_a_cookie_request(api_env: Any) -> None:
    """Until P2 Task 13 the console's bearer token is admitted (D21), and a
    bearer is not ambient: the browser never attaches it by itself. MCP's
    in-process calls and the assistant's loopback are bearer requests too."""
    org_id = await api_env.create_org("csrf-bearer")
    sub = await api_env.create_user(org_id, "read-only")
    response = await _patch(api_env, await api_env.headers(org_id, sub))
    assert response.status_code == 200, response.text


async def test_the_refusal_is_in_the_requests_language(api_env: Any, signed: Any) -> None:
    headers = {"Cookie": cookie_header(signed.token), "Accept-Language": "es"}
    response = await _patch(api_env, headers)
    assert response.status_code == 403
    assert response.json()["detail"] == "se rechazó esta petición porque no vino de la consola"


async def test_an_mcp_call_meets_the_guard_before_this_rule(api_env: Any, signed: Any) -> None:
    """Protects the ORDER in `app/main.py`: this rule is added BEFORE
    `McpNeverExposedGuard`, so the guard is the outer layer. An in-process MCP
    call to a never-exposed route that also carries a session cookie and no
    console header is refused by BOTH; it must get the guard's answer, not
    `csrf` -- the one every never-exposed `/api/v1/auth/` route gives the
    channel from Task 7 on (`tests/test_mcp_channel_api.py::
    test_every_never_exposed_route_refuses_the_channel`). Adding the rule
    after the guard turns this red with a 403 `csrf`."""
    async with marked_client(api_env) as client:
        response = await client.post(
            "/api/v1/enrollment-tokens", json={}, headers={"Cookie": cookie_header(signed.token)}
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "this action is never available to an AI app"}


@pytest.mark.parametrize(
    ("configured", "origin", "host"),
    [
        ("https://ITMS.example.com/", "https://itms.example.com", "itms.example.com"),
        ("https://itms.example.com:443", "https://itms.example.com", "itms.example.com"),
        (
            "https://itms.example.com:8443/console",
            "https://itms.example.com:8443",
            "itms.example.com",
        ),
        ("http://localhost:5173", "http://localhost:5173", "localhost"),
        ("ftp://itms.example.com", None, None),
        ("", None, None),
        (None, None, None),
    ],
)
def test_the_console_origin_is_read_from_the_setting(
    configured: str | None, origin: str | None, host: str | None
) -> None:
    class _Settings:
        public_console_url = configured

    assert console_origin(_Settings()) == origin
    assert console_host(_Settings()) == host
