"""Enrollment: token lifecycle and agent onboarding (docs/kernel/spec.md §7).

Token format: "<token_id>.<secret>". Only sha256(secret) is stored; the
plaintext is shown exactly once at creation. Enrollment resolves the token
(admin engine — no tenant context exists yet), then creates the Device and
Agent inside the tenant session and has the CA bind the device identity into
the issued certificate.
"""

import hashlib
import math
import secrets as pysecrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.ca import CAService
from app.core.client_address import IPAddress
from app.core.client_scope import ClientScope
from app.core.events import publish as publish_event
from app.core.i18n import tr
from app.core.signing import SigningService
from app.core.tenancy import org_session
from app.models import Agent, Client, Device, EnrollmentToken, Site, SuggestedAssetMatch
from app.services import install_link as link_svc
from app.services.client_sites import CLIENT_ARCHIVED, site_for_new_rows
from app.services.matching import resolve_enrolment_target


class EnrollmentError(Exception):
    """Invalid, expired, revoked, or exhausted token — always a 403, with no
    detail leaked about which check failed."""


class LinkNotFound(Exception):
    """The install link is off, past its switch-off time, the code is wrong or
    malformed, or the request came from outside every listed network. Always a
    404 with nothing to tell the cases apart (spec §1.1)."""


class LinkRateLimited(Exception):
    """This organisation's hourly limit of link enrollments is used up: 429.

    `retry_after_s` is how long until the rolling window frees a slot, never
    less than a minute: the route sends it as `Retry-After`, and a 1.6.0 agent
    waits that long before trying again instead of giving up.
    """

    def __init__(self, retry_after_s: int) -> None:
        super().__init__(  # i18n-exempt: the route sends its own text and Retry-After
            f"retry after {retry_after_s} seconds"
        )
        self.retry_after_s = retry_after_s


#: The shortest `Retry-After` a link 429 ever sends.
MIN_LINK_RETRY_AFTER_S = 60


class DeviceFacts(BaseModel):
    hostname: str = Field(min_length=1, max_length=255)
    os_family: str = Field(pattern="^(windows|linux|macos)$")
    os_version: str = ""
    architecture: str = ""
    serial: str | None = None
    #: Physical MAC addresses, for matching this machine to an inventory record
    #: that already exists (`docs/modules/asset-inventory/spec.md` §4 rule 2,
    #: as amended 2026-08-18).
    #:
    #: ADDITIVE AND DEFAULTED, which is the whole compatibility story: a fleet
    #: is always mixed-version, an agent in the field is older than the control
    #: plane it talks to, and a field a new server REQUIRED would be an outage
    #: rather than a feature. An agent that sends none is treated exactly as
    #: one whose MACs matched nothing -- the suggestion path.
    #:
    #: NOT TRUSTED AS "PHYSICAL" JUST BECAUSE THE AGENT FILTERED THEM. The
    #: server re-applies the locally-administered and multicast checks before
    #: matching (`matching.py`): the field arrives over the wire, and the
    #: consequence of a bad entry is a MERGE of two records, not a wrong
    #: display value.
    macs: list[str] = Field(default_factory=list, max_length=64)


#: The most machines ONE enrollment token may ever enrol.
#:
#: WHAT `max_uses` USED TO BE. `POST /enrollment-tokens` defaulted it to
#: `None`, and `enroll()` reads `None` as UNLIMITED -- so the field an operator
#: left alone minted a credential that turns any number of unmanaged machines
#: into devices of this organisation, each with a certificate, each in the
#: fleet list, each submitting inventory the console believes, and each a
#: legitimate target for a signed job whose params may carry a secret. That is
#: not a default anybody chose; it is the shape of the form.
#:
#: DEFINED HERE AND ASSERTED EQUAL TO
#: `modules/agent_distribution/managed.MAX_DEPLOYMENT_USES`, not imported from
#: it. The kernel must not depend on a module (CLAUDE.md: "modules register
#: into the kernel; they do not fork it"), and two copies of a number are two
#: chances to disagree -- so `tests/test_enrollment.py` holds them equal
#: instead, exactly as `device_groups.MAX_GROUP_DEPTH` is held equal to
#: health's. The value is the managed-distribution spec's
#: (`docs/modules/agent-distribution/spec-managed-distribution.md` §3.3): above
#: Toeshee's 250-300 endpoints and below "a fleet nobody is counting".
#:
#: THE CEILING IS WHAT MAKES THIS A BOUND. `ge=1` alone would let a caller type
#: `2**31 - 1` and call it finite.
MAX_TOKEN_USES = 1000


