"""The user-lifecycle surface: `/api/v1/users`.

Spec `docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §2;
invitations, factor resets and ended sessions since P2 phase 1
(`docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §7).

    GET    /api/v1/users                              users.read
    GET    /api/v1/users/{id}/effective-permissions   users.read
    POST   /api/v1/users/invite                       users.manage
    POST   /api/v1/users/{id}/invite/resend           users.manage
    DELETE /api/v1/users/{id}/invite                  users.manage
    POST   /api/v1/users/{id}/roles                   users.manage
    DELETE /api/v1/users/{id}/roles/{assignment_id}   users.manage
    POST   /api/v1/users/{id}/disable                 users.manage
    POST   /api/v1/users/{id}/enable                  users.manage
    POST   /api/v1/users/{id}/factors/reset           users.manage, and a recent second factor
    DELETE /api/v1/users/{id}/sessions                users.manage
    DELETE /api/v1/users/{id}                         users.manage

TWO CODES, NOT ONE, and deliberately NOT `orgs.manage`. `users.read` answers
"who has access here"; `users.manage` GRANTS it. `orgs.manage`'s description has
always promised "settings, users, and roles" while what it actually gates is the
org name/settings PATCH and remote-tunnel authorization (spec §0.7) -- attaching
identity lifecycle to it would upgrade every current holder from "can rename the
org" to "can mint a fleet admin", a privilege escalation delivered by a
description string.

NO ROUTE TAKES AN ORG ID. `ctx.org_id` comes from the validated token and no
body can name a target, exactly as `app/api/v1/orgs.py` states for the org
PATCH. Every body model is `extra="forbid"`, so a caller who sends `org_id` gets
a 422 rather than a silent no-op that reads like success.

WHY THESE HANDLERS ARE THIN. The rails, the tenant predicates and the audit
records are all in `service.py`, because none of them is an HTTP concern and
all of them are worth testing without a client. A handler opens the tenant
session, calls one function, and translates `UserAdminError` into the status
the service chose.

AUDIT (invariant #4). Every write records an event through `audit_service` in
the SAME transaction as the change, so the record and the change commit or
roll back together. A refused write raises out of `org_session`'s
transaction, which rolls the audit row back too -- a log saying a change
happened when it did not is worse than no log.

NO RESPONSE ON THIS SURFACE CARRIES A CREDENTIAL (invariant #3, rule 1) but
one: an invitation link, in the answer to the invite or the resend, when no
mail carried it (design §7). `tests/test_user_admin_api.py` drives every route
here -- with the mail working -- and scans the bodies.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    AuthContext,
    require_permission,
    require_permission_on_every_client,
    require_step_up,
)
from app.core.auth import notices
from app.core.auth.refusals import AuthRefusal
from app.core.i18n import tr
from app.core.identity.policy import resolve_console_domain
from app.core.tenancy import org_session
from app.models import User
from app.modules.user_admin import service
from app.modules.user_admin.service import UserAdminError

#: `users.manage` with a scope of EVERY client. Technicians and their grants
#: are Novieri-global (MSP clients spec §1.4, "Técnicos y roles"), and a
#: per-client grant of this code must not become one that reaches every client:
#: a technician holding it on one client could otherwise grant themselves -- or
#: a colleague -- a role on all of them, or disable the administrators who do
#: hold every client. Reads stay open to any scope.
_MANAGE = require_permission_on_every_client("users.manage")

router = APIRouter(tags=["users"])

# A create body cannot ask for more roles than any organization plausibly has.
# A cheap refusal for an absurd payload; the service is what refuses a role that
# does not exist or may not be granted.
MAX_ROLES = 64
MAX_EMAIL = 320
MAX_DISPLAY_NAME = 200


def _console_url(request: Request) -> str:
    """Where an invitation link points: `ITS_PUBLIC_CONSOLE_URL`, never the
    request's Host header (host-header injection). Checked before anything is
    written, so a deployment without it creates nobody it cannot invite.

    NAMES NO SETTING, as the 503 it replaces did not (`docs/superpowers/specs/
    2026-08-19-invisible-identity-layer-design.md` §1.3): the variable is
    documented in `deploy/.env.example`, where whoever sets the deployment up
    reads it."""
    url = request.app.state.settings.public_console_url
    if not url:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            tr(
                "this installation's console address is not configured, so an invitation link "
                "cannot be made. That is a server setting, not something this screen can fix."
            ),
        )
    return str(url)


async def _mailer(session: AsyncSession, request: Request, ctx: AuthContext) -> service.Deliver:
    """The invitation mail, from the administrator who asked for it, sent in
    the request's session (Task 3: the relay credential's checkout is audited
    there, and a failed send is caught inside it, `notices.deliver`)."""
    inviter = (
        await session.execute(
            select(User.display_name, User.email).where(
                User.id == ctx.user_id, User.org_id == ctx.org_id
            )
        )
    ).one()
    name = inviter.display_name or inviter.email

    async def deliver(user: service.UserView, link: str) -> bool:
        subject, text = notices.invitation_mail(link, inviter=name)
        return await notices.deliver(
            session,
            org_id=ctx.org_id,
            to=user.email,
            subject=subject,
            text=text,
            vault=request.app.state.vault,
            actor_id=ctx.user_id,
        )

    return deliver


def _http(exc: UserAdminError) -> HTTPException:
    """`{"detail": ...}`, and `"code"` beside it when the refusal carries one
    the console acts on (`AuthRefusal`, whose handler writes both)."""
    if exc.code is not None:
        return AuthRefusal(exc.status_code, code=exc.code, detail=exc.detail)
    return HTTPException(exc.status_code, exc.detail)


# --------------------------------------------------------------------------- #
# response models
# --------------------------------------------------------------------------- #


class RoleAssignmentOut(BaseModel):
    """One grant held by one user."""

    assignment_id: UUID = Field(
        description=(
            "The `user_roles` row. Address a revoke to THIS, not to `role_id`: the "
            "same role may be held twice for different clients, so 'remove "
            "technician' is ambiguous."
        )
    )
    role_id: UUID = Field(description="The role granted.")
    role_name: str = Field(description="The role's name, e.g. `technician`.")
    client_id: UUID | None = Field(
        description=(
            "`null` for a grant on every client, including clients created later; a "
            "client id when the grant applies only within that client."
        )
    )


class UserOut(BaseModel):
    """One user in this organization. Carries no credential of any kind."""

    id: UUID = Field(description="The ITMS user id. Address every route here to this.")
    email: str = Field(description="The address this person signs in with.")
    display_name: str = Field(description="How the console shows this person.")
    status: str = Field(
        description=(
            "`active`, `disabled` or `deleted`. Only `active` is admitted at the "
            "three admission doors. A `deleted` user's row is RETAINED so the audit "
            "log keeps resolving them."
        )
    )
    roles: list[RoleAssignmentOut] = Field(description="Every role this user holds.")
    invite_pending: bool = Field(
        description=(
            "True while this person has an invitation they have not accepted -- expired or "
            "not: resend it or cancel it."
        )
    )
    invite_expires_at: datetime | None = Field(
        description=(
            "When that invitation's link expires; null once they have chosen a password, or "
            "when no invitation is open."
        )
    )


class UserListOut(BaseModel):
    users: list[UserOut] = Field(
        description="Everyone in the calling user's organization, deleted users included."
    )


class InvitedOut(BaseModel):
    """An invitation. `invite_link` is the one secret this surface returns:
    once, and only when no mail carried it (design §7)."""

    user_id: UUID = Field(description="The invited technician's ITMS user id.")
    invite_link: str | None = Field(
        description=(
            "The one-time link to hand over another way when the mail did not go; null when it "
            "did. It is in this answer only."
        )
    )


class InviteLinkOut(BaseModel):
    """A resent invitation: the new link, under the same rule as the first."""

    invite_link: str | None = Field(
        description=(
            "The new one-time link when the mail did not go; null when it did. The previous "
            "link no longer works."
        )
    )


class CodeGrantOut(BaseModel):
    code: str = Field(description="A permission code the user effectively holds.")
    via_role: str = Field(description="The role that granted it.")
    client_id: UUID | None = Field(
        description="`null` when the grant covers every client, else the client it is scoped to."
    )


class EffectivePermissionsOut(BaseModel):
    """What this user can actually do, and why -- spec §1.5.

    A code granted by two roles appears twice, once per grant, because "you have
    this because of X" is the question this view exists to answer.
    """

    user_id: UUID
    codes: list[CodeGrantOut]


# --------------------------------------------------------------------------- #
# request models -- all `extra="forbid"`
# --------------------------------------------------------------------------- #


class GrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_id: UUID
    client_id: UUID | None = Field(
        default=None,
        description="Omit for every client; a client to scope the grant to it.",
    )


class UserInvite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=MAX_EMAIL)
    display_name: str = Field(default="", max_length=MAX_DISPLAY_NAME)
    grants: list[GrantIn] = Field(default_factory=list, max_length=MAX_ROLES)


class RoleAssign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_id: UUID
    client_id: UUID | None = Field(
        default=None,
        description=(
            "Omit for a grant on every client; supply a client to scope the grant to it. "
            "Not the unassigned client, and not an archived one."
        ),
    )


# --------------------------------------------------------------------------- #
# translation helpers
# --------------------------------------------------------------------------- #


def _user_out(view: service.UserView) -> UserOut:
    return UserOut(
        id=view.id,
        email=view.email,
        display_name=view.display_name,
        status=view.status,
        invite_pending=view.invite_expires_at is not None,
        invite_expires_at=view.invite_expires_at,
        roles=[
            RoleAssignmentOut(
                assignment_id=r.assignment_id,
                role_id=r.role_id,
                role_name=r.role_name,
                client_id=r.client_id,
            )
            for r in view.roles
        ],
    )


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #


@router.get("/users", response_model=UserListOut)
async def list_users(
    request: Request,
    ctx: AuthContext = Depends(require_permission("users.read")),
) -> UserListOut:
    """Everyone in the calling user's organization, with their role assignments
    and, while an invitation is open, when it expires.

    The ITMS rows, which are what every other surface in the platform joins
    against.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        views = await service.list_users(session, org_id=ctx.org_id)
    return UserListOut(users=[_user_out(v) for v in views])


