"""The token and revocation endpoints (autenticación propia design §5.2,
§5.3; plan 2026-11-06, Task 7).

- a code is single-use, 60 s, bound to its app, its exact redirect and its
  PKCE challenge; a failed exchange spends it (RFC 6749 §10.5); reused after
  a success, it ends the grant it opened (RFC 6749 §4.1.2);
- the token request may leave out `redirect_uri` (OAuth 2.1-17 §10.2) and
  `resource` (RFC 8707 §2.1), and write the resource's scheme and host in
  capitals (MCP);
- a refresh token rotates on every use; a used one ends its family with no
  grace (design §5.3, falsifier 6), and so does a person who is no longer
  active, audited;
- every refusal of a grant is `invalid_grant` [S33]; the request is RFC 6749's
  (form-encoded, public client, no parameter twice, at most 16 KiB);
- one network is held to `TOKEN_REQUESTS_PER_MINUTE` across both endpoints,
  and Redis down lets it through (B3-24);
- RFC 7009 revocation ends the family, and answers 200 to a token it does not
  know;
- nothing is stored in clear, and OpenBao down spends nothing.

And, from the coordinator's integration notes (operator decision D2,
2026-11-12):

- THE PERSON'S ROW FIRST. A code exchange and a refresh take the person's
  `users` row before anything of theirs (`guards.lock_user_row`), then the
  app's (`clients.hold`), then the code or the token and its family: an
  exchange that meets a disable in flight waits for it and opens no grant a
  later re-enable would bring back; a refresh at the app's idle boundary
  meets its purge with no deadlock, and a use that commits first spares the
  app;
- a code an account act ended (`expires_at = now()`, Task 10) is a plain
  `invalid_grant`: never `mcp.code_reused`, never a family revoked;
- a console address that names no issuer is a 503 before any signature;
- a revocation holds neither the person's row nor the app's (the carve-out).

And from the review's fix round 1:

- THE CLOCK IS READ AFTER THE LOCKS. A code an act ended while the exchange
  was on its way to its locks, or waiting on the act, is refused: any
  `expires_at` the act wrote up to its commit is in the past by then;
- every value the two endpoints read is in its RFC character set, or the
  request is `invalid_request` -- a NUL or a non-ASCII character never
  reaches a query (a 500 until then);
- two requests with one code, or one refresh token, meet on the person's row:
  one wins, the other ends the grant it opened, audited;
- a used code another app presents names both apps in its audit row.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import keys
from app.core.auth.guards import lock_user_row
from app.core.auth.oauth import access, grants, policy
from app.core.auth.oauth.store import oauth_session
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, User
from app.models.oauth import OAuthClient, OAuthCode, OAuthRefreshFamily, OAuthRefreshToken
from app.modules.user_admin import service as ua
from tests.oauth_support import (
    BASE,
    CLAUDE_CALLBACK,
    RESOURCE,
    forget_token_limit,
    oauth_http,
    oauth_org,
    pkce_pair,
    seed_client,
    seed_code,
    token_request,
)
from tests.race_helpers import backend_pid, normalised, until_blocked_by
from tests.session_support import user_id_of

SPENT = "authorization code refused and spent"


class Env:
    def __init__(self, api: Any, org_id: UUID, sub: str, app: OAuthClient) -> None:
        self.api, self.org_id, self.sub, self.app = api, org_id, sub, app


@pytest.fixture()
async def env(api_env) -> Env:
    org_id, _ = await oauth_org(api_env)
    sub = await api_env.create_user(org_id, "org-admin")
    app = await seed_client(
        api_env, org_id, redirect_uris=(CLAUDE_CALLBACK, "http://127.0.0.1/callback")
    )
    return Env(api_env, org_id, sub, app)


async def _code(env: Env, **kw: Any) -> tuple[str, str]:
    verifier, challenge = pkce_pair()
    code = await seed_code(env.api, env.org_id, env.sub, app=env.app, challenge=challenge, **kw)
    return code, verifier


async def _exchange(
    env: Env, code: str, verifier: str, *, redirect_uri: str | None = CLAUDE_CALLBACK, **extra: str
) -> Any:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": env.app.client_id,
        "code_verifier": verifier,
        **({} if redirect_uri is None else {"redirect_uri": redirect_uri}),
        **extra,
    }
    return await token_request(env.api, **form)


async def _tokens(env: Env) -> dict[str, Any]:
    code, verifier = await _code(env)
    resp = await _exchange(env, code, verifier)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _refresh(env: Env, refresh_token: str, **extra: str) -> Any:
    return await token_request(
        env.api,
        grant_type="refresh_token",
        refresh_token=refresh_token,
        client_id=env.app.client_id,
        **extra,
    )


async def _families(env: Env) -> list[OAuthRefreshFamily]:
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        return list((await session.execute(select(OAuthRefreshFamily))).scalars())


async def _events(env: Env, action: str) -> list[AuditEvent]:
    async with AsyncSession(env.api.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == env.org_id, AuditEvent.action == action
                    )
                )
            ).scalars()
        )


async def _disable(env: Env) -> None:
    async with AsyncSession(env.api.admin_engine) as session:
        await session.execute(
            update(User)
            .where(User.id == await user_id_of(env.api, env.sub))
            .values(status="disabled")
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# The code
# --------------------------------------------------------------------------- #


async def test_a_code_is_exchanged_for_an_mcp_token_and_a_refresh_token(env: Env) -> None:
    code, verifier = await _code(env)
    resp = await _exchange(env, code, verifier, resource=RESOURCE)
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["pragma"] == "no-cache"
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 600
    assert body["scope"] == "mcp"
    assert len(body["refresh_token"]) == 43
    grant = await access.verify_access_token(body["access_token"], settings=env.api.settings)
    assert grant is not None
    assert grant.user_id == await user_id_of(env.api, env.sub)
    assert grant.client_id == env.app.client_id
    (family,) = await _families(env)
    assert grant.family_id == family.id
    assert family.resource == RESOURCE
    assert family.revoked_at is None
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        stored = (await session.execute(select(OAuthCode))).scalar_one()
        token_row = (await session.execute(select(OAuthRefreshToken))).scalar_one()
    assert stored.used_at is not None and stored.family_id == family.id
    # Stored as digests: neither value is in its row.
    assert stored.code_hash == grants.hash_secret(code) and code.encode() not in stored.code_hash
    assert token_row.token_hash == grants.hash_secret(body["refresh_token"])


@pytest.mark.parametrize("redirect", ["claude", "loopback"])
async def test_the_exchange_may_leave_out_the_redirect_uri(env: Env, redirect: str) -> None:
    """OAuth 2.1-17 §10.2: an OAuth 2.1 client does not send it (B3-23)."""
    kw = {} if redirect == "claude" else {"redirect_uri": "http://127.0.0.1:51004/callback"}
    code, verifier = await _code(env, **kw)
    resp = await _exchange(env, code, verifier, redirect_uri=None)
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "resource", [None, "HTTPS://ITMS.EXAMPLE.COM/mcp", "https://Itms.Example.com/mcp"]
)
async def test_the_resource_may_be_left_out_or_its_scheme_and_host_capitalised(
    env: Env, resource: str | None
) -> None:
    """RFC 8707 §2.1's default resource, and MCP's "SHOULD accept uppercase
    scheme and host components" (B3-05); the grant names the canonical one."""
    code, verifier = await _code(env)
    extra = {} if resource is None else {"resource": resource}
    resp = await _exchange(env, code, verifier, **extra)
    assert resp.status_code == 200, resp.text
    (family,) = await _families(env)
    assert family.resource == RESOURCE


