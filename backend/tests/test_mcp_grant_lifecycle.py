"""An act on the account ends the person's AI connections (autenticación
propia design §2.4, §5.3, §7; plan 2026-11-06, Task 10).

The acts that forget a person's remembered browsers end their grants too, with
the act as the reason and the count in the act's own audit event: being
disabled or deleted, a factor reset (by an administrator or by the recovery
link), a password change (their own, or by the mail link) and "sign out
everywhere". An administrator ending the person's sessions forgets no browser,
and ends no grant (B3-07).

And, from the coordinator's integration notes (operator decision D2, part c,
2026-11-12): each of those acts also ends the person's UNSPENT authorization
codes -- `expires_at` set to the act's clock, `used_at` never set -- before it
ends their grants, in the same transaction. An act that leaves the person
active (signing out everywhere, a new password, a factor reset) would
otherwise let a code approved a moment before it be exchanged after it into a
new, live grant: the exchange waits on the person's row and then simply runs.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import action_tokens, flows
from app.core.auth.oauth import grants
from app.core.auth.oauth.store import oauth_session
from app.core.mcp_channel import mcp_resource
from app.models import AuditEvent
from app.models.oauth import OAuthCode, OAuthRefreshFamily
from tests.auth_support import browser, capture_mail, console_ready, link_token, signed_in
from tests.mcp_support import mcp_app
from tests.oauth_support import CLAUDE_CALLBACK, pkce_pair, token_request
from tests.race_helpers import backend_pid, normalised, until_blocked_by

NEW_PASSWORD = "Pelican-Orbit-Lantern-58"


@pytest.fixture()
def lifecycle_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def _connected(env: Any, org_id: UUID, user_id: UUID) -> UUID:
    """A live grant of the person to the AI app "claude": what approving it on
    the consent screen leaves (Task 8)."""
    resource = mcp_resource(env.settings)
    assert resource is not None
    app_id = await mcp_app(env, org_id)
    async with oauth_session(org_id, env.app_engine) as session:
        family, _ = await grants.open_family(
            session,
            org_id=org_id,
            user_id=user_id,
            oauth_client_id=app_id,
            resource=resource,
            now=datetime.now(UTC),
        )
        family_id: UUID = family.id
    return family_id


async def _reason(env: Any, family_id: UUID) -> str | None:
    async with AsyncSession(env.admin_engine) as session:
        reason: str | None = (
            await session.execute(
                select(OAuthRefreshFamily.revoked_reason).where(OAuthRefreshFamily.id == family_id)
            )
        ).scalar_one()
    return reason


async def _counted(env: Any, org_id: UUID, action: str) -> Any:
    """`ai_grants_revoked` of the act's latest audit event (B3-30)."""
    async with AsyncSession(env.admin_engine) as session:
        event = (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.org_id == org_id, AuditEvent.action == action)
                .order_by(AuditEvent.id.desc())
                .limit(1)
            )
        ).scalar_one()
    return event.meta.get("ai_grants_revoked")


async def _issue(env: Any, org_id: UUID, user_id: UUID, challenge: str) -> tuple[str, UUID]:
    """A code the person approved for "claude" a moment ago and the app has
    not exchanged yet (Task 8's `issue_code`): the value and its row."""
    resource = mcp_resource(env.settings)
    assert resource is not None
    app_id = await mcp_app(env, org_id)
    async with oauth_session(org_id, env.app_engine) as session:
        code = await grants.issue_code(
            session,
            org_id=org_id,
            user_id=user_id,
            oauth_client_id=app_id,
            redirect_uri=CLAUDE_CALLBACK,
            code_challenge=challenge,
            resource=resource,
            now=datetime.now(UTC),
        )
        row_id: UUID = (
            await session.execute(
                select(OAuthCode.id).where(OAuthCode.code_hash == grants.hash_secret(code))
            )
        ).scalar_one()
    return code, row_id


async def _pending(env: Any, org_id: UUID, user_id: UUID) -> UUID:
    """An unspent code of the person's, by its row."""
    _, row_id = await _issue(env, org_id, user_id, pkce_pair()[1])
    return row_id


