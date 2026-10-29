"""Role administration: the service layer behind `/api/v1/roles`.

Spec `docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §1.

The RBAC *engine* (`app/core/rbac.py`) has been complete since Phase 0 and has
never had a write surface -- 92 permission codes, roles, role->permission
grants and user->role assignments that nothing but SQL could change. This module
is that write surface, and it exists as a service rather than as route bodies
because three of the four things it has to get right are not HTTP concerns at
all.

THE MATRIX IS WRITTEN THROUGH A FUNCTION, NEVER THROUGH THE TABLE
-----------------------------------------------------------------
`deploy/postgres/grant-app-privileges.sql:59` REVOKES INSERT/UPDATE/DELETE on
`role_permissions` from `app_user`, and its own rationale block says why:
without it, `app_user` could write the RBAC catalogue directly, "a way to
self-grant permissions that bypasses the API's own RBAC checks entirely". That
control is load-bearing and it stays. Every write here therefore goes through
`set_role_permissions(uuid, text[])` -- a SECURITY DEFINER function owned by
`its_admin`, created by migration 0065, which validates its arguments before
writing anything.

This is not a detail that could be swapped for a `GRANT` later without anyone
noticing: a route that reached for the table directly would get `permission
denied for table role_permissions` from Postgres, and `tests/
test_rbac_matrix_function.py::test_app_user_still_cannot_write_role_permissions
_directly` is the non-vacuity check that the REVOKE is still there to produce
that error.

CLONE-ON-WRITE, AND WHY "EDITABLE" DOES NOT MEAN "EDIT THAT ROW"
-----------------------------------------------------------------
`Role.org_id` is NULLABLE and the five seeded roles (migration 0003) leave it
NULL, so `technician` is ONE ROW SHARED BY EVERY TENANT (spec §0.5). The
operator asked for seeded roles to be fully editable; taken literally that means
one org's edit silently reshapes another tenant's `technician`.

So a write against a global role transparently creates an ORG-SCOPED COPY
carrying the same name, description and current code set, REPOINTS this org's
`user_roles` rows at the copy, and applies the edit to the copy.
`UniqueConstraint("org_id", "name")` makes the copy legal beside the global row.
The administrator sees "I edited technician" and gets exactly that; what they do
not get is a reach into another tenant, which was never what "editable" asked
for.

Two consequences that are easy to get wrong and are handled explicitly below:

  * **The global row stays visible to the org after cloning.** RLS's
    `roles_select` policy is `org_id IS NULL OR org_id = current_org`, so a
    second write addressed to the *global* id must resolve to the org's existing
    copy rather than cloning again into a unique-constraint violation.
    `_resolve_for_write` does that.
  * **A listing must not show `technician` twice.** `list_roles` shadows a
    global role whose name the org has taken locally, and `create_role` refuses
    a name that already resolves -- so the shadow is only ever the deliberate
    one this module created.

`super-admin` IS NOT REACHABLE FROM HERE AT ALL
-----------------------------------------------
Never listed, never created, never edited, never cloned, never deleted. It is
cross-org platform administration ("Cross-org platform administration (SaaS
operator)"), and an org-scoped console that could grant or reshape it is a
tenant-escalation path. The database function refuses it too, and that is the
rail that holds for a caller who bypasses this module entirely -- but it is
refused *here* as well so the operator gets a clean 403 rather than a 500 with a
Postgres exception in it.

THE LOCKOUT RAILS ARE MEASURED, NOT SIMULATED -- AND THERE ARE TWO
-------------------------------------------------------------------
`set_permissions` records the state, applies the change, and recomputes it. If
the answer got worse, the whole transaction is rolled back by raising.
Measuring the real outcome rather than predicting it is what makes the rails
hold through clone-on-write, through a caller holding the role twice, and
through any future change to how grants compose -- a predicate that reasoned
about "does this role grant it" would have to be rewritten for each of those,
and would be wrong in between.

  * **Per caller**: `roles.manage` or `orgs.manage` held before the change and
    gone after it is refused.
  * **Per org**: `orgs.manage` held by somebody in the org before the change and
    by nobody after it is refused. The first rail does not imply the second and
    the gap between them is reachable: a caller holding `roles.manage` through
    some OTHER role can clone `org-admin`, strip `orgs.manage` from it, keep
    every code they personally hold, and leave the organization unable to
    administer itself -- with a 200. See spec §1.4, second bullet.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
------------------------------------------
It does not assign roles to users -- `user_roles` writes belong to the user
lifecycle surface, and so does refusing to UNASSIGN the last `org-admin` (spec
§1.4). What belongs here is the half of that same rail this surface can breach:
a role edit that empties the org of `orgs.manage` holders reaches the identical
end state without touching an assignment, so it is refused here too. The two
guards are the same rule enforced at the two doors that can break it.

It also does not stop a caller with `roles.manage` from adding codes to a role
they hold, which is a genuine self-grant path. That is inherent in what
`roles.manage` means ("set what permissions they carry") and in who holds it --
migration 0065 grants it to `super-admin` and `org-admin` only, and `org-admin`
already holds every code an organization can hold. Narrowing it further would
be a spec change, not a fix here.

WITH ONE EXCEPTION, AND IT IS THE EXCEPTION THAT PROVES THE SENTENCE ABOVE.
Migration 0068 took `platform.errors.*` off `org-admin`: the routes those codes
gate read `platform_error_event`, which carries every tenant's `org_id`,
`user_id`, `job_id` and `device_id` behind an RLS policy of `USING (true)`, so
the code IS the tenant boundary (audit
`docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md` F1). The moment
those codes left `org-admin`, "org-admin already holds every code" stopped
being true of them -- and the self-grant path above became a way to take back
what the migration had just removed. So `PLATFORM_OPERATOR_CODES`
(`app/core/rbac.py`) is excluded from `list_permissions` and refused by
`_validate_codes`, and 0068 refuses it a second time inside
`set_role_permissions` for a caller who never comes through this module.

MIGRATION 0070 PUT `agent.release.manage` IN THAT SAME SET, and it is the
member that makes the rail matter most. `agent_releases` carries no `org_id`
either -- one catalogue every tenant shares -- and that code publishes the
bytes every endpoint on the deployment downloads and runs. An org administrator
who could tick it onto a role and assign it to themselves would have arbitrary
code execution on every other tenant's fleet, from inside the console, with
`role.created` and `user.role_assigned` as the only trace. Nothing in this
module needed changing for it: the set is the mechanism, and it grew.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import distinct, func, or_, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.rbac import PERMISSIONS, PLATFORM_OPERATOR_CODES, user_permission_codes
from app.models import Permission, Role, RolePermission, UserRole

__all__ = [
    "GUARDED_CODES",
    "ORG_ADMIN_CODE",
    "SUPER_ADMIN",
    "PermissionView",
    "RoleAdminError",
    "RoleView",
    "SetPermissionsResult",
    "create_role",
    "delete_role",
    "list_permissions",
    "list_roles",
    "set_permissions",
    "update_role",
]

# The one role name that no route in this module may read, write, clone or
# delete. Compared by NAME because that is what the database function compares
# (migration 0065) -- two spellings of the same rail would eventually disagree.
SUPER_ADMIN = "super-admin"

# Removing one of these from a role the CALLER holds strands them: `roles.manage`
# gates this surface, `orgs.manage` gates the org-settings write and
# remote-tunnel authorization. Neither is recoverable from the console once
# lost, only by SQL against production.
GUARDED_CODES = frozenset({"roles.manage", "orgs.manage"})

# The code that must still have a holder somewhere in the org after any matrix
# write. This is spec §1.4's second rail -- "the last `org-admin` assignment in
# an org may not be removed" -- expressed as the PROPERTY it protects rather
# than as a statement about one role's name, because a rail keyed on the name
# `org-admin` is escaped by cloning it, renaming the clone, or granting
# `orgs.manage` through some other role instead.
#
# Scoped to `orgs.manage` and not to the whole of `GUARDED_CODES`: that is the
# hole the review named, and widening it to `roles.manage` is a different
# decision about a different failure mode, not a free extra.
ORG_ADMIN_CODE = "orgs.manage"


class RoleAdminError(Exception):
    """A refusal with the HTTP status the route should return.

    The service layer decides *why* something is refused; the route only
    translates. Carrying the status here keeps the two from drifting -- a route
    that mapped every service failure to one status would turn "you cannot lock
    yourself out" and "no such role" into the same message.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class PermissionView:
    """One cell of the matrix, derived from `app.core.rbac.PERMISSIONS`."""

    code: str
    module: str
    action: str
    description: str


