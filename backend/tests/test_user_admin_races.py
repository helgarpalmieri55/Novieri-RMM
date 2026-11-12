"""A technician's account under an administrator's hand, against the
technician's own link (plan 2026-11-06, Task 8, fix round 2).

THE LOCK ORDER IS THE PERSON'S `users` ROW, THEN THEIR LINK AND SESSIONS, THEN
THE AUDIT CHAIN -- for a technician as for a contact. Fix round 1 made every
use of a link take the person first (`flows._consumed`): an invitation's
acceptance and a password reset, which technicians share with contacts. But
`user_admin.service.cancel_invite` revoked the link and ended the sessions
BEFORE it marked the row deleted, and `delete_user` likewise, so each could
hold what the link's use waited on while waiting on the row the use held: a
40P01, and a 500 for the person accepting or resetting. Before round 1 the
same interleavings ended cleanly (the use took the link first and never
waited on the row).

Both now lock the target's row first (`_target(..., lock=True)`). Each test
pauses the administrator's act at the point named, inside its transaction,
until the link's use is SEEN waiting on it (`tests/race_helpers.py`), says
WHERE it waits, lets the act finish, and checks both outcomes.

The first test is the reviewer's own reproduction (round 1 re-review),
brought in with its outcome asserted.

THE FINAL REVIEW OF PHASE 2 (2026-11-11) found the acts that still read their
target unlocked: `set_enabled` (item 1), `assign_role` and `resend_invite`
(item 2), and `remove_role`, the last writer of a person's rows outside the
order `reset_factors`'s docstring claims for every one. Against a deletion or
a cancelled invitation in flight each passed its liveness check on the old
row and then wrote over -- or beside -- the deletion: a deleted account
switched back on, holding a live grant, sent a new invitation, or audited
for a removal that never happened. Each now takes the row first and finds
the account deleted. And the MCP door (item 3), which refreshed a person's
address by taking the audit chain BEFORE their row: against an
administrator holding the row and waiting on the chain, a 40P01.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import action_tokens
from app.core.client_scope import ClientScope
from app.core.mcp_channel import mcp_resource
from app.core.tenancy import org_session
from app.models import AuditEvent, Role, User, UserRole
from app.models.auth import AuthActionToken, AuthPassword
from app.modules.user_admin import service as ua
from tests.auth_support import add_passkey, browser, capture_mail, console_ready, staff
from tests.mcp_support import marked_client
from tests.race_helpers import backend_pid, normalised, until_blocked_by
from tests.session_support import sign_in


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def _id(env: Any, sub: str) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        user_id = await s.scalar(select(User.id).where(User.external_subject == sub))
    assert user_id is not None
    return user_id


async def _link(env: Any, org: UUID, user_id: UUID, purpose: str, actor: UUID) -> str:
    async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as s:
        return await action_tokens.issue(
            s,
            org_id=org,
            user_id=user_id,
            purpose=purpose,
            ttl=action_tokens.INVITE_TTL,
            created_by=actor,
        )


def _pause(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> tuple[asyncio.Event, asyncio.Event, dict[str, int]]:
    """Hold the administrator's act at its call to `user_admin.service.<name>`,
    inside its transaction: (paused, go, {"pid": the act's backend})."""
    paused, go = asyncio.Event(), asyncio.Event()
    seen: dict[str, int] = {}
    real: Callable[..., Awaitable[Any]] = getattr(ua, name)

    async def held(session: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        seen["pid"] = await backend_pid(session)
        paused.set()
        await go.wait()
        return await real(session, *args, **kwargs)

    monkeypatch.setattr(ua, name, held)
    return paused, go, seen


def _on_the_person(waiting: list[str]) -> bool:
    return bool(waiting) and all(
        "FROM USERS " in normalised(q) and "FOR NO KEY UPDATE" in normalised(q) for q in waiting
    )


async def test_technician_accept_vs_admin_cancel(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's reproduction. An administrator cancels a technician's
    invitation and is held where it ends the sessions -- the link already
    revoked. The technician accepts meanwhile. The acceptance waits on the
    technician's ROW, which the cancel now holds from its first read (at
    5c160ab it waited on the link, and the cancel's later write to the row
    closed the cycle: DeadlockDetectedError, a 500). The cancel commits; the
    acceptance finds the account deleted and the link dead: 410."""
    env = auth_env
    org = await env.create_org("tech-race")
    admin_id = await _id(env, await env.create_user(org, "org-admin"))
    tech_id = await _id(env, await env.create_user(org))
    token = await _link(env, org, tech_id, action_tokens.INVITE, admin_id)
    paused, go, seen = _pause(monkeypatch, "end_all_for_user")

    async def cancel() -> str:
        async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as s:
            await ua.cancel_invite(s, org_id=org, actor_id=admin_id, user_id=tech_id)
        return "cancel committed"

    cancelling = asyncio.create_task(cancel())
    await asyncio.wait_for(paused.wait(), 30)
    accepting = asyncio.create_task(
        env.client.post(
            "/api/v1/auth/invite/accept",
            headers=browser(),
            json={"token": token, "password": "Another-Harbor-Kettle-2026"},
        )
    )
    try:
        waiting = await until_blocked_by(env.app_engine, seen["pid"])
        held = not accepting.done()
    finally:
        go.set()
    cancelled, accepted = await asyncio.wait_for(
        asyncio.gather(cancelling, accepting, return_exceptions=True), timeout=30
    )
    assert held
    assert _on_the_person(waiting), (waiting, cancelled, accepted)
    assert cancelled == "cancel committed", cancelled
    assert not isinstance(accepted, BaseException), accepted
    assert accepted.status_code == 410, accepted.text
    async with AsyncSession(env.admin_engine) as s:
        assert await s.scalar(select(User.status).where(User.id == tech_id)) == "deleted"
        assert await s.get(AuthPassword, tech_id) is None


async def test_technician_reset_password_vs_admin_delete(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An administrator deletes a technician -- one with a password, a second
    factor, a live session and a reset link -- and is held where it forgets
    the devices: the sessions already ended. The technician follows the
    reset link meanwhile. The reset waits on the technician's ROW, which the
    delete now holds from its first read (at 5c160ab the reset took the row,
    then waited on the ended sessions, and the delete's later write to the
    row closed the cycle). The delete commits; the reset finds the account
    deleted: 410, and the password is not changed."""
    env = auth_env
    org = await env.create_org("tech-race")
    admin_id = await _id(env, await env.create_user(org, "org-admin"))
    tech = await staff(env, org)
    await add_passkey(env, tech)
    await sign_in(env, org, tech.sub)
    async with AsyncSession(env.admin_engine) as s:
        before = await s.scalar(
            select(AuthPassword.hash).where(AuthPassword.user_id == tech.user_id)
        )
    token = await _link(env, org, tech.user_id, action_tokens.RESET, admin_id)
    paused, go, seen = _pause(monkeypatch, "revoke_all_for_user")

    async def remove() -> str:
        async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as s:
            await ua.delete_user(s, org_id=org, actor_id=admin_id, user_id=tech.user_id)
        return "delete committed"

    deleting = asyncio.create_task(remove())
    await asyncio.wait_for(paused.wait(), 30)
    resetting = asyncio.create_task(
        env.client.post(
            "/api/v1/auth/password/reset",
            headers=browser(),
            json={"token": token, "password": "Another-Harbor-Kettle-2026"},
        )
    )
    try:
        waiting = await until_blocked_by(env.app_engine, seen["pid"])
        held = not resetting.done()
    finally:
        go.set()
    deleted, reset = await asyncio.wait_for(
        asyncio.gather(deleting, resetting, return_exceptions=True), timeout=30
    )
    assert held
    assert _on_the_person(waiting), (waiting, deleted, reset)
    assert deleted == "delete committed", deleted
    assert not isinstance(reset, BaseException), reset
    assert reset.status_code == 410, reset.text
    async with AsyncSession(env.admin_engine) as s:
        assert await s.scalar(select(User.status).where(User.id == tech.user_id)) == "deleted"
        after = await s.scalar(
            select(AuthPassword.hash).where(AuthPassword.user_id == tech.user_id)
        )
    assert after == before


# --------------------------------------------------------------------------- #
# the final review of phase 2 (2026-11-11), items 1-3
# --------------------------------------------------------------------------- #


async def _fresh_admin(env: Any, org: UUID) -> tuple[UUID, dict[str, str]]:
    """An org-admin and a console session of theirs with a recent second
    factor: enable, a grant and a resend all ask for one (S1, S5)."""
    sub = await env.create_user(org, "org-admin")
    return await _id(env, sub), await env.headers(org, sub)


def _deleting(env: Any, org: UUID, admin_id: UUID, user_id: UUID) -> "asyncio.Task[str]":
    async def remove() -> str:
        async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as s:
            await ua.delete_user(s, org_id=org, actor_id=admin_id, user_id=user_id)
        return "delete committed"

    return asyncio.create_task(remove())


async def _waiting_or_done(env: Any, blocker: int, act: "asyncio.Task[Any]") -> list[str]:
    """`until_blocked_by`, or [] as soon as `act` finishes without waiting on
    `blocker` at all -- what an act that reads the person unlocked and finds
    nothing it writes held does (`assign_role` before item 2): the test then
    says so on `held`, with the act's answer, instead of timing out."""
    watching = asyncio.create_task(until_blocked_by(env.app_engine, blocker))
    await asyncio.wait({watching, act}, return_when=asyncio.FIRST_COMPLETED)
    if watching.done():
        return watching.result()
    watching.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await watching
    return []


async def _actions(env: Any, org: UUID, action: str) -> list[AuditEvent]:
    async with AsyncSession(env.admin_engine) as s:
        return list(
            (
                await s.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org, AuditEvent.action == action)
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )


@pytest.mark.parametrize("act", ["enable", "disable"])
async def test_set_enabled_vs_admin_delete(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch, act: str
) -> None:
    """Final review, item 1 (reproduced by the reviewer). An administrator
    deletes a technician and is held where it ends the sessions -- the grants
    already gone, the row held. Another switches the same account on (or
    off) meanwhile. At ab0d7aa the toggle read the row unlocked, passed
    `_require_live` on the OLD status, waited on its own UPDATE, and then
    wrote `active` (or `disabled`) over `deleted`: 200, and a deleted account
    resurrected. Now it waits on the person's ROW, finds it deleted: 409."""
    env = auth_env
    org = await env.create_org("tech-race")
    admin_id, headers = await _fresh_admin(env, org)
    tech = await staff(env, org)
    if act == "enable":
        off = await env.client.post(f"/api/v1/users/{tech.user_id}/disable", headers=headers)
        assert off.status_code == 200, off.text
    paused, go, seen = _pause(monkeypatch, "end_all_for_user")
    deleting = _deleting(env, org, admin_id, tech.user_id)
    await asyncio.wait_for(paused.wait(), 30)
    toggling = asyncio.create_task(
        env.client.post(f"/api/v1/users/{tech.user_id}/{act}", headers=headers)
    )
    try:
        waiting = await until_blocked_by(env.app_engine, seen["pid"])
        held = not toggling.done()
    finally:
        go.set()
    deleted, toggled = await asyncio.wait_for(
        asyncio.gather(deleting, toggling, return_exceptions=True), timeout=30
    )
    assert held
    assert _on_the_person(waiting), (waiting, deleted, toggled)
    assert deleted == "delete committed", deleted
    assert not isinstance(toggled, BaseException), toggled
    assert toggled.status_code == 409, toggled.text
    assert toggled.json()["detail"] == "this user has been deleted"
    async with AsyncSession(env.admin_engine) as s:
        assert await s.scalar(select(User.status).where(User.id == tech.user_id)) == "deleted"


async def test_assign_role_vs_admin_delete(auth_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Final review, item 2 (reproduced by the reviewer). The deletion is
    held after it removed the grants. At ab0d7aa a grant meanwhile read the
    row unlocked, passed `_require_live`, and INSERTED: nothing it touched
    was held, so it never waited -- 201, and a deleted account holding a live
    grant. Now it waits on the person's ROW, finds it deleted: 409, and the
    account holds nothing."""
    env = auth_env
    org = await env.create_org("tech-race")
    admin_id, headers = await _fresh_admin(env, org)
    tech = await staff(env, org)
    async with AsyncSession(env.admin_engine) as s:
        role_id = await s.scalar(
            select(Role.id).where(Role.name == "technician", Role.org_id.is_(None))
        )
    paused, go, seen = _pause(monkeypatch, "end_all_for_user")
    deleting = _deleting(env, org, admin_id, tech.user_id)
    await asyncio.wait_for(paused.wait(), 30)
    granting = asyncio.create_task(
        env.client.post(
            f"/api/v1/users/{tech.user_id}/roles",
            headers=headers,
            json={"role_id": str(role_id)},
        )
    )
    try:
        waiting = await _waiting_or_done(env, seen["pid"], granting)
        held = not granting.done()
    finally:
        go.set()
    deleted, granted = await asyncio.wait_for(
        asyncio.gather(deleting, granting, return_exceptions=True), timeout=30
    )
    assert held, (granted, deleted)
    assert _on_the_person(waiting), (waiting, deleted, granted)
    assert deleted == "delete committed", deleted
    assert not isinstance(granted, BaseException), granted
    assert granted.status_code == 409, granted.text
    assert granted.json()["detail"] == "this user has been deleted"
    async with AsyncSession(env.admin_engine) as s:
        assert await s.scalar(select(User.status).where(User.id == tech.user_id)) == "deleted"
        grants = await s.scalar(
            select(func.count()).select_from(UserRole).where(UserRole.user_id == tech.user_id)
        )
    assert grants == 0
    assert await _actions(env, org, "user.role_assigned") == []


async def test_remove_role_vs_admin_delete(auth_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Final review, item 2's docstring check: `remove_role` was the last
    writer of a person's rows not taking the row first. The deletion is held
    after it removed the grants. At ab0d7aa the removal read the grant the
    deletion had not committed away yet, waited on ITS row, deleted nothing
    once the deletion committed -- and audited `user.role_removed` for a
    removal that never happened: 204. Now it waits on the person's ROW, finds
    no such grant: 404, and nothing audited."""
    env = auth_env
    org = await env.create_org("tech-race")
    admin_id, headers = await _fresh_admin(env, org)
    tech_id = await _id(env, await env.create_user(org, "technician"))
    async with AsyncSession(env.admin_engine) as s:
        assignment_id = await s.scalar(select(UserRole.id).where(UserRole.user_id == tech_id))
    assert assignment_id is not None
    paused, go, seen = _pause(monkeypatch, "end_all_for_user")
    deleting = _deleting(env, org, admin_id, tech_id)
    await asyncio.wait_for(paused.wait(), 30)
    removing = asyncio.create_task(
        env.client.delete(f"/api/v1/users/{tech_id}/roles/{assignment_id}", headers=headers)
    )
    try:
        waiting = await _waiting_or_done(env, seen["pid"], removing)
        held = not removing.done()
    finally:
        go.set()
    deleted, removed = await asyncio.wait_for(
        asyncio.gather(deleting, removing, return_exceptions=True), timeout=30
    )
    assert held, (removed, deleted)
    assert _on_the_person(waiting), (waiting, deleted, removed)
    assert deleted == "delete committed", deleted
    assert not isinstance(removed, BaseException), removed
    assert removed.status_code == 404, removed.text
    assert await _actions(env, org, "user.role_removed") == []


async def test_resend_invite_vs_admin_cancel(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review, item 2. An administrator cancels a technician's
    invitation and is held where it ends the sessions -- the link already
    revoked, the row held. Another resends the invitation meanwhile. At
    ab0d7aa the resend read the row unlocked and the invitation as still
    open, waited on the LINK the cancel had revoked, and then issued a new
    one and mailed it: 200, and a live invitation to a deleted account. Now
    it waits on the person's ROW, finds it deleted: 409, no new link, no
    mail, nothing audited."""
    env = auth_env
    org = await env.create_org("tech-race")
    admin_id, headers = await _fresh_admin(env, org)
    tech_id = await _id(env, await env.create_user(org))
    await _link(env, org, tech_id, action_tokens.INVITE, admin_id)
    sent_before = len(env.mail.sent)
    paused, go, seen = _pause(monkeypatch, "end_all_for_user")

    async def cancel() -> str:
        async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as s:
            await ua.cancel_invite(s, org_id=org, actor_id=admin_id, user_id=tech_id)
        return "cancel committed"

    cancelling = asyncio.create_task(cancel())
    await asyncio.wait_for(paused.wait(), 30)
    resending = asyncio.create_task(
        env.client.post(f"/api/v1/users/{tech_id}/invite/resend", headers=headers)
    )
    try:
        waiting = await _waiting_or_done(env, seen["pid"], resending)
        held = not resending.done()
    finally:
        go.set()
    cancelled, resent = await asyncio.wait_for(
        asyncio.gather(cancelling, resending, return_exceptions=True), timeout=30
    )
    assert held, (resent, cancelled)
    assert _on_the_person(waiting), (waiting, cancelled, resent)
    assert cancelled == "cancel committed", cancelled
    assert not isinstance(resent, BaseException), resent
    assert resent.status_code == 409, resent.text
    assert resent.json()["detail"] == "this user has been deleted"
    async with AsyncSession(env.admin_engine) as s:
        assert await s.scalar(select(User.status).where(User.id == tech_id)) == "deleted"
        open_links = await s.scalar(
            select(func.count())
            .select_from(AuthActionToken)
            .where(AuthActionToken.user_id == tech_id, AuthActionToken.used_at.is_(None))
        )
    assert open_links == 0
    assert len(env.mail.sent) == sent_before
    assert await _actions(env, org, "user.invite_resent") == []


def _mcp_call(env: Any, slug: str, sub: str, email: str) -> "asyncio.Task[Any]":
    """`GET /me` over the MCP channel with a token whose address claim is
    `email` -- the first call after the address changed at the identity
    provider, which the door writes to the person's row."""
    token = env.idp.token(sub, slug, aud=mcp_resource(env.settings), azp="claude", email=email)

    async def call() -> Any:
        async with marked_client(env) as client:
            return await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})

    return asyncio.create_task(call())


async def _until_waiting_on_people(env: Any, count: int, *, timeout_s: float = 30.0) -> list[str]:
    """The queries of `count` backends of this database blocked on a
    person's `users` row FOR NO KEY UPDATE -- whoever blocks them: the second
    of two waiters on one row queues behind the first, not the holder."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    async with env.app_engine.connect() as conn:
        while True:
            blocked = [
                str(query)
                for query in (
                    await conn.execute(
                        text(
                            "SELECT query FROM pg_stat_activity "
                            "WHERE datname = current_database() "
                            "AND cardinality(pg_blocking_pids(pid)) > 0"
                        )
                    )
                ).scalars()
            ]
            people = [query for query in blocked if _on_the_person([query])]
            if len(people) >= count:
                return people
            assert loop.time() < deadline, f"never {count} waiting on a person: {blocked}"
            await asyncio.sleep(0.02)
            await conn.rollback()


def _ending_sessions(env: Any, org: UUID, admin_id: UUID, user_id: UUID) -> "asyncio.Task[str]":
    async def end() -> str:
        async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as s:
            await ua.end_sessions(s, org_id=org, actor_id=admin_id, user_id=user_id)
        return "end_sessions committed"

    return asyncio.create_task(end())


async def test_patch_me_vs_admin_end_sessions(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review of phase 2, round 2, item 1 (the re-review's probe). An
    administrator ends a technician's sessions and is held after it took the
    row, before the chain. The technician saves their own profile meanwhile
    (`PATCH /me`). At ab3b300 the profile assigned its fields to the row it
    read unlocked and audited: `audit_service.record` takes the chain with a
    `text()` statement, which does not autoflush, and the row was written
    only at the record's next ORM read -- the chain held while waiting on the
    row, against an act holding the row and then waiting on the chain: a
    40P01. Now it locks the person's ROW first and waits there; both
    finish. (Admitted before the sessions ended, the save goes through.)"""
    env = auth_env
    org = await env.create_org("tech-race")
    admin_id = await _id(env, await env.create_user(org, "org-admin"))
    tech_sub = await env.create_user(org, "technician")
    tech_id = await _id(env, tech_sub)
    headers = await env.headers(org, tech_sub)
    paused, go, seen = _pause(monkeypatch, "end_all_for_user")
    ending = _ending_sessions(env, org, admin_id, tech_id)
    await asyncio.wait_for(paused.wait(), 30)
    saving = asyncio.create_task(
        env.client.patch("/api/v1/me", headers=headers, json={"display_name": "New Name"})
    )
    try:
        waiting = await until_blocked_by(env.app_engine, seen["pid"])
        held = not saving.done()
    finally:
        go.set()
    ended, saved = await asyncio.wait_for(
        asyncio.gather(ending, saving, return_exceptions=True), timeout=30
    )
    assert held
    assert _on_the_person(waiting), (waiting, ended, saved)
    assert ended == "end_sessions committed", ended
    assert not isinstance(saved, BaseException), saved
    assert saved.status_code == 200, saved.text
    async with AsyncSession(env.admin_engine) as s:
        name = await s.scalar(select(User.display_name).where(User.id == tech_id))
    assert name == "New Name"
    assert len(await _actions(env, org, "user.profile_updated")) == 1
    assert len(await _actions(env, org, "auth.sessions_ended")) == 1


async def test_mcp_door_email_refresh_vs_admin_end_sessions(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review, item 3. An administrator ends a technician's sessions
    and is held after it took the row, before the chain. The technician's AI
    app makes its first MCP call since their address changed at the identity
    provider. At ab0d7aa the door audited the change -- taking the chain --
    and only then wrote the row, at an autoflush: it held the chain while
    waiting on the row, the administrator held the row and then waited on
    the chain -- a 40P01, a 500 for one of them. Now the door takes the
    person's ROW first and the chain last, like every other writer: it waits
    on the row, both finish, and the change is audited once."""
    env = auth_env
    org = await env.create_org("tech-race")
    slug = await env.org_slug(org)
    admin_id = await _id(env, await env.create_user(org, "org-admin"))
    tech_sub = await env.create_user(org, "technician")
    tech_id = await _id(env, tech_sub)
    paused, go, seen = _pause(monkeypatch, "end_all_for_user")
    ending = _ending_sessions(env, org, admin_id, tech_id)
    await asyncio.wait_for(paused.wait(), 30)
    calling = _mcp_call(env, slug, tech_sub, "renamed@test.local")
    try:
        waiting = await until_blocked_by(env.app_engine, seen["pid"])
        held = not calling.done()
    finally:
        go.set()
    ended, called = await asyncio.wait_for(
        asyncio.gather(ending, calling, return_exceptions=True), timeout=30
    )
    assert held
    assert _on_the_person(waiting), (waiting, ended, called)
    assert ended == "end_sessions committed", ended
    assert not isinstance(called, BaseException), called
    assert called.status_code == 200, called.text
    changed = await _actions(env, org, "auth.email_changed")
    assert [(event.meta["from"], event.meta["to"]) for event in changed] == [
        (f"{tech_sub}@test.local", "renamed@test.local")
    ]
    async with AsyncSession(env.admin_engine) as s:
        email = await s.scalar(select(User.email).where(User.id == tech_id))
    assert email == "renamed@test.local"


async def test_two_mcp_calls_audit_one_email_change(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review, item 3. Two MCP calls of the same person after their
    address changed, both arriving while an administrator holds the row. At
    ab0d7aa each read the old address unlocked and set out to audit the
    change -- the second with a `from` the first was replacing -- and
    neither queued on the row (the first waited at its UPDATE, the second on
    the chain the first held). Now both queue on the person's ROW, and the
    second re-reads it after the first committed: one `auth.email_changed`."""
    env = auth_env
    org = await env.create_org("tech-race")
    slug = await env.org_slug(org)
    admin_id = await _id(env, await env.create_user(org, "org-admin"))
    tech_sub = await env.create_user(org, "technician")
    tech_id = await _id(env, tech_sub)
    paused, go, _seen = _pause(monkeypatch, "end_all_for_user")
    ending = _ending_sessions(env, org, admin_id, tech_id)
    await asyncio.wait_for(paused.wait(), 30)
    first = _mcp_call(env, slug, tech_sub, "renamed@test.local")
    second = _mcp_call(env, slug, tech_sub, "renamed@test.local")
    try:
        waiting = await _until_waiting_on_people(env, 2)
    finally:
        go.set()
    ended, one, two = await asyncio.wait_for(
        asyncio.gather(ending, first, second, return_exceptions=True), timeout=30
    )
    assert len(waiting) == 2, waiting
    assert ended == "end_sessions committed", ended
    for answer in (one, two):
        assert not isinstance(answer, BaseException), answer
        assert answer.status_code == 200, answer.text
    changed = await _actions(env, org, "auth.email_changed")
    assert [(event.meta["from"], event.meta["to"]) for event in changed] == [
        (f"{tech_sub}@test.local", "renamed@test.local")
    ]
    async with AsyncSession(env.admin_engine) as s:
        email = await s.scalar(select(User.email).where(User.id == tech_id))
    assert email == "renamed@test.local"