class TokenBoundsError(ValueError):
    """`max_uses` outside `1..MAX_TOKEN_USES`, refused before a secret is
    generated.

    RAISED BY THE SERVICE AND NOT ONLY BY THE ROUTE. Three call sites mint
    tokens -- the kernel route, `agent_distribution` twice, and `provisioning`
    -- and only one of them has a Pydantic model in front of it. A bound that
    lives in a request schema is a bound the next caller does not inherit.
    """


def _checked_max_uses(max_uses: int) -> int:
    if not isinstance(max_uses, int) or isinstance(max_uses, bool):
        raise TokenBoundsError(tr("max_uses must be a whole number"))
    if max_uses < 1 or max_uses > MAX_TOKEN_USES:
        raise TokenBoundsError(
            tr(
                "max_uses must be between 1 and {max_token_uses}; an enrollment token is a "
                "credential that puts new machines into this organisation, and there is no "
                "unlimited one",
                max_token_uses=MAX_TOKEN_USES,
            )
        )
    return max_uses


@dataclass(frozen=True)
class IssuedToken:
    id: UUID
    token: str
    name: str
    expires_at: datetime
    #: FINITE, ALWAYS, on anything this platform mints from now on. The COLUMN
    #: is still nullable and `TokenListItem.max_uses` is still `int | None`,
    #: because rows issued before this existed are grandfathered -- see
    #: `create_token` and `enroll`.
    max_uses: int
    #: The client and site a machine enrolled with this token lands in (MSP
    #: clients spec §1.5).
    client_id: UUID
    site_id: UUID


@dataclass(frozen=True)
class EnrollResult:
    device_id: UUID
    cert_pem: str
    chain_pem: str
    ca_bundle_pem: str
    gateway_url: str
    job_signing_public_key_pem: str


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


async def create_token(
    session: AsyncSession,
    *,
    org_id: UUID,
    site_id: UUID,
    created_by: UUID,
    name: str,
    expires_in_hours: int | None = None,
    expires_in_minutes: int | None = None,
    max_uses: int,
) -> IssuedToken:
    """Mint one bootstrap credential. `max_uses` IS REQUIRED AND IS FINITE.

    `site_id` IS REQUIRED TOO (MSP clients spec §1.5): a machine enrolled with
    the token lands in that site and its client, and `enroll` opens its
    session on that client alone. The site is loaded HERE, in the caller's own
    session, and the client is taken from it -- never from the caller, so the
    pair cannot disagree. RLS hides a site of another organisation or of a
    client outside the caller's scope (`client_sites.SiteNotFound`), and an
    archived site or client takes no new token (`NotAcceptingNewRows`); both
    before a secret is generated.

    IT USED TO BE `int | None`, DEFAULTED TO `None` BY THE ROUTE, AND `None`
    MEANS UNLIMITED AT `enroll()`. `docs/modules/agent-distribution/spec-
    managed-distribution.md` §3.2 named that "an unbounded token is [a
    long-lived shared secret] wearing a shorter name", and §3.5 recorded
    leaving it alone as *found and not fixed*, on compatibility grounds. This
    fixes it in the only direction that is not silent:

    * **A REFUSAL AT CREATION IS LOUD.** A caller that omits `max_uses` now
      gets a 422 naming the field, at the moment a human is looking at the
      screen that asked for it.
    * **A CHANGED MEANING AT REDEMPTION WOULD BE SILENT.** The alternative --
      re-reading stored `NULL` as "one" -- fails at the ENDPOINT, inside the
      guarded UPDATE in `enroll()`, where every rejection is deliberately the
      same 403 with no detail. A fleet token that quietly became single-use
      presents to its operator as a revoked token, on machines they are not
      sitting at. That is why the stored value is left exactly as it is.

    So: nothing new is unlimited, and nothing already issued changes meaning.
    NO MIGRATION TOUCHES `enrollment_tokens`, and that is a decision rather
    than an omission -- `list_tokens` counts what is left over and the console
    offers a filter that finds it, because the answer to a credential nobody
    chose the bounds of is to let a human revoke it, not to guess a number on
    their behalf and write it into their database.
    """
    _checked_max_uses(max_uses)
    site = await site_for_new_rows(session, org_id=org_id, site_id=site_id)
    client_id = site.client_id
    # EXACTLY ONE OF hours/minutes. A minute-granularity expiry exists for one
    # caller and one reason: agent push install mints a token DELIVERED BY A
    # MACHINE rather than typed by a human, and its spec caps that token at 30
    # minutes and one use (spec-push-install.md §7.1). The console path keeps
    # naming hours; nothing about its behaviour changes.
    if (expires_in_hours is None) == (expires_in_minutes is None):
        raise ValueError(  # i18n-exempt: caller contract, unhandled 500
            "create_token requires exactly one of expires_in_hours / "
            "expires_in_minutes"
        )
    if expires_in_minutes is not None:
        if expires_in_minutes < 1:
            raise ValueError(  # i18n-exempt: caller contract, unhandled 500
                "expires_in_minutes must be at least 1"
            )
        lifetime = timedelta(minutes=expires_in_minutes)
    else:
        assert expires_in_hours is not None
        lifetime = timedelta(hours=expires_in_hours)
    secret = pysecrets.token_urlsafe(32)
    row = EnrollmentToken(
        org_id=org_id,
        client_id=client_id,
        site_id=site_id,
        token_hash=_hash_secret(secret),
        name=name,
        expires_at=datetime.now(UTC) + lifetime,
        max_uses=max_uses,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=created_by,
        action="enrollment.token_created",
        target_type="enrollment_token",
        target_id=str(row.id),
        metadata={
            "name": name,
            "max_uses": max_uses,
            "client_id": str(client_id),
            "site_id": str(site_id),
        },
    )
    return IssuedToken(
        id=row.id,
        token=f"{row.id}.{secret}",
        name=name,
        expires_at=row.expires_at,
        max_uses=max_uses,
        client_id=client_id,
        site_id=site_id,
    )


