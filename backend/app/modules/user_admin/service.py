"""User lifecycle: the decisions, separately from the HTTP.

`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §2, and
the platform's own accounts since P2 phase 1
(`docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §7):
invite, resend, cancel, reset the factors, disable, delete.

Everything worth arguing about lives here rather than in `api.py`: the rails
that stop an administrator locking the organization out of itself, the tenant
predicates, and the audit records. The route handlers open a tenant session,
call one function, and translate `UserAdminError` into the status this module
chose.

THE THREE THINGS THIS MODULE REFUSES TO DO, each stated where it is enforced
below but collected here because they are the reason the module exists:

1. **It never sees a password.** A technician chooses their own on the page a
   one-time link opens (`app.core.auth.flows.accept_invite`,
   `flows.reset_password`). Nothing here generates, stores, displays or
   returns one, and `tests/test_user_admin_api.py` scans every response body
   on the surface to keep it that way. The one secret that leaves is an
   invitation link -- once, to the administrator who asked for it, and only
   when no mail carried it (design §7).
2. **It never hard-deletes a user.** 89 foreign-key columns across 21 tables
   reference `users.id` -- audit `actor_id` among them -- so deleting the row
   would either fail on the constraints or turn the audit log from evidence into
   a list of anonymous events. The row is marked `status='deleted'` and kept.
3. **It never calls an identity provider.** Since P2 phase 1 the platform is
   its own. A technician invited here has no Keycloak identity --
   `external_subject` is a placeholder no provider issued
   (`LOCAL_SUBJECT_PREFIX`) -- so until phase 3 moves the MCP server onto the
   platform's own sign-in they cannot connect an AI app over MCP (plan
   decision D5, accepted: nothing is deployed before the four projects end).

DISABLE, DELETE AND A FACTOR RESET END THE PERSON'S SESSIONS AND FORGET THEIR
REMEMBERED DEVICES in the same transaction as the change (design §2.4):
`users.status` refuses the next admission at every door, and ending the
sessions stops the cookie already in a browser -- at once, not at its expiry.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.auth import action_tokens, notices
from app.core.auth.devices import revoke_all_for_user
from app.core.auth.factors import clear_second_factors
from app.core.auth.sessions import end_all_for_user
from app.core.i18n import tr
from app.core.identity import STAFF_KIND, domain_refusal
from app.core.vault import VaultService
from app.core.wire_words import say
from app.models import Client, Permission, Role, RolePermission, User, UserRole
from app.models.auth import AuthActionToken, AuthPassword
from app.models.clients import CLIENT_KIND_UNASSIGNED
from app.modules.user_admin.words import (
    ACTION_WORDS,
    CANNOT_BE_DELETED,
    CANNOT_BE_DISABLED,
    CANNOT_BE_INVITED,
    CANNOT_BE_STRIPPED,
    DELETE,
    DISABLE,
    OUTCOME_WORDS,
    RESET_FACTORS,
)

__all__ = [
    "ACTIVE",
    "DELETED",
    "DISABLED",
    "LOCAL_SUBJECT_PREFIX",
    "ORG_ADMIN",
    "SUPER_ADMIN",
    "CodeGrant",
    "Deliver",
    "Grant",
    "InvitedUser",
    "RoleAssignmentView",
    "UserAdminError",
    "UserView",
    "assign_role",
    "cancel_invite",
    "create_user",
    "delete_user",
    "effective_permissions",
    "end_sessions",
    "invite_user",
    "list_users",
    "remove_role",
    "reset_factors",
    "resend_invite",
    "set_enabled",
]

# The three values migration 0065's CHECK constrains `users.status` to, and the
# only ones this module writes. `app.core.identity.status_refusal` admits
# exactly `active`.
ACTIVE = "active"
DISABLED = "disabled"
DELETED = "deleted"

# Cross-org platform administration. Never grantable from an org-scoped surface:
# a console that could assign it is a tenant-escalation path. Compared by NAME,
# the same way `app/core/rbac_admin.py` and migration 0065's database function
# compare it -- two spellings of one rail would eventually disagree.
SUPER_ADMIN = "super-admin"

# The organization's own recovery path. With no `org-admin` left, nobody can
# grant the role back and the fix is SQL against production.
ORG_ADMIN = "org-admin"

#: The `external_subject` of a technician the platform created itself (rule 3):
#: no identity provider issued it, so it can never match a token's `sub`.
LOCAL_SUBJECT_PREFIX = "itms-local:"


class UserAdminError(Exception):
    """A refusal carrying the HTTP status the route should return.

    The service decides *why* something is refused; the route only translates.
    Keeping the status here stops "you cannot lock yourself out" and "no such
    user" collapsing into one message, which is what happens when a route maps
    every service failure to a single code.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class RoleAssignmentView:
    """One `user_roles` row.

    `assignment_id` is what DELETE addresses, and it has to be the row id rather
    than the role id: `UniqueConstraint(user_id, role_id, client_id)` permits
    the same role twice for different clients, so "remove technician" is
    ambiguous and "remove this assignment" is not.
    """

    assignment_id: UUID
    role_id: UUID
    role_name: str
    client_id: UUID | None


