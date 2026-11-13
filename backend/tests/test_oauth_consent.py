"""The consent screen's two routes (autenticación propia design §5.2; plan
2026-11-06, Task 8).

- who may connect is asked first: the deployment's organization, MCP on,
  `mcp.use` (plan D11, B3-09); while they may not, no document is read; with
  no deployment organization at all, the installation's 503;
- the request is validated in RFC 6749 §4.1.2.1's two halves: a bad client or
  redirect (one outside RFC 3986, or on port 0, included) is shown and sent
  nowhere; an error the app should hear, a decline
  and a person who may not connect are shown with their host and go back only
  on a click (B3-15); only an approval is a `redirect`;
- the consent names the app, the host its answer goes to, the host of its
  metadata document, and warns when it only returns to this computer [S30];
- approving needs the second factor confirmed in the last ten minutes, on a
  remembered device too, issues a 60-second code the token endpoint accepts,
  is audited `mcp.app_authorized` and tells the person by mail (C1);
  declining issues, audits and mails nothing;
- an approval takes the person's row first and refuses a session that ended
  while it waited (operator decision D2);
- the routes need the console's own session: no cookie, no MCP, no CSRF.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.oauth import cimd, consent
from app.core.auth.oauth.store import oauth_session
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent
from app.models.auth import AuthSession
from app.models.oauth import OAuthClient, OAuthCode
from app.modules.user_admin import service as ua
from tests.auth_support import capture_mail
from tests.mcp_support import marked_client
from tests.oauth_support import (
    BASE,
    CLAUDE_CALLBACK,
    authorize_query,
    code_of,
    decide,
    oauth_org,
    pkce_pair,
    seed_client,
    token_request,
)
from tests.race_helpers import backend_pid, normalised, until_blocked_by
from tests.session_support import refusal_code, remembered_device, sign_in, user_id_of

DOCUMENT_URL = "https://claude.example/oauth/client-metadata.json"
CLAUDE_REDIRECT = "redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback&"
MALFORMED = "this AI app's request is malformed; going back tells the app what was wrong"


class Env:
    def __init__(self, api: Any, org_id: UUID, sub: str) -> None:
        self.api, self.org_id, self.sub = api, org_id, sub


@pytest.fixture()
async def env(api_env, monkeypatch) -> Env:
    org_id, _ = await oauth_org(api_env)
    api_env.settings.mcp_registration = "open"
    api_env.mail = capture_mail(monkeypatch)
    sub = await api_env.create_user(org_id, "org-admin")
    return Env(api_env, org_id, sub)


async def _details(env: Env, query: str, sub: str | None = None, org_id: UUID | None = None) -> Any:
    org = org_id or env.org_id
    return await env.api.client.post(
        "/api/v1/oauth/authorize/details",
        json={"query": query},
        headers=await env.api.headers(org, sub or env.sub),
    )


def _params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


async def _registered(env: Env, *uris: str) -> Any:
    return await seed_client(env.api, env.org_id, redirect_uris=uris or (CLAUDE_CALLBACK,))


async def _codes(env: Env) -> list[OAuthCode]:
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        return list((await session.execute(select(OAuthCode))).scalars())


async def _authorized(env: Env) -> list[AuditEvent]:
    async with AsyncSession(env.api.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == env.org_id, AuditEvent.action == "mcp.app_authorized"
                    )
                )
            ).scalars()
        )


def _document(client_id: str) -> cimd.ClientDocument:
    assert client_id == DOCUMENT_URL
    return cimd.ClientDocument(
        client_id=DOCUMENT_URL,
        client_name="Claude Code",
        redirect_uris=("http://localhost/callback",),
        cache_for=cimd.MIN_CACHE,
    )


# --------------------------------------------------------------------------- #
# What the person is asked
# --------------------------------------------------------------------------- #


async def test_the_consent_names_the_app_its_return_and_the_account(env: Env) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    resp = await _details(env, authorize_query(app.client_id, challenge=challenge))
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == {
        "outcome": "consent",
        "client": {
            "name": "Claude",
            "client_id": app.client_id,
            "kind": "registered",
            "client_host": None,
            "redirect_host": "claude.ai",
            "loopback_only": False,
        },
        "scope": "mcp",
        "account": f"{env.sub}@test.local",
    }


@pytest.mark.parametrize(
    "uris", [("http://127.0.0.1/callback", "http://localhost/callback"), ("https://localhost/cb",)]
)
async def test_an_app_that_only_returns_to_this_computer_is_marked(
    env: Env, uris: tuple[str, ...]
) -> None:
    """Whatever the scheme (A3-05): the answer stays on this machine."""
    app = await _registered(env, *uris)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge, redirect_uri=uris[0])
    body = (await _details(env, query)).json()
    assert body["client"]["loopback_only"] is True


async def test_a_metadata_document_client_names_its_host(env: Env, monkeypatch) -> None:
    async def document(client_id: str) -> cimd.ClientDocument:
        return _document(client_id)

    monkeypatch.setattr(cimd, "fetch_document", document)
    _, challenge = pkce_pair()
    query = authorize_query(
        DOCUMENT_URL, challenge=challenge, redirect_uri="http://localhost:6123/callback"
    )
    client = (await _details(env, query)).json()["client"]
    assert client["kind"] == "metadata_document"
    assert client["client_host"] == "claude.example"
    assert client["name"] == "Claude Code"


@pytest.mark.parametrize("resource", [None, "HTTPS://ITMS.EXAMPLE.COM/mcp"])
async def test_a_request_with_no_resource_or_a_capitalised_one_is_for_the_mcp_server(
    env: Env, resource: str | None
) -> None:
    """RFC 8707 §2.1's default resource and MCP's uppercase scheme and host
    (Task 8, decision 1)."""
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge).replace(
        "&resource=https%3A%2F%2Fitms.example.com%2Fmcp", ""
    )
    if resource is not None:
        query += "&resource=" + resource.replace(":", "%3A").replace("/", "%2F")
    assert (await _details(env, query)).json()["outcome"] == "consent"


async def test_with_one_registered_redirect_the_request_may_leave_it_out(env: Env) -> None:
    """OAuth 2.1-17 §4.1.1 (Task 8, decision 6)."""
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge).replace(CLAUDE_REDIRECT, "")
    assert (await _details(env, query)).json()["outcome"] == "consent"
    body = (await decide(env.api, env.org_id, env.sub, query)).json()
    assert body["redirect_to"].startswith(CLAUDE_CALLBACK + "?code=")


# --------------------------------------------------------------------------- #
# RFC 6749 §4.1.2.1: shown, never sent back
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("no-client", "invalid_request"),
        ("unknown-client", "invalid_client"),
        ("two-clients", "invalid_request"),
        ("no-redirect", "invalid_request"),
        ("unregistered-redirect", "invalid_redirect_uri"),
        ("two-redirects", "invalid_request"),
        # Task 8 review, fix round 1: `%0A` decodes to a line feed that
        # `urlsplit` drops, so the code would be bound to one string and the
        # browser sent to another; and no browser connects to port 0.
        ("loopback-line-feed", "invalid_redirect_uri"),
        ("loopback-port-zero", "invalid_redirect_uri"),
    ],
)
async def test_a_bad_client_or_redirect_is_shown_and_never_redirected(
    env: Env, case: str, error: str
) -> None:
    app = await _registered(env, CLAUDE_CALLBACK, "http://127.0.0.1/callback")
    _, challenge = pkce_pair()
    good = authorize_query(app.client_id, challenge=challenge)
    query = {
        "no-client": good.replace(f"client_id={app.client_id}&", ""),
        "unknown-client": authorize_query("itms_nobody", challenge=challenge),
        "two-clients": good + f"&client_id={app.client_id}",
        "no-redirect": good.replace(CLAUDE_REDIRECT, ""),  # two registered: required
        "unregistered-redirect": authorize_query(
            app.client_id, challenge=challenge, redirect_uri="https://evil.example/cb"
        ),
        "two-redirects": good + "&redirect_uri=https%3A%2F%2Fevil.example%2Fcb",
        "loopback-line-feed": authorize_query(
            app.client_id, challenge=challenge, redirect_uri="http://127.0.0.1:5000/call\nback"
        ),
        "loopback-port-zero": authorize_query(
            app.client_id, challenge=challenge, redirect_uri="http://127.0.0.1:0/callback"
        ),
    }[case]
    for route in ("details", "decision"):
        resp = await env.api.client.post(
            f"/api/v1/oauth/authorize/{route}",
            json={"query": query, **({"approve": True} if route == "decision" else {})},
            headers=await env.api.headers(env.org_id, env.sub),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "refused", (route, body)
        assert body["error"] == error, (route, body)
        assert "redirect_to" not in body
        assert body["detail"]
    assert await _codes(env) == []
    assert await _authorized(env) == []


async def test_an_unreadable_metadata_document_is_shown(env: Env, monkeypatch) -> None:
    async def refused(client_id: str) -> cimd.ClientDocument:
        raise cimd.DocumentRefused("mismatch")

    monkeypatch.setattr(cimd, "fetch_document", refused)
    _, challenge = pkce_pair()
    body = (await _details(env, authorize_query(DOCUMENT_URL, challenge=challenge))).json()
    assert body["outcome"] == "refused"
    assert body["error"] == "invalid_client"
    assert "claude.example" in body["detail"]


async def test_a_document_that_cannot_be_read_just_now_says_to_try_again(
    env: Env, monkeypatch
) -> None:
    """CIMD-03 §6.1: a transient failure is not a verdict on the app."""

    async def down(client_id: str) -> cimd.ClientDocument:
        raise cimd.DocumentRefused("unavailable")

    monkeypatch.setattr(cimd, "fetch_document", down)
    _, challenge = pkce_pair()
    body = (await _details(env, authorize_query(DOCUMENT_URL, challenge=challenge))).json()
    assert (body["outcome"], body["error"]) == ("refused", "temporarily_unavailable")
    assert "claude.example" in body["detail"]


async def test_a_refusal_is_in_the_persons_language(env: Env) -> None:
    _, challenge = pkce_pair()
    resp = await env.api.client.post(
        "/api/v1/oauth/authorize/details",
        json={"query": authorize_query("itms_nobody", challenge=challenge)},
        headers={**await env.api.headers(env.org_id, env.sub), "Accept-Language": "es"},
    )
    assert resp.json()["detail"] == (
        "esta aplicación de IA no está registrada aquí o su registro caducó. Vuelve a "
        "conectarla desde la aplicación."
    )


# --------------------------------------------------------------------------- #
# What the app must hear: shown with its host, sent on a click (B3-15)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"code_challenge": "short"}, "invalid_request"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"resource": "https://itms.example.com"}, "invalid_target"),
        ({"resource": "https://itms.example.com/MCP"}, "invalid_target"),
    ],
)
async def test_a_bad_request_is_shown_with_where_it_goes_and_never_followed(
    env: Env, change: dict[str, str], error: str
) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge, **change)
    for route in ("details", "decision"):
        resp = await env.api.client.post(
            f"/api/v1/oauth/authorize/{route}",
            json={"query": query, **({"approve": True} if route == "decision" else {})},
            headers=await env.api.headers(env.org_id, env.sub),
        )
        body = resp.json()
        assert (body["outcome"], body["reason"], body["redirect_host"]) == (
            "back",
            "error",
            "claude.ai",
        )
        assert body["detail"] == MALFORMED
        assert body["redirect_to"].startswith(CLAUDE_CALLBACK + "?")
        params = _params(body["redirect_to"])
        assert params["error"] == [error]
        assert params["state"] == ["st4te"]
        assert params["iss"] == [BASE]
        assert "code" not in params
    assert await _codes(env) == []


async def test_a_missing_method_is_plain_and_refused(env: Env) -> None:
    """RFC 7636 §4.3: no `code_challenge_method` means "plain"."""
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge).replace(
        "&code_challenge_method=S256", ""
    )
    params = _params((await _details(env, query)).json()["redirect_to"])
    assert params["error"] == ["invalid_request"]


async def test_a_parameter_twice_goes_back_without_a_state_it_cannot_trust(env: Env) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge) + "&state=other"
    params = _params((await _details(env, query)).json()["redirect_to"])
    assert params["error"] == ["invalid_request"]
    assert "state" not in params


# --------------------------------------------------------------------------- #
# Who may connect (plan D11, B3-09), asked before the request is read
# --------------------------------------------------------------------------- #


async def test_with_mcp_off_the_person_is_told_and_offered_the_way_back(api_env) -> None:
    org_id, _ = await oauth_org(api_env, mcp_enabled=False)
    sub = await api_env.create_user(org_id, "org-admin")
    app = await seed_client(api_env, org_id)
    _, challenge = pkce_pair()
    env = Env(api_env, org_id, sub)
    body = (await _details(env, authorize_query(app.client_id, challenge=challenge))).json()
    assert (body["outcome"], body["reason"], body["redirect_host"]) == (
        "back",
        "denied",
        "claude.ai",
    )
    assert body["detail"].startswith("MCP is turned off for this organization.")
    assert _params(body["redirect_to"])["error"] == ["access_denied"]
    query = authorize_query(app.client_id, challenge=challenge)
    approved = (await decide(api_env, org_id, sub, query)).json()
    assert (approved["outcome"], approved["reason"]) == ("back", "denied")


async def test_with_mcp_off_no_metadata_document_is_read_or_stored(api_env, monkeypatch) -> None:
    """A3-31: a person who may not connect makes the server fetch nothing."""

    async def never(client_id: str) -> cimd.ClientDocument:
        raise AssertionError("a document was read while MCP is off")

    monkeypatch.setattr(cimd, "fetch_document", never)
    org_id, _ = await oauth_org(api_env, mcp_enabled=False)
    sub = await api_env.create_user(org_id, "org-admin")
    env = Env(api_env, org_id, sub)
    _, challenge = pkce_pair()
    body = (await _details(env, authorize_query(DOCUMENT_URL, challenge=challenge))).json()
    assert (body["outcome"], body["error"]) == ("refused", "access_denied")
    assert body["detail"].startswith("MCP is turned off for this organization.")
    assert "redirect_to" not in body
    async with oauth_session(org_id, api_env.app_engine) as session:
        assert (await session.execute(select(OAuthClient))).scalars().all() == []


async def test_without_mcp_use_the_person_is_told(env: Env) -> None:
    technician = await env.api.create_user(env.org_id, "technician")
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    body = (await _details(env, query, sub=technician)).json()
    assert (body["outcome"], body["reason"]) == ("back", "denied")
    assert body["detail"] == "requires mcp.use"


async def test_without_mcp_use_no_metadata_document_is_read_or_stored(
    env: Env, monkeypatch
) -> None:
    """A3-31 with MCP on (Task 8 review, fix round 1): a person who may not
    connect makes the server fetch nothing, however the switch stands --
    neither the question nor the answer, approving or declining."""

    async def never(client_id: str) -> cimd.ClientDocument:
        raise AssertionError("a document was read for a person without mcp.use")

    monkeypatch.setattr(cimd, "fetch_document", never)
    technician = await env.api.create_user(env.org_id, "technician")
    _, challenge = pkce_pair()
    query = authorize_query(DOCUMENT_URL, challenge=challenge)
    asked = (await _details(env, query, sub=technician)).json()
    assert (asked["outcome"], asked["error"], asked["detail"]) == (
        "refused",
        "access_denied",
        "requires mcp.use",
    )
    for approve in (True, False):
        answered = (await decide(env.api, env.org_id, technician, query, approve=approve)).json()
        assert (answered["outcome"], answered["error"]) == ("refused", "access_denied")
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        assert (await session.execute(select(OAuthClient))).scalars().all() == []


@pytest.mark.parametrize("slug", ["", "no-such-organization"])
async def test_without_a_deployment_organization_the_consent_is_unavailable(
    env: Env, slug: str
) -> None:
    """Task 8 review, fix round 1. With `ITS_DEFAULT_ORG_SLUG` unset, or
    naming no organization, nobody can connect an AI app here -- the token
    endpoint redeems codes in that organization only. Every person was told
    they were "signed in to another one"; it is the installation's 503,
    as `_ready` answers a console address with no issuer."""
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    headers = await env.api.headers(env.org_id, env.sub)
    env.api.settings.default_org_slug = slug
    for route, extra in (
        ("details", {}),
        ("decision", {"approve": True}),
        ("decision", {"approve": False}),
    ):
        resp = await env.api.client.post(
            f"/api/v1/oauth/authorize/{route}", json={"query": query, **extra}, headers=headers
        )
        assert resp.status_code == 503, (route, extra, resp.text)
        assert resp.json()["detail"] == (
            "MCP is not available: ITS_DEFAULT_ORG_SLUG names no organization on this installation."
        )
    assert await _codes(env) == []


async def test_a_person_of_another_organization_cannot_connect_here(env: Env) -> None:
    """B3-09: the code would be redeemed in the deployment's organization."""
    other, _ = await oauth_org(env.api, "other")
    env.api.settings.default_org_slug = await env.api.org_slug(env.org_id)
    stranger = await env.api.create_user(other, "org-admin")
    app = await seed_client(env.api, other)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    body = (await _details(env, query, sub=stranger, org_id=other)).json()
    assert (body["outcome"], body["reason"]) == ("back", "denied")
    assert body["detail"] == (
        "AI apps connect to this installation's own organization, and you are signed in to "
        "another one"
    )
    approved = (await decide(env.api, other, stranger, query)).json()
    assert (approved["outcome"], approved["reason"]) == ("back", "denied")
    async with oauth_session(other, env.api.app_engine) as session:
        assert (await session.execute(select(OAuthCode))).scalars().all() == []


