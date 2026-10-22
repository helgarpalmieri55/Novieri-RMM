"""User lifecycle: the decisions, separately from the HTTP and from Keycloak.

`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §2.

Everything worth arguing about lives here rather than in `api.py`: the ordering
of the two halves of a create, the rails that stop an administrator locking the
organization out of itself, the tenant predicates, and the audit records. The
route handlers open a tenant session, call one function, and translate
`UserAdminError` into the status this module chose.

THE THREE THINGS THIS MODULE REFUSES TO DO, each stated where it is enforced
below but collected here because they are the reason the module exists:

1. **It never handles a password.** "Reset password" is `execute-actions-email`
   with `UPDATE_PASSWORD`; Keycloak generates the token, sends the mail and
   validates the link. There is no code path here that could produce, store or
   return a credential, and `tests/test_user_admin_api.py` scans every response
   body on the surface to keep it that way.
2. **It never hard-deletes a user.** 89 foreign-key columns across 21 tables
   reference `users.id` -- audit `actor_id` among them -- so deleting the row
   would either fail on the constraints or turn the audit log from evidence into
   a list of anonymous events. The row is marked `status='deleted'` and kept.
3. **It never writes the ITMS row before the Keycloak identity exists.** See
   `create_user`.

WHY THE KEYCLOAK CALL SITS INSIDE THE DATABASE TRANSACTION on disable, enable
and delete. Two systems cannot be updated atomically, so the question is only
which half is recoverable. Writing the ITMS row first and calling Keycloak
before the commit means a Keycloak failure raises, the transaction rolls back,
and NOTHING happened -- which the operator can retry. The alternative leaves the
console and Keycloak disagreeing about who is allowed to sign in, and nothing
detects it. The residual window is a commit that fails after Keycloak succeeded;
that is narrower than the alternative and is named here rather than hidden.

WHY `create_user` IS THE EXCEPTION and calls Keycloak FIRST: see its docstring.
"""

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

import httpx
from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.identity import domain_refusal
from app.core.wire_words import say
from app.models import DeviceGroup, Permission, Role, RolePermission, User, UserRole
from app.modules.user_admin import kc_client
from app.modules.user_admin.kc_client import (
    ENROLMENT_ACTIONS,
    PASSWORD_RESET_ACTIONS,
    KeycloakAdminConfig,
    KeycloakAdminError,
)
from app.modules.user_admin.words import (
    ACTION_WORDS,
    CANNOT_BE_DELETED,
    CANNOT_BE_DISABLED,
    CANNOT_BE_STRIPPED,
    DELETE,
    DISABLE,
    OUTCOME_WORDS,
    PROVIDER_ACTION_WORDS,
)