@dataclass(frozen=True)
class UserView:
    id: UUID
    email: str
    display_name: str
    status: str
    roles: list[RoleAssignmentView]
    #: When the invitation this person has not accepted yet expires; None once
    #: they chose a password, or when no invitation is open.
    invite_expires_at: datetime | None = None


@dataclass(frozen=True)
class CodeGrant:
    """One permission code, and how the user came to hold it.

    `via_role` and `client_id` are the whole point (spec §1.5): an
    access-control screen whose result you have to simulate in your head is one
    you will get wrong, and a view that flattened the client scope would report
    a grant as every-client when it applies to one (MSP clients spec §2.1).
    """

    code: str
    via_role: str
    client_id: UUID | None


@dataclass(frozen=True)
class Grant:
    """One role to grant: on every client (`client_id` None) or on one."""

    role_id: UUID
    client_id: UUID | None = None


@dataclass(frozen=True)
class InvitedUser:
    """An invitation: the person, its link, and whether a mail carried it.

    The link is in the RESULT so the route can show it -- once, and only when
    `delivered` is False (design §7). It is never stored in clear (only its
    hash, `action_tokens`) and never audited."""

    user: UserView
    link: str
    delivered: bool


#: Hands the invitation link to the invitee; True when a mail took it.
Deliver = Callable[[UserView, str], Awaitable[bool]]


# --------------------------------------------------------------------------- #
# lookups -- every one carries an explicit org predicate
# --------------------------------------------------------------------------- #