@router.get("/users/{user_id}/effective-permissions", response_model=EffectivePermissionsOut)
async def effective_permissions(
    user_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("users.read")),
) -> EffectivePermissionsOut:
    """The union of this user's roles' codes, annotated with which role granted
    each and whether the grant covers every client or one client."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            grants = await service.effective_permissions(
                session, org_id=ctx.org_id, user_id=user_id
            )
        except UserAdminError as exc:
            raise _http(exc) from exc
    return EffectivePermissionsOut(
        user_id=user_id,
        codes=[
            CodeGrantOut(
                code=g.code, via_role=g.via_role, client_id=g.client_id
            )
            for g in grants
        ],
    )


# --------------------------------------------------------------------------- #
# invite
# --------------------------------------------------------------------------- #


@router.post("/users/invite", response_model=InvitedOut, status_code=status.HTTP_201_CREATED)
async def invite_user(
    body: UserInvite,
    request: Request,
    ctx: AuthContext = Depends(_MANAGE),
) -> InvitedOut:
    """Create a technician with their grants and send them a one-time link,
    valid 72 hours, to choose a password and set up their second factor.

    The email domain is checked against the org's console email-domain
    setting, resolved by the same function the doors use.
    """
    console = _console_url(request)
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            invited = await service.invite_user(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                email=body.email,
                display_name=body.display_name,
                grants=[service.Grant(g.role_id, g.client_id) for g in body.grants],
                email_domain=await resolve_console_domain(
                    session, ctx.org_id, request.app.state.settings
                ),
                console_url=console,
                deliver=await _mailer(session, request, ctx),
            )
        except UserAdminError as exc:
            raise _http(exc) from exc
    return InvitedOut(
        user_id=invited.user.id, invite_link=None if invited.delivered else invited.link
    )


@router.post("/users/{user_id}/invite/resend", response_model=InviteLinkOut)
async def resend_invite(
    user_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(_MANAGE),
) -> InviteLinkOut:
    """A new invitation link for somebody who has not accepted yet; the old
    link stops working. Refused once they have chosen a password."""
    console = _console_url(request)
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            invited = await service.resend_invite(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                user_id=user_id,
                console_url=console,
                deliver=await _mailer(session, request, ctx),
            )
        except UserAdminError as exc:
            raise _http(exc) from exc
    return InviteLinkOut(invite_link=None if invited.delivered else invited.link)


@router.delete("/users/{user_id}/invite", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_invite(
    user_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(_MANAGE),
) -> Response:
    """Cancel an invitation not accepted yet: the link dies and the account is
    marked deleted (its row is kept). Refused once they have chosen a password.
    The address stays taken for good: a deleted account's address cannot be
    invited again, in this organization, under any name."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await service.cancel_invite(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, user_id=user_id
            )
        except UserAdminError as exc:
            raise _http(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# roles
# --------------------------------------------------------------------------- #


@router.post(
    "/users/{user_id}/roles",
    response_model=RoleAssignmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def assign_role(
    user_id: UUID,
    body: RoleAssign,
    request: Request,
    ctx: AuthContext = Depends(_MANAGE),
) -> RoleAssignmentOut:
    """Grant a role for every client, or for one client (MSP clients spec §2.1)."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            view = await service.assign_role(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                user_id=user_id,
                role_id=body.role_id,
                client_id=body.client_id,
            )
        except UserAdminError as exc:
            raise _http(exc) from exc
        return RoleAssignmentOut(
            assignment_id=view.assignment_id,
            role_id=view.role_id,
            role_name=view.role_name,
            client_id=view.client_id,
        )


@router.delete(
    "/users/{user_id}/roles/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_role(
    user_id: UUID,
    assignment_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(_MANAGE),
) -> Response:
    """Revoke one assignment.

    Refused when it would leave the organization with no active `org-admin`,
    and refused outright for `super-admin`: no console surface can grant that
    role back, so revoking it is a one-way door.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await service.remove_role(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                user_id=user_id,
                assignment_id=assignment_id,
            )
        except UserAdminError as exc:
            raise _http(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# disable / enable
# --------------------------------------------------------------------------- #


async def _set_enabled(
    request: Request,
    ctx: AuthContext,
    user_id: UUID,
    enabled: bool,
) -> UserOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            view = await service.set_enabled(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                user_id=user_id,
                enabled=enabled,
            )
        except UserAdminError as exc:
            raise _http(exc) from exc
        return _user_out(view)


@router.post("/users/{user_id}/disable", response_model=UserOut)
async def disable_user(
    user_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(_MANAGE),
) -> UserOut:
    """Stop this account signing in, and end its sessions and remembered
    devices now.

    Refused for your OWN account, for the organization's last active
    `org-admin`, and for any holder of `super-admin` -- all three are
    denial-of-service against a recovery path only SQL on production can undo.
    """
    return await _set_enabled(request, ctx, user_id, enabled=False)


@router.post("/users/{user_id}/enable", response_model=UserOut)
async def enable_user(
    user_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(_MANAGE),
) -> UserOut:
    """Undo a disable. A deleted user is not re-enabled here."""
    return await _set_enabled(request, ctx, user_id, enabled=True)


# --------------------------------------------------------------------------- #
# somebody else's factors and sessions
# --------------------------------------------------------------------------- #


@router.post("/users/{user_id}/factors/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_factors(
    user_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(_MANAGE),
) -> Response:
    """Remove this person's passkeys, authenticator app and recovery codes,
    end their sessions and forget their devices; they set up a new second
    factor at their next sign-in, and are told by mail. Needs your own second
    factor confirmed in the last ten minutes. Not for your own account.
    Refused (409 `reset_needs_recovery`) while their password was set with an
    emailed reset link and no second factor has been used since: use the
    server's recovery command then."""
    # Design §2.5: the administrator's OWN second factor, confirmed in the last
    # ten minutes (Task 6; plan decision 5). Before anything is read.
    require_step_up(ctx)
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await service.reset_factors(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                user_id=user_id,
                vault=request.app.state.vault,
            )
        except UserAdminError as exc:
            raise _http(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/users/{user_id}/sessions", status_code=status.HTTP_204_NO_CONTENT)
async def end_sessions(
    user_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(_MANAGE),
) -> Response:
    """End every console session of this person now."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await service.end_sessions(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, user_id=user_id
            )
        except UserAdminError as exc:
            raise _http(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(_MANAGE),
) -> Response:
    """Mark the user deleted and end their sessions, remembered devices and any
    open invitation. The ITMS row is RETAINED, marked `deleted`.

    Retained because 89 foreign-key columns across 21 tables reference
    `users.id`, audit `actor_id` among them: a hard delete either fails on the
    constraints or leaves an audit log that can say what happened but not who
    did it.

    Refused for your own account, for the last active `org-admin`, and for any
    holder of `super-admin`.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await service.delete_user(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, user_id=user_id
            )
        except UserAdminError as exc:
            raise _http(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