@dataclass(frozen=True)
class TokenPage:
    """One page of an org's enrollment tokens, plus the number withheld.

    WRAPPED RATHER THAN A BARE LIST, for the reason `DeviceGroupPage` and
    `CandidateListResponse` already are: the counts are the half that keeps the
    screen honest, and a list cannot hold them. Hiding revoked rows by default
    is only defensible while the operator can see that something WAS hidden --
    otherwise a revocation, which is one of the most security-relevant acts on
    this platform, quietly leaves the console.
    """

    rows: Sequence[EnrollmentToken]
    #: Tokens matching the filter, BEFORE `limit`/`offset`. It used to equal
    #: `len(rows)` because the route had no limit at all; the note here said
    #: the number was carried against the day one was added, "because
    #: provisioning mints a token per run, so one will be wanted". That day is
    #: this one. `total` is now the only thing that tells a short page from the
    #: end of the list.
    total: int
    #: Revoked tokens in this org, counted whether or not they are in `rows`.
    #: This is what the "Show revoked" control is offering to reveal.
    revoked_count: int
    #: Tokens in this org that are STILL REDEEMABLE and carry NO USE LIMIT --
    #: `max_uses IS NULL`, not revoked, not yet expired.
    #:
    #: THIS IS THE GRANDFATHERED SET, AND IT IS THE WHOLE REASON NO MIGRATION
    #: REWRITES THOSE ROWS. `create_token` refuses to mint another one, but a
    #: token already in the field keeps the meaning it was issued under, and a
    #: bound this platform invented after the fact would either be a number no
    #: human chose or -- if it landed below the row's `use_count` -- an instant
    #: rejection at the endpoint indistinguishable from a revocation.
    #:
    #: SO THE ANSWER IS A HUMAN WITH A REVOKE BUTTON, AND THIS IS WHAT SENDS
    #: THEM TO IT. Counted org-wide and before `limit`/`offset`, like
    #: `revoked_count`, because a count taken from the page would understate
    #: the standing risk exactly on the deployment that has the most of it.
    #: `list_tokens(unlimited_only=True)` is how the console then finds them.
    unlimited_count: int


#: The route's page size when a caller names none, and its ceiling. The house
#: numbers -- `GET /patch/patches`, `GET /devices`, `GET /audit` and
#: `GET /device-groups` all take `limit=Query(default=100, ge=1, le=500)`.
#: The CEILING is what makes this a bound: a `limit` a caller could set to
#: anything would be the unbounded route with an extra parameter on it.
TOKEN_PAGE_DEFAULT = 100
TOKEN_PAGE_MAX = 500


