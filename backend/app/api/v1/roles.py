"""Roles and the permission matrix: the console's access-control surface.

Spec `docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §1.

    GET    /api/v1/permissions              roles.read
    GET    /api/v1/roles                    roles.read
    POST   /api/v1/roles                    roles.manage
    PATCH  /api/v1/roles/{role_id}          roles.manage
    PUT    /api/v1/roles/{role_id}/permissions   roles.manage
    DELETE /api/v1/roles/{role_id}          roles.manage

Two codes, not one. `roles.read` answers "what does `technician` mean here";
`roles.manage` REDEFINES it for everyone holding it. Deliberately NOT
`orgs.manage`, whose description has always promised "settings, users, and
roles" while what it gates is the org name/settings PATCH and remote-tunnel
authorization -- attaching this surface to it would upgrade every current holder
from "can rename the org" to "can mint a fleet admin", a privilege escalation
delivered by a description string (spec §0.7).

THERE IS NO ROUTE THAT TAKES AN ORG ID, for the reason `app/api/v1/orgs.py`
gives at length: `ctx.org_id` comes from the validated token and the body cannot
name a target. Every body model is `extra="forbid"`, so a caller who sends
`org_id` gets a 422 rather than a silent no-op that reads like success.

WHY THESE HANDLERS ARE THIN. Everything interesting -- the SECURITY DEFINER
write path, clone-on-write for shared global roles, the `super-admin` exclusion
and the self-lockout rail -- lives in `app/core/rbac_admin.py`, because none of
it is an HTTP concern and all of it is worth testing without a client. The
handlers open the tenant session, call one service function, and translate
`RoleAdminError` into the status the service chose. See that module's docstring
for the reasoning behind each rule.

AUDIT (invariant #4). Every write records a `role.*` event through
`audit_service`, inside the SAME transaction as the change -- the service does
that, so the record and the change commit or roll back together. A refused write
(unknown code, self-lockout) raises out of `org_session`'s transaction, which
rolls back the audit row along with everything else, because a log saying an
edit happened when it did not is worse than no log.

THE RESPONSE CARRIES NO SECRET. Roles, codes and counts only; nothing here
touches `credentials` (invariant #3).
"""


from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthContext, require_permission
from app.core import rbac_admin
from app.core.i18n import tr
from app.core.rbac_admin import RoleAdminError
from app.core.tenancy import org_session

router = APIRouter(tags=["roles"])

# A body cannot ask for more codes than the platform has. The bound is a cheap
# refusal for an absurd payload; `rbac_admin._validate_codes` is what refuses a
# code that does not exist.
MAX_CODES = 512
MAX_DESCRIPTION = 1000


class PermissionOut(BaseModel):
    """One permission code, as the matrix renders it: a `<module>.<action>`
    cell with the platform's own description of what it grants."""

    code: str = Field(description="The permission code, e.g. `devices.manage`.")
    module: str = Field(
        description="The part before the first dot -- the matrix's row, e.g. `devices`."
    )
    action: str = Field(
        description="The rest of the code -- the matrix's column, e.g. `manage`."
    )
    description: str = Field(description="What holding this code allows.")


class PermissionCatalogueOut(BaseModel):
    """The whole permission catalogue, served from `app.core.rbac.PERMISSIONS`
    so a code added to the platform appears here with no second edit."""

    permissions: list[PermissionOut] = Field(
        description="Every permission code the platform defines, ordered by module then action."
    )


class RoleOut(BaseModel):
    """One role available to this organization."""

    id: UUID = Field(description="The role's id. Address writes to this.")
    name: str = Field(description="The role's name, unique within the organization.")
    description: str = Field(description="What the role is for.")
    is_global: bool = Field(
        description=(
            "True for a platform-seeded role shared by every organization. Editing "
            "one takes a copy for this organization first, so a later read returns "
            "a different id with `is_global` false."
        )
    )
    assigned_users: int = Field(
        description="How many users in this organization currently hold this role."
    )
    codes: list[str] = Field(description="The permission codes this role grants.")


class RoleListOut(BaseModel):
    """This organization's roles plus the platform-wide ones it has not taken a
    copy of. `super-admin` is never included."""

    roles: list[RoleOut] = Field(description="Roles this organization may assign, by name.")


