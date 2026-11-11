"""A contact's way into the portal (design §3; plan decisions D39, D40, D43;
Task 8, decisions 3, 5, 6 and 7).

WHO: a technician with `clients.manage` on the contact's client. EVERY
FUNCTION TAKES THE CALLER'S SESSION, scoped by the request (`ctx.clients`): a
contact of a client outside it does not exist here (404), and every row
written -- the link, the wiped credentials -- is the contact's client's, which
that scope may write (migration 0231). Nothing here holds `ClientScope.all()`.

THE STATES (`access_of`, decision 6): `active`, the contact is switched on
and has a way in (`methods.with_sign_in_method`); `invited`, switched on, no
way in, and an invitation neither used NOR EXPIRED, with its expiry -- one
that ran out is no invitation, its link answers 410 (B2-29); `none`,
everything else, a contact who is switched off included: they cannot sign in
now, and whatever they hold comes back with them when switched on.

THE ACTS, each audited:
- `invite` (and resend): a new 72-hour link, replacing the open one. Refused
  for an `active` contact (reset its access instead), a disabled one, or one
  of an archived client.
- `cancel_invite`: the open link dies; the contact stays (a client's
  directory entry, not an account to burn).
- `reset_access`: everything the contact signs in with goes and a new
  invitation is issued (decision 3); the route asks the technician's recent
  confirmation (design §2.5).

The link is in the result so the route can show it ONCE when no mail carried
it (design §7); it is never stored in clear and never audited.

THE MAIL GOES BEFORE THE AUDIT ROW in `invite` and `reset_access` (decision
7, A2-45), because the row records whether the link was shown
(`link_shown`), which only the delivery knows: the precedent of
`user_admin.service.invite_user` (`service.py:735-746`), for the same
reason. No refusal follows the mail; the window is a database error while
writing the row, which would leave a mail whose link the rollback unmade.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.auth import action_tokens
from app.core.auth.devices import revoke_all_for_user
from app.core.auth.factors import clear_second_factors, lock_person
from app.core.auth.methods import with_sign_in_method
from app.core.auth.sessions import end_all_for_user
from app.core.i18n import tr
from app.core.identity import ACTIVE_STATUS, CONTACT_KIND
from app.models import Client, User
from app.models.auth import AuthActionToken, AuthExternalIdentity, AuthPassword
from app.models.clients import CLIENT_ARCHIVED
from app.services.clients import ClientError

ContactAccessState = Literal["none", "invited", "active"]
#: Hands a contact their link: (contact, link, reset) -> True when a mail took it.
Deliver = Callable[[User, str, bool], Awaitable[bool]]


@dataclass(frozen=True)
class ContactAccess:
    state: ContactAccessState
    invite_expires_at: datetime | None


@dataclass(frozen=True)
class ContactInvited:
    contact: User
    access: ContactAccess
    link: str
    delivered: bool


async def access_of(
    session: AsyncSession, *, org_id: UUID, users: Sequence[User]
) -> dict[UUID, ContactAccess]:
    """The state of each contact (module docstring), in two queries
    whatever their number (A2-46): the ways in, and the open invitations."""
    ids = [user.id for user in users]
    if not ids:
        return {}
    with_way_in = await with_sign_in_method(session, ids)
    # `.all()`: a `Result` has `keys()`, so `dict()` would read it as a mapping.
    invited: dict[UUID, datetime] = dict(
        (
            await session.execute(
                select(AuthActionToken.user_id, func.max(AuthActionToken.expires_at))
                .where(
                    AuthActionToken.org_id == org_id,
                    AuthActionToken.user_id.in_(ids),
                    AuthActionToken.purpose == action_tokens.INVITE,
                    AuthActionToken.used_at.is_(None),
                    # An invitation that ran out is no invitation (B2-29).
                    AuthActionToken.expires_at > func.now(),
                )
                .group_by(AuthActionToken.user_id)
            )
        )
        .tuples()
        .all()
    )
    found: dict[UUID, ContactAccess] = {}
    for user in users:
        if user.status != ACTIVE_STATUS:
            found[user.id] = ContactAccess("none", None)
        elif user.id in with_way_in:
            found[user.id] = ContactAccess("active", None)
        elif user.id in invited:
            found[user.id] = ContactAccess("invited", invited[user.id])
        else:
            found[user.id] = ContactAccess("none", None)
    return found


async def _one(session: AsyncSession, org_id: UUID, user: User) -> ContactAccess:
    return (await access_of(session, org_id=org_id, users=[user]))[user.id]


async def _contact(
    session: AsyncSession, *, org_id: UUID, client_id: UUID, contact_id: UUID
) -> User:
    """The contact of a visible, active client, LOCKED for the act (Task 8,
    fix round 1). The client FOR SHARE first, as `services.clients.
    update_contact` reads it, so an archive in flight is waited for and seen;
    then the contact's own row FOR NO KEY UPDATE, so an address correction or
    a switch-off in flight is waited for too, and what it wrote is what the
    act checks and uses -- never a link mailed to the address of before
    (decision 4, A2-44), nor one that outlives a switch-off (D43).

    THE LOCK ORDER is the one every writer of a contact's access keeps: the
    client, the person's `users` row, their links and credentials, the audit
    chain (`flows._consumed`; `tests/test_contact_access_races.py`). The
    contact's own sign-in steps do not keep it yet -- they write
    `users.last_login_at` last -- and `services.clients.archive_client` says
    what that leaves."""
    status = (
        await session.execute(
            select(Client.status)
            .where(Client.id == client_id, Client.org_id == org_id)
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()
    user = (
        await session.execute(
            select(User)
            .where(
                User.id == contact_id,
                User.org_id == org_id,
                User.kind == CONTACT_KIND,
                User.client_id == client_id,
            )
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if status is None or user is None:
        raise ClientError(404, tr("no such contact"))
    if status == CLIENT_ARCHIVED:
        raise ClientError(409, tr("that client is archived"))
    if user.status != ACTIVE_STATUS:
        raise ClientError(409, tr("switch the contact on first"))
    return user


async def _issue(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    user: User,
    console_url: str,
    deliver: Deliver,
    reset: bool,
) -> tuple[str, bool]:
    token = await action_tokens.issue(
        session,
        org_id=org_id,
        user_id=user.id,
        purpose=action_tokens.INVITE,
        ttl=action_tokens.INVITE_TTL,
        created_by=actor_id,
    )
    link = action_tokens.link(console_url, "invite", token)
    return link, await deliver(user, link, reset)


async def invite(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    client_id: UUID,
    contact_id: UUID,
    console_url: str,
    deliver: Deliver,
) -> ContactInvited:
    user = await _contact(session, org_id=org_id, client_id=client_id, contact_id=contact_id)
    before = await _one(session, org_id, user)
    if before.state == "active":
        raise ClientError(409, tr("that contact already signs in; reset their access instead"))
    link, delivered = await _issue(
        session,
        org_id=org_id,
        actor_id=actor_id,
        user=user,
        console_url=console_url,
        deliver=deliver,
        reset=False,
    )
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="contact.invited",
        target_type="user",
        target_id=str(user.id),
        client_id=user.client_id,
        metadata={
            "client_id": str(client_id),
            "email": user.email,
            "resent": before.state == "invited",
            "link_shown": not delivered,
        },
    )
    return ContactInvited(user, await _one(session, org_id, user), link, delivered)


async def cancel_invite(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, client_id: UUID, contact_id: UUID
) -> User:
    user = await _contact(session, org_id=org_id, client_id=client_id, contact_id=contact_id)
    if (await _one(session, org_id, user)).state != "invited":
        raise ClientError(409, tr("that contact has no pending invitation"))
    await action_tokens.revoke_open(session, user_id=user.id, purpose=action_tokens.INVITE)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="contact.invite_cancelled",
        target_type="user",
        target_id=str(user.id),
        client_id=user.client_id,
        metadata={"client_id": str(client_id), "email": user.email},
    )
    return user


async def reset_access(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    client_id: UUID,
    contact_id: UUID,
    console_url: str,
    deliver: Deliver,
) -> ContactInvited:
    """Decision 3: every way in and everything they opened go, under the
    person's lock (`factors.lock_person`), and a new invitation is issued."""
    user = await _contact(session, org_id=org_id, client_id=client_id, contact_id=contact_id)
    if (await _one(session, org_id, user)).state != "active":
        raise ClientError(409, tr("that contact has not signed in yet; send an invitation instead"))
    await lock_person(session, user.id)
    await session.execute(delete(AuthPassword).where(AuthPassword.user_id == user.id))
    await clear_second_factors(session, user.id)
    await session.execute(
        delete(AuthExternalIdentity).where(AuthExternalIdentity.user_id == user.id)
    )
    ended = await end_all_for_user(session, user.id, "factors_reset")
    await revoke_all_for_user(session, user.id)
    await action_tokens.revoke_open(session, user_id=user.id, purpose=action_tokens.RESET)
    link, delivered = await _issue(
        session,
        org_id=org_id,
        actor_id=actor_id,
        user=user,
        console_url=console_url,
        deliver=deliver,
        reset=True,
    )
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="contact.access_reset",
        target_type="user",
        target_id=str(user.id),
        client_id=user.client_id,
        metadata={
            "client_id": str(client_id),
            "email": user.email,
            "sessions_ended": ended,
            "link_shown": not delivered,
        },
    )
    return ContactInvited(user, await _one(session, org_id, user), link, delivered)