__all__ = [
    "ACTIVE",
    "DELETED",
    "DISABLED",
    "ORG_ADMIN",
    "SUPER_ADMIN",
    "CodeGrant",
    "CreatedUser",
    "RoleAssignmentView",
    "UserAdminError",
    "UserView",
    "assign_role",
    "create_user",
    "delete_user",
    "effective_permissions",
    "list_users",
    "remove_role",
    "resend_enrolment",
    "send_password_reset",
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


logger = logging.getLogger(__name__)


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
    than the role id: `UniqueConstraint(user_id, role_id, device_group_id)`
    permits the same role twice for different device groups, so "remove
    technician" is ambiguous and "remove this assignment" is not.
    """

    assignment_id: UUID
    role_id: UUID
    role_name: str
    device_group_id: UUID | None


@dataclass(frozen=True)
class UserView:
    id: UUID
    email: str
    display_name: str
    status: str
    roles: list[RoleAssignmentView]


@dataclass(frozen=True)
class CodeGrant:
    """One permission code, and how the user came to hold it.

    `via_role` and `device_group_id` are the whole point (spec §1.5): an
    access-control screen whose result you have to simulate in your head is one
    you will get wrong, and a view that flattened the group scope would report a
    grant as org-wide when it applies to one group.
    """

    code: str
    via_role: str
    device_group_id: UUID | None


@dataclass(frozen=True)
class CreatedUser:
    """The new user, plus what happened to the enrolment mail.

    The mail outcome is part of the RESULT rather than an exception because the
    user genuinely exists either way, and because the realm this ships against
    has no `smtpServer` today (spec §2.3). Reporting "created" while silently
    sending nothing produces an account nobody can ever sign into, with every
    status green.
    """

    user: UserView
    enrolment_email_sent: bool
    enrolment_email_error: str | None


# --------------------------------------------------------------------------- #
# talking to Keycloak
# --------------------------------------------------------------------------- #


@contextmanager
def _translating(what: str) -> Iterator[None]:
    """Turn a Keycloak failure into a refusal that says what failed.

    THE RESPONSE BODY IS NEVER INCLUDED, only the status. Keycloak's error
    bodies are not under this project's control and can echo request content;
    forwarding them to an API response is how a credential ends up somewhere it
    was never meant to be (invariant 3). The status is what an operator needs
    and the rest is in Keycloak's own log.

    AND THE WORD ITSELF STOPS HERE, which is the second rule and a separate
    one. These details are rendered: `api.py` turns them straight into an
    `HTTPException`, and `create_user` copies one into
    `CreatedUser.enrolment_email_error`, a field the Users tab prints after a
    successful create. A buyer does not know what Keycloak is and must never
    need to
    (`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md`
    §1.3). This module's own prose still names the vendor, because it is what
    the module talks to and pretending otherwise would be a lie; what crosses
    into a response says "the identity provider".

    `KeycloakAdminError`'S OWN WORDING IS LOGGED, NOT FORWARDED. `kc_client`
    raises it with sentences like "Keycloak accepted the user but returned no
    Location header" -- accurate, useful to whoever runs the deployment, and
    the vendor's name assembled at RUNTIME, where no scan of source literals
    could ever have caught it. So the operator gets it in the log and the
    caller gets the same shape of answer as the other two branches.
    `tests/test_no_vendor_names_backend.py` drives all three.
    """
    try:
        yield
    except httpx.HTTPStatusError as exc:
        raise UserAdminError(
            502,
            tr(
                "the identity provider refused to {what} (HTTP {status_code})",
                what=say(PROVIDER_ACTION_WORDS, what),
                status_code=exc.response.status_code,
            ),
        ) from exc
    except KeycloakAdminError as exc:
        # `exc_info=`, never a %-arg: `str(exc)` is exactly what must not be
        # interpolated into a template (`tests/test_logging_discipline.py`).
        logger.warning("the identity provider could not complete a user action", exc_info=exc)
        raise UserAdminError(
            502,
            tr(
                "the identity provider could not {what}",
                what=say(PROVIDER_ACTION_WORDS, what),
            ),
        ) from exc
    except httpx.HTTPError as exc:
        raise UserAdminError(
            502,
            tr(
                "the identity provider was unreachable while trying to {what} ({error_type})",
                what=say(PROVIDER_ACTION_WORDS, what),
                error_type=type(exc).__name__,
            ),
        ) from exc


# --------------------------------------------------------------------------- #
# lookups -- every one carries an explicit org predicate
# --------------------------------------------------------------------------- #


async def _target(session: AsyncSession, org_id: UUID, user_id: UUID) -> User:
    """This organization's user, or a 404.

    `User.org_id == org_id` mirrors the RLS policy rather than relying on it.
    RLS is the backstop; a route with no predicate of its own passes every test
    that runs through a tenant session, which is why the tenancy tests drive
    these routes through the BYPASSRLS engine and why this line has to exist.

    A 404 rather than a 403: whether a user id exists in another tenant is
    itself information this organization has no business learning.
    """
    user = (
        await session.execute(
            select(User).where(User.id == user_id, User.org_id == org_id)
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


async def _visible_group(
    session: AsyncSession, org_id: UUID, group_id: UUID
) -> DeviceGroup:
    """A grant scoped to another tenant's device group is either meaningless or
    a cross-tenant reference. Neither gets written.

    AN ARCHIVED GROUP IS REFUSED TOO, and this is the one consumer-side refusal
    the device-group archive design builds (spec §7.1). Deploy, patch, backup,
    remote, health and bcp are deliberately NOT taught about `archived_at`,
    because the archive is refused while the group holds anything at all -- so
    an action aimed at an archived group hits an empty set, which is safe. A
    role grant is the exception: `user_roles.device_group_id` narrows RBAC, and
    a grant scoped to a group no surface can show is a permission nobody can
    audit. It is also a WRITE that would newly reference the group, which is
    precisely what "empty and unreferenced by construction" has to keep true.
    """
    group = (
        await session.execute(
            select(DeviceGroup).where(
                DeviceGroup.id == group_id, DeviceGroup.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if group is None:
        raise UserAdminError(404, tr("no such device group"))
    if group.archived_at is not None:
        raise UserAdminError(409, tr("that device group is archived"))
    return group


async def _active_org_admins(session: AsyncSession, org_id: UUID) -> set[UUID]:
    """Users in this org who hold `org-admin` AND can currently sign in.

    STATUS MATTERS: a disabled or deleted `org-admin` cannot grant the role back,
    so counting them would let the rail be satisfied by an account that is no
    help at all. The role is matched by NAME, so an org that took a clone of the
    seeded role through the roles surface (spec §1.3) still counts.
    """
    rows = await session.execute(
        select(User.id)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            User.org_id == org_id,
            User.status == ACTIVE,
            Role.name == ORG_ADMIN,
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
        select(UserRole.id, UserRole.user_id, Role.id, Role.name, UserRole.device_group_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.org_id == org_id, UserRole.user_id.in_(user_ids))
        .order_by(Role.name, UserRole.id)
    )
    grouped: dict[UUID, list[RoleAssignmentView]] = {uid: [] for uid in user_ids}
    for assignment_id, user_id, role_id, role_name, group_id in rows:
        grouped[user_id].append(
            RoleAssignmentView(
                assignment_id=assignment_id,
                role_id=role_id,
                role_name=role_name,
                device_group_id=group_id,
            )
        )
    return grouped


async def list_users(session: AsyncSession, *, org_id: UUID) -> list[UserView]:
    """Every user in THIS organization, with their role assignments.

    Deleted users are included, carrying `status: "deleted"`. Hiding them would
    make the row's retention (rule 2) invisible to the operator, who would then
    reasonably conclude the delete failed and try again.
    """
    users = list(
        (
            await session.execute(
                select(User).where(User.org_id == org_id).order_by(User.email)
            )
        ).scalars()
    )
    grouped = await _assignments(session, org_id, [u.id for u in users])
    return [
        UserView(
            id=u.id,
            email=u.email,
            display_name=u.display_name,
            status=u.status,
            roles=grouped.get(u.id, []),
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

    The same code granted twice -- by two roles, or by one role org-wide and
    another scoped to a group -- appears once per grant, because "you have this
    because of X" is the question the view answers.
    """
    await _target(session, org_id, user_id)
    rows = await session.execute(
        select(Permission.code, Role.name, UserRole.device_group_id)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id == user_id, UserRole.org_id == org_id)
        .order_by(Permission.code, Role.name)
    )
    return [
        CodeGrant(code=code, via_role=role_name, device_group_id=group_id)
        for code, role_name, group_id in rows
    ]