async def test_a_reused_code_is_refused_and_ends_the_grant_it_opened(env: Env) -> None:
    code, verifier = await _code(env)
    first = await _exchange(env, code, verifier)
    again = await _exchange(env, code, verifier)
    assert first.status_code == 200
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_grant"
    (family,) = await _families(env)
    assert family.revoked_reason == "code_reused"
    (event,) = await _events(env, "mcp.code_reused")
    assert event.actor_type == "system"
    assert event.target_id == str(await user_id_of(env.api, env.sub))
    assert event.meta["app"] == env.app.client_id
    # The grant's refresh token is dead too.
    assert (await _refresh(env, first.json()["refresh_token"])).json()["error"] == "invalid_grant"


@pytest.mark.parametrize("wrong", ["verifier", "redirect", "client"])
async def test_a_failed_exchange_spends_the_code_and_says_so(
    env: Env, wrong: str, caplog: pytest.LogCaptureFixture
) -> None:
    """RFC 6749 §10.5, B3-21: a legitimate client never sends these wrong,
    so the attempt costs the code."""
    code, verifier = await _code(env)
    with caplog.at_level(logging.WARNING, logger="its.auth.oauth.grants"):
        if wrong == "verifier":
            refused = await _exchange(env, code, pkce_pair()[0])
        elif wrong == "redirect":
            refused = await _exchange(env, code, verifier, redirect_uri="http://127.0.0.1/callback")
        else:
            other = await seed_client(env.api, env.org_id)
            refused = await _exchange(env, code, verifier, client_id=other.client_id)
    assert refused.status_code == 400
    assert refused.json()["error"] == "invalid_grant"
    if wrong == "verifier":
        assert (
            refused.json()["error_description"]
            == "the code_verifier does not match the code_challenge"
        )
    (record,) = [r for r in caplog.records if r.name == "its.auth.oauth.grants"]
    assert (record.levelname, record.getMessage(), record.reason) == ("WARNING", SPENT, wrong)
    assert code not in caplog.text and verifier not in caplog.text
    # Spent: the right request after it gets nothing, and nothing was opened.
    assert (await _exchange(env, code, verifier)).json()["error"] == "invalid_grant"
    assert await _families(env) == []
    assert await _events(env, "mcp.code_reused") == []  # no grant existed to end


@pytest.mark.parametrize("change", ["expired", "other-port", "unknown"])
async def test_a_code_outside_its_bounds_is_invalid_grant(env: Env, change: str) -> None:
    if change == "expired":
        code, verifier = await _code(env, now=datetime.now(UTC) - timedelta(seconds=61))
        resp = await _exchange(env, code, verifier)
    elif change == "other-port":
        code, verifier = await _code(env, redirect_uri="http://127.0.0.1:51004/callback")
        resp = await _exchange(env, code, verifier, redirect_uri="http://127.0.0.1:51005/callback")
    else:
        _, verifier = await _code(env)
        resp = await _exchange(env, "c" * 43, verifier)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
    assert await _families(env) == []


async def test_a_loopback_code_is_redeemed_at_the_port_it_was_asked_for(env: Env) -> None:
    code, verifier = await _code(env, redirect_uri="http://127.0.0.1:51004/callback")
    resp = await _exchange(env, code, verifier, redirect_uri="http://127.0.0.1:51004/callback")
    assert resp.status_code == 200, resp.text


async def test_an_unknown_client_is_invalid_client_and_reads_no_code(env: Env) -> None:
    code, verifier = await _code(env)
    resp = await _exchange(env, code, verifier, client_id="itms_nobody")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client"
    assert (await _exchange(env, code, verifier)).status_code == 200


