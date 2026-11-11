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
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import action_tokens
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import User
from app.models.auth import AuthPassword
from app.modules.user_admin import service as ua
from tests.auth_support import add_passkey, browser, capture_mail, console_ready, staff
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