def _unlimited_predicates(org_id: UUID) -> tuple[Any, ...]:
    """A token of this org that carries no use limit AND can still be redeemed.

    NOT MERELY `max_uses IS NULL`. A revoked or expired row is history; the
    number an operator is asked to act on has to be the number of credentials
    that would enrol a machine if somebody presented one right now, or the
    banner cries wolf on every deployment that ever issued one.

    THE `org_id` PREDICATE IS DELIBERATE AND IS NOT REDUNDANT, for the reason
    the module docstring gives: `org_session` runs NOBYPASSRLS, so deleting it
    turns no route test red. `test_enrollment.py` runs this on the BYPASSRLS
    role, where it is the only thing scoping the count.
    """
    return (
        EnrollmentToken.org_id == org_id,
        EnrollmentToken.max_uses.is_(None),
        EnrollmentToken.revoked.is_(False),
        EnrollmentToken.expires_at > datetime.now(UTC),
    )


async def list_tokens(
    session: AsyncSession,
    *,
    org_id: UUID,
    include_revoked: bool = False,
    unlimited_only: bool = False,
    limit: int = TOKEN_PAGE_DEFAULT,
    offset: int = 0,
) -> TokenPage:
    """This org's enrollment tokens, newest first, REVOKED ONES EXCLUDED BY
    DEFAULT, with a count of the revoked ones either way.

    WHY A FILTER AND NOT A DELETE. The operator's report was that revoked
    entries pile up with no way to clear them, and the obvious reading -- give
    them a delete button -- is the one this platform cannot take:

    * A token row is the only record that a bootstrap credential ever existed,
      who created it (`created_by`), what it was called and when it was
      withdrawn. `enrollment.token_created` and `enrollment.token_revoked` are
      in the hash-chained audit log (invariant 4) and both name the token by
      id; delete the row and the trail points at something the platform can no
      longer describe.
    * `provision_runs.enrollment_token_id` REFERENCES this table with no
      `ON DELETE` (migration `0015`), so a token that provisioned a machine
      cannot be deleted at all without either a foreign-key violation or
      destroying the record of which credential built that device.

    So `revoked` stays what `15ed5de` made patch approvals and `0073` made
    device groups: a soft state, excluded from the default read, and brought
    back by an explicit `include_revoked`.

    PAGED, AND IT WAS NOT. Tokens only accumulate -- one per device onboarded,
    one per provisioning run, and none ever deleted for the reasons above -- so
    an unbounded response grew forever on the one screen an operator is on
    while onboarding a fleet, until it timed out or exhausted the console's
    memory. `limit`/`offset` with a capped `limit`, the shape every other list
    on this platform already takes.

    NEITHER COUNT LEARNS ABOUT THE PAGE. `total` is the tokens matching the
    filter and `revoked_count` is the org's revocations, both computed before
    `limit`/`offset` -- a `revoked_count` taken from the page would say "1
    hidden" on a screen withholding forty, and the control that offers to
    reveal them would be lying about its own blast radius.

    THE `org_id` PREDICATES ARE DELIBERATE AND ARE NOT REDUNDANT. RLS scopes
    both statements already; `test_enrollment.py::test_the_token_query_is_
    tenant_scoped_where_rls_is_not_the_thing_rejecting` runs them on the
    BYPASSRLS role so that deleting either one goes red. `revoked_count` is an
    aggregate over the whole table, which is exactly where an org-wide answer
    hides behind a correctly-filtered page.
    """
    query = select(EnrollmentToken).where(EnrollmentToken.org_id == org_id)
    if not include_revoked:
        query = query.where(EnrollmentToken.revoked.is_(False))
    if unlimited_only:
        # THE SAME PREDICATE `unlimited_count` COUNTS, so the control that
        # says "3 tokens have no use limit" and the list it opens cannot
        # disagree about which 3. A filter that found a different set from the
        # number that advertised it would be worse than no filter.
        query = query.where(*_unlimited_predicates(org_id))
    total = (
        await session.execute(select(func.count()).select_from(query.subquery()))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                query.order_by(EnrollmentToken.id.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    revoked_count = (
        await session.execute(
            select(func.count())
            .select_from(EnrollmentToken)
            .where(
                EnrollmentToken.org_id == org_id,
                EnrollmentToken.revoked.is_(True),
            )
        )
    ).scalar_one()
    unlimited_count = (
        await session.execute(
            select(func.count())
            .select_from(EnrollmentToken)
            .where(*_unlimited_predicates(org_id))
        )
    ).scalar_one()
    return TokenPage(
        rows=rows,
        total=total,
        revoked_count=revoked_count,
        unlimited_count=unlimited_count,
    )


async def revoke_token(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, token_id: UUID
) -> bool:
    row = (
        await session.execute(select(EnrollmentToken).where(EnrollmentToken.id == token_id))
    ).scalar_one_or_none()
    if row is None:
        return False
    row.revoked = True
    session.add(row)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="enrollment.token_revoked",
        target_type="enrollment_token",
        target_id=str(token_id),
    )
    return True


async def lock_live_tokens(
    session: AsyncSession,
    *,
    org_id: UUID,
    now: datetime,
    client_id: UUID | None = None,
    site_id: UUID | None = None,
) -> list[EnrollmentToken]:
    """Every unrevoked, unexpired token of one client or one site, locked FOR
    UPDATE.

    For archiving (MSP clients Task 9). TAKEN BEFORE THE ARCHIVE COUNTS
    DEVICES (review 1, concern 5): an enrollment holds its token's row lock
    from the guarded UPDATE that consumes it until it commits its device, so
    the archive either waits for that device and counts it, or holds the lock
    first and the enrollment's UPDATE then finds the token revoked.
    """
    if (client_id is None) == (site_id is None):
        raise ValueError(  # i18n-exempt: caller contract, unhandled 500
            "lock_live_tokens takes exactly one of client_id / site_id"
        )
    query = select(EnrollmentToken).where(
        EnrollmentToken.org_id == org_id,
        EnrollmentToken.revoked.is_(False),
        EnrollmentToken.expires_at > now,
    )
    if client_id is not None:
        query = query.where(EnrollmentToken.client_id == client_id)
    else:
        query = query.where(EnrollmentToken.site_id == site_id)
    rows = await session.execute(query.order_by(EnrollmentToken.id).with_for_update())
    return list(rows.scalars())


async def revoke_tokens(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    reason: str,
    tokens: Sequence[EnrollmentToken],
) -> int:
    """Revoke tokens `lock_live_tokens` returned, each audited
    `enrollment.token_revoked` with `reason`. Returns how many.

    `enroll` already refuses a token whose site or client is archived;
    revoking them as well is what the token list then shows.
    """
    for row in tokens:
        row.revoked = True
        await session.flush()
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=actor_id,
            action="enrollment.token_revoked",
            target_type="enrollment_token",
            target_id=str(row.id),
            metadata={
                "reason": reason,
                "client_id": str(row.client_id),
                "site_id": str(row.site_id),
            },
        )
    return len(tokens)


