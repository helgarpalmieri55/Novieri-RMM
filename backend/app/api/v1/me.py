"""The caller's own identity, permissions, and account profile.

`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §7.

UNGATED BEYOND AUTHENTICATION, BOTH VERBS, AND THAT IS THE DESIGN. `GET /me`
returns the caller's own identity and permission list; a permission code on it
would be a code every role must hold. The PATCH beside it is the same argument
one step further: it writes four presentation fields on the CALLER'S OWN row,
so requiring a code would mean an operator could be locked out of setting their
own timezone. `tests/test_route_gating.py::UNGATED_BY_DESIGN` records both, and
its sibling test refuses any entry without a stated reason.

That is only safe because of the rule below, which is the whole of this
module's security argument:

    THE TARGET IS `ctx.user_id`, FROM THE VALIDATED TOKEN, NEVER THE BODY.

`app/api/v1/orgs.py::update_current_org` states the same rule for the org
PATCH. Here it is load-bearing in a stronger way: `orgs.manage` gates that
route, so its caller is already trusted with the tenant; this one is reachable
by every authenticated principal in the fleet. A `user_id` honoured from the
body would turn the least privileged surface in the application into a write
primitive against any row in the org.

WHAT THE PROFILE MAY WRITE, AND WHY THE LIST IS CLOSED. Four fields:
`display_name`, `phone`, `timezone`, `locale`. `ProfileUpdate` forbids
everything else (`extra="forbid"`) rather than ignoring it, because "ignored"
and "written" are one careless model edit apart, and the fields not on the list
are `email` (Keycloak's, per §7.1 -- changing it there is a re-authenticated
flow that ITMS deliberately does not reimplement), `status` (the §3 admission
gate: a self-service form that re-enables a disabled account is the disable
feature deleted), `org_id` and `external_subject` (identity, not preference),
and anything role-shaped (privilege escalation, stated plainly). A request
carrying any of them is refused with 422 and writes nothing at all -- not even
the legitimate fields alongside it.

`locale` STAYS IN THE PLATFORM. Until P2 phase 1 it was mirrored to the
caller's Keycloak user so that Keycloak's pages and mails followed it; the
sign-in pages and the account mails are the platform's own now and read
`users.locale` directly (`app/core/auth/notices.py`), so nothing is mirrored
(P2 design §1.3).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthContext, bound_auth_context
from app.core.client_scope import ClientScope
from app.core.i18n import tr
from app.core.identity.own_profile import (
    OwnProfile,
    OwnRowMissing,
    read_own_profile,
    update_own_profile,
)

router = APIRouter(tags=["me"])


class MeClient(BaseModel):
    """A client the caller holds a role in (MSP clients phase 3 design §1.3)."""

    id: UUID
    name: str
    kind: str = Field(description="customer, internal (Novieri's own IT) or unassigned")
    enabled_modules: list[str] = Field(description="Optional modules on: budget, bcp, change")
    permissions: list[str] = Field(description="The codes the caller holds in this client")


class MeResponse(BaseModel):
    user_id: UUID
    org_id: UUID
    email: str
    display_name: str
    phone: str | None
    timezone: str | None
    locale: str | None
    #: The codes the caller holds in AT LEAST ONE client of this request's
    #: scope: `clients`, narrowed by `X-Client-Id` when it is sent. That is
    #: `require_permission`'s rule, so it is what the console may offer for
    #: the client chosen in its selector.
    permissions: list[str]
    #: The codes the caller holds on EVERY client (an all-clients grant):
    #: `require_permission_on_every_client`'s rule, for global configuration.
    #: The header does not narrow it; the console never sends one there.
    permissions_everywhere: list[str]
    #: Every client the caller holds a role in, archived ones left out, by
    #: name with `unassigned` last, and what they hold in each. Not narrowed
    #: by `X-Client-Id`: it is the selector's list. Needs no `clients.read`:
    #: these are the caller's own grants.
    clients: list[MeClient]
    #: The names of the roles the caller holds. The console renders these in
    #: the account chip, which used to show the constant "Operator" for every
    #: user on every deployment.
    roles: list[str]


class ProfileUpdate(BaseModel):
    """The closed set of self-writable fields. See this module's docstring.

    Every field is optional and PATCH semantics are by `model_fields_set`, so
    an omitted field is untouched while an explicit `null` clears it -- except
    `display_name`, which the column declares NOT NULL, so null there is a 400
    rather than a silent write of ''.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    timezone: str | None = Field(default=None, max_length=64)
    #: A language tag: letters and hyphens, e.g. `en`, `es`, `en-GB`. The
    #: console writes only `en` or `es`; the pattern keeps older stored values
    #: valid while refusing markup and one-letter tags (spec §4).
    locale: str | None = Field(
        default=None, max_length=16, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$"
    )