async def _target(session: AsyncSession, org_id: UUID, user_id: UUID) -> User:
    """This organization's TECHNICIAN, or a 404.

    `User.org_id == org_id` mirrors the RLS policy rather than relying on it.
    RLS is the backstop; a route with no predicate of its own passes every test
    that runs through a tenant session, which is why the tenancy tests drive
    these routes through the BYPASSRLS engine and why this line has to exist.

    A 404 rather than a 403: whether a user id exists in another tenant is
    itself information this organization has no business learning.

    STAFF ONLY (MSP clients Task 9 review 1). A contact (`users.kind =
    'contact'`, spec §1.3) is one client's person, managed from that client's
    page (`/api/v1/contacts/{id}`, `clients.manage` on the client); here it is
    a 404 on every read and write: a contact's sessions, factors and grants
    are its client's business, and this surface's `users.manage` on every
    client is a technician administrator's.
    """
    user = (
        await session.execute(
            select(User).where(
                User.id == user_id, User.org_id == org_id, User.kind == STAFF_KIND
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise UserAdminError(404, tr("no such user"))
    return user


async def _visible_role(session: AsyncSession, org_id: UUID, role_id: UUID) -> Role:
    """A role this org may assign: its own, or a platform-wide one.

    `super-admin` is refused by name even though it is visible as a global role,
    for the reason `SUPER_ADMIN` gives above.
    """
    role = (
        await session.execute(
            select(Role).where(
                Role.id == role_id, or_(Role.org_id == org_id, Role.org_id.is_(None))
            )
        )
    ).scalar_one_or_none()
    if role is None:
        raise UserAdminError(404, tr("no such role"))
    if role.name == SUPER_ADMIN:
        raise UserAdminError(
            403,
            tr(
                "{role} is cross-org platform administration and cannot be "
                "granted from an organization",
                role=SUPER_ADMIN,
            ),
        )
    return role


async def _grantable_client(session: AsyncSession, org_id: UUID, client_id: UUID) -> Client:
    """The client a grant is scoped to (MSP clients spec §2.1-2.2).

    UNKNOWN AND ANOTHER TENANT'S ARE THE SAME ANSWER, 422 "unknown client":
    whether a client id exists in another organization is not this one's to
    learn, and the `org_id` predicate mirrors RLS rather than relying on it.

    THE UNASSIGNED CLIENT IS REFUSED (spec §2.2): it is visible only to an
    every-client scope, by construction, and a grant on it would hand somebody
    the tickets of unknown senders without that scope.

    AN ARCHIVED CLIENT IS REFUSED TOO, for the reason an archived device group
    was when groups were the scope: a grant on a client no selector shows is a
    permission nobody can audit.

    Device groups no longer scope a grant at all (spec §2.1: "los grupos de
    equipos dejan de dar permisos") -- they organize devices and policies.
    """
    client = (
        await session.execute(
            select(Client).where(Client.id == client_id, Client.org_id == org_id)
        )
    ).scalar_one_or_none()
    if client is None:
        raise UserAdminError(422, tr("unknown client"))
    if client.kind == CLIENT_KIND_UNASSIGNED:
        raise UserAdminError(422, tr("a role cannot be granted on the unassigned client"))
    if client.status == "archived":
        raise UserAdminError(409, tr("that client is archived"))
    return client


async def _active_org_admins(session: AsyncSession, org_id: UUID) -> set[UUID]:
    """Users in this org who hold `org-admin` ON EVERY CLIENT AND can
    currently sign in.

    STATUS MATTERS: a disabled or deleted `org-admin` cannot grant the role back,
    so counting them would let the rail be satisfied by an account that is no
    help at all. The role is matched by NAME, so an org that took a clone of the
    seeded role through the roles surface (spec §1.3) still counts.

    SO DOES SCOPE (MSP clients spec §2.1). Managing users needs `users.manage`
    on every client (`require_permission_on_every_client`), so an `org-admin`
    grant on ONE client cannot grant the role back either: only
    `client_id IS NULL` counts, as in `rbac_admin._holders_of`.
    """
    rows = await session.execute(
        select(User.id)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            User.org_id == org_id,
            User.kind == STAFF_KIND,
            User.status == ACTIVE,
            Role.name == ORG_ADMIN,
            UserRole.client_id.is_(None),
        )
    )
    return set(rows.scalars())


async def _refuse_stranding_the_org(
    session: AsyncSession, org_id: UUID, user_id: UUID, verb: str
) -> None:
    admins = await _active_org_admins(session, org_id)
    if admins == {user_id}:
        raise UserAdminError(
            403,
            tr(
                "this is the organization's last active {role} and cannot be "
                "{verb}; grant {grant_role} to somebody else first",
                role=ORG_ADMIN,
                verb=say(OUTCOME_WORDS, verb),
                grant_role=ORG_ADMIN,
            ),
        )


async def _refuse_touching_a_super_admin(
    session: AsyncSession, org_id: UUID, user_id: UUID, verb: str
) -> None:
    """`super-admin` is refused at GRANT time. This is the other half.

    THE TWO DIRECTIONS ARE NOT SYMMETRICAL, which is the whole argument.
    `_visible_role` refuses to grant `super-admin` from an org-scoped surface,
    and migration 0065's database function refuses it below the service layer
    too -- so once it is gone there is NO console path that can put it back, and
    the fix is SQL against production. Removing it is therefore irreversible in
    a way that removing `technician` is not, and an irreversible action reachable
    by every `users.manage` holder in a tenant is a rail missing rather than a
    permission working.

    Refused here for the two entry points that end with the holder unable to
    sign in -- disable and delete -- and separately in `remove_role` for the
    revoke, because a rail on two of the three doors to the same end state is
    not a rail (the same reasoning `remove_role` already applies to
    `org-admin`).

    NOT refused for `enable`: restoring an account takes nothing away.
    """
    held = (
        await session.execute(
            select(UserRole.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                UserRole.user_id == user_id,
                UserRole.org_id == org_id,
                Role.name == SUPER_ADMIN,
            )
        )
    ).first()
    if held is not None:
        raise UserAdminError(
            403,
            tr(
                "this account holds {role}, which no console surface can "
                "grant back, so it cannot be {verb} from here",
                role=SUPER_ADMIN,
                verb=say(OUTCOME_WORDS, verb),
            ),
        )


def _refuse_self(actor_id: UUID, user_id: UUID, verb: str) -> None:
    """The first mis-click must not be the unrecoverable one.

    Disabling yourself takes effect on the NEXT request -- `users.status` is
    consulted at all three admission doors -- so there is no second chance to
    undo it from the console.
    """
    if actor_id == user_id:
        raise UserAdminError(
            403,
            tr("you cannot {verb} your own account", verb=say(ACTION_WORDS, verb)),
        )


def _require_live(user: User) -> None:
    if user.status == DELETED:
        raise UserAdminError(409, tr("this user has been deleted"))


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #


async def _assignments(
    session: AsyncSession, org_id: UUID, user_ids: Sequence[UUID]
) -> dict[UUID, list[RoleAssignmentView]]:
    if not user_ids:
        return {}
    rows = await session.execute(
        select(UserRole.id, UserRole.user_id, Role.id, Role.name, UserRole.client_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.org_id == org_id, UserRole.user_id.in_(user_ids))
        .order_by(Role.name, UserRole.id)
    )
    grouped: dict[UUID, list[RoleAssignmentView]] = {uid: [] for uid in user_ids}
    for assignment_id, user_id, role_id, role_name, client_id in rows:
        grouped[user_id].append(
            RoleAssignmentView(
                assignment_id=assignment_id,
                role_id=role_id,
                role_name=role_name,
                client_id=client_id,
            )
        )
    return grouped


async def _open_invites(
    session: AsyncSession, org_id: UUID, user_ids: Sequence[UUID]
) -> dict[UUID, datetime]:
    """The expiry of each person's unused invitation, for people who have not
    chosen a password yet (plan decision 8). Read under the request's scope:
    these are global rows (a technician's), which a pinned scope may read.

    THE ONE DEFINITION OF A PENDING INVITATION: an INVITE link with `used_at`
    still empty -- `consume`, `revoke_open` and a newer `issue` are what fill it
    -- of somebody without a password. Expiry is NOT part of it: an expired
    link is still an invitation to resend or cancel. `invite_pending` reads it,
    and so do the resend and the cancel (`_require_pending_invitation`)."""
    if not user_ids:
        return {}
    rows = await session.execute(
        select(AuthActionToken.user_id, func.max(AuthActionToken.expires_at))
        .where(
            AuthActionToken.org_id == org_id,
            AuthActionToken.user_id.in_(list(user_ids)),
            AuthActionToken.purpose == action_tokens.INVITE,
            AuthActionToken.used_at.is_(None),
            ~exists().where(AuthPassword.user_id == AuthActionToken.user_id),
        )
        .group_by(AuthActionToken.user_id)
    )
    return {user_id: expires for user_id, expires in rows.tuples()}


async def _accepted(session: AsyncSession, user_id: UUID) -> bool:
    """Has this person chosen a password -- accepted their invitation?"""
    return await session.get(AuthPassword, user_id) is not None


async def _require_pending_invitation(session: AsyncSession, org_id: UUID, user: User) -> None:
    """A 409 unless this person has a pending invitation (`_open_invites`).

    "NO PASSWORD" IS NOT "INVITED" (Task 8 review). Every account from before
    this phase has no password -- a super-admin's included -- and a resend that
    asked only that handed the administrator a link to set that person's
    password and act as them. So the resend and the cancel act only on an
    invitation that was issued and is still open, by the same predicate that
    shows `invite_pending`. The refusal says which of the two it is."""
    if user.id in await _open_invites(session, org_id, [user.id]):
        return
    if await _accepted(session, user.id):
        raise UserAdminError(409, tr("this person has already accepted their invitation"))
    raise UserAdminError(409, tr("this person has no pending invitation"))


async def list_users(session: AsyncSession, *, org_id: UUID) -> list[UserView]:
    """Every user in THIS organization, with their role assignments.

    Deleted users are included, carrying `status: "deleted"`. Hiding them would
    make the row's retention (rule 2) invisible to the operator, who would then
    reasonably conclude the delete failed and try again.

    Technicians only: a contact is listed on its client's page (`_target`).
    """
    users = list(
        (
            await session.execute(
                select(User)
                .where(User.org_id == org_id, User.kind == STAFF_KIND)
                .order_by(User.email)
            )
        ).scalars()
    )
    grouped = await _assignments(session, org_id, [u.id for u in users])
    invites = await _open_invites(session, org_id, [u.id for u in users])
    return [
        UserView(
            id=u.id,
            email=u.email,
            display_name=u.display_name,
            status=u.status,
            roles=grouped.get(u.id, []),
            invite_expires_at=invites.get(u.id),
        )
        for u in users
    ]


async def effective_permissions(
    session: AsyncSession, *, org_id: UUID, user_id: UUID
) -> list[CodeGrant]:
    """The union of the user's roles' codes, annotated with how each was granted.

    Computed from the same tables `app.core.rbac.has_permission` evaluates, so
    the screen and the enforcement cannot disagree. It is NOT read from
    `rbac.PERMISSIONS`: that dict is the catalogue of what codes exist, not of
    what this person holds.

    The same code granted twice -- by two roles, or by one role for every
    client and another for one client -- appears once per grant, because "you
    have this because of X" is the question the view answers.
    """
    await _target(session, org_id, user_id)
    rows = await session.execute(
        select(Permission.code, Role.name, UserRole.client_id)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id == user_id, UserRole.org_id == org_id)
        .order_by(Permission.code, Role.name)
    )
    return [
        CodeGrant(code=code, via_role=role_name, client_id=client_id)
        for code, role_name, client_id in rows
    ]


async def _view(session: AsyncSession, org_id: UUID, user: User) -> UserView:
    grouped = await _assignments(session, org_id, [user.id])
    invites = await _open_invites(session, org_id, [user.id])
    return UserView(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        status=user.status,
        roles=grouped.get(user.id, []),
        invite_expires_at=invites.get(user.id),
    )


# --------------------------------------------------------------------------- #
# create and invite -- rules 1 and 3
# --------------------------------------------------------------------------- #


def _normalise_email(email: str) -> str:
    """One `@`, lowercased, or a 422.

    Refusing rather than parsing anything with other than exactly one `@` is the
    same call `is_allowed_domain` makes and for the same reason: quoted local
    parts are legal RFC and carrying a second parser for them buys nothing.
    """
    cleaned = email.strip().lower()
    local, _, domain = cleaned.partition("@")
    if cleaned.count("@") != 1 or not local or not domain:
        raise UserAdminError(422, tr("that is not an email address this console can use"))
    return cleaned


async def create_user(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    email: str,
    display_name: str,
    grants: Sequence[Grant],
    email_domain: str | None,
) -> UserView:
    """The local row and its grants -- and nothing else (rules 1 and 3).

    Shared by the invitation and the first boot (`app/api/v1/setup.py`), so
    the domain rule, the role rails, the address check and the `user.created`
    record are one implementation. The person gets no credential here: the
    invitation's link (`invite_user`) or the setup form's password
    (`staff_password.store`) gives them one.

    Everything that can be refused is refused before the first write.
    """
    address = _normalise_email(email)

    # THE DOMAIN RULE, ENFORCED HERE TOO (spec §3, rule 4). A console that
    # refuses an address at sign-in but invites it has not enforced anything --
    # it has moved the lockout to the new colleague's first day.
    #
    # `domain_refusal`, NOT `is_allowed_domain` behind a truthiness test. The
    # helper is what every admission door calls, and it is the piece that
    # decides what an EMPTY or WHITESPACE-ONLY setting means:
    # `ITS_CONSOLE_EMAIL_DOMAIN=` yields `""`, not `None`, and it is read as NO
    # RESTRICTION because read as a restriction it matches no address at all and
    # locks the whole deployment out.
    refusal = domain_refusal(address, email_domain)
    if refusal is not None:
        raise UserAdminError(422, refusal)

    if len({(g.role_id, g.client_id) for g in grants}) != len(grants):
        raise UserAdminError(422, tr("the same role is listed twice for the same client"))
    roles = [await _visible_role(session, org_id, g.role_id) for g in grants]
    for grant in grants:
        if grant.client_id is not None:
            await _grantable_client(session, org_id, grant.client_id)

    # ANY user, contacts included and deliberately not `STAFF_KIND` alone: one
    # address is one person (MSP clients Task 9 review 1, I1), and a technician
    # created with a contact's address would make that address's inbound mail
    # ambiguous. LIMIT 1: rows from before D2's unique index may share an
    # address, and that is a 409 here, not a MultipleResultsFound.
    existing = (
        await session.execute(
            select(User.id).where(User.org_id == org_id, User.email == address).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise UserAdminError(409, tr("a user with that email address already exists here"))

    user = User(
        org_id=org_id,
        external_subject=f"{LOCAL_SUBJECT_PREFIX}{uuid4()}",
        email=address,
        display_name=display_name,
        status=ACTIVE,
    )
    session.add(user)
    try:
        await session.flush()
        for grant, role in zip(grants, roles, strict=True):
            session.add(
                UserRole(org_id=org_id, user_id=user.id, role_id=role.id, client_id=grant.client_id)
            )
        await session.flush()
    except IntegrityError as exc:
        # The address in another case (`ux_users_org_lower_email_staff`, D2)
        # or a concurrent create: raising rolls the whole transaction back.
        raise UserAdminError(409, tr("a user with that email address already exists here")) from exc

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="user.created",
        target_type="user",
        target_id=str(user.id),
        metadata={
            "email": address,
            "display_name": display_name,
            "roles": sorted(r.name for r in roles),
            "grants": [
                {"role": role.name, "client_id": str(g.client_id) if g.client_id else None}
                for g, role in zip(grants, roles, strict=True)
            ],
        },
    )
    return await _view(session, org_id, user)


async def _issue_invitation(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    user: User,
    console_url: str,
    deliver: Deliver,
) -> InvitedUser:
    """A new 72-hour link (replacing any earlier one), handed to `deliver`."""
    token = await action_tokens.issue(
        session,
        org_id=org_id,
        user_id=user.id,
        purpose=action_tokens.INVITE,
        ttl=action_tokens.INVITE_TTL,
        created_by=actor_id,
    )
    link = action_tokens.link(console_url, "invite", token)
    view = await _view(session, org_id, user)
    return InvitedUser(user=view, link=link, delivered=await deliver(view, link))


async def invite_user(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    email: str,
    display_name: str,
    grants: Sequence[Grant],
    email_domain: str | None,
    console_url: str,
    deliver: Deliver,
) -> InvitedUser:
    """Design §7: the technician, their grants, and a one-time link to choose a
    password and set up the second factor."""
    created = await create_user(
        session,
        org_id=org_id,
        actor_id=actor_id,
        email=email,
        display_name=display_name,
        grants=grants,
        email_domain=email_domain,
    )
    user = await _target(session, org_id, created.id)
    invited = await _issue_invitation(session, org_id, actor_id, user, console_url, deliver)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="user.invited",
        target_type="user",
        target_id=str(user.id),
        # `link_shown`, never the link: the audit log is readable by every
        # auditor in the organization (plan decision 4).
        metadata={"email": user.email, "link_shown": not invited.delivered},
    )
    return invited


async def resend_invite(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    user_id: UUID,
    console_url: str,
    deliver: Deliver,
) -> InvitedUser:
    """A new link for an invitation not accepted yet; the old one stops working.

    Only for a pending invitation (`_require_pending_invitation`), and never
    for a `super-admin`: an organization cannot grant that role, so it cannot
    invite its holder either -- and the link is a password for the account."""
    user = await _target(session, org_id, user_id)
    _require_live(user)
    await _require_pending_invitation(session, org_id, user)
    await _refuse_touching_a_super_admin(session, org_id, user.id, CANNOT_BE_INVITED)
    invited = await _issue_invitation(session, org_id, actor_id, user, console_url, deliver)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="user.invite_resent",
        target_type="user",
        target_id=str(user.id),
        metadata={"email": user.email, "link_shown": not invited.delivered},
    )
    return invited