@pytest.mark.parametrize("resource", ["https://itms.example.com", "https://itms.example.com/MCP"])
async def test_another_resource_is_invalid_target(env: Env, resource: str) -> None:
    code, verifier = await _code(env)
    resp = await _exchange(env, code, verifier, resource=resource)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_target"


async def test_a_person_disabled_since_the_approval_gets_nothing(env: Env) -> None:
    code, verifier = await _code(env)
    await _disable(env)
    resp = await _exchange(env, code, verifier)
    assert resp.json()["error"] == "invalid_grant"
    assert await _families(env) == []


async def test_the_apps_last_use_is_written_at_most_once_an_hour(env: Env) -> None:
    """A3-37: the row every person of the organization shares is not
    written by every token request."""
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        recent = datetime.now(UTC) - timedelta(minutes=5)
        await session.execute(
            update(OAuthClient).where(OAuthClient.id == env.app.id).values(last_used_at=recent)
        )
    await _tokens(env)
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        row = await session.get(OAuthClient, env.app.id)
        assert row is not None and row.last_used_at == recent


# --------------------------------------------------------------------------- #
# The refresh token
# --------------------------------------------------------------------------- #


async def test_a_refresh_rotates_and_the_old_token_ends_the_family(env: Env) -> None:
    first = await _tokens(env)
    rotated = await _refresh(env, first["refresh_token"])
    assert rotated.status_code == 200, rotated.text
    second = rotated.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert await access.verify_access_token(second["access_token"], settings=env.api.settings)
    # The old one again, however soon: the family ends, the new one with it
    # (falsifier 6; no grace window, B3-27).
    reused = await _refresh(env, first["refresh_token"])
    assert reused.status_code == 400
    assert reused.json()["error"] == "invalid_grant"
    (family,) = await _families(env)
    assert family.revoked_reason == "refresh_reused"
    assert (await _refresh(env, second["refresh_token"])).json()["error"] == "invalid_grant"
    (event,) = await _events(env, "mcp.refresh_reused")
    assert event.actor_type == "system"
    assert event.meta["family"] == str(family.id)


async def test_a_refresh_moves_the_idle_limit_and_an_idle_family_is_dead(env: Env) -> None:
    first = await _tokens(env)
    (family,) = await _families(env)
    now = datetime.now(UTC)
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        # Opened 31 days ago and idle since (`expires_at > created_at` holds).
        await session.execute(
            update(OAuthRefreshFamily)
            .where(OAuthRefreshFamily.id == family.id)
            .values(
                created_at=now - timedelta(days=31),
                last_used_at=now - timedelta(days=31),
                expires_at=now - timedelta(seconds=1),
            )
        )
    resp = await _refresh(env, first["refresh_token"])
    assert resp.json()["error"] == "invalid_grant"


async def test_a_refresh_extends_the_family_by_thirty_days(env: Env) -> None:
    first = await _tokens(env)
    before = datetime.now(UTC)
    assert (await _refresh(env, first["refresh_token"])).status_code == 200
    (family,) = await _families(env)
    assert family.expires_at >= before + timedelta(days=30)


async def test_a_refresh_by_another_client_is_invalid_grant(env: Env) -> None:
    first = await _tokens(env)
    other = await seed_client(env.api, env.org_id)
    resp = await token_request(
        env.api,
        grant_type="refresh_token",
        refresh_token=first["refresh_token"],
        client_id=other.client_id,
    )
    assert resp.json()["error"] == "invalid_grant"
    (family,) = await _families(env)
    assert family.revoked_at is None  # not a reuse: the owner's token still works
    assert (await _refresh(env, first["refresh_token"])).status_code == 200


async def test_a_disabled_person_cannot_refresh_and_the_end_is_audited(env: Env) -> None:
    first = await _tokens(env)
    await _disable(env)
    assert (await _refresh(env, first["refresh_token"])).json()["error"] == "invalid_grant"
    (family,) = await _families(env)
    assert family.revoked_reason == "person_inactive"
    (event,) = await _events(env, "mcp.grant_ended")
    assert event.actor_type == "system"
    assert event.meta == {
        "app": env.app.client_id,
        "family": str(family.id),
        "reason": "person_inactive",
    }


async def test_a_refresh_is_not_audited(env: Env) -> None:
    """B3-22: it continues a consent already audited."""
    first = await _tokens(env)
    assert (await _refresh(env, first["refresh_token"])).status_code == 200
    async with AsyncSession(env.api.admin_engine) as session:
        actions = set(
            (
                await session.execute(
                    select(AuditEvent.action).where(
                        AuditEvent.org_id == env.org_id, AuditEvent.action.like("mcp.%")
                    )
                )
            ).scalars()
        )
    assert actions == set()


@pytest.mark.parametrize(
    ("scope", "ok"), [("mcp", True), ("mcp offline_access", True), ("openid admin", False)]
)
async def test_a_refresh_may_ask_for_no_more_than_mcp(env: Env, scope: str, ok: bool) -> None:
    first = await _tokens(env)
    resp = await _refresh(env, first["refresh_token"], scope=scope)
    if ok:
        assert resp.status_code == 200 and resp.json()["scope"] == "mcp"
    else:
        assert resp.json()["error"] == "invalid_scope"


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("grant_type", ["client_credentials", "password", "implicit", "urn:x"])
async def test_only_two_grant_types_exist(env: Env, grant_type: str) -> None:
    """No machine-to-machine token: every one names a person who consented
    (invariant 1c(a))."""
    resp = await token_request(env.api, grant_type=grant_type, client_id=env.app.client_id)
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"