@dataclass(frozen=True)
class RoleView:
    """One role as the console sees it.

    `is_global` is the seeded/shared flag -- a role the org has not (yet) taken
    a copy of. `assigned_users` counts users IN THIS ORG holding it, which is
    what tells an operator that "edit role" is not a local edit.
    """

    id: UUID
    name: str
    description: str
    is_global: bool
    assigned_users: int
    codes: list[str]


@dataclass(frozen=True)
class SetPermissionsResult:
    """`role_id` is the id the codes were written to, which is NOT the requested
    id when clone-on-write fired. Returning it is how the console learns to
    address the org's own copy from now on."""

    role_id: UUID
    codes: list[str]
    affected_users: int


def list_permissions() -> list[PermissionView]:
    """The whole catalogue, from `app.core.rbac.PERMISSIONS` -- the platform's
    single source of truth for what codes exist.

    NEVER a second hand-typed list. A code added to the platform must appear in
    the matrix without another edit; a copy here would drift the moment a module
    added a code, and the drift would present as a permission the console cannot
    grant rather than as a failing test.

    `<module>.<action>`, split at the FIRST dot: `remote.recordings.read` is the
    `remote` module with action `recordings.read`, exactly as migration 0010
    seeds it into the `permissions` table. Splitting at the last dot instead
    would invent a `remote.recordings` module that no migration agrees with.

    MINUS `PLATFORM_OPERATOR_CODES`, and that is not a drifting copy -- it is
    the same dict with one named set removed. This matrix is what an ORG
    administrator may tick onto a role, and `_validate_codes` refuses those
    codes on write (as does migration 0068's database function). Listing a code
    the write path will 403 on renders a checkbox that silently fails, which is
    the worst failure mode an access-control screen has.
    """
    views: list[PermissionView] = []
    for code, description in PERMISSIONS.items():
        if code in PLATFORM_OPERATOR_CODES:
            continue
        module, _, action = code.partition(".")
        views.append(
            PermissionView(code=code, module=module, action=action, description=description)
        )
    return sorted(views, key=lambda v: (v.module, v.action))