async def cancel_invite(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, user_id: UUID
) -> None:
    """Plan decision 3: the link dies, the grants go, the account is marked
    deleted -- with delete's rails, the own-account one included (it is a
    second way to delete an account). Anything but a pending invitation is a
    409 (`_require_pending_invitation`).

    THE ADDRESS IS BURNT FOR GOOD (final review C-M4). The row is kept, as
    every deleted account's is (rule 2), and `create_user` refuses any row
    with the address -- deleted ones included -- as does the
    `ux_users_org_lower_email_staff` index. So a cancelled invitation's
    address, like a deleted technician's, can never be invited again in this
    organization. The route's description says so."""
    user = await _target(session, org_id, user_id)
    _require_live(user)
    _refuse_self(actor_id, user.id, DELETE)
    await _require_pending_invitation(session, org_id, user)
    await _refuse_touching_a_super_admin(session, org_id, user.id, CANNOT_BE_DELETED)
    await _refuse_stranding_the_org(session, org_id, user.id, CANNOT_BE_DELETED)
    await action_tokens.revoke_open(session, user_id=user.id, purpose=action_tokens.INVITE)
    # Counted before the delete, as `delete_user` does: `Result.rowcount` is
    # not on the type `session.execute` returns.
    grants = list(
        (
            await session.execute(
                select(UserRole.id).where(UserRole.user_id == user.id, UserRole.org_id == org_id)
            )
        ).scalars()
    )
    await session.execute(
        delete(UserRole).where(UserRole.user_id == user.id, UserRole.org_id == org_id)
    )
    # Plan decision 7: no `"deleted"` reason exists; the account cannot sign
    # in, as a disabled one cannot.
    await end_all_for_user(session, user.id, "disabled")
    user.status = DELETED
    session.add(user)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="user.invite_cancelled",
        target_type="user",
        target_id=str(user.id),
        metadata={"email": user.email, "roles_removed": len(grants)},
    )


