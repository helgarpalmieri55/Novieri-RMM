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
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import action_tokens, flows
from app.core.auth.oauth import consent, grants
from app.core.auth.oauth.store import oauth_session
from app.core.mcp_channel import mcp_resource
from app.models import AuditEvent, Organization
from app.models.oauth import OAuthCode, OAuthRefreshFamily
from tests.auth_support import browser, capture_mail, console_ready, link_token, signed_in
from tests.mcp_support import mcp_app
from tests.oauth_support import (
    CLAUDE_CALLBACK,
    authorize_query,
    code_of,
    pkce_pair,
    token_request,
)
from tests.race_helpers import backend_pid, normalised, pause_at, until_blocked_by
from tests.session_support import user_id_of

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
    paused, go, seen = pause_at(monkeypatch, flows, "end_all_for_user")
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


# --------------------------------------------------------------------------- #
# One clock (Task 10 review, Important 1)
# --------------------------------------------------------------------------- #

#: How far ahead of the database the API host's clock runs below.
AHEAD = timedelta(seconds=5)


class _ApiClockAhead(datetime):
    """`datetime`, whose `now` is the API host's clock running `AHEAD` of
    the database's."""

    @classmethod
    def now(cls, tz: Any = None) -> "_ApiClockAhead":
        real = datetime.now(tz) + AHEAD
        return cls.fromtimestamp(real.timestamp(), tz)


