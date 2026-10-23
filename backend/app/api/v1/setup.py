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
causes the provisioning secret to be read off disk.

WHAT THIS ROUTE DOES NOT DO: create an administrator. `user_admin.service.
create_user` does that, here exactly as on the Users tab, and the ordering it
enforces (identity first, ITMS row second), the enrolment mail, the domain rule
and the `user.created` audit record all come with it. A second implementation
here would be a second set of rails to keep in step, and the one it would skip
is the one that matters most -- see spec falsifier 6, and the test that pins it.

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
from app.core.config import Settings
from app.core.i18n import tr
from app.core.setup_code import ensure_setup_code, setup_code_path, verify_setup_code
from app.core.tenancy import admin_session
from app.models import Organization, Role, User
from app.modules.health.baseline import ensure_baseline_contained
from app.modules.user_admin import service as user_admin
from app.modules.user_admin.kc_client import KeycloakAdminConfig
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
    organization name, and whether provisioning is configured are all things
    they would only need if they were about to claim it.
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


class ClaimedOut(BaseModel):
    """What was created. Carries no credential and no install code."""

    organization_id: UUID = Field(description="The organization that now owns this installation.")
    organization_slug: str = Field(
        description="Its slug -- the value a sign-in token's organization claim resolves to."
    )
    user_id: UUID = Field(description="The first administrator's ITMS user id.")
    email: str = Field(description="The address the invitation was addressed to.")
    invitation_sent: bool = Field(
        description=(
            "Whether the identity provider accepted the invitation email for delivery. "
            "A fresh installation usually has no email configured yet, in which case "
            "this is false and the administrator cannot sign in until it is."
        )
    )
    invitation_error: str | None = Field(
        description="What went wrong when it did not. Never a credential, never a vendor name."
    )


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


#: "The stack is younger than you are." `keycloak-init` writes the provisioning
#: secret on every `docker compose up`, and NOTHING gates the api on it finishing
#: -- deliberately, because making `api` depend on that job turns an init failure
#: into a product that will not start at all. So on a cold host there is a window
#: where the deployment is configured correctly and the file is simply not there
#: yet, and the only useful instruction in that window is "wait".
#:
#: The other state, and the reason it gets its own sentence: no amount of waiting
#: fixes it. Somebody has to set the deployment up. (Both sentences are written
#: out where they are raised, so that each goes through `tr()`.)


def _provisioning(settings: Settings) -> KeycloakAdminConfig:
    """The provisioning credential, or a 503 an installer can act on.

    TWO ABSENCES, TWO DIFFERENT NEXT ACTIONS, and collapsing them into one
    message is what makes a 503 useless. `KeycloakAdminConfig.from_settings` has
    a deliberate three-way contract and it is not weakened here -- it is READ
    here, which is the whole difference:

      returns None -> nothing is configured. Waiting will never change that.
      RAISES       -> configured, and the secret file is missing or empty. On
                      the lifecycle routes that means an operator who believes
                      provisioning is running, which is why it raises rather
                      than degrading. On THIS route, which by construction only
                      ever runs on an installation minutes old, it overwhelmingly
                      means the init job has not written the file yet.

    Either way it is a 503 and not a 500 -- a stack trace on the first screen of
    a new product is indistinguishable from a broken one -- and either way it is
    raised BEFORE the first write, so the retry the message asks for actually
    works instead of hitting "this platform is already claimed".
    """
    try:
        config = KeycloakAdminConfig.from_settings(settings)
    except (OSError, ValueError) as exc:
        # `exc_info=`, never a %-arg: `str(exc)` on a config error can carry the
        # path it failed to read. `tests/test_logging_discipline.py` enforces this.
        logger.warning(
            "setup claim refused: the provisioning credential could not be read",
            exc_info=exc,
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            tr(
                "this installation is still starting up, so it cannot create accounts yet. "
                "Wait a moment and try again. If it says this for more than a few minutes, "
                "the installation did not finish preparing itself and needs attention from "
                "whoever set it up."
            ),
        ) from exc
    if config is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            tr(
                "this installation cannot create accounts yet: its identity provisioning is "
                "not configured. That is a server setting, not something this screen can fix."
            ),
        )
    return config


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