async def test_a_person_who_may_not_connect_keeps_no_idle_app_alive(env: Env) -> None:
    """Re-review f3-2, M-3: without `mcp.use`, neither the question nor the
    answer writes the app's `last_used_at`, which decides its retention."""
    technician = await env.api.create_user(env.org_id, "technician")
    long_ago = datetime.now(UTC) - timedelta(days=20)
    app = await seed_client(env.api, env.org_id, last_used_at=long_ago)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    asked = (await _details(env, query, sub=technician)).json()
    answered = (await decide(env.api, env.org_id, technician, query)).json()
    assert (asked["outcome"], asked["reason"]) == ("back", "denied")
    assert (answered["outcome"], answered["reason"]) == ("back", "denied")
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        row = await session.get(OAuthClient, app.id)
        assert row is not None and row.last_used_at == long_ago


async def test_a_document_app_is_approved_and_redeemed_with_registration_closed(
    env: Env, monkeypatch
) -> None:
    """Operator decision C2 at the consent route: a client metadata document
    needs no `ITS_MCP_REGISTRATION`, only `POST /oauth/register` does
    (re-review f3-2, M-6)."""
    env.api.settings.mcp_registration = ""

    async def document(client_id: str) -> cimd.ClientDocument:
        return _document(client_id)

    monkeypatch.setattr(cimd, "fetch_document", document)
    verifier, challenge = pkce_pair()
    redirect = "http://localhost:6123/callback"
    query = authorize_query(DOCUMENT_URL, challenge=challenge, redirect_uri=redirect)
    body = (await decide(env.api, env.org_id, env.sub, query)).json()
    assert body["outcome"] == "redirect", body
    token = await token_request(
        env.api,
        grant_type="authorization_code",
        code=code_of(body["redirect_to"]),
        redirect_uri=redirect,
        client_id=DOCUMENT_URL,
        code_verifier=verifier,
    )
    assert token.status_code == 200, token.text