async def revoke_agent(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, device_id: UUID
) -> str | None:
    """Mark an enrolled agent's certificate revoked and audit it. Returns the
    cert serial (for CRL revocation by the caller) or None if not found.

    The DB flag is the gateway's admission gate, so it is set and committed
    first; the caller then updates the CA CRL and drops any live connection.
    Committing the flag before the CRL fails safe: the agent is already blocked
    at the gateway even if the CRL write is retried later."""
    agent = (
        await session.execute(select(Agent).where(Agent.device_id == device_id))
    ).scalar_one_or_none()
    if agent is None:
        return None
    if agent.cert_revoked:
        return agent.cert_serial  # idempotent
    agent.cert_revoked = True
    session.add(agent)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="enrollment.agent_revoked",
        target_type="device",
        target_id=str(device_id),
        metadata={"cert_serial": agent.cert_serial, "cert_fingerprint": agent.cert_fingerprint},
    )
    return agent.cert_serial


async def enroll(
    *,
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    ca: CAService,
    signer: SigningService,
    gateway_url: str,
    token: str,
    csr_pem: str,
    facts: DeviceFacts,
    capabilities: dict[str, Any] | None = None,
) -> EnrollResult:
    """Consume a token, create the Device and Agent, and have the CA bind the
    device identity into the issued certificate.

    `capabilities` is what the agent says it can run (C4). It is stored as
    advertised and is DEFAULTED, not required: an agent that does not send it
    stores `{}`, which is the column default and today's behaviour for every
    enrolled device. It is never read as an authorization -- see
    `EnrollRequest`'s docstring.
    """
    try:
        token_id_raw, secret = token.split(".", 1)
        token_id = UUID(token_id_raw)
    except ValueError as exc:
        raise EnrollmentError(  # i18n-exempt: route answers a fixed enrollment rejected
            "malformed token"
        ) from exc

    # Resolve the token's org and client (no tenant context exists yet) —
    # admin engine, read-only. This only picks the tenant and the client the
    # session is opened on; it does NOT authorize enrollment.
    async with AsyncSession(admin_engine) as session:
        owner = (
            await session.execute(
                select(EnrollmentToken.org_id, EnrollmentToken.client_id).where(
                    EnrollmentToken.id == token_id
                )
            )
        ).one_or_none()
    if owner is None:
        raise EnrollmentError(  # i18n-exempt: route answers a fixed enrollment rejected
            "token rejected"
        )
    org_id, scoped_client = owner

    # THE TOKEN'S CLIENT AND NOTHING ELSE (MSP clients spec §1.5). Under this
    # scope `resolve_enrolment_target` cannot see, and so can never link, an
    # asset row of another client, and the guarded UPDATE below can consume
    # only a token of this client.
    async with org_session(
        org_id, engine=app_engine, clients=ClientScope.only([scoped_client])
    ) as session:
        # Atomically consume the token: one guarded UPDATE re-checks the hash,
        # revocation, expiry, and use-count and increments in a single statement.
        # Zero rows affected = rejected. This closes the TOCTOU / lost-update
        # window — concurrent requests on a single-use token can consume it at
        # most once. Consumption and device/agent creation share this
        # transaction, so any later failure rolls the consumption back too.
        #
        # `max_uses IS NULL` STILL MEANS UNLIMITED HERE, AND MUST. `create_token`
        # no longer mints such a row — `MAX_TOKEN_USES` is required and capped —
        # but rows issued before that are in the field, and this is the one
        # statement where changing their meaning would be invisible. Every
        # failure of this UPDATE is the same `EnrollmentError`, answered as a
        # 403 with no detail about which check failed (deliberately: the detail
        # is an oracle). So a stored NULL re-read as "one" would present to an
        # operator as a token that had been revoked, on machines nobody is
        # sitting at, with the server logs saying nothing more. The grandfathered
        # rows are surfaced instead — `TokenPage.unlimited_count` counts them and
        # the console offers the filter that lists them — so a human revokes
        # them, which is an act with a name and an audit entry.
        #
        # AN ARCHIVED SITE OR CLIENT ENROLS NOTHING (MSP clients Task 9), even
        # with a token that predates the archive: archiving revokes the live
        # tokens, and this is the rule where it binds -- one more condition of
        # the same guarded UPDATE, so it is the same 403 with no detail.
        site_open = (
            select(Site.id)
            .join(Client, Client.id == Site.client_id)
            .where(
                Site.id == EnrollmentToken.site_id,
                Site.archived_at.is_(None),
                Client.status != CLIENT_ARCHIVED,
            )
            .exists()
        )
        consumed = (
            await session.execute(
                update(EnrollmentToken)
                .where(
                    EnrollmentToken.id == token_id,
                    EnrollmentToken.token_hash == _hash_secret(secret),
                    EnrollmentToken.revoked.is_(False),
                    EnrollmentToken.expires_at > datetime.now(UTC),
                    (EnrollmentToken.max_uses.is_(None))
                    | (EnrollmentToken.use_count < EnrollmentToken.max_uses),
                    site_open,
                )
                .values(use_count=EnrollmentToken.use_count + 1)
                # The client and site come from the row this statement
                # consumed, not from the unlocked read above.
                .returning(EnrollmentToken.id, EnrollmentToken.client_id, EnrollmentToken.site_id)
            )
        ).one_or_none()
        if consumed is None:
            raise EnrollmentError(  # i18n-exempt: route answers a fixed enrollment rejected
                "token rejected"
            )
        _, client_id, site_id = consumed

        now = datetime.now(UTC)

        # DOES THE INVENTORY ALREADY HAVE THIS MACHINE? An asset an operator
        # typed in, or promoted from a discovery, must be ENRICHED rather than
        # duplicated -- two rows for one machine, with no way to tell which is
        # real, is the defect that makes the whole asset feature worse than not
        # having it (asset-inventory spec §4).
        outcome = await resolve_enrolment_target(session, org_id, facts)
        device = None
        if outcome.device_id is not None:
            # A LINKED ASSET KEEPS ITS OWN SITE. Under this session's scope it is
            # one of the token's client's, and a person filed it under a site;
            # the token's site is only where a NEW row goes.
            device = await session.get(Device, outcome.device_id)
        if device is None:
            device = Device(
                org_id=org_id,
                client_id=client_id,
                site_id=site_id,
                hostname=facts.hostname,
                os_family=facts.os_family,
                os_version=facts.os_version,
                architecture=facts.architecture,
                serial=facts.serial,
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(device)
        else:
            # LINKED. The machine-reported facts land on the record that is
            # already there -- that is the enrichment -- but `source` is left
            # alone: it records how the record CAME TO EXIST, and an agent
            # enrolling onto a discovered asset does not retroactively make it
            # agent-created.
            device.hostname = facts.hostname
            device.os_family = facts.os_family
            device.os_version = facts.os_version
            device.architecture = facts.architecture
            if facts.serial:
                device.serial = facts.serial
            device.first_seen_at = device.first_seen_at or now
            device.last_seen_at = now
        await session.flush()

        for suggestion in outcome.suggestions:
            # A question for a human, not a link. `ON CONFLICT DO NOTHING`
            # against the partial unique index: a machine that re-enrols must
            # not stack a second identical open question, because an operator
            # facing forty copies of one stops reading the queue.
            await session.execute(
                pg_insert(SuggestedAssetMatch)
                .values(
                    org_id=org_id,
                    device_id=device.id,
                    candidate_device_id=suggestion.candidate_device_id,
                    basis=suggestion.basis,
                )
                .on_conflict_do_nothing(
                    index_elements=["device_id", "candidate_device_id"],
                    index_where=text("resolved_at IS NULL"),
                )
            )

        issued = await ca.sign_csr(csr_pem, device_id=device.id, org_id=org_id)
        session.add(
            Agent(
                org_id=org_id,
                device_id=device.id,
                cert_fingerprint=issued.fingerprint_sha256,
                cert_serial=issued.serial,
                cert_not_after=issued.not_after,
                enrolled_at=now,
                capabilities=capabilities or {},
            )
        )
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="agent",
            actor_id=None,
            action="enrollment.device_enrolled",
            target_type="device",
            target_id=str(device.id),
            metadata={
                "hostname": facts.hostname,
                "os_family": facts.os_family,
                "token_id": str(token_id),
                "cert_fingerprint": issued.fingerprint_sha256,
                # Where the machine landed: the device's own, which for a
                # linked asset is the asset's site, not necessarily the token's.
                "client_id": str(device.client_id),
                "site_id": str(device.site_id),
            },
        )
        if outcome.device_id is not None:
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="agent",
                actor_id=None,
                action="asset.enrolment_linked",
                target_type="device",
                target_id=str(device.id),
                # BOTH SIDES AND THE EVIDENCE. This is the moment two
                # identities become one, and it is the single hardest thing to
                # reconstruct afterwards if nobody wrote down which record the
                # agent joined and what made the platform sure.
                metadata={
                    "device_id": str(device.id),
                    "matched_on": outcome.matched_on or "",
                    "hostname": facts.hostname,
                },
            )

        # Provisioning correlation: if this token belongs to an in-flight
        # provisioning run, bind the new device to it and advance the run.
        # No-op (returns None) for ordinary enrollments.
        from app.modules.provisioning.enroll import bind_provisioned_device

        await bind_provisioned_device(
            session, org_id=org_id, enrollment_token_id=token_id, device_id=device.id, now=now
        )

        device_id = device.id

    return EnrollResult(
        device_id=device_id,
        cert_pem=issued.cert_pem,
        chain_pem=issued.chain_pem,
        ca_bundle_pem=await ca.ca_bundle_pem(),
        gateway_url=gateway_url,
        job_signing_public_key_pem=await signer.public_key_pem(),
    )


