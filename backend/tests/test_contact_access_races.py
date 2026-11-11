"""The lock order of a contact's portal access (plan 2026-11-06, Task 8, fix
round 1): THE PERSON'S `users` ROW, THEN THEIR LINK, THEN THE AUDIT CHAIN.

Every writer here takes them in that order:
- the archive (`_lock_contacts`, then `_cut_access`);
- a switch-off or an address change (`update_contact`: its UPDATE, then the
  links);
- an invitation, a cancel and a reset (`contact_access._contact`);
- and, since this round, the acceptance and the reset link
  (`flows._consumed`).

The bug this pins: the acceptance consumed the link first, and wrote
`users.last_login_at` last (`sessions.start_session`). An archive or a
switch-off in flight held the contact's row and waited on that link, so the
two waited on each other, and Postgres aborted one with 40P01: a 500.

Each test holds one side's transaction open until the other is SEEN waiting
on it (`tests/race_helpers.py`) and says WHERE it waits, then commits, so the
interleaving is the one named, never whichever a scheduler picked.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import action_tokens, flows
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, User
from app.models.auth import AuthActionToken, AuthSession
from app.services.clients import archive_client, update_contact
from tests.auth_support import (
    CONTACT_PASSWORD,
    browser,
    capture_mail,
    console_ready,
    contact,
)
from tests.race_helpers import backend_pid, normalised, until_blocked_by
from tests.session_support import http_request


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def _admin(env: Any, org_id: UUID) -> tuple[UUID, dict[str, str]]:
    sub = await env.create_user(org_id, "org-admin")
    async with AsyncSession(env.admin_engine) as s:
        user_id = await s.scalar(select(User.id).where(User.external_subject == sub))
    assert user_id is not None
    return user_id, await env.headers(org_id, sub)


async def _issue(env: Any, who: Any, purpose: str) -> str:
    async with org_session(who.org_id, engine=env.app_engine, clients=ClientScope.all()) as s:
        return await action_tokens.issue(
            s,
            org_id=who.org_id,
            user_id=who.user_id,
            purpose=purpose,
            ttl=action_tokens.INVITE_TTL,
            created_by=None,
        )


@contextlib.asynccontextmanager
async def _held(env: Any, who: Any, actor: UUID, act: str) -> AsyncIterator[int]:
    """`act` -- archiving the contact's client, switching the contact off, or
    correcting its address -- done through the service in a transaction kept
    open until the block exits, when it commits. Yields its backend's pid."""
    async with org_session(who.org_id, engine=env.app_engine, clients=ClientScope.all()) as s:
        if act == "archive":
            await archive_client(
                s,
                org_id=who.org_id,
                actor_id=actor,
                client_id=who.client_id,
                now=datetime.now(UTC),
            )
        else:
            changes = {"status": "disabled"} if act == "switch_off" else {"email": "fix@acme.test"}
            await update_contact(
                s, org_id=who.org_id, actor_id=actor, contact_id=who.user_id, changes=changes
            )
        yield await backend_pid(s)


def _on_the_person(waiting: list[str]) -> bool:
    """Every waiter waits on the person's `users` row -- its locking read or
    its UPDATE -- and none on a link."""
    return bool(waiting) and all(
        ("FROM USERS " in normalised(q) and "FOR NO KEY UPDATE" in normalised(q))
        or normalised(q).startswith("UPDATE USERS ")
        for q in waiting
    )


#: The three paths that consume a contact's link, and their request.
_USES: dict[str, tuple[str, str, dict[str, Any]]] = {
    "accept": (
        action_tokens.INVITE,
        "/api/v1/auth/invite/accept",
        {"password": CONTACT_PASSWORD},
    ),
    "passkey": (
        action_tokens.INVITE,
        "/api/v1/auth/invite/passkey",
        {"credential": {"id": "x"}, "name": "Phone"},
    ),
    "reset": (
        action_tokens.RESET,
        "/api/v1/auth/password/reset",
        {"password": "Another-Harbor-Kettle-2026"},
    ),
}


@pytest.mark.parametrize("act", ["archive", "switch_off"])
@pytest.mark.parametrize("use", sorted(_USES))
async def test_a_link_used_while_its_contact_is_switched_off_waits_on_the_person(
    auth_env: Any, act: str, use: str
) -> None:
    """The archive or the switch-off holds the contact's row and has revoked
    the link. The link's use waits on THE PERSON'S ROW, before it touches the
    link (without the fix it waited on the link row, the other order), and
    then finds the link dead: 410, nothing set."""
    purpose, url, body = _USES[use]
    org = await auth_env.create_org("contact-race")
    who = await contact(
        auth_env,
        org,
        password=CONTACT_PASSWORD if purpose == action_tokens.RESET else None,
        role=purpose == action_tokens.RESET,
    )
    token = await _issue(auth_env, who, purpose)
    actor, _headers = await _admin(auth_env, org)

    async with _held(auth_env, who, actor, act) as holder:
        used = asyncio.create_task(
            auth_env.client.post(url, headers=browser(), json={"token": token, **body})
        )
        waiting = await until_blocked_by(auth_env.app_engine, holder)
        assert not used.done()
        assert _on_the_person(waiting), waiting
    answer = await asyncio.wait_for(used, timeout=30)
    assert answer.status_code == 410, answer.text