# --------------------------------------------------------------------------- #
# The decision
# --------------------------------------------------------------------------- #


async def test_approving_issues_a_code_the_token_endpoint_accepts(env: Env) -> None:
    app = await _registered(env)
    verifier, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    resp = await decide(env.api, env.org_id, env.sub, query)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["outcome"] == "redirect"
    params = _params(body["redirect_to"])
    assert params["state"] == ["st4te"] and params["iss"] == [BASE]
    token = await token_request(
        env.api,
        grant_type="authorization_code",
        code=code_of(body["redirect_to"]),
        redirect_uri=CLAUDE_CALLBACK,
        client_id=app.client_id,
        code_verifier=verifier,
    )
    assert token.status_code == 200, token.text
    (event,) = await _authorized(env)
    assert event.actor_type == "user"
    assert event.meta["app"] == app.client_id
    assert event.meta["redirect_host"] == "claude.ai"
    assert code_of(body["redirect_to"]) not in str(event.meta)


async def test_approving_needs_the_second_factor_confirmed_in_the_last_ten_minutes(
    env: Env,
) -> None:
    """Operator decision C1 (2026-11-06): on a remembered device too, and a
    confirmation eleven minutes old is not recent. Declining needs none."""
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    device = await remembered_device(env.api, env.org_id, env.sub)
    remembered = await sign_in(env.api, env.org_id, env.sub, trusted_device_id=device)
    stale = await sign_in(env.api, env.org_id, env.sub, step_up=True)
    async with AsyncSession(env.api.admin_engine) as session:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.id == stale.session_id)
            .values(step_up_at=datetime.now(UTC) - timedelta(minutes=11))
        )
        await session.commit()
    for signed in (remembered, stale):
        refused = await env.api.client.post(
            "/api/v1/oauth/authorize/decision",
            json={"query": query, "approve": True},
            headers=signed.headers,
        )
        assert refused.status_code == 403, refused.text
        assert refusal_code(refused) == "step_up_required"
        declined = await env.api.client.post(
            "/api/v1/oauth/authorize/decision",
            json={"query": query, "approve": False},
            headers=signed.headers,
        )
        assert (declined.json()["outcome"], declined.json()["reason"]) == ("back", "declined")
    assert await _codes(env) == []
    assert await _authorized(env) == []
    assert env.api.mail.sent == []


