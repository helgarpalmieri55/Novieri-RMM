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

`locale` ALSO REACHES THE SIGN-IN PROVIDER (spec §3.5): after the commit, the
saved value is mirrored to the caller's own Keycloak user so its pages and
emails follow it. The target is the `external_subject` of the row `ctx.user_id`
selected, never a request field, and a failure there never fails the PATCH.
"""

import asyncio
import logging
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, bound_auth_context
from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.rbac import user_permission_codes, user_role_names
from app.core.tenancy import org_session
from app.models import User
from app.modules.user_admin.kc_client import (
    KeycloakAdminConfig,
    KeycloakAdminError,
    set_locale_attribute,
)

logger = logging.getLogger(__name__)

#: The whole Keycloak mirror -- token, read, write and check -- gets this long.
#: The save is already committed, but the request is held open while we wait,
#: and a wedged identity provider must not hold it for four 15-second calls.
_MIRROR_TIMEOUT_SECONDS = 5

router = APIRouter(tags=["me"])


class MeResponse(BaseModel):
    user_id: UUID
    org_id: UUID
    email: str
    display_name: str
    phone: str | None
    timezone: str | None
    locale: str | None
    permissions: list[str]
    #: The names of the roles the caller holds. The console renders these in
    #: the account chip, which used to show the constant "Operator" for every
    #: user on every deployment.
    roles: list[str]
    #: Set only by `PATCH /me` when `locale` was supplied: `True` when the
    #: language was also written to the sign-in provider, `False` when that
    #: write failed (the console setting is saved either way), `None` when no
    #: write was attempted -- locale not supplied, or no provider configured.
    identity_locale_synced: bool | None = None


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


def _response(user: User, codes: set[str], roles: list[str]) -> MeResponse:
    return MeResponse(
        user_id=user.id,
        org_id=user.org_id,
        email=user.email,
        display_name=user.display_name,
        phone=user.phone,
        timezone=user.timezone,
        locale=user.locale,
        permissions=sorted(codes),
        roles=roles,
    )


async def _load(session: AsyncSession, user_id: UUID) -> User:
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:  # pragma: no cover -- admission created or found this row
        raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such user"))
    return user


@router.get("/me", response_model=MeResponse)
async def me(request: Request, ctx: AuthContext = Depends(bound_auth_context)) -> MeResponse:
    """Read from the ROW, not from the token claims.

    `display_name` used to be `ctx.claims.name`, which made the §7.1 override
    unobservable: the profile form could write it and this route would keep
    reporting Keycloak's value. `email` reads from the row too, and that is
    only correct because admission now refreshes it from the claim
    (`app/api/deps.py`, §7.2) -- before that fix the row was the stale source
    and this would have been the bug rather than the fix.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        user = await _load(session, ctx.user_id)
        codes = await user_permission_codes(session, ctx.user_id)
        return _response(user, codes, await user_role_names(session, ctx.user_id))


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

    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        user = await _load(session, ctx.user_id)
        previous_locale = user.locale
        # WRITTEN OUT, not `setattr(user, field, ...)` over `supplied`. The
        # reflective form is one model edit away from assigning to any User
        # attribute whose name a request field happens to share; these four
        # lines are the write surface, readable as such by whoever reviews
        # this next.
        if "display_name" in supplied and body.display_name is not None:
            user.display_name = body.display_name
        if "phone" in supplied:
            user.phone = body.phone
        if "timezone" in supplied:
            user.timezone = body.timezone
        if "locale" in supplied:
            user.locale = body.locale
        session.add(user)

        # KEYS ONLY, never values. A phone number is directory data about a
        # person and the audit log is read far more widely than the profile
        # form; recording which fields moved is what makes the event useful
        # without turning the chain into a contact list.
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="user.profile_updated",
            target_type="user",
            target_id=str(ctx.user_id),
            metadata={"fields": sorted(supplied)},
        )
        codes = await user_permission_codes(session, ctx.user_id)
        roles = await user_role_names(session, ctx.user_id)
        await session.flush()
        response = _response(user, codes, roles)
        subject = user.external_subject

    # AFTER THE COMMIT (the block above has exited), so a Keycloak that is down
    # or slow can never lose the console setting. The target is the caller's
    # own row's `external_subject`, read from the row `ctx.user_id` selected --
    # no request field reaches this call.
    #
    # ONLY WHEN IT CHANGED. The profile form re-sends the stored value on every
    # save, and a null stored value would otherwise delete a `locale` an
    # administrator set in Keycloak. Picking the language again (a different
    # one, then back) is the retry path after a `false`.
    if "locale" in supplied and body.locale != previous_locale:
        response.identity_locale_synced = await _mirror_locale(
            request, subject=subject, locale=body.locale
        )
        # OVERLAPPING SAVES. Two quick switches (or two tabs) each commit, then
        # mirror; the earlier request's write can land in Keycloak after the
        # later one's. So once ours is written, look again: if the saved value
        # has moved on, write that instead. Whichever request finishes last
        # reads the latest committed value, so Keycloak ends on it.
        if response.identity_locale_synced:
            async with org_session(
                ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
            ) as session:
                current = (await _load(session, ctx.user_id)).locale
            if current != body.locale:
                response.identity_locale_synced = await _mirror_locale(
                    request, subject=subject, locale=current
                )
    return response


async def _mirror_locale(request: Request, *, subject: str, locale: str | None) -> bool | None:
    """Write the saved language to the caller's Keycloak user.

    `None` when this deployment has no provisioning client (nothing attempted),
    `True` on success, `False` on any failure. Never raises: the setting is
    already committed, and the form is told rather than the request failed.
    The log line is a static template plus the exception's own traceback
    (`exc_info`); the token and the user representation are never passed to it.
    """
    try:
        # `TimeoutError` is an `OSError`, so the clause below already catches it.
        async with asyncio.timeout(_MIRROR_TIMEOUT_SECONDS):
            config = KeycloakAdminConfig.from_settings(request.app.state.settings)
            if config is None:
                return None
            await set_locale_attribute(config, user_id=subject, locale=locale)
    except (KeycloakAdminError, httpx.HTTPError, OSError, ValueError, KeyError):
        logger.warning(
            "could not mirror the saved language to the identity provider", exc_info=True
        )
        return False
    return True
