"""First boot: the setup screen's two routes.

`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §3.

    GET  /api/v1/setup/status   UNGATED -- "does this installation have an owner?"
    POST /api/v1/setup/claim    UNGATED -- "it does now, and it is me"

WHY UNGATED, AND WHAT REPLACES THE GATE. These two answer before the platform
has a single user, so there is no token to validate, no organization to scope
to, and no permission to require. Nothing about that is a relaxation of
invariant 6: a gate needs an identity, and the entire purpose of this surface is
the moment before one exists. What stands in its place is two facts that must
BOTH hold, and each is worthless alone:

  1. **The platform has zero users.** Checked through the BYPASSRLS admin
     engine, and that detail is the whole check. Through a tenant session with
     no organization pinned, RLS answers "no users" for every platform on
     earth, so the same query one engine over reports a fully populated
     production system as unclaimed.
  2. **The caller knows the install code** the installer printed
     (`app/core/setup_code.py`). Without it, whoever reaches the URL of a newly
     racked appliance first becomes the administrator of the fleet -- and a
     machine answers on the network long before its owner sits down at it.

The refusals are ordered accordingly: the code is checked FIRST, so an
unauthenticated caller who does not have it never reaches the database and never
learns even the password rule.

WHAT THIS ROUTE DOES NOT DO: create an administrator by hand.
`user_admin.service.create_user` makes the row, here exactly as for an
invitation, so the domain rule, the role rails and the `user.created` record
come with it; and the password goes through `app.core.auth.staff_password`,
the same rule and the same store a reset uses. The first sign-in then enrols
the second factor (P2 phase 1 design §7: "el primer factor se registra al
entrar"). A second implementation here would be a second set of rails to keep
in step -- see spec falsifier 6, and the test that pins it.

THE ORGANIZATION AND THE FIRST USER ARE ONE TRANSACTION. An organization row
with no members still counts as claimed by check 1 above, so a claim that
half-succeeded would leave an appliance nobody can administer and nothing in the
product can repair. Every failure below therefore either happens before the
first write or rolls it back.
"""

import logging
import re
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.auth import staff_password
from app.core.config import Settings
from app.core.i18n import tr
from app.core.setup_code import ensure_setup_code, setup_code_path, verify_setup_code
from app.core.tenancy import admin_session
from app.models import Organization, Role, User
from app.modules.health.baseline import ensure_baseline_contained
from app.modules.user_admin import service as user_admin
from app.modules.user_admin.service import ORG_ADMIN, UserAdminError

router = APIRouter(tags=["setup"])

logger = logging.getLogger(__name__)

#: The audit actor for a claim, and it is deliberately not a user id.
#:
#: `user_admin.service.create_user` records who asked for an account. On this
#: path nobody did: the request IS the one that creates the platform's first
#: identity, and there is no signed-in operator to attribute it to. The nil UUID
#: says exactly that -- "no user did this" -- rather than attributing the fleet's
#: first administrator to an arbitrary account, and it can never collide with a
#: real row (`users.id` is uuid7, which is never all-zero). The claim's own
#: `setup.claimed` event beside it carries `actor_type="system"`, which is the
#: honest description of what happened.
NO_ACTOR_YET = UUID(int=0)

MAX_NAME = 200
MAX_EMAIL = 320
#: Long enough for any install code this platform generates plus whatever
#: whitespace a paste drags along; short enough that the comparison is never
#: handed a megabyte by an anonymous caller.
MAX_CODE = 200


class SetupStatusOut(BaseModel):
    """Deliberately one field.

    Every additional fact here is a fact an anonymous stranger learns about an
    installation they do not own. Whether the platform has an owner is the
    minimum the sign-in page cannot function without; the install code, the
    organization name, and the password rule are all things they would only
    need if they were about to claim it.
    """

    needed: bool = Field(
        description=(
            "True while this installation has no users at all -- the setup screen "
            "should be shown instead of the sign-in page. Never reveals the install "
            "code, which is the other half of what a claim requires."
        )
    )