# --------------------------------------------------------------------------- #
# role assignment
# --------------------------------------------------------------------------- #


async def assign_role(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    user_id: UUID,
    role_id: UUID,
    client_id: UUID | None,
) -> RoleAssignmentView:
    """Grant a role for every client (`client_id` None) or for one client
    (MSP clients spec §2.1).

    Roles are an ITMS concept, held in `user_roles` and nowhere else.
    """
    user = await _target(session, org_id, user_id)
    _require_live(user)
    role = await _visible_role(session, org_id, role_id)
    if client_id is not None:
        await _grantable_client(session, org_id, client_id)

    assignment = UserRole(org_id=org_id, user_id=user.id, role_id=role.id, client_id=client_id)
    session.add(assignment)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise UserAdminError(409, tr("this user already holds that role at that scope")) from exc

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="user.role_assigned",
        target_type="user",
        target_id=str(user.id),
        metadata={
            "role_id": str(role.id),
            "role_name": role.name,
            "client_id": str(client_id) if client_id else None,
        },
    )
    return RoleAssignmentView(
        assignment_id=assignment.id,
        role_id=role.id,
        role_name=role.name,
        client_id=client_id,
    )


async def remove_role(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    user_id: UUID,
    assignment_id: UUID,
) -> None:
    """Revoke one assignment.

    THE THIRD DOOR TO THE SAME END STATE. Disable, delete and this all end with
    an organization that has no administrator; a rail on two of the three is not
    a rail, so the last-`org-admin` check is here too.
    """
    user = await _target(session, org_id, user_id)
    row = (
        await session.execute(
            select(UserRole, Role.name)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                UserRole.id == assignment_id,
                UserRole.user_id == user.id,
                UserRole.org_id == org_id,
            )
        )
    ).first()
    if row is None:
        raise UserAdminError(404, tr("no such role assignment for this user"))
    assignment, role_name = row

    if role_name == SUPER_ADMIN:
        # THE REVOKE, and the reason it is refused rather than merely audited.
        # `_visible_role` will not grant this role from an org-scoped surface
        # and neither will migration 0065's database function, so this DELETE is
        # a one-way door: press it and the platform operator's own access is
        # gone until somebody runs SQL on production.
        raise UserAdminError(
            403,
            tr(
                "{role} is cross-org platform administration; it cannot be "
                "granted from an organization and so is not revoked from one either",
                role=SUPER_ADMIN,
            ),
        )

    # Only an every-client `org-admin` grant keeps the organization
    # administrable (`_active_org_admins`), so only removing one can strand it,
    # and only another one of the same person's means they keep the role.
    if role_name == ORG_ADMIN and assignment.client_id is None:
        remaining = (
            await session.execute(
                select(UserRole.id)
                .join(Role, Role.id == UserRole.role_id)
                .where(
                    UserRole.user_id == user.id,
                    UserRole.org_id == org_id,
                    UserRole.id != assignment_id,
                    UserRole.client_id.is_(None),
                    Role.name == ORG_ADMIN,
                )
            )
        ).first()
        if remaining is None:
            await _refuse_stranding_the_org(session, org_id, user.id, CANNOT_BE_STRIPPED)

    await session.execute(delete(UserRole).where(UserRole.id == assignment.id))
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="user.role_removed",
        target_type="user",
        target_id=str(user.id),
        metadata={
            "role_id": str(assignment.role_id),
            "role_name": role_name,
            "client_id": str(assignment.client_id) if assignment.client_id else None,
        },
    )