def _pg_detail(exc: DBAPIError) -> str:
    """The message the database function raised, without the driver noise.

    Every condition `set_role_permissions` raises on is also checked in Python
    before the call, so reaching this means the two disagreed -- which is worth
    surfacing verbatim rather than flattening into "internal error".
    """
    original = getattr(exc, "orig", None)
    return str(original) if original is not None else str(exc)


async def _codes_of(session: AsyncSession, role_id: UUID) -> set[str]:
    rows = await session.execute(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id)
    )
    return set(rows.scalars())


async def _assigned_users(session: AsyncSession, org_id: UUID, role_id: UUID) -> int:
    """Users IN THIS ORG holding the role. The `org_id` predicate is redundant
    with RLS on `user_roles` and is written anyway -- RLS is the backstop, not
    the design, and this same count is read by tests that drive the routes
    through the BYPASSRLS engine."""
    return int(
        (
            await session.execute(
                select(func.count(distinct(UserRole.user_id))).where(
                    UserRole.role_id == role_id, UserRole.org_id == org_id
                )
            )
        ).scalar_one()
    )


async def _holders_of(session: AsyncSession, org_id: UUID, code: str) -> int:
    """How many users IN THIS ORG actually hold `code` for every client.

    `client_id IS NULL` is an all-clients grant, and only that counts as
    holding an org-level code: a single-client grant does not. It matches
    `user_permission_codes`, which counts a client-scoped grant only when the
    question names that same client: a user holding `orgs.manage` for one
    client cannot administer the organization, so counting them as a holder
    would leave the org locked out while the rail below reported it safe.
    """
    return int(
        (
            await session.execute(
                select(func.count(distinct(UserRole.user_id)))
                .join(RolePermission, RolePermission.role_id == UserRole.role_id)
                .join(Permission, Permission.id == RolePermission.permission_id)
                .where(
                    UserRole.org_id == org_id,
                    UserRole.client_id.is_(None),
                    Permission.code == code,
                )
            )
        ).scalar_one()
    )