async def _view(session: AsyncSession, org_id: UUID, user: User) -> UserView:
    grouped = await _assignments(session, org_id, [user.id])
    return UserView(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        status=user.status,
        roles=grouped.get(user.id, []),
    )


# --------------------------------------------------------------------------- #
# create -- rule 3 (ordering) and rule 5 (SMTP)
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
    config: KeycloakAdminConfig,
    *,
    org_id: UUID,
    actor_id: UUID,
    email: str,
    display_name: str,
    role_ids: Sequence[UUID],
    email_domain: str | None,
) -> CreatedUser:
    """Create the Keycloak identity, THEN the ITMS row. Rule 3, and the order is
    the whole point.

    Keycloak's create returns the new user's id in `Location`, and that id IS
    the `sub` claim the token will carry -- so the ITMS row can be written with
    the correct `external_subject` immediately rather than waiting for a first
    login to reconcile.

    It also fails safe in the direction that matters. If the ITMS half fails,
    what is left is a Keycloak account holding no roles: exactly the state of
    any federated user before their first login, which `app/api/deps.py`
    already self-heals by provisioning a row on admission. The REVERSE order
    strands an ITMS row holding roles that no identity can ever claim, and
    nothing self-heals that.

    Everything that can be refused is refused BEFORE the Keycloak call -- the
    domain rule, the roles, an address already in use -- so a validation failure
    never mints an identity that then has to be cleaned up by hand.
    """
    address = _normalise_email(email)

    # THE DOMAIN RULE, ENFORCED HERE TOO (spec §3, rule 4). A console that
    # refuses an address at sign-in but mints it in the create form has not
    # enforced anything -- it has moved the lockout to the new colleague's first
    # day.
    #
    # `domain_refusal`, NOT `is_allowed_domain` behind a truthiness test. The
    # helper is what all three admission doors call, and it is the piece that
    # decides what an EMPTY or WHITESPACE-ONLY setting means:
    # `ITS_CONSOLE_EMAIL_DOMAIN=` yields `""`, not `None`, and it is read as NO
    # RESTRICTION because read as a restriction it matches no address at all and
    # locks the whole deployment out. `if email_domain and ...` reads that same
    # string as a restriction and refuses every create -- one setting, two
    # answers, and the disagreement only shows up on the deployment that typed
    # the variable and left it blank.
    refusal = domain_refusal(address, email_domain)
    if refusal is not None:
        raise UserAdminError(422, refusal)

    roles = [await _visible_role(session, org_id, role_id) for role_id in role_ids]

    existing = (
        await session.execute(
            select(User.id).where(User.org_id == org_id, User.email == address)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise UserAdminError(409, tr("a user with that email address already exists here"))

    with _translating("create the user"):
        subject = await kc_client.create_user(
            config, email=address, display_name=display_name
        )

    user = User(
        org_id=org_id,
        external_subject=subject,
        email=address,
        display_name=display_name,
        status=ACTIVE,
    )
    session.add(user)
    try:
        await session.flush()
        for role in roles:
            session.add(
                UserRole(org_id=org_id, user_id=user.id, role_id=role.id)
            )
        await session.flush()
    except IntegrityError as exc:
        # The ITMS half failed after the identity was created. Raising rolls the
        # whole transaction back and leaves the fail-safe state described above:
        # an account with no roles. Reported as a conflict rather than a 500
        # because that is what it is -- a row already claims this subject or
        # this address.
        raise UserAdminError(
            409,
            tr("the identity was created but the local record conflicts with an existing one"),
        ) from exc

    # THE MAIL IS ATTEMPTED, NOT ASSUMED (rule 5, spec §2.3). A user created
    # this way has NO credential at all, so without this mail the account cannot
    # be signed into; and `deploy/keycloak/realm-it-suite.json` has no
    # `smtpServer` today, so failing is the expected first experience. The
    # failure is REPORTED and the user is KEPT -- rolling back here would delete
    # the ITMS row while the Keycloak identity stayed, which is the one
    # inconsistency this ordering exists to avoid, and `resend_enrolment` is
    # what makes the account recoverable.
    sent = True
    error: str | None = None
    try:
        with _translating("send the enrolment email"):
            await kc_client.execute_actions_email(
                config, user_id=subject, actions=ENROLMENT_ACTIONS
            )
    except UserAdminError as exc:
        sent = False
        error = exc.detail

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
            "enrolment_email_sent": sent,
        },
    )
    return CreatedUser(
        user=await _view(session, org_id, user),
        enrolment_email_sent=sent,
        enrolment_email_error=error,
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
    device_group_id: UUID | None,
) -> RoleAssignmentView:
    """Grant a role, org-wide or scoped to one device group.

    NO KEYCLOAK CALL. Roles are an ITMS concept; Keycloak knows only that the
    identity exists. Which means this route keeps working on a deployment that
    has not configured provisioning at all.
    """
    user = await _target(session, org_id, user_id)
    _require_live(user)
    role = await _visible_role(session, org_id, role_id)
    if device_group_id is not None:
        await _visible_group(session, org_id, device_group_id)

    assignment = UserRole(
        org_id=org_id, user_id=user.id, role_id=role.id, device_group_id=device_group_id
    )
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
            "device_group_id": str(device_group_id) if device_group_id else None,
        },
    )
    return RoleAssignmentView(
        assignment_id=assignment.id,
        role_id=role.id,
        role_name=role.name,
        device_group_id=device_group_id,
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

    if role_name == ORG_ADMIN:
        remaining = (
            await session.execute(
                select(UserRole.id)
                .join(Role, Role.id == UserRole.role_id)
                .where(
                    UserRole.user_id == user.id,
                    UserRole.org_id == org_id,
                    UserRole.id != assignment_id,
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
            "device_group_id": (
                str(assignment.device_group_id) if assignment.device_group_id else None
            ),
        },
    )


# --------------------------------------------------------------------------- #
# disable / enable
# --------------------------------------------------------------------------- #


async def set_enabled(
    session: AsyncSession,
    config: KeycloakAdminConfig,
    *,
    org_id: UUID,
    actor_id: UUID,
    user_id: UUID,
    enabled: bool,
) -> UserView:
    """Turn an account off, or back on -- in BOTH systems.

    NEITHER HALF IS SUFFICIENT ALONE, which is the whole reason both are here.
    Keycloak's `enabled: false` stops the NEXT token being issued and does
    nothing to the one already sitting in the operator's browser. `users.status`
    is what the three admission doors consult on every request, so it is what
    closes that window (spec §3.2) -- and it is what a deployment with no
    Keycloak provisioning would have nothing else to rely on.
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
    # LAST, AND INSIDE THE TRANSACTION. A refusal here raises, the transaction
    # rolls back, and the two systems still agree -- see the module docstring.
    with _translating("update the user"):
        await kc_client.set_enabled(
            config, user_id=user.external_subject, enabled=enabled
        )
    return await _view(session, org_id, user)


# --------------------------------------------------------------------------- #
# the two mails -- rule 1 and rule 5
# --------------------------------------------------------------------------- #


async def _mail(
    session: AsyncSession,
    config: KeycloakAdminConfig,
    *,
    org_id: UUID,
    actor_id: UUID,
    user: User,
    actions: Sequence[str],
    action_name: str,
    what: str,
) -> None:
    """Ask Keycloak to send one action mail, and record that we asked.

    A FAILURE IS AN ERROR STATUS, not a flag in a 200 body -- the opposite of
    `create_user`, deliberately. There the account exists whatever the mail
    does, so the mail is one fact about a larger result. Here the mail IS the
    entire action: there is no partial success to report, and answering 200
    would tell the operator a link is on its way when the realm has no SMTP
    server at all.

    The audit record is written first and rolls back with the failure, so the
    chain never claims a mail was requested that was refused.
    """
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action=action_name,
        target_type="user",
        target_id=str(user.id),
        metadata={"email": user.email, "actions": list(actions)},
    )
    with _translating(what):
        await kc_client.execute_actions_email(
            config, user_id=user.external_subject, actions=actions
        )


async def send_password_reset(
    session: AsyncSession,
    config: KeycloakAdminConfig,
    *,
    org_id: UUID,
    actor_id: UUID,
    user_id: UUID,
) -> None:
    """RULE 1, IN FULL. This is the entire implementation of "reset this
    person's password": ask Keycloak to email them a link.

    ITMS does not generate a password, does not store one, does not display one
    and does not return one. There is no branch here that could, and there is no
    admin API call anywhere in this module that sets a credential. The reset
    token is minted by Keycloak, sent by Keycloak and validated by Keycloak;
    nothing secret crosses this boundary in either direction.
    """
    user = await _target(session, org_id, user_id)
    _require_live(user)
    await _mail(
        session,
        config,
        org_id=org_id,
        actor_id=actor_id,
        user=user,
        actions=PASSWORD_RESET_ACTIONS,
        action_name="user.password_reset_requested",
        what="send the password-reset email",
    )


async def resend_enrolment(
    session: AsyncSession,
    config: KeycloakAdminConfig,
    *,
    org_id: UUID,
    actor_id: UUID,
    user_id: UUID,
) -> None:
    """RULE 5's other half, and the reason it is not optional.

    A user created by ITMS has no credential, so the enrolment mail is the only
    thing that makes the account usable -- and Keycloak 26 exposes no admin API
    that returns the action link for an operator to pass on by hand. Without
    this route, one transient mail failure leaves an account nobody can ever
    sign into and no way to fix it short of deleting and recreating the person.
    """
    user = await _target(session, org_id, user_id)
    _require_live(user)
    await _mail(
        session,
        config,
        org_id=org_id,
        actor_id=actor_id,
        user=user,
        actions=ENROLMENT_ACTIONS,
        action_name="user.enrolment_resent",
        what="send the enrolment email",
    )


# --------------------------------------------------------------------------- #
# delete -- rule 2
# --------------------------------------------------------------------------- #


async def delete_user(
    session: AsyncSession,
    config: KeycloakAdminConfig,
    *,
    org_id: UUID,
    actor_id: UUID,
    user_id: UUID,
) -> None:
    """Remove the Keycloak identity; KEEP the ITMS row, marked `deleted`.

    RULE 2, and §0.6 is the reason: 89 foreign-key columns across 21 tables
    reference `users.id`, audit `actor_id` among them. A hard delete either
    fails on those constraints or shreds the history that makes the audit log
    evidence -- an event that says something happened but not who did it is not
    a record of anything.

    The ROLE ASSIGNMENTS are removed, and that is not a contradiction: they are
    grants, not history. Leaving live grants attached to an account nobody can
    authenticate as is a dormant privilege waiting for a status column to be
    flipped back by hand. Nothing in the audit chain references `user_roles`.
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
    with _translating("delete the user"):
        await kc_client.delete_user(config, user_id=user.external_subject)