class SetupClaim(BaseModel):
    """The setup form. `extra="forbid"`, so a caller who invents a field --
    `role_ids`, `organization_id`, `status` -- is refused rather than quietly
    ignored on the one route with no authorization behind it."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        max_length=MAX_CODE,
        description="The install code printed by the installer at first start.",
    )
    email: str = Field(
        min_length=3, max_length=MAX_EMAIL, description="The first administrator's address."
    )
    display_name: str = Field(
        min_length=1, max_length=MAX_NAME, description="How the console will show them."
    )
    organization_name: str = Field(
        min_length=1, max_length=MAX_NAME, description="The organization being set up."
    )
    #: No length bound here (P2 Task 7's decision 16): a field failure would
    #: repeat the password in its 422. The rule of design §2.1 bounds it in
    #: `staff_password.require_acceptable` (`too_short`, `too_long`).
    password: str = Field(
        description="The first administrator's password: 12 characters at least (design §2.1).",
    )


class ClaimedOut(BaseModel):
    """What was created. Carries no credential and no install code."""

    organization_id: UUID = Field(description="The organization that now owns this installation.")
    organization_slug: str = Field(
        description="Its slug -- the organization the sign-in page signs into."
    )
    user_id: UUID = Field(description="The first administrator's ITMS user id.")
    email: str = Field(description="The address the first administrator signs in with.")


# --------------------------------------------------------------------------- #
# the two facts a claim rests on
# --------------------------------------------------------------------------- #


async def _no_users_yet(session: AsyncSession) -> bool:
    """Has this installation ever had a user?

    ANY ROW COUNTS, whatever its status. `users` rows are retained on delete
    (`user_admin/service.py` rule 2), and counting only active ones would mean
    disabling or deleting the last account -- ordinary administrative acts --
    reopens an unauthenticated route that mints an org-admin.

    THE SESSION MUST BE AN ADMIN (BYPASSRLS) ONE. Under a tenant session with
    no organization pinned, RLS matches nothing and this returns True for a
    platform with ten thousand users in it.
    """
    return (await session.execute(select(User.id).limit(1))).first() is None


def _refuse_without_the_code(presented: str) -> None:
    if not verify_setup_code(setup_code_path(), presented):
        # 403 rather than 401: there is no credential to re-present and no
        # sign-in loop to send the caller round. The message says nothing about
        # whether a code exists, how long it is, or where it lives.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            tr(
                "that install code is not correct. It is printed in the server log at first "
                "start and stored beside this installation's other secrets.",
            ),
        )


def _org_slug(settings: Settings, organization_name: str) -> str:
    """The slug the first organization gets, and it is not cosmetic.

    `app/core/security.py` resolves a token's organization claim to a slug, and
    falls back to `default_org_slug` when the claim is absent -- which is every
    token on a single-org installation. An organization created under any other
    slug is one its own administrator cannot sign in to: `auth_context` answers
    "unknown organization" and the setup screen's promise breaks at the first
    redirect. So when this deployment declares a slug, that IS the slug.
    """
    declared = settings.default_org_slug
    if declared:
        return str(declared)
    slug = re.sub(r"[^a-z0-9]+", "-", organization_name.lower()).strip("-")[:80]
    return slug or "organization"


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #


@router.get("/setup/status", response_model=SetupStatusOut)
async def setup_status(request: Request) -> SetupStatusOut:
    """Should the browser show the setup screen instead of the sign-in page?

    UNGATED, and recorded as such in `tests/test_route_gating.py`. It reveals
    exactly one bit: whether this installation has any users. That bit is
    already observable -- an unclaimed platform has no sign-in that succeeds --
    and the frontend cannot ask anything before it.

    IT ALSO GENERATES THE INSTALL CODE, once, on an installation that needs one.
    That is the only trigger there is: nothing else runs before the first
    request. A failure to write it does NOT fail this route -- an operator whose
    secrets volume did not mount still needs to see the screen that tells them
    something is wrong -- but it does mean the claim will refuse, because there
    is no code for anybody to present.
    """
    engine: AsyncEngine = request.app.state.admin_engine
    async with admin_session(engine=engine) as session:
        needed = await _no_users_yet(session)

    if needed:
        try:
            ensure_setup_code(setup_code_path())
        except OSError as exc:
            logger.warning("could not write the install code", exc_info=exc)

    return SetupStatusOut(needed=needed)


@router.post("/setup/claim", response_model=ClaimedOut, status_code=status.HTTP_201_CREATED)
async def claim_platform(body: SetupClaim, request: Request) -> ClaimedOut:
    """Create this installation's organization and its first administrator.

    THE ORDER OF THE REFUSALS IS PART OF THE DESIGN:

      1. the install code, before anything else -- a caller without it never
         reaches the database and never learns even the password rule;
      2. the password rule (design §2.1), before the first write;
      3. zero users, so a leaked code cannot re-claim a running platform.

    Then one transaction: the organization, the administrator (through the same
    service an invitation uses), their password, and the audit records.
    """
    settings: Settings = request.app.state.settings
    engine: AsyncEngine = request.app.state.admin_engine

    _refuse_without_the_code(body.code)
    await staff_password.require_acceptable(
        body.password,
        email=body.email.strip().lower(),
        display_name=body.display_name,
        # The organization this claim is about to create (Task 2's decision 5).
        org_name=body.organization_name.strip(),
    )

    async with admin_session(engine=engine) as session:
        # SERIALIZE CLAIMS. Two requests arriving together would both read zero
        # users and both create an organization; a double-clicked button is
        # exactly that. The lock is held to the end of this transaction, so the
        # second claim reads the first one's committed user and is refused.
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('setup:claim'))"))

        if not await _no_users_yet(session):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                tr(
                    "this installation has already been set up. Sign in, or ask one of its "
                    "administrators to add you.",
                ),
            )

        role_id = (
            await session.execute(
                select(Role.id).where(Role.name == ORG_ADMIN, Role.org_id.is_(None))
            )
        ).scalar_one_or_none()
        if role_id is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                tr("this installation is missing its built-in roles and cannot be set up."),
            )

        slug = _org_slug(settings, body.organization_name)
        taken = (
            await session.execute(select(Organization.id).where(Organization.slug == slug))
        ).scalar_one_or_none()
        if taken is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                tr(
                    "an organization with the identifier {slug} already exists on this "
                    "installation.",
                    slug=repr(slug),
                ),
            )

        org = Organization(name=body.organization_name.strip(), slug=slug)
        session.add(org)
        await session.flush()

        # Monitoring from day one. In a SAVEPOINT: a seed that fails must not
        # fail the claim, and the worker's hourly reconcile retries it.
        await ensure_baseline_contained(session, org.id)

        try:
            # THE SAME FUNCTION AN INVITATION CALLS. Everything that makes
            # creating an account safe lives in it, and none of it is repeated
            # here: the email-domain rule, the role rails, the `user.created`
            # audit entry.
            created = await user_admin.create_user(
                session,
                org_id=org.id,
                actor_id=NO_ACTOR_YET,
                email=body.email,
                display_name=body.display_name,
                grants=[user_admin.Grant(role_id=role_id)],
                email_domain=settings.console_email_domain,
            )
        except UserAdminError as exc:
            # Raising here rolls the organization row back with it: the whole
            # point of doing both in one transaction. Every refusal left is
            # about the request (an address, a role), so it is said as it is.
            raise HTTPException(exc.status_code, exc.detail) from exc

        await staff_password.store(
            session, org_id=org.id, user_id=created.id, plain=body.password
        )
        await audit_service.record(
            session,
            org_id=org.id,
            actor_type="user",
            actor_id=created.id,
            action="auth.password_changed",
            target_type="user",
            target_id=str(created.id),
            metadata={"via": "setup"},
        )
        await audit_service.record(
            session,
            org_id=org.id,
            actor_type="system",
            actor_id=None,
            action="setup.claimed",
            target_type="organization",
            target_id=str(org.id),
            metadata={
                # NEVER THE INSTALL CODE. It is still valid for as long as this
                # transaction might roll back, and the audit log is readable by
                # every auditor in the organization afterwards.
                "organization_name": org.name,
                "organization_slug": org.slug,
                "administrator_id": str(created.id),
                "email": created.email,
            },
        )

        return ClaimedOut(
            organization_id=org.id,
            organization_slug=org.slug,
            user_id=created.id,
            email=created.email,
        )