def _validate_codes(codes: list[str]) -> list[str]:
    """Refuse unknown codes HERE, before anything is written or cloned.

    The database function refuses them too, but it refuses them *after* a clone
    would already have been created in the same transaction. Checking first
    means a typo cannot be the reason an org acquires a copy of `technician`.
    """
    unknown = sorted({c for c in codes if c not in PERMISSIONS})
    if unknown:
        raise RoleAdminError(422, tr(
            "unknown permission code(s): {unknown}",
            unknown=", ".join(unknown),
        ))
    # AND REFUSE THE PLATFORM OPERATOR'S CODES, which is a 403 rather than a
    # 422: they are real, catalogued codes, and the answer is "not from here"
    # rather than "no such thing".
    #
    # This is the rail that makes migration 0068 mean anything. 0068 takes
    # `platform.errors.*` off `org-admin` because the routes they gate read
    # every tenant's error correlation ids with no tenant predicate under them
    # (audit 2026-08-21-tenancy-test-efficacy F1). `org-admin` also holds
    # `roles.manage`, so without this an administrator could tick the code onto
    # a role, assign it to themselves with `users.manage`, and undo the
    # revocation from the console.
    #
    # THIS MODULE'S DOCSTRING USED TO SAY the self-grant path was acceptable
    # because "`org-admin` already holds every code". 0068 is precisely the
    # change that made that premise false, so the rail arrives with it.
    operator = sorted({c for c in codes if c in PLATFORM_OPERATOR_CODES})
    if operator:
        raise RoleAdminError(
            403,
            tr(
                "platform operator permission(s) are not grantable from an organization: "
                "{operator}",
                operator=", ".join(operator),
            ),
        )
    return sorted(set(codes))


async def _apply_codes(session: AsyncSession, role_id: UUID, codes: list[str]) -> None:
    """The ONLY write path to `role_permissions` in this application.

    `app_user` holds no INSERT/UPDATE/DELETE on that table (see the module
    docstring); this calls the SECURITY DEFINER function instead. Raising here
    aborts the enclosing transaction, so a refused apply leaves nothing behind
    -- including a clone created moments earlier.
    """
    try:
        await session.execute(
            text("SELECT set_role_permissions(:role_id, :codes)"),
            {"role_id": str(role_id), "codes": codes},
        )
    except DBAPIError as exc:
        raise RoleAdminError(409, _pg_detail(exc)) from exc


def _refuse_super_admin(name: str) -> None:
    if name == SUPER_ADMIN:
        raise RoleAdminError(
            403,
            tr(
                "{super_admin} is cross-org platform administration and is not readable, "
                "editable or deletable from an organization",
                super_admin=SUPER_ADMIN,
            ),
        )


async def _visible_role(session: AsyncSession, org_id: UUID, role_id: UUID) -> Role:
    """This org's own role, or a global one -- and nothing else.

    The predicate mirrors RLS's `roles_select` policy rather than relying on it:
    a route driven through the BYPASSRLS engine (which is how the tenancy tests
    check that the application predicate exists at all) must still refuse
    another org's role.
    """
    role = (
        await session.execute(
            select(Role).where(
                Role.id == role_id, or_(Role.org_id == org_id, Role.org_id.is_(None))
            )
        )
    ).scalar_one_or_none()
    if role is None:
        raise RoleAdminError(404, tr("no such role"))
    _refuse_super_admin(role.name)
    return role


async def _local_role_named(session: AsyncSession, org_id: UUID, name: str) -> Role | None:
    """This org's OWN role of that name. At most one exists --
    `UniqueConstraint("org_id", "name")` -- so `scalar_one_or_none` is safe here
    in a way it is NOT in `_resolves_to`, which spans the global rows too."""
    return (
        await session.execute(
            select(Role).where(Role.org_id == org_id, Role.name == name)
        )
    ).scalar_one_or_none()