# --------------------------------------------------------------------------- #
# disable / enable
# --------------------------------------------------------------------------- #


async def set_enabled(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    user_id: UUID,
    enabled: bool,
) -> UserView:
    """Turn an account off, or back on.

    OFF MEANS NOW (design §2.4). `users.status` refuses the next admission at
    every door -- an AI app over MCP included -- and the person's sessions and
    remembered devices end in this same transaction, so the cookie already in
    a browser stops working too, and stays dead if the account is re-enabled.
    ON restores the status only: the person signs in again, with their second
    factor.
    """
    user = await _target(session, org_id, user_id)
    _require_live(user)
    if not enabled:
        _refuse_self(actor_id, user.id, DISABLE)
        await _refuse_touching_a_super_admin(session, org_id, user.id, CANNOT_BE_DISABLED)
        await _refuse_stranding_the_org(session, org_id, user.id, CANNOT_BE_DISABLED)

    user.status = ACTIVE if enabled else DISABLED
    session.add(user)
    await session.flush()
    if not enabled:
        await end_all_for_user(session, user.id, "disabled")
        await revoke_all_for_user(session, user.id)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="user.enabled" if enabled else "user.disabled",
        target_type="user",
        target_id=str(user.id),
        metadata={"email": user.email, "status": user.status},
    )
    return await _view(session, org_id, user)