@pytest.mark.parametrize("act", ["archive", "switch_off"])
async def test_an_acceptance_in_flight_holds_the_person_so_a_switch_off_waits_and_no_cycle_forms(
    auth_env: Any, act: str
) -> None:
    """The 40P01 itself, in the order that made it: the acceptance has taken
    its link and is about to write `users.last_login_at`. The archive or the
    switch-off now waits on the PERSON (`_lock_contacts`, or its UPDATE),
    which the acceptance already holds -- not on the link, as before, which
    with the acceptance's own write to the row closed the cycle. The
    acceptance finishes, and the switch-off then ends the session it made."""
    org = await auth_env.create_org("contact-race")
    who = await contact(auth_env, org, password=None, role=False)
    token = await _issue(auth_env, who, action_tokens.INVITE)
    _actor, headers = await _admin(auth_env, org)

    async with org_session(org, engine=auth_env.app_engine, clients=ClientScope.all()) as s:
        user = await flows.invitee(s, digest=action_tokens.digest(token), consume=True)
        if act == "archive":
            request = auth_env.client.post(
                f"/api/v1/clients/{who.client_id}/archive", headers=headers
            )
        else:
            request = auth_env.client.patch(
                f"/api/v1/contacts/{who.user_id}", headers=headers, json={"status": "disabled"}
            )
        off = asyncio.create_task(request)
        waiting = await until_blocked_by(auth_env.app_engine, await backend_pid(s))
        assert not off.done()
        assert _on_the_person(waiting), waiting
        # What deadlocked: the acceptance's own write to the person's row.
        # This test stores no password before it, so the acceptance has no
        # row of the caller's own to record first (Task 9b fix round 1).
        async def nothing_of_its_own() -> None:
            return None

        signed = await flows.contact_admitted(
            s,
            http_request(auth_env),
            user,
            method="password",
            recorded_first=nothing_of_its_own,
        )
        assert signed.state == "signed_in"
    answer = await asyncio.wait_for(off, timeout=30)
    assert answer.status_code == 200, answer.text
    async with AsyncSession(auth_env.admin_engine) as s:
        status = await s.scalar(select(User.status).where(User.id == who.user_id))
        live = await s.scalar(
            select(func.count())
            .select_from(AuthSession)
            .where(AuthSession.user_id == who.user_id, AuthSession.ended_at.is_(None))
        )
    assert (status, live) == ("disabled", 0)


@pytest.mark.parametrize("act", ["address", "switch_off"])
async def test_an_invitation_waits_for_an_edit_of_its_contact_and_acts_on_what_it_wrote(
    auth_env: Any, act: str
) -> None:
    """Fix round 1, item 2 (decision 4, A2-44; D43): `_contact` locks the
    contact's row after its client's, so an address correction or a
    switch-off in flight is waited for, and the invitation sees what it
    wrote -- the link goes to the NEW address, or no link goes at all.
    Without the lock the invitation read the old row, mailed the old
    address (or a switched-off contact) and waited only on the audit chain."""
    org = await auth_env.create_org("contact-race")
    who = await contact(auth_env, org, password=None, role=False)
    actor, headers = await _admin(auth_env, org)

    async with _held(auth_env, who, actor, act) as holder:
        invited = asyncio.create_task(
            auth_env.client.post(
                f"/api/v1/clients/{who.client_id}/contacts/{who.user_id}/invite", headers=headers
            )
        )
        waiting = await until_blocked_by(auth_env.app_engine, holder)
        assert not invited.done()
        assert _on_the_person(waiting), waiting
    answer = await asyncio.wait_for(invited, timeout=30)
    async with AsyncSession(auth_env.admin_engine) as s:
        live = await s.scalar(
            select(func.count())
            .select_from(AuthActionToken)
            .where(
                AuthActionToken.user_id == who.user_id,
                AuthActionToken.used_at.is_(None),
                AuthActionToken.expires_at > func.now(),
            )
        )
        invited_rows = await s.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "contact.invited", AuditEvent.target_id == str(who.user_id))
        )
    if act == "address":
        assert answer.status_code == 200, answer.text
        assert [m.to for m in auth_env.mail.sent] == ["fix@acme.test"]
        assert (live, invited_rows) == (1, 1)
    else:
        assert answer.status_code == 409, answer.text
        assert answer.json()["detail"] == "switch the contact on first"
        assert auth_env.mail.sent == []
        assert (live, invited_rows) == (0, 0)