async def _resolves_to(
    session: AsyncSession, org_id: UUID, name: str, *, excluding: UUID | None = None
) -> Role | None:
    """The role this org would resolve `name` to, or None if the name is free.

    NOT `scalar_one_or_none()` over `name == n AND (org_id = me OR org_id IS
    NULL)`. That predicate matches ONE row only until clone-on-write fires;
    after it, the global `technician` and this org's copy of it BOTH match and
    `scalar_one_or_none` raises `MultipleResultsFound` -- a 500 on the most
    ordinary mistake an operator can make on this screen, a duplicate name, and
    on the first org that has ever edited a seeded role.

    Two rows matching is not an ambiguity to be refused, it is the design:
    `list_roles` shadows the global row behind the local one, so the name
    resolves to the LOCAL row and that is the one the refusal should describe.
    Ordering on `org_id IS NULL` puts it first (false sorts before true).
    """
    stmt = select(Role).where(
        Role.name == name, or_(Role.org_id == org_id, Role.org_id.is_(None))
    )
    if excluding is not None:
        stmt = stmt.where(Role.id != excluding)
    return (await session.execute(stmt.order_by(Role.org_id.is_(None)))).scalars().first()


async def _clone_into_org(
    session: AsyncSession, org_id: UUID, role: Role, actor_id: UUID
) -> Role:
    """Copy a global role into this org and repoint this org's assignments.

    The copy carries the same name, description and CURRENT code set, so nobody
    holding the role gains or loses anything at the moment of cloning -- the
    edit that follows is the only thing that changes.

    The `user_roles` repoint is the half that makes the illusion true. Without
    it the org's users would still hold the global row, and the subsequent edit
    would apply to a role nobody has.
    """
    codes = sorted(await _codes_of(session, role.id))
    clone = Role(org_id=org_id, name=role.name, description=role.description)
    session.add(clone)
    await session.flush()
    await _apply_codes(session, clone.id, codes)
    await session.execute(
        update(UserRole)
        .where(UserRole.role_id == role.id, UserRole.org_id == org_id)
        .values(role_id=clone.id)
        .execution_options(synchronize_session=False)
    )
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="role.cloned",
        target_type="role",
        target_id=str(clone.id),
        metadata={
            "role_name": clone.name,
            "cloned_from": str(role.id),
            "codes": codes,
            "reason": "a shared global role is copied into the org before it is edited",
        },
    )
    return clone


async def _resolve_for_write(
    session: AsyncSession, org_id: UUID, role_id: UUID, actor_id: UUID
) -> tuple[Role, UUID | None]:
    """The role this org may actually write to, plus the global id it came from.

    Three cases, and the middle one is the one a naive implementation misses:
    the role is already this org's; the role is global and the org has no copy
    (clone it); the role is global and the org ALREADY has a copy, because a
    previous edit cloned it and RLS still shows the org the global row. Cloning
    again would violate `UniqueConstraint("org_id", "name")`.
    """
    role = await _visible_role(session, org_id, role_id)
    if role.org_id is not None:
        return role, None
    existing = await _local_role_named(session, org_id, role.name)
    if existing is not None:
        return existing, role.id
    return await _clone_into_org(session, org_id, role, actor_id), role.id


async def _view(session: AsyncSession, org_id: UUID, role: Role) -> RoleView:
    return RoleView(
        id=role.id,
        name=role.name,
        description=role.description,
        is_global=role.org_id is None,
        assigned_users=await _assigned_users(session, org_id, role.id),
        codes=sorted(await _codes_of(session, role.id)),
    )