async def enroll_via_link(
    *,
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    ca: CAService,
    signer: SigningService,
    gateway_url: str,
    link_code: str,
    source: IPAddress | None,
    csr_pem: str,
    facts: DeviceFacts,
    capabilities: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> EnrollResult:
    """Enrol a machine through a site's install link (spec §1.4).

    THE LINK'S CLIENT AND SITE (MSP clients spec §1.5): the session is opened
    on the link's client alone, and the device lands in the link's site. The
    hourly limit counts the machines that joined that site.

    ONE TRANSACTION: the link row is locked `FOR UPDATE`, so two concurrent
    enrollments cannot both be the 50th, and the device, its agent, the audit
    row and the notification event commit together or not at all.

    THE DEVICE IS ALWAYS NEW (plan D3). `resolve_enrolment_target` is not
    consulted: it links on agent-reported MACs, and a machine nobody has
    reviewed must not inherit a row somebody vouched for.

    A 429 IS COMMITTED BEFORE IT IS RAISED, so the once-an-hour audit row is
    not rolled back with the refusal.
    """
    stamp = now or datetime.now(UTC)
    target = await link_svc.resolve_target(admin_engine, link_code)
    if target is None:
        raise LinkNotFound()
    org_id = target.org_id

    limited = False
    retry_after_s = MIN_LINK_RETRY_AFTER_S
    created: tuple[UUID, Any] | None = None
    async with org_session(
        org_id, engine=app_engine, clients=ClientScope.only([target.client_id])
    ) as session:
        link = await link_svc.live_link_for(
            session, target.site_id, code=link_code, source=source, now=stamp, for_update=True
        )
        if link is None:
            raise LinkNotFound()
        recent = (
            await session.execute(
                select(func.count())
                .select_from(Device)
                .where(
                    Device.org_id == org_id,
                    Device.site_id == link.site_id,
                    Device.link_joined_at > stamp - link_svc.RATE_WINDOW,
                )
            )
        ).scalar_one()
        if recent >= link.hourly_limit:
            limited = True
            # The slot that frees first is the one that takes the count back
            # under the limit: the (recent - limit + 1)-th oldest join in the
            # window ages out of it.
            freeing = (
                await session.execute(
                    select(Device.link_joined_at)
                    .where(
                        Device.org_id == org_id,
                        Device.site_id == link.site_id,
                        Device.link_joined_at > stamp - link_svc.RATE_WINDOW,
                    )
                    .order_by(Device.link_joined_at)
                    .offset(recent - link.hourly_limit)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if freeing is not None:
                wait = (freeing + link_svc.RATE_WINDOW - stamp).total_seconds()
                retry_after_s = max(MIN_LINK_RETRY_AFTER_S, math.ceil(wait))
            due = link.rate_limited_audit_at is None or (
                link.rate_limited_audit_at <= stamp - link_svc.RATE_WINDOW
            )
            if due:
                link.rate_limited_audit_at = stamp
                await audit_service.record(
                    session,
                    org_id=org_id,
                    actor_type="system",
                    actor_id=None,
                    action="enrollment.link_rate_limited",
                    target_type="install_link",
                    target_id=str(link.site_id),
                    metadata={"hourly_limit": link.hourly_limit, "source_address": str(source)},
                )
        else:
            device = Device(
                org_id=org_id,
                client_id=link.client_id,
                site_id=link.site_id,
                hostname=facts.hostname,
                os_family=facts.os_family,
                os_version=facts.os_version,
                architecture=facts.architecture,
                serial=facts.serial,
                source="agent",
                provenance="agent",
                review_state="pending",
                link_joined_from=str(source),
                link_joined_at=stamp,
                first_seen_at=stamp,
                last_seen_at=stamp,
            )
            session.add(device)
            await session.flush()
            issued = await ca.sign_csr(csr_pem, device_id=device.id, org_id=org_id)
            session.add(
                Agent(
                    org_id=org_id,
                    device_id=device.id,
                    cert_fingerprint=issued.fingerprint_sha256,
                    cert_serial=issued.serial,
                    cert_not_after=issued.not_after,
                    enrolled_at=stamp,
                    capabilities=capabilities or {},
                )
            )
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="agent",
                actor_id=None,
                action="agent.enrolled_via_link",
                target_type="device",
                target_id=str(device.id),
                metadata={
                    "hostname": facts.hostname,
                    "os_family": facts.os_family,
                    "source_address": str(source),
                    "cert_fingerprint": issued.fingerprint_sha256,
                },
            )
            await publish_event(
                session,
                org_id=org_id,
                event_type="device.joined_via_link",
                subject_type="device",
                subject_id=device.id,
                payload={
                    "device_id": str(device.id),
                    "hostname": facts.hostname,
                    "os_family": facts.os_family,
                    "source_address": str(source),
                    "joined_at": stamp.isoformat(),
                },
                actor_user_id=None,
                client_id=device.client_id,
            )
            created = (device.id, issued)

    if limited:
        raise LinkRateLimited(retry_after_s)
    assert created is not None
    device_id, issued = created
    return EnrollResult(
        device_id=device_id,
        cert_pem=issued.cert_pem,
        chain_pem=issued.chain_pem,
        ca_bundle_pem=await ca.ca_bundle_pem(),
        gateway_url=gateway_url,
        job_signing_public_key_pem=await signer.public_key_pem(),
    )
