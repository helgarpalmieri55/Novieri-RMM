"""The user-lifecycle surface: `/api/v1/users`.

Spec `docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §2.

    GET    /api/v1/users                              users.read
    GET    /api/v1/users/{id}/effective-permissions   users.read
    POST   /api/v1/users                              users.manage
    POST   /api/v1/users/{id}/roles                   users.manage
    DELETE /api/v1/users/{id}/roles/{assignment_id}   users.manage
    POST   /api/v1/users/{id}/disable                 users.manage
    POST   /api/v1/users/{id}/enable                  users.manage
    POST   /api/v1/users/{id}/reset-password          users.manage
    POST   /api/v1/users/{id}/resend-enrolment        users.manage
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

WHY THESE HANDLERS ARE THIN. The ordering of the two halves of a create, the
rails, the tenant predicates and the audit records are all in `service.py`,
because none of them is an HTTP concern and all of them are worth testing
without a client. A handler opens the tenant session, calls one function, and
translates `UserAdminError` into the status the service chose.

AUDIT (invariant #4). Every write records a `user.*` event through
`audit_service` in the SAME transaction as the change, so the record and the
change commit or roll back together. A refused write raises out of
`org_session`'s transaction, which rolls the audit row back too -- a log saying
a change happened when it did not is worse than no log.

NO RESPONSE ON THIS SURFACE CARRIES A CREDENTIAL (invariant #3, rule 1).
Keycloak's error bodies are never forwarded -- only their status -- and
`tests/test_user_admin_api.py` drives every route here and scans the bodies.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthContext, require_permission
from app.core.i18n import N_, tr_marked
from app.core.identity.policy import resolve_console_domain
from app.core.tenancy import org_session
from app.modules.user_admin import service
from app.modules.user_admin.kc_client import KeycloakAdminConfig
from app.modules.user_admin.service import UserAdminError

router = APIRouter(tags=["users"])

# A create body cannot ask for more roles than any organization plausibly has.
# A cheap refusal for an absurd payload; the service is what refuses a role that
# does not exist or may not be granted.
MAX_ROLES = 64
MAX_EMAIL = 320
MAX_DISPLAY_NAME = 200


def keycloak_config(request: Request) -> KeycloakAdminConfig | None:
    """The provisioning credential, or `None` on a deployment without one.

    A DEPENDENCY RATHER THAN A DIRECT CALL so the lifecycle routes can be driven
    in a test without a live Keycloak, and so "not configured" is answered once,
    in one place, with one status.

    `None` IS A SUPPORTED DEPLOYMENT STATE (`KeycloakAdminConfig.from_settings`),
    so `_provisioning` below turns it into a 503 naming the setting -- not a 500.
    An operator who has not set the provisioning client up needs to be told that,
    not handed a stack trace. Note that a deployment CONFIGURED with a missing or
    empty secret raises instead, because that operator believes it is running.

    THIS READS A SECRET OFF DISK, so every route that declares it declares
    `require_permission` FIRST. FastAPI resolves dependencies in declaration
    order; with this one ahead of the gate, an anonymous request would open the
    `manage-users` credential before anything had established who was asking.
    Nothing reaches the caller either way, which is exactly why the ordering has
    to be deliberate rather than incidental -- and why
    `test_authorization_runs_before_the_provisioning_secret_is_read` pins it.
    """
    return KeycloakAdminConfig.from_settings(request.app.state.settings)


#: The 503 for a deployment whose provisioning client was never configured.
#:
#: NAMES NEITHER THE VENDOR NOR ITS SETTINGS, and the settings are the part
#: that is easy to miss: `ITS_KEYCLOAK_ADMIN_URL` and
#: `ITS_KEYCLOAK_ADMIN_SECRET_PATH` carry the word inside the identifier, so
#: printing them to a console user fails the buyer test just as squarely as a
#: sentence would (`docs/superpowers/specs/2026-08-19-invisible-identity-layer-
#: design.md` §1.3). Those two names are the right ones and stay exactly as
#: they are -- here, in a comment, where whoever has to set the deployment up
#: will be reading the source anyway, and in `deploy/.env.example` where they
#: are documented. `app/api/v1/setup.py`'s `_NOT_CONFIGURED` says the same
#: thing the same way, for the same reason.
_NOT_CONFIGURED = N_(
    "this installation cannot manage accounts yet: its identity provisioning is "
    "not configured. That is a server setting, not something this screen can fix."
)


def _provisioning(config: KeycloakAdminConfig | None) -> KeycloakAdminConfig:
    if config is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, tr_marked(_NOT_CONFIGURED))
    return config


def _http(exc: UserAdminError) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


# --------------------------------------------------------------------------- #
# response models
# --------------------------------------------------------------------------- #


class RoleAssignmentOut(BaseModel):
    """One grant held by one user."""

    assignment_id: UUID = Field(
        description=(
            "The `user_roles` row. Address a revoke to THIS, not to `role_id`: the "
            "same role may be held twice at different device-group scopes, so "
            "'remove technician' is ambiguous."
        )
    )
    role_id: UUID = Field(description="The role granted.")
    role_name: str = Field(description="The role's name, e.g. `technician`.")
    device_group_id: UUID | None = Field(
        description=(
            "`null` for an org-wide grant; a device group id when the grant applies "
            "only within that group."
        )
    )


class UserOut(BaseModel):
    """One user in this organization. Carries no credential of any kind."""

    id: UUID = Field(description="The ITMS user id. Address every route here to this.")
    email: str = Field(description="The address the identity provider asserts.")
    display_name: str = Field(description="How the console shows this person.")
    status: str = Field(
        description=(
            "`active`, `disabled` or `deleted`. Only `active` is admitted at the "
            "three admission doors. A `deleted` user's row is RETAINED so the audit "
            "log keeps resolving them."
        )
    )
    roles: list[RoleAssignmentOut] = Field(description="Every role this user holds.")


class UserListOut(BaseModel):
    users: list[UserOut] = Field(
        description="Everyone in the calling user's organization, deleted users included."
    )


class CreatedUserOut(UserOut):
    """A newly created user, plus what became of the enrolment email.

    The mail outcome is in the RESPONSE rather than an error because the account
    exists either way, and because the shipped realm has no `smtpServer` (spec
    §2.3): reporting success while silently sending nothing produces an account
    nobody can ever sign into. `POST /users/{id}/resend-enrolment` is the fix.
    """

    enrolment_email_sent: bool = Field(
        description="Whether the identity provider accepted the enrolment email for delivery."
    )
    enrolment_email_error: str | None = Field(
        description=(
            "Why it did not, when it did not. The identity provider's status only -- "
            "response bodies are never forwarded."
        )
    )


class CodeGrantOut(BaseModel):
    code: str = Field(description="A permission code the user effectively holds.")
    via_role: str = Field(description="The role that granted it.")
    device_group_id: UUID | None = Field(
        description="`null` when the grant is org-wide, else the group it is scoped to."
    )


class EffectivePermissionsOut(BaseModel):
    """What this user can actually do, and why -- spec §1.5.

    A code granted by two roles appears twice, once per grant, because "you have
    this because of X" is the question this view exists to answer.
    """

    user_id: UUID
    codes: list[CodeGrantOut]


class MailResultOut(BaseModel):
    """A mail this platform asked Keycloak to send.

    ONE FIELD, AND THE OMISSION IS DELIBERATE. An earlier draft echoed the
    Keycloak required-action names back (`["UPDATE_PASSWORD"]`), which is
    informative and harmless -- and which put the literal string "password" in
    an API response, so the rule-1 sweep in `tests/test_user_admin_api.py` could
    only stay green by carving out an exemption for it. An exemption is exactly
    how the next thing slips through. The route path already says which mail was
    sent, so the field bought nothing and cost the strongest test on the surface.
    """

    sent: bool = Field(description="Always true; a refusal is a 502, not a false here.")


# --------------------------------------------------------------------------- #
# request models -- all `extra="forbid"`
# --------------------------------------------------------------------------- #


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=MAX_EMAIL)
    display_name: str = Field(default="", max_length=MAX_DISPLAY_NAME)
    role_ids: list[UUID] = Field(default_factory=list, max_length=MAX_ROLES)


class RoleAssign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_id: UUID
    device_group_id: UUID | None = Field(
        default=None,
        description="Omit for an org-wide grant; supply a group to scope the grant to it.",
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
        roles=[
            RoleAssignmentOut(
                assignment_id=r.assignment_id,
                role_id=r.role_id,
                role_name=r.role_name,
                device_group_id=r.device_group_id,
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
    """Everyone in the calling user's organization, with their role assignments.

    Needs no Keycloak: this is the ITMS projection, which is what every other
    surface in the platform joins against.
    """
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        views = await service.list_users(session, org_id=ctx.org_id)
    return UserListOut(users=[_user_out(v) for v in views])


@router.get("/users/{user_id}/effective-permissions", response_model=EffectivePermissionsOut)
async def effective_permissions(
    user_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("users.read")),
) -> EffectivePermissionsOut:
    """The union of this user's roles' codes, annotated with which role granted
    each and whether the grant is org-wide or scoped to a device group."""
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
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
                code=g.code, via_role=g.via_role, device_group_id=g.device_group_id
            )
            for g in grants
        ],
    )


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #


@router.post("/users", response_model=CreatedUserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    request: Request,
    # ORDER MATTERS, AND IT IS THE SECURITY HALF OF THIS SIGNATURE. FastAPI
    # resolves dependencies in declaration order, and `keycloak_config` reads
    # the provisioning client secret off disk -- so the RBAC gate goes first,
    # and an unauthenticated or unauthorized request is refused before anything
    # opens the credential. See the note on `keycloak_config`.
    ctx: AuthContext = Depends(require_permission("users.manage")),
    config: KeycloakAdminConfig | None = Depends(keycloak_config),
) -> CreatedUserOut:
    """Create the Keycloak identity, then the ITMS row holding its roles.

    The email domain is checked against the org's own console email-domain
    setting, resolved by the same function the three admission doors use, so an
    address this console will not admit is never given an identity. Resolved
    inside the tenant session for the same reason the doors resolve it there:
    the value belongs to the tenant, and the console must be able to change it
    without a restart.
    """
    provisioning = _provisioning(config)
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            created = await service.create_user(
                session,
                provisioning,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                email=body.email,
                display_name=body.display_name,
                role_ids=body.role_ids,
                email_domain=await resolve_console_domain(
                    session, ctx.org_id, request.app.state.settings
                ),
            )
        except UserAdminError as exc:
            raise _http(exc) from exc
        out = _user_out(created.user)
        return CreatedUserOut(
            **out.model_dump(),
            enrolment_email_sent=created.enrolment_email_sent,
            enrolment_email_error=created.enrolment_email_error,
        )


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
    ctx: AuthContext = Depends(require_permission("users.manage")),
) -> RoleAssignmentOut:
    """Grant a role, org-wide or scoped to one device group.

    No Keycloak involvement -- roles are an ITMS concept -- so this works on a
    deployment that has not configured provisioning.
    """
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            view = await service.assign_role(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                user_id=user_id,
                role_id=body.role_id,
                device_group_id=body.device_group_id,
            )
        except UserAdminError as exc:
            raise _http(exc) from exc
        return RoleAssignmentOut(
            assignment_id=view.assignment_id,
            role_id=view.role_id,
            role_name=view.role_name,
            device_group_id=view.device_group_id,
        )


@router.delete(
    "/users/{user_id}/roles/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_role(
    user_id: UUID,
    assignment_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("users.manage")),
) -> Response:
    """Revoke one assignment.

    Refused when it would leave the organization with no active `org-admin`,
    and refused outright for `super-admin`: no console surface can grant that
    role back, so revoking it is a one-way door.
    """
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
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
    config: KeycloakAdminConfig | None,
    user_id: UUID,
    enabled: bool,
) -> UserOut:
    provisioning = _provisioning(config)
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            view = await service.set_enabled(
                session,
                provisioning,
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
    # RBAC BEFORE THE SECRET READ -- see `create_user`.
    ctx: AuthContext = Depends(require_permission("users.manage")),
    config: KeycloakAdminConfig | None = Depends(keycloak_config),
) -> UserOut:
    """Stop this account signing in, in Keycloak AND in ITMS.

    Both, because neither alone is enough: Keycloak stops the next token being
    issued, `users.status` refuses the one already in the browser.

    Refused for your OWN account, for the organization's last active
    `org-admin`, and for any holder of `super-admin` -- all three are
    denial-of-service against a recovery path only SQL on production can undo.
    """
    return await _set_enabled(request, ctx, config, user_id, enabled=False)


@router.post("/users/{user_id}/enable", response_model=UserOut)
async def enable_user(
    user_id: UUID,
    request: Request,
    # RBAC BEFORE THE SECRET READ -- see `create_user`.
    ctx: AuthContext = Depends(require_permission("users.manage")),
    config: KeycloakAdminConfig | None = Depends(keycloak_config),
) -> UserOut:
    """Undo a disable, in both systems. A deleted user is not re-enabled here --
    their identity no longer exists, so there is nothing to enable."""
    return await _set_enabled(request, ctx, config, user_id, enabled=True)


# --------------------------------------------------------------------------- #
# the two mails
# --------------------------------------------------------------------------- #


@router.post("/users/{user_id}/reset-password", response_model=MailResultOut)
async def reset_password(
    user_id: UUID,
    request: Request,
    # RBAC BEFORE THE SECRET READ -- see `create_user`.
    ctx: AuthContext = Depends(require_permission("users.manage")),
    config: KeycloakAdminConfig | None = Depends(keycloak_config),
) -> MailResultOut:
    """Ask Keycloak to email this user an `UPDATE_PASSWORD` link.

    THAT IS THE ENTIRE IMPLEMENTATION. ITMS does not generate, store, display or
    return a password, and this response carries no credential -- only which
    action was requested. A refusal is a 502 rather than `sent: false`, because
    the mail IS the action and there is no partial success to report.
    """
    provisioning = _provisioning(config)
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            await service.send_password_reset(
                session, provisioning, org_id=ctx.org_id, actor_id=ctx.user_id, user_id=user_id
            )
        except UserAdminError as exc:
            raise _http(exc) from exc
    return MailResultOut(sent=True)


@router.post("/users/{user_id}/resend-enrolment", response_model=MailResultOut)
async def resend_enrolment(
    user_id: UUID,
    request: Request,
    # RBAC BEFORE THE SECRET READ -- see `create_user`.
    ctx: AuthContext = Depends(require_permission("users.manage")),
    config: KeycloakAdminConfig | None = Depends(keycloak_config),
) -> MailResultOut:
    """Send the first-contact mail again.

    Without this route one transient mail failure -- or the missing `smtpServer`
    the shipped realm has today -- leaves an account nobody can ever sign into,
    and Keycloak 26 exposes no admin API returning the link for an operator to
    pass on by hand.
    """
    provisioning = _provisioning(config)
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            await service.resend_enrolment(
                session, provisioning, org_id=ctx.org_id, actor_id=ctx.user_id, user_id=user_id
            )
        except UserAdminError as exc:
            raise _http(exc) from exc
    return MailResultOut(sent=True)


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: UUID,
    request: Request,
    # RBAC BEFORE THE SECRET READ -- see `create_user`.
    ctx: AuthContext = Depends(require_permission("users.manage")),
    config: KeycloakAdminConfig | None = Depends(keycloak_config),
) -> Response:
    """Remove the Keycloak identity. The ITMS row is RETAINED, marked `deleted`.

    Retained because 89 foreign-key columns across 21 tables reference
    `users.id`, audit `actor_id` among them: a hard delete either fails on the
    constraints or leaves an audit log that can say what happened but not who
    did it.

    Refused for your own account, for the last active `org-admin`, and for any
    holder of `super-admin`.
    """
    provisioning = _provisioning(config)
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            await service.delete_user(
                session, provisioning, org_id=ctx.org_id, actor_id=ctx.user_id, user_id=user_id
            )
        except UserAdminError as exc:
            raise _http(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