def _refusal_detail(exc: UserAdminError) -> str:
    """Translate the service's refusal for a reader who has never signed in.

    The 4xx refusals are about the request -- a malformed address, an address
    already in use -- and say so in words a setup screen can show. The 5xx ones
    are about the deployment's own plumbing ("the identity provider was
    unreachable while trying to create the account"), which is true, is worth
    logging in full, and is not a sentence to hand somebody on the first screen
    of a product they have just installed. So they are logged and replaced.

    CORRECTED 2026-08-19: this docstring used to say the 5xx details "name the
    identity vendor, which no user-visible string in this product may do". The
    rule is right and unchanged; the fact was fixed at its source instead --
    `user_admin.service._translating` no longer puts the word in a detail at
    all, and `tests/test_no_vendor_names_backend.py` now guards every one of
    them. This function is no longer the only thing standing between that word
    and the setup screen, which is why the reason it still exists is restated
    above rather than left as a stale claim.
    """
    if exc.status_code < 500:
        return exc.detail
    # The English cause, never `exc.detail`: that is worded in the request's
    # language, and a log is read by whoever runs the deployment.
    logger.warning(
        "setup claim failed (status %s)", exc.status_code, exc_info=exc.__cause__
    )
    return tr(
        "the identity provider could not create the first administrator. "
        "Nothing was created; the server log has the detail."
    )


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
         reaches the database and never causes the provisioning secret to be
         read off disk;
      2. zero users, so a leaked code cannot re-claim a running platform;
      3. the provisioning credential, before the first write, so a deployment
         whose init job has not finished can simply try again.

    Then one transaction: the organization, the administrator (through the same
    service the Users tab calls), and both audit records.
    """
    settings: Settings = request.app.state.settings
    engine: AsyncEngine = request.app.state.admin_engine

    _refuse_without_the_code(body.code)

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

        provisioning = _provisioning(settings)

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
            # THE SAME FUNCTION THE USERS TAB CALLS. Everything that makes
            # creating an account safe lives in it, and none of it is repeated
            # here: identity first then the ITMS row, the email-domain rule, the
            # enrolment mail, the `user.created` audit entry.
            created = await user_admin.create_user(
                session,
                provisioning,
                org_id=org.id,
                actor_id=NO_ACTOR_YET,
                email=body.email,
                display_name=body.display_name,
                role_ids=[role_id],
                email_domain=settings.console_email_domain,
            )
        except UserAdminError as exc:
            # Raising here rolls the organization row back with it: the whole
            # point of doing both in one transaction.
            raise HTTPException(exc.status_code, _refusal_detail(exc)) from exc

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
                "administrator_id": str(created.user.id),
                "email": created.user.email,
                "invitation_sent": created.enrolment_email_sent,
            },
        )

        return ClaimedOut(
            organization_id=org.id,
            organization_slug=org.slug,
            user_id=created.user.id,
            email=created.user.email,
            invitation_sent=created.enrolment_email_sent,
            invitation_error=_invitation_error(created),
        )


def _invitation_error(created: user_admin.CreatedUser) -> str | None:
    """Say that the invitation did not arrive, in the words this screen needs.

    The service reports what failed at the identity provider ("the identity
    provider could not send the enrolment email"), which is accurate and
    logged. On the first screen of a product somebody has just installed it is
    also not actionable: on a fresh installation the cause is almost always
    that email has not been configured yet, and that is the sentence that has
    to be here instead.

    CORRECTED 2026-08-19: this used to say the service's wording "says
    'Keycloak' in it". It did; it no longer does
    (`user_admin.service._translating`), so the reason this function exists is
    restated above rather than resting on a fact that has since been fixed.
    """
    if created.enrolment_email_sent:
        return None
    logger.warning("setup claim: invitation not sent: %s", created.enrolment_email_error)
    return tr(
        "The invitation email could not be sent, so no sign-in link has arrived. "
        "Email delivery is most likely not set up on this installation yet."
    )