@pytest.mark.parametrize(
    ("language", "subject"), [("en", "You connected claude.ai"), ("es", "Has conectado claude.ai")]
)
async def test_an_approval_tells_the_person_which_app_they_connected(
    env: Env, language: str, subject: str
) -> None:
    """C1: the mail names the host the screen named, never the code."""
    app = await _registered(env)
    _, challenge = pkce_pair()
    resp = await env.api.client.post(
        "/api/v1/oauth/authorize/decision",
        json={"query": authorize_query(app.client_id, challenge=challenge), "approve": True},
        headers={**await env.api.headers(env.org_id, env.sub), "Accept-Language": language},
    )
    assert resp.json()["outcome"] == "redirect"
    (mail,) = env.api.mail.sent
    assert (mail.to, mail.subject) == (f"{env.sub}@test.local", subject)
    assert code_of(resp.json()["redirect_to"]) not in mail.text


async def test_a_document_apps_mail_names_its_documents_host(env: Env, monkeypatch) -> None:
    async def document(client_id: str) -> cimd.ClientDocument:
        return _document(client_id)

    monkeypatch.setattr(cimd, "fetch_document", document)
    _, challenge = pkce_pair()
    query = authorize_query(
        DOCUMENT_URL, challenge=challenge, redirect_uri="http://localhost:6123/callback"
    )
    assert (await decide(env.api, env.org_id, env.sub, query)).json()["outcome"] == "redirect"
    (mail,) = env.api.mail.sent
    assert mail.subject == "You connected claude.example"