class RolePermissionsOut(BaseModel):
    """The result of a matrix write."""

    role_id: UUID = Field(
        description=(
            "The role the codes were written to. NOT the requested id when a "
            "platform-wide role was copied into this organization first -- "
            "address that copy from now on."
        )
    )
    codes: list[str] = Field(description="The role's complete code set after the write.")
    affected_users: int = Field(
        description="How many users in this organization hold the role, and so were affected."
    )


class RoleCreate(BaseModel):
    """`extra="forbid"`: a body carrying `org_id` or `id` is a 422, not a no-op."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=MAX_DESCRIPTION)
    codes: list[str] = Field(default_factory=list, max_length=MAX_CODES)


class RoleUpdate(BaseModel):
    """Both fields optional; `model_fields_set` distinguishes "absent" from
    "explicitly null", so PATCH is a genuine partial update."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION)


class RolePermissionsIn(BaseModel):
    """The role's COMPLETE code set. The matrix is a set, not a log: a code the
    console unticks is one absent from this list."""

    model_config = ConfigDict(extra="forbid")

    codes: list[str] = Field(max_length=MAX_CODES)


def _http(exc: RoleAdminError) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


def _role_out(view: rbac_admin.RoleView) -> RoleOut:
    return RoleOut(
        id=view.id,
        name=view.name,
        description=view.description,
        is_global=view.is_global,
        assigned_users=view.assigned_users,
        codes=view.codes,
    )


@router.get("/permissions", response_model=PermissionCatalogueOut)
async def list_permissions(
    ctx: AuthContext = Depends(require_permission("roles.read")),
) -> PermissionCatalogueOut:
    """Every permission code the platform defines, with its module, action and
    description -- the axes of the permission matrix.

    No database read: the catalogue IS `app.core.rbac.PERMISSIONS`, the same
    dict `has_permission` validates against. Serving it from anywhere else would
    let the screen and the enforcement disagree.
    """
    return PermissionCatalogueOut(
        permissions=[
            PermissionOut(
                code=p.code, module=p.module, action=p.action, description=p.description
            )
            for p in rbac_admin.list_permissions()
        ]
    )


@router.get("/roles", response_model=RoleListOut)
async def list_roles(
    request: Request,
    ctx: AuthContext = Depends(require_permission("roles.read")),
) -> RoleListOut:
    """Roles this organization may assign: its own, plus the platform-wide ones
    it has not taken a copy of, each with its code set and how many local users
    hold it."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        views = await rbac_admin.list_roles(session, org_id=ctx.org_id)
    return RoleListOut(roles=[_role_out(v) for v in views])


@router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(
    body: RoleCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("roles.manage")),
) -> RoleOut:
    """Create a role in this organization and set its permission codes."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            view = await rbac_admin.create_role(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                name=body.name,
                description=body.description,
                codes=body.codes,
            )
        except RoleAdminError as exc:
            raise _http(exc) from exc
        return _role_out(view)


@router.patch("/roles/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: UUID,
    body: RoleUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("roles.manage")),
) -> RoleOut:
    """Rename or re-describe a role.

    Editing a platform-wide role copies it into this organization first and
    edits the copy, so the returned `id` may differ from `role_id` -- see
    `app/core/rbac_admin.py`.
    """
    if not body.model_fields_set:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            tr("no fields supplied; send name and/or description"),
        )
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            view = await rbac_admin.update_role(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                role_id=role_id,
                name=body.name,
                description=body.description,
            )
        except RoleAdminError as exc:
            raise _http(exc) from exc
        return _role_out(view)


@router.put("/roles/{role_id}/permissions", response_model=RolePermissionsOut)
async def set_role_permissions(
    role_id: UUID,
    body: RolePermissionsIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission("roles.manage")),
) -> RolePermissionsOut:
    """Replace the role's whole permission set, and report how many users in
    this organization that changed things for.

    The write goes through the `set_role_permissions` database function, never
    the `role_permissions` table -- `app_user` has no write privilege on it, by
    design (`deploy/postgres/grant-app-privileges.sql`).
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            result = await rbac_admin.set_permissions(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                role_id=role_id,
                codes=body.codes,
            )
        except RoleAdminError as exc:
            raise _http(exc) from exc
        return RolePermissionsOut(
            role_id=result.role_id, codes=result.codes, affected_users=result.affected_users
        )


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("roles.manage")),
) -> Response:
    """Delete one of this organization's own roles.

    A platform-wide role is refused (it is shared by every tenant), and so is a
    role users still hold -- with the count, so the operator knows what to do
    next.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await rbac_admin.delete_role(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, role_id=role_id
            )
        except RoleAdminError as exc:
            raise _http(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