# --------------------------------------------------------------------------- #
# the factors and the sessions of somebody else (design §2.5, §7)
# --------------------------------------------------------------------------- #


async def reset_factors(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    user_id: UUID,
    vault: VaultService,
) -> None:
    """Every passkey, the TOTP, the recovery codes and the failure counters
    go; the sessions end and the remembered devices are forgotten; the person
    is told. Their next sign-in enrols a new factor (design §7). The route
    has already checked the administrator's own recent confirmation.

    No `super-admin` rail (plan decision 5, kept by the operator on
    2026-11-05): a reset removes factors and gives nobody access without the
    password -- and since the final fix wave (K1) no reset link goes to an
    account left without a second factor, so the mailbox cannot supply the
    password either. `_refuse_self` stays: one's own factors are changed
    under My security."""
    user = await _target(session, org_id, user_id)
    _require_live(user)
    _refuse_self(actor_id, user.id, RESET_FACTORS)
    await clear_second_factors(session, user.id)
    await end_all_for_user(session, user.id, "factors_reset")
    await revoke_all_for_user(session, user.id)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="user.factors_reset",
        target_type="user",
        target_id=str(user.id),
        metadata={"email": user.email, "via": "administrator"},
    )
    await notices.tell(session, user, "factors_reset", vault=vault, actor_id=actor_id)