def _reject(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail)


def _response(profile: OwnProfile, scope: ClientScope) -> MeResponse:
    """`scope` is the request's (`ctx.clients`): the person's own, narrowed
    by `X-Client-Id`. It narrows `permissions` and nothing else."""
    return MeResponse(
        user_id=profile.user_id,
        org_id=profile.org_id,
        email=profile.email,
        display_name=profile.display_name,
        phone=profile.phone,
        timezone=profile.timezone,
        locale=profile.locale,
        permissions=sorted(profile.codes_in(scope)),
        permissions_everywhere=sorted(profile.codes_everywhere),
        clients=[
            MeClient(
                id=client.id,
                name=client.name,
                kind=client.kind,
                enabled_modules=list(client.enabled_modules),
                permissions=sorted(profile.codes_by_client[client.id]),
            )
            for client in profile.clients
        ],
        roles=profile.roles,
    )


def _no_such_user() -> HTTPException:  # pragma: no cover -- admission created or found this row
    return HTTPException(status.HTTP_404_NOT_FOUND, tr("no such user"))


@router.get("/me", response_model=MeResponse)
async def me(request: Request, ctx: AuthContext = Depends(bound_auth_context)) -> MeResponse:
    """Read from the ROW, not from the token claims.

    `display_name` used to be `ctx.claims.name`, which made the §7.1 override
    unobservable: the profile form could write it and this route would keep
    reporting Keycloak's value. `email` reads from the row too, and that is
    only correct because admission now refreshes it from the claim
    (`app/api/deps.py`, §7.2) -- before that fix the row was the stale source
    and this would have been the bug rather than the fix.

    Under EVERY client, not `ctx.clients`: the profile is the person's, and a
    person with no grant has the empty scope, which sees no staff row
    (`app.core.identity.own_profile`). Its client map is read under the
    person's own scope there; `ctx.clients` narrows only `permissions`.
    """
    try:
        profile = await read_own_profile(
            org_id=ctx.org_id, user_id=ctx.user_id, engine=request.app.state.app_engine
        )
    except OwnRowMissing:  # pragma: no cover -- admission created or found this row
        raise _no_such_user() from None
    return _response(profile, ctx.clients)


@router.patch("/me", response_model=MeResponse)
async def update_me(
    body: ProfileUpdate,
    request: Request,
    ctx: AuthContext = Depends(bound_auth_context),
) -> MeResponse:
    """Update the caller's own profile. The target is `ctx.user_id`, always."""
    supplied = body.model_fields_set
    if not supplied:
        raise _reject(tr("no fields supplied; send display_name, phone, timezone and/or locale"))
    if "display_name" in supplied and body.display_name is None:
        raise _reject(tr("display_name cannot be null; send \"\" to clear it"))

    # The caller's own row, under EVERY client (`app.core.identity.
    # own_profile`, where the four written-out assignments and the audit
    # event live): `ctx.user_id`, never the body.
    try:
        profile, _previous_locale = await update_own_profile(
            org_id=ctx.org_id,
            user_id=ctx.user_id,
            engine=request.app.state.app_engine,
            supplied=supplied,
            display_name=body.display_name,
            phone=body.phone,
            timezone=body.timezone,
            locale=body.locale,
        )
    except OwnRowMissing:  # pragma: no cover -- admission created or found this row
        raise _no_such_user() from None
    return _response(profile, ctx.clients)