async def test_a_loopback_approval_returns_to_the_port_that_asked(env: Env) -> None:
    app = await _registered(env, "http://127.0.0.1/callback")
    _, challenge = pkce_pair()
    query = authorize_query(
        app.client_id, challenge=challenge, redirect_uri="http://127.0.0.1:51004/callback"
    )
    body = (await decide(env.api, env.org_id, env.sub, query)).json()
    assert body["redirect_to"].startswith("http://127.0.0.1:51004/callback?code=")


async def test_declining_is_shown_with_its_host_and_issues_nothing(env: Env) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    body = (await decide(env.api, env.org_id, env.sub, query, approve=False)).json()
    assert (body["outcome"], body["reason"], body["redirect_host"]) == (
        "back",
        "declined",
        "claude.ai",
    )
    params = _params(body["redirect_to"])
    assert params["error"] == ["access_denied"]
    assert params["state"] == ["st4te"] and params["iss"] == [BASE]
    assert "code" not in params
    assert await _codes(env) == []
    assert await _authorized(env) == []
    assert env.api.mail.sent == []


async def test_an_app_purged_between_the_question_and_the_hold_issues_nothing(
    env: Env, monkeypatch
) -> None:
    """Task 8 review, fix round 1. The request is validated and its app
    resolved; then, before the approval holds the app's row, an idle app's
    purge takes it (`clients.hold` re-reads it: None). The approval is
    refused as an app not registered here -- the same sentence the request's
    validation gives -- and issues, audits and mails nothing."""
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    real = consent.validate

    async def then_purged(*args: Any, **kwargs: Any) -> Any:
        checked = await real(*args, **kwargs)
        async with oauth_session(env.org_id, env.api.app_engine) as session:
            await session.execute(delete(OAuthClient).where(OAuthClient.id == app.id))
        return checked

    monkeypatch.setattr(consent, "validate", then_purged)
    resp = await decide(env.api, env.org_id, env.sub, query)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "outcome": "refused",
        "error": "invalid_client",
        "detail": (
            "this AI app is not registered here, or its registration expired. Connect it again "
            "from the app."
        ),
    }
    assert await _codes(env) == []
    assert await _authorized(env) == []
    assert env.api.mail.sent == []