async def list_roles(session: AsyncSession, *, org_id: UUID) -> list[RoleView]:
    """Every role this org may use: its own, plus the global ones it has not
    taken a copy of.

    A global role whose name the org holds locally is SHADOWED, not listed
    twice. After clone-on-write the global `technician` row is still visible to
    the org (RLS's `roles_select` is `org_id IS NULL OR org_id = current_org`),
    and a list showing two rows called `technician` is a screen on which the
    operator cannot tell which one their users hold.

    `super-admin` is not listed at all -- see the module docstring.
    """
    roles = list(
        (
            await session.execute(
                select(Role)
                .where(or_(Role.org_id == org_id, Role.org_id.is_(None)))
                .order_by(Role.name)
            )
        ).scalars()
    )
    local_names = {r.name for r in roles if r.org_id is not None}
    visible = [
        r
        for r in roles
        if r.name != SUPER_ADMIN and (r.org_id is not None or r.name not in local_names)
    ]
    if not visible:
        return []

    ids = [r.id for r in visible]
    codes: dict[UUID, list[str]] = {rid: [] for rid in ids}
    for role_id, code in (
        await session.execute(
            select(RolePermission.role_id, Permission.code)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(RolePermission.role_id.in_(ids))
            .order_by(Permission.code)
        )
    ).all():
        codes[role_id].append(code)

    counts: dict[UUID, int] = {rid: 0 for rid in ids}
    for role_id, count in (
        await session.execute(
            select(UserRole.role_id, func.count(distinct(UserRole.user_id)))
            .where(UserRole.org_id == org_id, UserRole.role_id.in_(ids))
            .group_by(UserRole.role_id)
        )
    ).all():
        counts[role_id] = int(count)

    return [
        RoleView(
            id=r.id,
            name=r.name,
            description=r.description,
            is_global=r.org_id is None,
            assigned_users=counts[r.id],
            codes=codes[r.id],
        )
        for r in visible
    ]


async def create_role(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    name: str,
    description: str,
    codes: list[str],
) -> RoleView:
    """A new role in this org, with its code set applied in the same transaction.

    The name must not already RESOLVE for this org -- neither a local role nor a
    visible global one. A local `technician` created beside the global one would
    shadow it for every future read while the org's existing users still held
    the global row, and it is also the collision clone-on-write would later hit.
    """
    _refuse_super_admin(name)
    wanted = _validate_codes(codes)
    clash = await _resolves_to(session, org_id, name)
    if clash is not None:
        raise RoleAdminError(
            409,
            tr(
                "a role named {name} already exists (it is a platform-wide role)",
                name=repr(name),
            )
            if clash.org_id is None
            else tr("a role named {name} already exists", name=repr(name)),
        )

    role = Role(org_id=org_id, name=name, description=description)
    session.add(role)
    await session.flush()
    await _apply_codes(session, role.id, wanted)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="role.created",
        target_type="role",
        target_id=str(role.id),
        metadata={"role_name": name, "codes_added": wanted, "codes_removed": []},
    )
    return await _view(session, org_id, role)


async def update_role(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    role_id: UUID,
    name: str | None,
    description: str | None,
) -> RoleView:
    """Rename / re-describe a role. Clone-on-write applies: editing a global
    role edits this org's copy of it, creating that copy if needed.

    Renaming is where the shadowing rule bites twice, so the new name is checked
    against every OTHER visible role, global ones included.
    """
    role, _cloned_from = await _resolve_for_write(session, org_id, role_id, actor_id)
    metadata: dict[str, object] = {"role_name": role.name}
    if name is not None and name != role.name:
        _refuse_super_admin(name)
        clash = await _resolves_to(session, org_id, name, excluding=role.id)
        if clash is not None:
            raise RoleAdminError(409, tr("a role named {name} already exists", name=repr(name)))
        metadata["name_from"] = role.name
        metadata["name_to"] = name
        role.name = name
    if description is not None and description != role.description:
        metadata["description_changed"] = True
        role.description = description
    session.add(role)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="role.updated",
        target_type="role",
        target_id=str(role.id),
        metadata=metadata,
    )
    await session.flush()
    return await _view(session, org_id, role)