async def end_sessions(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, user_id: UUID
) -> None:
    """Every console session of the person ends; their remembered devices stay
    (a reset or a disable forgets those). A deleted account is refused
    (`_require_live`, final review C-M5): deleting ended its sessions, and a
    record of ending them again would be about nobody. No `super-admin` rail
    (operator decision 2, 2026-11-05)."""
    user = await _target(session, org_id, user_id)
    _require_live(user)
    await end_all_for_user(session, user.id, "ended_by_admin")
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="auth.sessions_ended",
        target_type="user",
        target_id=str(user.id),
        metadata={"by": "administrator", "scope": "everywhere"},
    )


# --------------------------------------------------------------------------- #
# delete -- rule 2
# --------------------------------------------------------------------------- #


async def delete_user(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    user_id: UUID,
) -> None:
    """Mark the row `deleted` and end everything it could still use.

    RULE 2, and §0.6 is the reason: 89 foreign-key columns across 21 tables
    reference `users.id`, audit `actor_id` among them. A hard delete either
    fails on those constraints or shreds the history that makes the audit log
    evidence -- an event that says something happened but not who did it is not
    a record of anything.

    The ROLE ASSIGNMENTS are removed, and that is not a contradiction: they are
    grants, not history. Leaving live grants attached to an account nobody can
    authenticate as is a dormant privilege waiting for a status column to be
    flipped back by hand. Nothing in the audit chain references `user_roles`.

    So do an open invitation, the sessions and the remembered devices, in the
    same transaction (design §2.4; plan decision 7: the sessions end as
    `"disabled"`, there being no `"deleted"` reason).
    """
    user = await _target(session, org_id, user_id)
    _require_live(user)
    _refuse_self(actor_id, user.id, DELETE)
    await _refuse_touching_a_super_admin(session, org_id, user.id, CANNOT_BE_DELETED)
    await _refuse_stranding_the_org(session, org_id, user.id, CANNOT_BE_DELETED)

    grants = list(
        (
            await session.execute(
                select(UserRole.id).where(
                    UserRole.user_id == user.id, UserRole.org_id == org_id
                )
            )
        ).scalars()
    )
    await session.execute(
        delete(UserRole).where(UserRole.user_id == user.id, UserRole.org_id == org_id)
    )
    await action_tokens.revoke_open(session, user_id=user.id, purpose=action_tokens.INVITE)
    await end_all_for_user(session, user.id, "disabled")
    await revoke_all_for_user(session, user.id)
    user.status = DELETED
    session.add(user)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="user.deleted",
        target_type="user",
        target_id=str(user.id),
        metadata={"email": user.email, "roles_removed": len(grants)},
    )