# --------------------------------------------------------------------------- #
# The person's row first (operator decision D2, 2026-11-12)
# --------------------------------------------------------------------------- #


def _pause_the_act(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[asyncio.Event, asyncio.Event, dict[str, int]]:
    """Hold an administrator's act on a person where it ends their sessions
    (`user_admin.service.end_all_for_user`), inside its transaction and with
    the person's row already held (`_target(lock=True)`): (paused, go,
    {"pid": the act's backend})."""
    paused, go = asyncio.Event(), asyncio.Event()
    seen: dict[str, int] = {}
    real: Callable[..., Awaitable[Any]] = ua.end_all_for_user

    async def held(session: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        seen["pid"] = await backend_pid(session)
        paused.set()
        await go.wait()
        return await real(session, *args, **kwargs)

    monkeypatch.setattr(ua, "end_all_for_user", held)
    return paused, go, seen


async def _act_on(env: Env, act: str, admin_id: UUID, person_id: UUID) -> str:
    async with org_session(
        env.org_id, engine=env.api.app_engine, clients=ClientScope.all()
    ) as session:
        if act == "end_sessions":
            await ua.end_sessions(session, org_id=env.org_id, actor_id=admin_id, user_id=person_id)
        else:
            await ua.set_enabled(
                session, org_id=env.org_id, actor_id=admin_id, user_id=person_id, enabled=False
            )
    return "committed"


def _on_the_person(waiting: list[str]) -> bool:
    """Every waiter waits on the person's `users` row, taken first FOR NO KEY
    UPDATE (`guards.lock_user_row`)."""
    return bool(waiting) and all(
        "FROM USERS " in normalised(q) and "FOR NO KEY UPDATE" in normalised(q) for q in waiting
    )


@pytest.mark.parametrize("act", ["end_sessions", "disable"])
async def test_an_approval_waits_on_the_persons_row_and_an_ended_session_issues_nothing(
    env: Env, act: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator decision D2. An administrator ends a person's sessions -- or
    switches them off -- and is held after it took the person's row. The
    person approves an AI app meanwhile, from a session admitted before the
    act, its factor just confirmed. Without the row the approval wrote the
    code, its audit row and the mail for a session that had ended. Now it
    waits on the person's ROW (`guards.locked_user`), finds its session ended
    once the act commits, and is refused: no code, no audit row, no mail."""
    admin_id = await user_id_of(env.api, env.sub)
    person = await env.api.create_user(env.org_id, "org-admin")
    person_id = await user_id_of(env.api, person)
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    headers = await env.api.headers(env.org_id, person)
    paused, go, seen = _pause_the_act(monkeypatch)
    acting = asyncio.create_task(_act_on(env, act, admin_id, person_id))
    await asyncio.wait_for(paused.wait(), 30)
    approving = asyncio.create_task(
        env.api.client.post(
            "/api/v1/oauth/authorize/decision",
            json={"query": query, "approve": True},
            headers=headers,
        )
    )
    watching = asyncio.create_task(until_blocked_by(env.api.app_engine, seen["pid"]))
    try:
        await asyncio.wait({watching, approving}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        seen_waiting = watching.done() and watching.exception() is None
        waiting = watching.result() if seen_waiting else []
        held = seen_waiting and not approving.done()
    finally:
        if not watching.done():
            watching.cancel()
        go.set()
    acted, approved = await asyncio.wait_for(
        asyncio.gather(acting, approving, return_exceptions=True), timeout=60
    )
    assert acted == "committed", acted
    assert not isinstance(approved, BaseException), approved
    assert held, approved.text
    assert _on_the_person(waiting), waiting
    assert approved.status_code == 401, approved.text
    assert refusal_code(approved) == "auth_required"
    assert await _codes(env) == []
    assert await _authorized(env) == []
    assert [m for m in env.api.mail.sent if m.subject.startswith("You connected")] == []


# --------------------------------------------------------------------------- #
# Only the console's own session
# --------------------------------------------------------------------------- #


async def test_without_a_session_both_routes_are_401(env: Env) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    for route in ("details", "decision"):
        resp = await env.api.client.post(
            f"/api/v1/oauth/authorize/{route}",
            json={"query": query, **({"approve": True} if route == "decision" else {})},
            headers={"X-ITMS-Request": "1", "Origin": BASE},
        )
        assert resp.status_code == 401, (route, resp.text)


async def test_a_decision_without_the_consoles_header_is_csrf(env: Env) -> None:
    app = await _registered(env)
    _, challenge = pkce_pair()
    headers = {
        k: v
        for k, v in (await env.api.headers(env.org_id, env.sub)).items()
        if k != "X-ITMS-Request"
    }
    resp = await env.api.client.post(
        "/api/v1/oauth/authorize/decision",
        json={"query": authorize_query(app.client_id, challenge=challenge), "approve": True},
        headers=headers,
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "csrf"


async def test_an_ai_app_can_never_approve_an_ai_app(env: Env) -> None:
    """1c: the decision mints a code that becomes the person's MCP tokens, so it
    is never exposed; the details need a console session the channel never has."""
    app = await _registered(env)
    _, challenge = pkce_pair()
    query = authorize_query(app.client_id, challenge=challenge)
    async with marked_client(env.api) as client:
        decision = await client.post(
            "/api/v1/oauth/authorize/decision", json={"query": query, "approve": True}
        )
        details = await client.post("/api/v1/oauth/authorize/details", json={"query": query})
    assert decision.status_code == 403
    assert decision.json()["detail"] == "this action is never available to an AI app"
    assert details.status_code == 401