async def set_permissions(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    role_id: UUID,
    codes: list[str],
) -> SetPermissionsResult:
    """Replace a role's whole code set. The matrix write.

    Order matters, and each step is here because doing it later would be wrong:

    1. resolve and refuse `super-admin` -- a 403, not a database exception;
    2. validate the codes -- so a typo cannot be the reason a clone exists;
    3. clone if the role is global -- see the module docstring;
    4. record the caller's effective codes AND the org's `orgs.manage` holder
       count, apply, and recompute both. If a guarded code the caller held is
       gone, or the org has been emptied of `orgs.manage` holders, raise: the
       enclosing transaction rolls back, taking the apply AND the clone with it.

    The rails measure rather than predict. A predicate that reasoned about
    "does this role grant it" would have to be re-derived for a caller holding
    the role through two assignments, or for any future change to how grants
    compose; recomputing the real answer is correct for all of them.
    """
    role = await _visible_role(session, org_id, role_id)
    wanted = _validate_codes(codes)
    target, _cloned_from = await _resolve_for_write(session, org_id, role.id, actor_id)

    before = await _codes_of(session, target.id)
    # EVERY-CLIENT GRANTS, BY DESIGN (plan 2026-10-26 Task 14 keeps it): the
    # rail guards the codes that administer the whole organisation, so what it
    # compares is what the actor holds everywhere -- a code kept on one client
    # would not stop the lock-out this refuses.
    held_before = await user_permission_codes(session, actor_id)
    admins_before = await _holders_of(session, org_id, ORG_ADMIN_CODE)

    await _apply_codes(session, target.id, wanted)

    held_after = await user_permission_codes(session, actor_id)
    lost = sorted((held_before & GUARDED_CODES) - held_after)
    if lost:
        raise RoleAdminError(
            403,
            tr(
                "this change would remove your own {lost}; an administrator cannot lock "
                "themselves out of access control",
                lost=", ".join(lost),
            ),
        )

    # ...and the same question asked about the ORG, which the rail above does
    # not answer. A caller holding `roles.manage` through some other role keeps
    # their own codes while stripping `orgs.manage` from every administrator
    # here -- the check above passes, the org is left unadministrable, and the
    # caller sees a 200. Measured the same way and for the same reason: the
    # count after the apply is the real answer for a caller holding the role
    # twice, for a role reached through clone-on-write, and for any future
    # change to how grants compose.
    if admins_before and not await _holders_of(session, org_id, ORG_ADMIN_CODE):
        raise RoleAdminError(
            403,
            tr(
                "this change would leave the organization with nobody holding "
                "{org_admin_code}; grant it to another user first",
                org_admin_code=ORG_ADMIN_CODE,
            ),
        )

    added = sorted(set(wanted) - before)
    removed = sorted(before - set(wanted))
    affected = await _assigned_users(session, org_id, target.id)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="role.permissions_set",
        target_type="role",
        target_id=str(target.id),
        metadata={
            "role_name": target.name,
            "codes_added": added,
            "codes_removed": removed,
            "affected_users": affected,
        },
    )
    return SetPermissionsResult(role_id=target.id, codes=wanted, affected_users=affected)


async def delete_role(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, role_id: UUID
) -> None:
    """Delete one of this org's own roles.

    A GLOBAL role is refused rather than cloned. Cloning to EDIT is coherent --
    the org ends up with its own copy carrying its own meaning. Cloning to
    DELETE is not: the row is shared by every tenant, so "delete technician"
    from one org either deletes another tenant's role or does nothing at all,
    and neither is what the operator asked for. There is no org-scoped meaning
    to give it, so it is refused with one.

    A role users still HOLD is refused too, with the count. Deleting it would
    either fail on `user_roles`' foreign key or silently strip permissions from
    everyone holding it -- and it is also what makes lockout-by-deletion
    impossible, since a caller who would be stranded necessarily holds the role.
    """
    role = await _visible_role(session, org_id, role_id)
    if role.org_id is None:
        raise RoleAdminError(
            409,
            tr(
                "{name} is a platform-wide role shared by every organization and cannot be "
                "deleted; edit it instead, which takes a copy for this organization",
                name=repr(role.name),
            ),
        )
    holders = await _assigned_users(session, org_id, role.id)
    if holders:
        raise RoleAdminError(
            409,
            tr(
                "{holders} user(s) still hold {name}; reassign them before deleting the role",
                holders=holders,
                name=repr(role.name),
            ),
        )

    codes = sorted(await _codes_of(session, role.id))
    # The grants go first: `role_permissions` references `roles`, and they are
    # only removable through the function -- an empty code set is how it deletes.
    await _apply_codes(session, role.id, [])
    await session.delete(role)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="role.deleted",
        target_type="role",
        target_id=str(role.id),
        metadata={"role_name": role.name, "codes_removed": codes},
    )
    await session.flush()