async def _ended(env: Any, code_id: UUID) -> bool:
    """The code was ENDED (D2, part c): its expiry is past by the database's
    clock, and it was never spent -- `used_at` would have read as a reuse
    (`mcp.code_reused`) at its next presentation."""
    async with AsyncSession(env.admin_engine) as session:
        row = (
            await session.execute(
                select(OAuthCode.used_at, OAuthCode.expires_at <= func.clock_timestamp()).where(
                    OAuthCode.id == code_id
                )
            )
        ).one()
    used_at, expired = row
    assert used_at is None, "an act never spends a code"
    return bool(expired)


async def test_disabling_a_person_ends_their_ai_connections(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("grant-disable")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")
    theirs = await _connected(env, org, member.user_id)
    mine = await _connected(env, org, admin.user_id)
    pending = await _pending(env, org, member.user_id)
    my_pending = await _pending(env, org, admin.user_id)

    resp = await env.client.post(f"/api/v1/users/{member.user_id}/disable", headers=admin.headers())

    assert resp.status_code == 200, resp.text
    assert await _reason(env, theirs) == "disabled"
    assert await _counted(env, org, "user.disabled") == 1
    # Only that person's: the administrator's own connection stands.
    assert await _reason(env, mine) is None
    assert await _ended(env, pending)
    assert not await _ended(env, my_pending)


async def test_enabling_a_person_counts_no_grant(lifecycle_env: Any) -> None:
    """The count exists only when the act ends grants: switching a person back
    on leaves `user.enabled` as it was."""
    env = lifecycle_env
    org = await env.create_org("grant-enable")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")
    off = await env.client.post(f"/api/v1/users/{member.user_id}/disable", headers=admin.headers())
    assert off.status_code == 200, off.text

    on = await env.client.post(f"/api/v1/users/{member.user_id}/enable", headers=admin.headers())

    assert on.status_code == 200, on.text
    async with AsyncSession(env.admin_engine) as session:
        enabled = (
            await session.execute(
                select(AuditEvent.meta).where(
                    AuditEvent.org_id == org, AuditEvent.action == "user.enabled"
                )
            )
        ).scalar_one()
    assert enabled == {"email": member.email, "status": "active"}


async def test_deleting_a_person_ends_their_ai_connections(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("grant-delete")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")
    family = await _connected(env, org, member.user_id)
    pending = await _pending(env, org, member.user_id)

    resp = await env.client.delete(f"/api/v1/users/{member.user_id}", headers=admin.headers())

    assert resp.status_code == 204, resp.text
    assert await _reason(env, family) == "deleted"
    assert await _counted(env, org, "user.deleted") == 1
    assert await _ended(env, pending)


async def test_an_administrators_factor_reset_ends_their_ai_connections(
    lifecycle_env: Any,
) -> None:
    env = lifecycle_env
    org = await env.create_org("grant-reset")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")
    family = await _connected(env, org, member.user_id)
    pending = await _pending(env, org, member.user_id)

    resp = await env.client.post(
        f"/api/v1/users/{member.user_id}/factors/reset", headers=admin.headers()
    )

    assert resp.status_code == 204, resp.text
    assert await _reason(env, family) == "factors_reset"
    assert await _counted(env, org, "user.factors_reset") == 1
    assert await _ended(env, pending)


async def test_changing_my_password_ends_my_ai_connections(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("grant-password")
    who = await signed_in(env, org, "technician")
    family = await _connected(env, org, who.user_id)
    pending = await _pending(env, org, who.user_id)

    resp = await env.client.put(
        "/api/v1/me/password",
        headers=who.headers(),
        json={"current": who.password, "new": NEW_PASSWORD},
    )

    assert resp.status_code == 204, resp.text
    assert await _reason(env, family) == "password_changed"
    assert await _counted(env, org, "auth.password_changed") == 1
    assert await _ended(env, pending)


async def test_signing_out_everywhere_ends_my_ai_connections(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("grant-everywhere")
    who = await signed_in(env, org, "technician")
    family = await _connected(env, org, who.user_id)
    pending = await _pending(env, org, who.user_id)

    resp = await env.client.post("/api/v1/auth/logout-everywhere", headers=who.headers())

    assert resp.status_code == 204, resp.text
    assert await _reason(env, family) == "signed_out_everywhere"
    assert await _counted(env, org, "auth.sessions_ended") == 1
    assert await _ended(env, pending)


async def test_a_password_reset_by_mail_ends_the_ai_connections(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("grant-mail")
    who = await signed_in(env, org, "technician")
    family = await _connected(env, org, who.user_id)
    pending = await _pending(env, org, who.user_id)

    forgot = await env.client.post(
        "/api/v1/auth/password/forgot", headers=browser(), json={"email": who.email}
    )
    assert forgot.status_code == 202, forgot.text
    (mail,) = [m for m in env.mail.sent if "reset" in m.subject.lower()]
    resp = await env.client.post(
        "/api/v1/auth/password/reset",
        headers=browser(),
        json={"token": link_token(mail.text), "password": NEW_PASSWORD},
    )

    assert resp.status_code == 204, resp.text
    assert await _reason(env, family) == "password_changed"
    assert await _counted(env, org, "auth.password_changed") == 1
    assert await _ended(env, pending)


async def test_the_recovery_link_ends_the_ai_connections(lifecycle_env: Any) -> None:
    env = lifecycle_env
    org = await env.create_org("grant-recover")
    who = await signed_in(env, org, "technician")
    family = await _connected(env, org, who.user_id)
    pending = await _pending(env, org, who.user_id)
    async with AsyncSession(env.admin_engine) as session:
        token = await action_tokens.issue(
            session,
            org_id=org,
            user_id=who.user_id,
            purpose=action_tokens.RECOVER,
            ttl=action_tokens.RECOVER_TTL,
            created_by=None,
        )
        await session.commit()

    resp = await env.client.post(
        "/api/v1/auth/recover/accept", headers=browser(), json={"token": token}
    )

    assert resp.status_code == 200, resp.text
    assert await _reason(env, family) == "factors_reset"
    assert await _counted(env, org, "user.recovery_link_used") == 1
    assert await _ended(env, pending)


async def test_an_administrator_ending_sessions_leaves_the_ai_connections(
    lifecycle_env: Any,
) -> None:
    env = lifecycle_env
    org = await env.create_org("grant-sessions")
    admin = await signed_in(env, org, "org-admin")
    member = await signed_in(env, org, "technician")
    family = await _connected(env, org, member.user_id)
    pending = await _pending(env, org, member.user_id)

    resp = await env.client.delete(
        f"/api/v1/users/{member.user_id}/sessions", headers=admin.headers()
    )

    assert resp.status_code == 204, resp.text
    assert await _reason(env, family) is None
    assert not await _ended(env, pending)


async def test_an_act_leaves_spent_and_expired_codes_as_they_were(lifecycle_env: Any) -> None:
    """Only UNSPENT, LIVE codes are ended. A spent code keeps its `used_at`
    and its expiry (its reuse is still `mcp.code_reused`); an expired one
    keeps the expiry it had -- never moved, the floor of
    `ck_oauth_codes_expiry` (`expires_at > created_at`) held either way."""
    env = lifecycle_env
    org = await env.create_org("grant-codes")
    who = await signed_in(env, org, "technician")
    spent, expired = await _pending(env, org, who.user_id), await _pending(env, org, who.user_id)
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(OAuthCode).where(OAuthCode.id == spent).values(used_at=func.clock_timestamp())
        )
        await session.execute(
            update(OAuthCode)
            .where(OAuthCode.id == expired)
            .values(
                created_at=func.clock_timestamp() - timedelta(minutes=2),
                expires_at=func.clock_timestamp() - timedelta(minutes=1),
            )
        )
        await session.commit()
    async with AsyncSession(env.admin_engine) as session:
        before = {
            row.id: (row.used_at, row.expires_at)
            for row in (
                await session.execute(select(OAuthCode).where(OAuthCode.id.in_([spent, expired])))
            ).scalars()
        }

    resp = await env.client.post("/api/v1/auth/logout-everywhere", headers=who.headers())

    assert resp.status_code == 204, resp.text
    async with AsyncSession(env.admin_engine) as session:
        after = {
            row.id: (row.used_at, row.expires_at)
            for row in (
                await session.execute(select(OAuthCode).where(OAuthCode.id.in_([spent, expired])))
            ).scalars()
        }
    assert after == before


async def test_an_act_ends_and_counts_only_live_grants(lifecycle_env: Any) -> None:
    """A grant past its idle limit is dead already: the act neither marks it
    nor counts it (the coordinator's notes: every family of an idle app is
    expired, so the act's UPDATE keeps clear of an idle app's purge)."""
    env = lifecycle_env
    org = await env.create_org("grant-expired")
    who = await signed_in(env, org, "technician")
    live = await _connected(env, org, who.user_id)
    resource = mcp_resource(env.settings)
    assert resource is not None
    app_id = await mcp_app(env, org)
    async with oauth_session(org, env.app_engine) as session:
        old, _ = await grants.open_family(
            session,
            org_id=org,
            user_id=who.user_id,
            oauth_client_id=app_id,
            resource=resource,
            now=datetime.now(UTC) - timedelta(days=31),
        )
        expired: UUID = old.id

    resp = await env.client.post("/api/v1/auth/logout-everywhere", headers=who.headers())

    assert resp.status_code == 204, resp.text
    assert await _reason(env, live) == "signed_out_everywhere"
    assert await _reason(env, expired) is None
    assert await _counted(env, org, "auth.sessions_ended") == 1


# --------------------------------------------------------------------------- #
# The race the codes close (operator decision D2, part c)
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


async def test_an_exchange_waiting_on_signing_out_everywhere_opens_no_grant(
    lifecycle_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2, part c, the race. A technician approved "claude" a moment ago; its
    code is not exchanged yet. They sign out everywhere, and the act is held
    where it ends their sessions -- their `users` row held. The app exchanges
    the code meanwhile, and waits on that ROW (`guards.lock_user_row`, the
    exchange's first lock). The act ends the person's unspent codes and their
    grants and commits. The person is still active, so the row lock alone
    would only have put the exchange after the act, and it would have opened
    a live grant there. It reads the code again under its lock, against a
    clock read after it (`grants._clock_after_the_locks`): expired. A plain
    `invalid_grant`, and no live grant is left."""
    env = lifecycle_env
    org = await env.create_org("grant-race")
    who = await signed_in(env, org, "technician")
    verifier, challenge = pkce_pair()
    code, code_id = await _issue(env, org, who.user_id, challenge)
    paused, go, seen = _pause(monkeypatch, flows, "end_all_for_user")
    signing_out = asyncio.create_task(
        env.client.post("/api/v1/auth/logout-everywhere", headers=who.headers())
    )
    await asyncio.wait_for(paused.wait(), 30)
    exchanging = asyncio.create_task(
        token_request(
            env,
            grant_type="authorization_code",
            code=code,
            client_id="claude",
            code_verifier=verifier,
        )
    )
    watching = asyncio.create_task(until_blocked_by(env.app_engine, seen["pid"]))
    try:
        await asyncio.wait({watching, exchanging}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        seen_waiting = watching.done() and watching.exception() is None
        waiting = watching.result() if seen_waiting else []
        held = seen_waiting and not exchanging.done()
    finally:
        if not watching.done():
            watching.cancel()
        go.set()
    signed_out, exchanged = await asyncio.wait_for(
        asyncio.gather(signing_out, exchanging, return_exceptions=True), timeout=60
    )

    assert not isinstance(signed_out, BaseException), signed_out
    assert signed_out.status_code == 204, signed_out.text
    assert not isinstance(exchanged, BaseException), exchanged
    assert held, exchanged.text
    assert waiting and all(
        "FROM USERS " in normalised(q) and "FOR NO KEY UPDATE" in normalised(q) for q in waiting
    ), waiting
    assert exchanged.status_code == 400, exchanged.text
    assert exchanged.json() == {
        "error": "invalid_grant",
        "error_description": "the authorization code is not valid",
    }
    assert await _ended(env, code_id)
    async with oauth_session(org, env.app_engine) as session:
        live = (
            await session.execute(
                select(OAuthRefreshFamily.id).where(
                    OAuthRefreshFamily.user_id == who.user_id,
                    OAuthRefreshFamily.revoked_at.is_(None),
                )
            )
        ).all()
    assert live == []
    # Not `mcp.code_reused`: the act ended the code, it did not spend it.
    async with AsyncSession(env.admin_engine) as session:
        reused = (
            await session.execute(
                select(AuditEvent.id).where(
                    AuditEvent.org_id == org, AuditEvent.action == "mcp.code_reused"
                )
            )
        ).all()
    assert reused == []