async def test_a_code_approved_on_a_clock_ahead_of_the_databases_is_ended_all_the_same(
    lifecycle_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API host's clock runs five seconds ahead of the database's. A
    technician approves "claude" on the consent screen, then signs out
    everywhere at once; the app exchanges the code a moment later. The act
    ended the code at the database's clock, and the exchange compares with
    the database's clock -- so the code's own times must come from it too.
    Written from the API's, `created_at` sat five seconds in the database's
    future, the act's floor (`created_at` + 1 µs, for `ck_oauth_codes_expiry`)
    put the code's end there, and the exchange inside those five seconds
    opened a live grant after the act."""
    env = lifecycle_env
    org = await env.create_org("grant-clock")
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(Organization)
            .where(Organization.id == org)
            .values(settings={"mcp_enabled": True})
        )
        await session.commit()
    who = await signed_in(env, org, "org-admin")
    await mcp_app(env, org)
    resource = mcp_resource(env.settings)
    assert resource is not None
    verifier, challenge = pkce_pair()
    monkeypatch.setattr(consent, "datetime", _ApiClockAhead)
    approved = await env.client.post(
        "/api/v1/oauth/authorize/decision",
        headers=who.headers(),
        json={
            "query": authorize_query("claude", challenge=challenge, resource=resource),
            "approve": True,
        },
    )
    assert approved.status_code == 200, approved.text
    code = code_of(approved.json()["redirect_to"])

    signed_out = await env.client.post("/api/v1/auth/logout-everywhere", headers=who.headers())
    exchanged = await token_request(
        env,
        grant_type="authorization_code",
        code=code,
        client_id="claude",
        code_verifier=verifier,
    )

    assert signed_out.status_code == 204, signed_out.text
    assert exchanged.status_code == 400, exchanged.text
    assert exchanged.json()["error"] == "invalid_grant"
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


# --------------------------------------------------------------------------- #
# The lock order (Task 10 review, Important 2)
# --------------------------------------------------------------------------- #


def _grant_row(
    table: str, row_id: UUID, org_id: UUID, user_id: UUID, app_id: UUID, resource: str
) -> OAuthCode | OAuthRefreshFamily:
    """A live code or a live family of the person's for the app, by `row_id`."""
    now = datetime.now(UTC)
    if table == "codes":
        return OAuthCode(
            id=row_id,
            org_id=org_id,
            user_id=user_id,
            oauth_client_id=app_id,
            code_hash=grants.hash_secret(grants.new_secret()),
            redirect_uri=CLAUDE_CALLBACK,
            code_challenge=pkce_pair()[1],
            resource=resource,
            expires_at=func.now() + timedelta(seconds=60),
        )
    return OAuthRefreshFamily(
        id=row_id,
        org_id=org_id,
        user_id=user_id,
        oauth_client_id=app_id,
        resource=resource,
        created_at=now,
        last_used_at=now,
        expires_at=now + timedelta(days=30),
    )


@pytest.mark.parametrize("table", ["codes", "families"])
@pytest.mark.parametrize("by", ["person", "app"])
async def test_a_revocation_locks_its_rows_in_id_order(
    lifecycle_env: Any, table: str, by: str
) -> None:
    """Two live rows of one person for one app -- two codes, or two grants
    after a reconnect -- stored in the REVERSE of their id order. Another
    transaction holds the lower id. An account act (`revoke_all_for_user`)
    or a Settings revocation (`revoke_app`) then waits on that row WITHOUT
    having taken the higher one: it locks its rows in id order, whatever
    order its plan reads them in, so two revocations sharing rows (an act on
    the person and a revoke of the app) take them in the same order and
    cannot deadlock. A plain UPDATE took them as its scan met them -- here
    the higher id first, then waited on the lower: the other order, which
    against a statement in id order is a 40P01."""
    env = lifecycle_env
    org = await env.create_org("grant-order")
    user_id = await user_id_of(env, await env.create_user(org, "technician"))
    app_id = await mcp_app(env, org)
    resource = mcp_resource(env.settings)
    assert resource is not None
    model: Any = OAuthCode if table == "codes" else OAuthRefreshFamily
    low, high = sorted([uuid4(), uuid4()])
    async with oauth_session(org, env.app_engine) as session:
        for row_id in (high, low):
            session.add(_grant_row(table, row_id, org, user_id, app_id, resource))
            await session.flush()
    async with AsyncSession(env.admin_engine) as session:
        stored = (
            await session.execute(
                select(model.id).where(model.id.in_([low, high])).order_by(text("ctid"))
            )
        ).scalars()
        assert list(stored) == [high, low]  # the scan's order is not the ids'

    async def revoke() -> int:
        async with oauth_session(org, env.app_engine) as session:
            if by == "person":
                return await grants.revoke_all_for_user(session, user_id, "signed_out_everywhere")
            return await grants.revoke_app(
                session,
                oauth_client_id=app_id,
                user_id=None,
                reason="revoked_in_settings",
                now=datetime.now(UTC),
            )

    async with AsyncSession(env.admin_engine) as blocker:
        await blocker.execute(select(model.id).where(model.id == low).with_for_update())
        revoking = asyncio.create_task(revoke())
        try:
            await until_blocked_by(env.app_engine, await backend_pid(blocker))
            async with AsyncSession(env.admin_engine) as probe:
                free = set(
                    (
                        await probe.execute(
                            select(model.id)
                            .where(model.id.in_([low, high]))
                            .with_for_update(skip_locked=True)
                        )
                    ).scalars()
                )
                await probe.rollback()
            held = not revoking.done()
        finally:
            await blocker.rollback()
    ended = await asyncio.wait_for(revoking, 30)

    assert held
    assert free == {high}, "the higher id was locked before the lower one"
    async with AsyncSession(env.admin_engine) as session:
        if table == "codes":
            assert ended == 0
            for code_id in (low, high):
                assert await _ended(env, code_id)
        else:
            assert ended == 2
            reasons = (
                await session.execute(
                    select(OAuthRefreshFamily.revoked_reason).where(
                        OAuthRefreshFamily.id.in_([low, high])
                    )
                )
            ).scalars()
            assert set(reasons) == {
                "signed_out_everywhere" if by == "person" else "revoked_in_settings"
            }


async def _blocked(env: Any, pid: int) -> None:
    """Until backend `pid` waits on a lock."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 30
    async with env.app_engine.connect() as conn:
        while True:
            blockers = (
                await conn.execute(text("SELECT pg_blocking_pids(:p)"), {"p": pid})
            ).scalar_one()
            if blockers:
                return
            assert loop.time() < deadline, f"backend {pid} never waited"
            await asyncio.sleep(0.02)
            await conn.rollback()


async def test_an_account_act_and_a_settings_revoke_of_one_app_never_deadlock(
    lifecycle_env: Any,
) -> None:
    """The cross-order the review found, made to happen. A person holds two
    live grants of one app, A then B in id order, stored in that order; A is
    then updated in place (a HOT update: an index entry still finds A at its
    first slot, a sequential scan meets it after B). The account act reads
    the person's grants by index -- A, B; the Settings revoke for everyone
    reads the app's by a sequential scan -- B, A. A third transaction holds
    A. The act waits on A holding nothing; the revoke takes B, then waits on
    A too. When A is let go, a plain UPDATE in each would close the cycle:
    the act, holding A, waits on B, and the revoke, holding B, on A --
    40P01, a 500 on "sign out everywhere" or on Settings. Locking in id
    order, the revoke also waits on A first, holding nothing: the act ends
    both grants, and the revoke, finding them ended, counts none."""
    env = lifecycle_env
    org = await env.create_org("grant-cycle")
    user_id = await user_id_of(env, await env.create_user(org, "technician"))
    app_id = await mcp_app(env, org)
    resource = mcp_resource(env.settings)
    assert resource is not None
    a, b = sorted([uuid4(), uuid4()])
    async with oauth_session(org, env.app_engine) as session:
        for row_id in (a, b):
            session.add(_grant_row("families", row_id, org, user_id, app_id, resource))
            await session.flush()
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(OAuthRefreshFamily)
            .where(OAuthRefreshFamily.id == a)
            .values(last_used_at=OAuthRefreshFamily.last_used_at)
        )
        await session.commit()
        stored = (
            await session.execute(
                select(OAuthRefreshFamily.id)
                .where(OAuthRefreshFamily.id.in_([a, b]))
                .order_by(text("ctid"))
            )
        ).scalars()
        assert list(stored) == [b, a]  # what a sequential scan meets

    pids: dict[str, int] = {}

    async def revoke(who: str, plan: tuple[str, ...]) -> int:
        async with oauth_session(org, env.app_engine) as session:
            for setting in plan:
                await session.execute(text(setting))
            pids[who] = await backend_pid(session)
            if who == "act":
                return await grants.revoke_all_for_user(session, user_id, "signed_out_everywhere")
            return await grants.revoke_app(
                session,
                oauth_client_id=app_id,
                user_id=None,
                reason="revoked_in_settings",
                now=datetime.now(UTC),
            )

    by_index = ("SET LOCAL enable_seqscan = off", "SET LOCAL enable_bitmapscan = off")
    by_scan = ("SET LOCAL enable_indexscan = off", "SET LOCAL enable_bitmapscan = off")
    async with AsyncSession(env.admin_engine) as blocker:
        await blocker.execute(
            select(OAuthRefreshFamily.id).where(OAuthRefreshFamily.id == a).with_for_update()
        )
        acting = asyncio.create_task(revoke("act", by_index))
        revoking: asyncio.Task[int] | None = None
        try:
            await until_blocked_by(env.app_engine, await backend_pid(blocker))
            revoking = asyncio.create_task(revoke("settings", by_scan))
            while "settings" not in pids:
                await asyncio.sleep(0.01)
            await _blocked(env, pids["settings"])
        finally:
            await blocker.rollback()
    assert revoking is not None
    acted, revoked = await asyncio.wait_for(
        asyncio.gather(acting, revoking, return_exceptions=True), timeout=60
    )

    assert not isinstance(acted, BaseException), acted
    assert not isinstance(revoked, BaseException), revoked
    assert (acted, revoked) == (2, 0)
    for family_id in (a, b):
        assert await _reason(env, family_id) == "signed_out_everywhere"