async def test_client_authentication_is_refused_with_its_challenge(env: Env) -> None:
    async with oauth_http(env.api) as client:
        resp = await client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": "x"},
            headers={"Authorization": "Basic aXRtc194OnNlY3JldA=="},
        )
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_client"
    assert resp.headers["www-authenticate"] == 'Basic realm="itms"'


async def test_the_request_is_one_form_with_no_parameter_twice(env: Env) -> None:
    async with oauth_http(env.api) as client:
        as_json = await client.post("/oauth/token", json={"grant_type": "refresh_token"})
        twice = await client.post(
            "/oauth/token",
            content=b"grant_type=refresh_token&refresh_token=a&refresh_token=b&client_id=x",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        missing = await client.post("/oauth/token", data={"grant_type": "refresh_token"})
    for resp in (as_json, twice, missing):
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"


@pytest.mark.parametrize("path", ["/oauth/token", "/oauth/revoke"])
async def test_a_body_over_16_kib_is_an_rfc_6749_413(env: Env, path: str) -> None:
    async with oauth_http(env.api) as client:
        resp = await client.post(
            path,
            content=b"token=" + b"a" * policy.MAX_REQUEST_BYTES,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert resp.status_code == 413
    assert resp.json() == {
        "error": "invalid_request",
        "error_description": "the request body is too large",
    }
    assert resp.headers["cache-control"] == "no-store"


async def test_one_network_is_held_to_its_requests_a_minute_on_both_endpoints(env: Env) -> None:
    """RFC 7009 §5 and B3-24: the token endpoint's countermeasures apply to
    revocation too; one bucket for both."""
    await forget_token_limit(env.api)
    redis = env.api.app.state.redis
    await redis.set("ratelimit:oauth-token:ip:127.0.0.1", policy.TOKEN_REQUESTS_PER_MINUTE, ex=60)
    async with oauth_http(env.api) as client:
        token = await client.post("/oauth/token", data={"grant_type": "refresh_token"})
        revoke = await client.post("/oauth/revoke", data={"token": "x", "client_id": "y"})
    for resp in (token, revoke):
        assert resp.status_code == 429
        assert resp.json()["error"] == "temporarily_unavailable"
        assert resp.headers["retry-after"] == "60"
    await forget_token_limit(env.api)


async def test_without_redis_the_limit_lets_the_request_through(env: Env, monkeypatch) -> None:
    from redis.exceptions import ConnectionError as RedisDown

    class Down:
        async def incr(self, _key: str) -> int:
            raise RedisDown("down")

    first = await _tokens(env)
    monkeypatch.setattr(env.api.app.state, "redis", Down())
    async with oauth_http(env.api) as client:
        resp = await client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": first["refresh_token"],
                "client_id": env.app.client_id,
            },
        )
    assert resp.status_code == 200, resp.text


async def test_without_the_deployments_organization_the_server_is_unavailable(env: Env) -> None:
    env.api.settings.default_org_slug = None
    resp = await token_request(
        env.api, grant_type="refresh_token", refresh_token="x", client_id="y"
    )
    assert resp.status_code == 503
    assert resp.json()["error"] == "temporarily_unavailable"


async def test_openbao_down_is_503_and_spends_nothing(env: Env, monkeypatch) -> None:
    code, verifier = await _code(env)

    async def down(*args: Any, **kwargs: Any) -> str:
        raise keys.AuthKeysUnavailable("down")

    monkeypatch.setattr(keys, "sign_jwt", down)
    resp = await _exchange(env, code, verifier)
    assert resp.status_code == 503
    assert resp.json()["error"] == "temporarily_unavailable"
    monkeypatch.undo()
    assert (await _exchange(env, code, verifier)).status_code == 200


# --------------------------------------------------------------------------- #
# RFC 7009
# --------------------------------------------------------------------------- #


async def _revoke(env: Env, token: str, client_id: str | None = None, **extra: str) -> Any:
    await forget_token_limit(env.api)
    async with oauth_http(env.api) as client:
        return await client.post(
            "/oauth/revoke",
            data={"token": token, "client_id": client_id or env.app.client_id, **extra},
        )


@pytest.mark.parametrize("which", ["refresh_token", "access_token"])
async def test_revoking_either_token_ends_the_grant(env: Env, which: str) -> None:
    tokens = await _tokens(env)
    resp = await _revoke(env, tokens[which], token_type_hint="not-a-real-hint")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    (family,) = await _families(env)
    assert family.revoked_reason == "revoked_by_app"
    assert (await _refresh(env, tokens["refresh_token"])).json()["error"] == "invalid_grant"
    (event,) = await _events(env, "mcp.grant_revoked")
    assert event.actor_type == "system"


async def test_revoking_a_token_nobody_issued_is_still_200(env: Env) -> None:
    assert (await _revoke(env, "nonsense")).status_code == 200
    assert (await _revoke(env, "a.b.c")).status_code == 200


async def test_revoking_another_clients_token_is_refused(env: Env) -> None:
    tokens = await _tokens(env)
    other = await seed_client(env.api, env.org_id)
    resp = await _revoke(env, tokens["refresh_token"], client_id=other.client_id)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
    (family,) = await _families(env)
    assert family.revoked_at is None


async def test_revoking_for_an_unknown_client_is_invalid_client(env: Env) -> None:
    resp = await _revoke(env, "x", client_id="itms_nobody")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client"


def test_a_resource_is_named_by_scheme_and_host_in_any_case_and_the_rest_exactly() -> None:
    assert grants.names_resource("HTTPS://ITMS.EXAMPLE.COM/mcp", RESOURCE)
    assert not grants.names_resource("https://itms.example.com/Mcp", RESOURCE)
    assert not grants.names_resource("https://itms.example.com:443/mcp", RESOURCE)
    assert not grants.names_resource("https://[::1", RESOURCE)


# --------------------------------------------------------------------------- #
# The coordinator's integration notes: a code an account act ended (D2, part
# c), and no signature without an issuer (Task 2's review).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("verifier_ok", [True, False])
async def test_a_code_an_account_act_ended_is_plain_invalid_grant(
    env: Env, verifier_ok: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """Operator decision D2, part c: the account acts end a person's unspent
    codes by setting `expires_at = now()`, never `used_at` (Task 10). The
    exchange answers such a code with a plain `invalid_grant`, whatever the
    request holds: it spends nothing, logs nothing, audits nothing and ends no
    family -- not `mcp.code_reused`, which a set `used_at` would have fired.
    Presented twice, the same."""
    first = await _tokens(env)  # a live grant of the same person and app
    code, verifier = await _code(env)
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        ended = await session.execute(
            update(OAuthCode)
            .where(OAuthCode.code_hash == grants.hash_secret(code), OAuthCode.used_at.is_(None))
            .values(expires_at=func.now())
        )
        assert ended.rowcount == 1
    presented = verifier if verifier_ok else pkce_pair()[0]
    with caplog.at_level(logging.WARNING, logger="its.auth.oauth.grants"):
        for _ in range(2):
            resp = await _exchange(env, code, presented)
            assert resp.status_code == 400
            assert resp.json() == {
                "error": "invalid_grant",
                "error_description": "the authorization code is not valid",
            }
    assert [r for r in caplog.records if r.name == "its.auth.oauth.grants"] == []
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        stored = (
            await session.execute(
                select(OAuthCode).where(OAuthCode.code_hash == grants.hash_secret(code))
            )
        ).scalar_one()
    assert stored.used_at is None and stored.family_id is None
    assert await _events(env, "mcp.code_reused") == []
    (family,) = await _families(env)
    assert family.revoked_at is None
    assert (await _refresh(env, first["refresh_token"])).status_code == 200


@pytest.mark.parametrize("console", ["http://itms.example.com", "https://itms.example.com/console"])
async def test_a_console_address_that_names_no_issuer_is_503_before_any_signature(
    env: Env, console: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 2's review: `access.mint_access_token` raises `ValueError` without
    an issuer. The routes answer 503 before they can reach it -- never a 500
    -- on every grant and on revocation, and spend nothing."""
    first = await _tokens(env)
    code, verifier = await _code(env)
    signed: list[str] = []
    real_sign = keys.sign_jwt

    async def counting(*args: Any, **kwargs: Any) -> str:
        signed.append("signed")
        return await real_sign(*args, **kwargs)

    monkeypatch.setattr(keys, "sign_jwt", counting)
    env.api.settings.public_console_url = console
    for resp in (
        await _exchange(env, code, verifier),
        await _refresh(env, first["refresh_token"]),
        await _revoke(env, first["refresh_token"]),
    ):
        assert resp.status_code == 503, resp.text
        assert resp.json() == {
            "error": "temporarily_unavailable",
            "error_description": "the authorization server is not configured on this installation",
        }
        assert resp.headers["cache-control"] == "no-store"
    assert signed == []
    env.api.settings.public_console_url = BASE
    assert (await _exchange(env, code, verifier)).status_code == 200
    assert (await _refresh(env, first["refresh_token"])).status_code == 200


# --------------------------------------------------------------------------- #
# The lock order (operator decision D2): the person's row, then the app's,
# then the grant rows; a revocation takes neither.
# --------------------------------------------------------------------------- #


def _pause(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, name: str
) -> tuple[asyncio.Event, asyncio.Event, dict[str, int]]:
    """Hold a transaction at its first call to `<module>.<name>`, inside it:
    (paused, go, {"pid": its backend})."""
    paused, go = asyncio.Event(), asyncio.Event()
    seen: dict[str, int] = {}
    real: Callable[..., Awaitable[Any]] = getattr(module, name)

    async def held(session: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        if "pid" not in seen:
            seen["pid"] = await backend_pid(session)
            paused.set()
            await go.wait()
        return await real(session, *args, **kwargs)

    monkeypatch.setattr(module, name, held)
    return paused, go, seen


def _on_the_person(waiting: list[str]) -> bool:
    """Every waiter waits on the person's `users` row, taken first FOR NO KEY
    UPDATE (`guards.lock_user_row`)."""
    return bool(waiting) and all(
        "FROM USERS " in normalised(q) and "FOR NO KEY UPDATE" in normalised(q) for q in waiting
    )


async def _switch(env: Env, actor_id: UUID, user_id: UUID, *, enabled: bool) -> str:
    async with org_session(
        env.org_id, engine=env.api.app_engine, clients=ClientScope.all()
    ) as session:
        await ua.set_enabled(
            session, org_id=env.org_id, actor_id=actor_id, user_id=user_id, enabled=enabled
        )
    return "committed"


async def test_an_exchange_waits_on_a_disable_and_leaves_no_grant_for_a_re_enable(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2, race (a). An administrator switches a technician off and is held
    where it ends the sessions -- the status written, the person's row held.
    The technician's AI app exchanges a code it was given just before. Read
    unlocked, the exchange saw the person active, opened a family the disable
    never saw, and a later re-enable brought the app's access back. Now it
    waits on the person's ROW, finds them disabled, opens nothing; after the
    re-enable no live grant is left."""
    admin_id = await user_id_of(env.api, env.sub)
    person = await env.api.create_user(env.org_id)
    person_id = await user_id_of(env.api, person)
    verifier, challenge = pkce_pair()
    code = await seed_code(env.api, env.org_id, person, app=env.app, challenge=challenge)
    paused, go, seen = _pause(monkeypatch, ua, "end_all_for_user")
    disabling = asyncio.create_task(_switch(env, admin_id, person_id, enabled=False))
    await asyncio.wait_for(paused.wait(), 30)
    exchanging = asyncio.create_task(_exchange(env, code, verifier))
    watching = asyncio.create_task(until_blocked_by(env.api.app_engine, seen["pid"]))
    try:
        await asyncio.wait({watching, exchanging}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        seen_waiting = watching.done() and watching.exception() is None
        waiting = watching.result() if seen_waiting else []
        held = seen_waiting and not exchanging.done()
    finally:
        if not watching.done():
            watching.cancel()
        go.set()
    disabled, exchanged = await asyncio.wait_for(
        asyncio.gather(disabling, exchanging, return_exceptions=True), timeout=60
    )
    assert disabled == "committed", disabled
    assert not isinstance(exchanged, BaseException), exchanged
    assert held, exchanged.text
    assert _on_the_person(waiting), waiting
    assert exchanged.status_code == 400, exchanged.text
    assert exchanged.json()["error"] == "invalid_grant"
    assert await _switch(env, admin_id, person_id, enabled=True) == "committed"
    assert [f for f in await _families(env) if f.revoked_at is None] == []


async def test_a_refresh_at_the_idle_boundary_meets_its_apps_purge_and_spares_it(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2, race (b). An app last used thirty days ago, less five minutes: a
    refresh still finds it; a purge ten minutes later (`clients.is_idle`
    inside its DELETE) would take it, its grants by cascade. The refresh is
    held where it writes its new refresh token -- the person's row, the app
    row (KEY SHARE) and its token and family held. The purge waits on the
    APP ROW, not in its cascade; the refresh goes on, signs, and writes the
    app's use without waiting on the queued purge; once it commits the
    purge's own condition no longer holds. No 40P01, and the app and its
    grant survive. Without the app row held first, the purge took the app
    and waited in its cascade on the family while the refresh's use waited
    on the app: a deadlock."""
    first = await _tokens(env)
    start = datetime.now(UTC)
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        await session.execute(
            update(OAuthClient)
            .where(OAuthClient.id == env.app.id)
            .values(last_used_at=start - policy.CLIENT_IDLE + timedelta(minutes=5))
        )
    purge_now = start + timedelta(minutes=10)

    async def purge() -> int:
        async with oauth_session(env.org_id, env.api.app_engine) as session:
            result = await session.execute(
                delete(OAuthClient).where(
                    OAuthClient.id == env.app.id,
                    OAuthClient.last_used_at <= purge_now - policy.CLIENT_IDLE,
                )
            )
            return int(result.rowcount)

    paused, go, seen = _pause(monkeypatch, grants, "_new_refresh_token")
    refreshing = asyncio.create_task(_refresh(env, first["refresh_token"]))
    await asyncio.wait_for(paused.wait(), 30)
    purging = asyncio.create_task(purge())
    try:
        waiting = await until_blocked_by(env.api.app_engine, seen["pid"])
        held = not purging.done()
    finally:
        go.set()
    refreshed, purged = await asyncio.wait_for(
        asyncio.gather(refreshing, purging, return_exceptions=True), timeout=60
    )
    assert held
    assert [normalised(q).split(" ")[0] for q in waiting] == ["DELETE"], waiting
    assert not isinstance(refreshed, BaseException), refreshed
    assert refreshed.status_code == 200, refreshed.text
    assert purged == 0, purged
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        app = await session.get(OAuthClient, env.app.id)
        assert app is not None and app.last_used_at >= start
    (family,) = await _families(env)
    assert family.revoked_at is None


async def test_a_revocation_holds_neither_the_persons_row_nor_the_apps(env: Env) -> None:
    """The carve-out (`oauth.store`): an RFC 7009 revocation is one UPDATE of
    the grant's family and then only the chain. It ends the grant while
    another transaction holds the person's row FOR NO KEY UPDATE and the app
    row FOR UPDATE, as an account act and a purge would."""
    tokens = await _tokens(env)
    person_id = await user_id_of(env.api, env.sub)
    async with oauth_session(env.org_id, env.api.app_engine) as session:
        assert await lock_user_row(session, person_id) is not None
        await session.execute(
            select(OAuthClient.id).where(OAuthClient.id == env.app.id).with_for_update()
        )
        resp = await asyncio.wait_for(_revoke(env, tokens["refresh_token"]), timeout=30)
        assert resp.status_code == 200, resp.text
    (family,) = await _families(env)
    assert family.revoked_reason == "revoked_by_app"


# --------------------------------------------------------------------------- #
# Fix round 1 (the review of Task 7).
# --------------------------------------------------------------------------- #

BAD_CODE_BODY = {
    "error": "invalid_grant",
    "error_description": "the authorization code is not valid",
}


def _end_codes(user_id: UUID, clock: str) -> Any:
    """What an account act does to a person's unspent codes (Task 10; D2,
    part c): `expires_at` set to the act's clock, `used_at` left alone."""
    value = func.now() if clock == "now" else func.clock_timestamp()
    return (
        update(OAuthCode)
        .where(OAuthCode.user_id == user_id, OAuthCode.used_at.is_(None))
        .values(expires_at=value)
    )


async def _live_families(env: Env) -> list[OAuthRefreshFamily]:
    return [f for f in await _families(env) if f.revoked_at is None]


@pytest.mark.parametrize("clock", ["now", "clock_timestamp"])
async def test_a_code_ended_while_the_exchange_reaches_its_locks_is_refused(
    env: Env, clock: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review, Important 1 (the reviewer's probe). The exchange is held just
    before it locks the person -- its request read, its code looked up. An
    account act ends the person's unspent codes and commits. Released, the
    exchange checks the code against a clock read AFTER its locks: expired, a
    plain `invalid_grant`, and no grant. With the clock it read on arrival it
    found the code live and opened one after the act."""
    code, verifier = await _code(env)
    person_id = await user_id_of(env.api, env.sub)
    paused, go, _seen = _pause(monkeypatch, grants, "lock_user_row")
    exchanging = asyncio.create_task(_exchange(env, code, verifier))
    try:
        await asyncio.wait_for(paused.wait(), 30)
        async with oauth_session(env.org_id, env.api.app_engine) as act:
            assert (await act.execute(_end_codes(person_id, clock))).rowcount == 1
    finally:
        go.set()
    resp = await asyncio.wait_for(exchanging, 30)
    assert (resp.status_code, resp.json()) == (400, BAD_CODE_BODY), resp.text
    assert await _live_families(env) == []


@pytest.mark.parametrize("clock", ["now", "clock_timestamp"])
async def test_a_code_ended_by_an_act_the_exchange_waited_on_is_refused(
    env: Env, clock: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review, Important 1. The act holds the person's row, as Task 10's acts
    do; the exchange, arrived before the act's clock, waits on that row. The
    act ends the person's unspent codes and grants and commits. The exchange
    reads its clock after the lock it waited for: the code is expired, a plain
    `invalid_grant`, and no live grant is left."""
    code, verifier = await _code(env)
    person_id = await user_id_of(env.api, env.sub)
    paused, go, _seen = _pause(monkeypatch, grants, "lock_user_row")
    exchanging = asyncio.create_task(_exchange(env, code, verifier))
    try:
        await asyncio.wait_for(paused.wait(), 30)
        async with oauth_session(env.org_id, env.api.app_engine) as act:
            assert await lock_user_row(act, person_id) is not None
            go.set()
            waiting = await until_blocked_by(env.api.app_engine, await backend_pid(act))
            held = not exchanging.done()
            assert (await act.execute(_end_codes(person_id, clock))).rowcount == 1
            await grants.revoke_all_for_user(act, person_id, "signed_out_everywhere")
    finally:
        go.set()
    resp = await asyncio.wait_for(exchanging, 30)
    assert held
    assert _on_the_person(waiting), waiting
    assert (resp.status_code, resp.json()) == (400, BAD_CODE_BODY), resp.text
    assert await _live_families(env) == []


async def test_a_refresh_reads_its_clock_after_its_locks(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review, Important 1, the refresh's side: a family whose idle limit
    falls while the refresh is on its way to its locks is dead when it gets
    there."""
    first = await _tokens(env)
    paused, go, _seen = _pause(monkeypatch, grants, "lock_user_row")
    refreshing = asyncio.create_task(_refresh(env, first["refresh_token"]))
    try:
        await asyncio.wait_for(paused.wait(), 30)
        async with oauth_session(env.org_id, env.api.app_engine) as act:
            await act.execute(update(OAuthRefreshFamily).values(expires_at=func.clock_timestamp()))
    finally:
        go.set()
    resp = await asyncio.wait_for(refreshing, 30)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_grant"


#: One valid request per (endpoint, grant), every value inside its set.
def _valid_form(env: Env, grant: str) -> dict[str, str]:
    if grant == "authorization_code":
        return {
            "grant_type": "authorization_code",
            "client_id": env.app.client_id,
            "code": "c" * 43,
            "code_verifier": "v" * 43,
            "redirect_uri": CLAUDE_CALLBACK,
            "resource": RESOURCE,
        }
    if grant == "refresh_token":
        return {
            "grant_type": "refresh_token",
            "client_id": env.app.client_id,
            "refresh_token": "r" * 43,
            "scope": "mcp",
        }
    return {"client_id": env.app.client_id, "token": "t" * 43}


READ_PARAMETERS = [
    ("/oauth/token", "authorization_code", "grant_type"),
    ("/oauth/token", "authorization_code", "client_id"),
    ("/oauth/token", "authorization_code", "code"),
    ("/oauth/token", "authorization_code", "code_verifier"),
    ("/oauth/token", "authorization_code", "redirect_uri"),
    ("/oauth/token", "authorization_code", "resource"),
    ("/oauth/token", "refresh_token", "refresh_token"),
    ("/oauth/token", "refresh_token", "scope"),
    ("/oauth/revoke", "revoke", "client_id"),
    ("/oauth/revoke", "revoke", "token"),
]


@pytest.mark.parametrize("value", ["a\x00b", "café", "a\x7fb"], ids=["nul", "non-ascii", "del"])
@pytest.mark.parametrize(("path", "grant", "name"), READ_PARAMETERS)
async def test_a_value_outside_its_character_set_is_invalid_request(
    env: Env, path: str, grant: str, name: str, value: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Review, Important 2 (the reviewer's probe). `parse_qsl` decodes `%00`,
    and PostgreSQL refuses a NUL in text: a 500 and an ERROR line on two
    unauthenticated endpoints. Every value these routes read is VSCHAR
    (`%x20-7E`; RFC 6749 Appendix A: `client_id` A.1, `code` A.11,
    `refresh_token` A.17), the verifier unreserved (RFC 7636 §4.1);
    anything else is `invalid_request`, before any query reads it."""
    caplog.set_level(logging.ERROR)
    form = {**_valid_form(env, grant), name: value}
    await forget_token_limit(env.api)
    async with oauth_http(env.api) as client:
        resp = await client.post(path, data=form)
    assert resp.status_code == 400, resp.text
    assert resp.json() == {
        "error": "invalid_request",
        "error_description": "the request is malformed",
    }
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.parametrize(
    "body",
    [
        b"grant_type=refresh_token&refresh_token=%FF&client_id=x",  # not UTF-8
        b"grant_type=refresh_token&refresh_token=a%0Ab&client_id=x",  # a line feed
    ],
)
async def test_an_escape_that_decodes_outside_the_set_is_invalid_request(
    env: Env, body: bytes
) -> None:
    async with oauth_http(env.api) as client:
        resp = await client.post(
            "/oauth/token",
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.parametrize("verifier", ["!" * 43, "a" * 42, "a" * 129])
async def test_a_verifier_outside_rfc_7636_is_invalid_request_and_spends_nothing(
    env: Env, verifier: str
) -> None:
    """RFC 7636 §4.1: `code-verifier = 43*128unreserved`. A value outside that
    grammar is malformed, refused before the code is read."""
    code, right = await _code(env)
    resp = await _exchange(env, code, verifier)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"
    assert (await _exchange(env, code, right)).status_code == 200


async def test_a_parameter_the_revocation_does_not_read_is_ignored(env: Env) -> None:
    """RFC 6749 §3.2: "MUST ignore unrecognized request parameters", and RFC
    7009 §2.2 ignores `token_type_hint`'s value: neither is held to a set."""
    tokens = await _tokens(env)
    resp = await _revoke(env, tokens["refresh_token"], token_type_hint="\x00", other="café")
    assert resp.status_code == 200, resp.text
    (family,) = await _families(env)
    assert family.revoked_reason == "revoked_by_app"


async def test_two_exchanges_of_one_code_meet_on_the_person_and_one_wins(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review, Minor 3. The first exchange is held where it opens its grant;
    the second, with the same code, waits on the person's row. Released, the
    first gets the tokens; the second finds the code used, ends the grant
    the first opened and says so: RFC 6749 §4.1.2."""
    code, verifier = await _code(env)
    paused, go, seen = _pause(monkeypatch, grants, "open_family")
    first = asyncio.create_task(_exchange(env, code, verifier))
    second: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(paused.wait(), 30)
        second = asyncio.create_task(_exchange(env, code, verifier))
        waiting = await until_blocked_by(env.api.app_engine, seen["pid"])
        held = not second.done()
    finally:
        go.set()
    assert second is not None
    won, lost = await asyncio.wait_for(asyncio.gather(first, second), timeout=60)
    assert held
    assert _on_the_person(waiting), waiting
    assert won.status_code == 200, won.text
    assert (lost.status_code, lost.json()) == (400, BAD_CODE_BODY)
    grant = await access.verify_access_token(won.json()["access_token"], settings=env.api.settings)
    (family,) = await _families(env)
    assert grant is not None and grant.family_id == family.id
    assert family.revoked_reason == "code_reused"
    (event,) = await _events(env, "mcp.code_reused")
    assert event.meta == {"app": env.app.client_id, "family": str(family.id)}


async def test_two_refreshes_of_one_token_meet_on_the_person_and_one_wins(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review, Minor 3. The first refresh is held where it writes its new
    token; the second, with the same token, waits on the person's row.
    Released, the first rotates; the second finds the token used and ends the
    family, the first's new token with it (design §5.3, no grace)."""
    tokens = await _tokens(env)
    paused, go, seen = _pause(monkeypatch, grants, "_new_refresh_token")
    first = asyncio.create_task(_refresh(env, tokens["refresh_token"]))
    second: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(paused.wait(), 30)
        second = asyncio.create_task(_refresh(env, tokens["refresh_token"]))
        waiting = await until_blocked_by(env.api.app_engine, seen["pid"])
        held = not second.done()
    finally:
        go.set()
    assert second is not None
    won, lost = await asyncio.wait_for(asyncio.gather(first, second), timeout=60)
    assert held
    assert _on_the_person(waiting), waiting
    assert won.status_code == 200, won.text
    assert lost.status_code == 400 and lost.json()["error"] == "invalid_grant"
    (family,) = await _families(env)
    assert family.revoked_reason == "refresh_reused"
    (event,) = await _events(env, "mcp.refresh_reused")
    assert event.meta == {"app": env.app.client_id, "family": str(family.id)}
    rotated = won.json()["refresh_token"]
    assert (await _refresh(env, rotated)).json()["error"] == "invalid_grant"


@pytest.mark.parametrize("presenter", ["its-own-app", "another-app"])
async def test_a_reused_codes_audit_row_names_the_app_that_presented_it(
    env: Env, presenter: str
) -> None:
    """Review, Minor 4. `app` is the app whose grant ended; when another app
    presents the used code -- the likeliest trace of a stolen one --
    `presented_by` names it too."""
    code, verifier = await _code(env)
    assert (await _exchange(env, code, verifier)).status_code == 200
    other = await seed_client(env.api, env.org_id)
    extra = {} if presenter == "its-own-app" else {"client_id": other.client_id}
    again = await _exchange(env, code, verifier, **extra)
    assert (again.status_code, again.json()) == (400, BAD_CODE_BODY)
    (family,) = await _families(env)
    assert family.revoked_reason == "code_reused"
    (event,) = await _events(env, "mcp.code_reused")
    expected = {"app": env.app.client_id, "family": str(family.id)}
    if presenter == "another-app":
        expected["presented_by"] = other.client_id
    assert event.meta == expected
