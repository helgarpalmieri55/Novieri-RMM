"""Retention of own authentication's bookkeeping (final review A-M6; plan
2026-11-06, Task 3; operator decision C7, 2026-11-06).

WHAT GOES, AND AFTER HOW LONG:

- a session ended, or past its maximum, more than `SESSION_RETENTION` (90
  days) ago. Its row is the only record of WHERE a sign-in came from (`ip`,
  `user_agent`): the audit chain says who and when, not from where, so after
  this nothing does. The operator chose 90 days knowing that (C7);
- a remembered device revoked or expired more than `RETENTION` (30 days) ago,
  once no remaining session names it (`fk_auth_sessions_device_user` has no ON
  DELETE, and a session started on a device may outlive it);
- a one-time link used more than `RETENTION` ago, or a reset or recovery link
  expired unused more than `RETENTION` ago. A TECHNICIAN'S UNUSED INVITATION IS
  NEVER SWEPT, expired or not: it is the one definition of a pending
  invitation (`user_admin.service._open_invites`), which an administrator
  resends or cancels. A CONTACT'S goes `RETENTION` after it expired: for a
  contact, expired is "no access" (`services.contact_access.access_of`), it
  shows nowhere, and there is at most one (`action_tokens.issue` retires the
  previous one);
- a WebAuthn challenge used or expired more than `CHALLENGE_RETENTION` (1 day)
  ago (`passkeys._issue` sweeps those of a person who comes back; this is for
  whoever does not);
- an OAuth authorization code `OAUTH_CODE_RETENTION` (10 minutes) after it
  expired, used or not: while it lasts, a second exchange finds it and ends
  the grant it opened (own authentication phase 3, plan 2026-11-06, Task 13);
- an AI app's grant (`oauth_refresh_families`) `OAUTH_GRANT_RETENTION` (1 day)
  after it was revoked or lapsed, its refresh tokens first, counted apart;
  its reason is also in the audit event of the act that ended it;
- an AI app unused for `CLIENT_IDLE` (30 days), with its grants and codes (ON
  DELETE CASCADE).

WHAT STAYS: everything live, the failure counters (one row per person and
factor), passwords, factors, Microsoft links, the record of what was revoked
from AI apps (`mcp_revocation`), and the audit chain.

THE SWEEP IS AUDITED (plan decision 2; invariant 4): deleting evidence is a
privileged act, as `ipam.retention` and `security_logs.retention` hold. One
`auth.retention_applied` per organization, only when something went, with a
count per step: every row a step's own statements delete is in that day's
row, whatever else fails (`sweep_org`) -- except the residual `sweep_org`
names: a purge error other than 55P03 or 40P01, after apps of the batch
committed.

EVERY STEP CARRIES AN EXPLICIT `org_id` PREDICATE, in addition to RLS (plan
decision 3; `app/core/ai/retention.py`'s rule): `sweep` takes any session,
and on one RLS does not filter, the predicate is what keeps one
organization's sweep off another's rows.

THE REGISTRY (`STEPS`) IS WHERE A NEW TABLE JOINS (plan decision 4): a
`(name, step)` pair, appended in order; each step deletes in the session it
is given, with its own `org_id` predicate, and returns how many rows went.
Phase 3 adds its authorization codes, its grants (refresh-token families,
whose tokens cascade) and its idle apps (`oauth_clients`) here.
Two of them in the end differ (THE OAUTH STEPS, below): the grants' tokens
go first, by a step of their own (`oauth_refresh_tokens`), not by cascade;
and the idle apps, whose rows may not go in the sweep's transaction, join
`APART_STEPS` instead, as such a table does.

THE OAUTH STEPS (own authentication phase 3, plan 2026-11-06, Task 13; the
lock map in `guards.lock_user_row`). The codes, the refresh tokens and the
grants go in the sweep's transaction, the apps before it and outside it:

- `oauth_codes`, `oauth_refresh_tokens` and `oauth_grants` hold no person's
  row and no app row, and WAIT ON NO ROW: each statement locks what it
  deletes FOR UPDATE SKIP LOCKED, with the ended condition inside it, and
  leaves a row another transaction holds to the next day's sweep. Its
  `org_id` predicate is where it chooses and locks those rows, so on a
  session RLS does not filter it does not even lock another organization's.
  They go leaves first -- the codes; the ended grants' refresh tokens; then
  the ended grants no token or code still names -- so no cascade of theirs
  reaches a row they did not lock. Why not plain DELETEs: a revocation's one
  UPDATE (`revoked_at IS NULL`) also matches a grant that lapsed unrevoked,
  in no order of the sweep's, and a refresh locks a token and then its
  family, the other way round from a family's cascading DELETE; an
  investigation that was not committed met both as 40P01. What the tests
  pin (`tests/test_oauth_retention.py`) is that THE SWEEP WAITS ON NO ROW:
  each writer holds its lock while the sweep runs under a lock timeout,
  which a plain DELETE trips. Waiting on nothing, with the chain last, the
  sweep can close no cycle with an exchange, a refresh, a revocation or an
  idle app's purge; each of them may wait on the sweep, and then find the
  row gone.
- `oauth_apps` is the registration's own purge (`clients.purge_idle`, the
  purge's shape in the lock map): one idle app per transaction, the idle
  condition inside its DELETE, a bounded wait and then giving way. Not in
  the sweep's transaction, which would hold one app's cascade while waiting
  in the next one's -- the cycle that shape exists to avoid -- and before
  it, so the sweep's one audit row counts the apps too; if that transaction
  fails, a row of their own records them (`sweep_org`). What an app's
  cascade takes, a spent code or an ended grant among it, no step counts.
  Its batch and budget are the registration's (`PURGE_BATCH`, 100 apps;
  `PURGE_BUDGET_S`, 2 s), not larger ones of its own, although no request
  waits on the sweep: an app it leaves is inert (`find_client` and `hold`
  read an idle app as absent) and goes the next day, oldest first; the
  ceiling of live registrations (200) bounds how many registrations can go
  idle in any thirty days; uncontended, one app costs milliseconds, so the
  budget covers the batch; contended, each costs up to the bounded wait,
  and a larger budget would only keep the worker's daily task, which sweeps
  every organization in turn, waiting on apps somebody is using.

ONE ORGANIZATION, EVERY CLIENT. A technician's rows have no client and only
an every-client scope deletes them (migration 0231), so `sweep_org` holds
`ClientScope.all()` (`ALL_SCOPE_ALLOWLIST`), over these tables only: phase
2's four and, since phase 3, `oauth_codes`, `oauth_refresh_tokens` and
`oauth_refresh_families`; and, only when that transaction failed after idle
apps went, the apps' own audit row. The idle apps go by
`clients.purge_idle`, under `oauth_session` (its own entry in that list).
The worker runs it daily (`app/worker.py`, `auth-retention`); no person is
behind it. No index helps these scans (`auth_sessions` has none on
`ended_at` or `expires_at`): one sequential scan per organization per day is
fine at this size, and a step on a large table decides for itself.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import aliased

from app.core.audit import audit_service
from app.core.auth.action_tokens import INVITE, RECOVER, RESET
from app.core.auth.oauth import clients
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import User
from app.models.auth import AuthActionToken, AuthSession, AuthTrustedDevice, AuthWebauthnChallenge
from app.models.oauth import OAuthCode, OAuthRefreshFamily, OAuthRefreshToken

logger = logging.getLogger("its.auth.retention")

#: How long an ended or expired session is kept, with its address and browser (C7).
SESSION_RETENTION = timedelta(days=90)
#: How long a forgotten device or a spent link is kept (C7).
RETENTION = timedelta(days=30)
#: How long a used or expired WebAuthn challenge is kept (C7).
CHALLENGE_RETENTION = timedelta(days=1)
#: How long an OAuth code is kept after it expires, and an ended grant after
#: it ended (plan 2026-11-06, Task 13; B3-25).
OAUTH_CODE_RETENTION = timedelta(minutes=10)
OAUTH_GRANT_RETENTION = timedelta(days=1)
#: The beat interval (`app/worker.py`).
SWEEP_INTERVAL_S = 86400.0
#: The audit action of a sweep that deleted something (plan decision 2).
AUDIT_ACTION = "auth.retention_applied"

#: One step of the sweep: delete in `session`, for `org_id`, as of `now`; how many went.
Step = Callable[[AsyncSession, UUID, datetime], Awaitable[int]]
#: One step that may not run in the sweep's transaction: given the engine, it
#: opens transactions of its own, for `org_id`, as of `now`; how many went.
ApartStep = Callable[[AsyncEngine, UUID, datetime], Awaitable[int]]


@dataclass(frozen=True)
class Swept:
    #: Rows deleted, per step name, in `STEPS` order (and, from `sweep_org`,
    #: `APART_STEPS`' after them).
    counts: Mapping[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


async def _deleted(session: AsyncSession, statement: Any) -> int:
    return len((await session.execute(statement)).all())


async def _sessions(session: AsyncSession, org_id: UUID, now: datetime) -> int:
    cutoff = now - SESSION_RETENTION
    return await _deleted(
        session,
        delete(AuthSession)
        .where(
            AuthSession.org_id == org_id,
            # Whichever came first counts: a bulk act (sign out everywhere, a
            # password change) stamps `ended_at` on rows that had long expired.
            # `expires_at < cutoff` is a dead session, never a live one.
            or_(AuthSession.ended_at < cutoff, AuthSession.expires_at < cutoff),
        )
        .returning(AuthSession.id),
    )


async def _devices(session: AsyncSession, org_id: UUID, now: datetime) -> int:
    cutoff = now - RETENTION
    named = (
        select(AuthSession.id)
        .where(
            AuthSession.org_id == org_id,
            AuthSession.trusted_device_id == AuthTrustedDevice.id,
        )
        .correlate(AuthTrustedDevice)
        .exists()
    )
    return await _deleted(
        session,
        delete(AuthTrustedDevice)
        .where(
            AuthTrustedDevice.org_id == org_id,
            or_(AuthTrustedDevice.revoked_at < cutoff, AuthTrustedDevice.expires_at < cutoff),
            ~named,
        )
        .returning(AuthTrustedDevice.id),
    )


async def _links(session: AsyncSession, org_id: UUID, now: datetime) -> int:
    cutoff = now - RETENTION
    of_a_contact = (
        select(User.id)
        .where(User.id == AuthActionToken.user_id, User.kind == "contact")
        .correlate(AuthActionToken)
        .exists()
    )
    expired_unused = and_(AuthActionToken.used_at.is_(None), AuthActionToken.expires_at < cutoff)
    return await _deleted(
        session,
        delete(AuthActionToken)
        .where(
            AuthActionToken.org_id == org_id,
            or_(
                AuthActionToken.used_at < cutoff,
                and_(expired_unused, AuthActionToken.purpose.in_((RESET, RECOVER))),
                and_(expired_unused, AuthActionToken.purpose == INVITE, of_a_contact),
            ),
        )
        .returning(AuthActionToken.id),
    )


async def _challenges(session: AsyncSession, org_id: UUID, now: datetime) -> int:
    stale = now - CHALLENGE_RETENTION
    return await _deleted(
        session,
        delete(AuthWebauthnChallenge)
        .where(
            AuthWebauthnChallenge.org_id == org_id,
            or_(AuthWebauthnChallenge.used_at < stale, AuthWebauthnChallenge.expires_at < stale),
        )
        .returning(AuthWebauthnChallenge.id),
    )


async def _oauth_codes(session: AsyncSession, org_id: UUID, now: datetime) -> int:
    """Codes expired more than `OAUTH_CODE_RETENTION` ago, used or not, each
    locked FOR UPDATE SKIP LOCKED: one an exchange holds is left (THE OAUTH
    STEPS). A code is a leaf -- nothing refers to it -- so its DELETE
    cascades nowhere."""
    code = aliased(OAuthCode)
    spent = (
        select(code.id)
        .where(code.org_id == org_id, code.expires_at < now - OAUTH_CODE_RETENTION)
        .with_for_update(skip_locked=True)
    )
    return await _deleted(
        session, delete(OAuthCode).where(OAuthCode.id.in_(spent)).returning(OAuthCode.id)
    )


def _ended(family: type[OAuthRefreshFamily], cutoff: datetime) -> ColumnElement[bool]:
    """Revoked, or lapsed, before `cutoff`: whichever came first counts."""
    return or_(family.revoked_at < cutoff, family.expires_at < cutoff)


async def _oauth_refresh_tokens(session: AsyncSession, org_id: UUID, now: datetime) -> int:
    """The refresh tokens of grants revoked or lapsed more than
    `OAUTH_GRANT_RETENTION` ago, before their grants (`_oauth_grants`, the
    next step), as a refresh locks a token before its grant; how many. Each
    locked FOR UPDATE SKIP LOCKED: one a refresh holds is left (THE OAUTH
    STEPS). Not those of a grant a code still names -- the codes step left
    that code, an exchange holding it -- so that grant keeps its tokens and
    both go together, on the day the code goes. A token is a leaf: its
    DELETE cascades nowhere."""
    cutoff = now - OAUTH_GRANT_RETENTION
    family = aliased(OAuthRefreshFamily)
    token = aliased(OAuthRefreshToken)
    code = aliased(OAuthCode)
    of_ended = (
        select(token.id)
        .join(family, family.id == token.family_id)
        .where(
            token.org_id == org_id,
            _ended(family, cutoff),
            ~exists().where(code.family_id == family.id),
        )
        .with_for_update(of=token, skip_locked=True)
    )
    return await _deleted(
        session,
        delete(OAuthRefreshToken)
        .where(OAuthRefreshToken.id.in_(of_ended))
        .returning(OAuthRefreshToken.id),
    )


async def _oauth_grants(session: AsyncSession, org_id: UUID, now: datetime) -> int:
    """Grants revoked or lapsed more than `OAUTH_GRANT_RETENTION` ago, once
    their tokens have gone (`_oauth_refresh_tokens`, the step before); how
    many. Each locked FOR UPDATE SKIP LOCKED (THE OAUTH STEPS), and only one
    no token and no code still names, so the DELETE's cascade -- the tokens,
    and the `family_id` of a code that opened the grant (ON DELETE SET NULL)
    -- finds nothing to wait on. A grant whose token a refresh holds, or
    whose spent code an exchange holds (the codes step left it), is left to
    the next sweep with them."""
    cutoff = now - OAUTH_GRANT_RETENTION
    family = aliased(OAuthRefreshFamily)
    token = aliased(OAuthRefreshToken)
    code = aliased(OAuthCode)
    unnamed = (
        select(family.id)
        .where(
            family.org_id == org_id,
            _ended(family, cutoff),
            ~exists().where(token.family_id == family.id),
            ~exists().where(code.family_id == family.id),
        )
        .with_for_update(of=family, skip_locked=True)
    )
    return await _deleted(
        session,
        delete(OAuthRefreshFamily)
        .where(OAuthRefreshFamily.id.in_(unnamed))
        .returning(OAuthRefreshFamily.id),
    )


async def _oauth_apps(engine: AsyncEngine, org_id: UUID, now: datetime) -> int:
    """Apps unused for `CLIENT_IDLE`, registered or known by their document,
    with their grants and codes: the registration's own purge
    (`clients.purge_idle`), its shape, its batch and its budget (THE OAUTH
    STEPS); how many apps went. What their cascade took, no step counts."""
    return (await clients.purge_idle(engine, org_id=org_id, now=now)).removed


#: The sweep, in order: sessions first, because a device is kept while a
#: remaining session names it. A new table joins by appending a pair here
#: (plan decision 4; phase 3).
STEPS: tuple[tuple[str, Step], ...] = (
    ("sessions", _sessions),
    ("devices", _devices),
    ("links", _links),
    ("challenges", _challenges),
    # Own authentication phase 3 (plan 2026-11-06, Task 13): the codes, the
    # ended grants' refresh tokens, then the grants -- leaves first, each
    # statement SKIP LOCKED (THE OAUTH STEPS). The idle apps are
    # `APART_STEPS`'.
    ("oauth_codes", _oauth_codes),
    ("oauth_refresh_tokens", _oauth_refresh_tokens),
    ("oauth_grants", _oauth_grants),
)

#: The steps that may not run in the sweep's transaction: each opens
#: transactions of its own, and `sweep_org` runs them BEFORE that one, so its
#: one audit row counts them too (THE OAUTH STEPS).
APART_STEPS: tuple[tuple[str, ApartStep], ...] = (("oauth_apps", _oauth_apps),)


async def sweep(session: AsyncSession, org_id: UUID, *, now: datetime) -> Swept:
    """Every step of `STEPS`, in the caller's session, for `org_id` only."""
    counts: dict[str, int] = {}
    for name, step in STEPS:
        counts[name] = await step(session, org_id, now)
    return Swept(counts=counts)


async def _record(
    session: AsyncSession, org_id: UUID, stamp: datetime, counts: Mapping[str, int]
) -> None:
    """The sweep's one audit row (plan decision 2), the chain's lock taken
    here, last."""
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        # A literal, as `ipam/retention.py` and `security_logs/retention.py`
        # write theirs: the vocabulary guard's tripwire counts any other.
        action="auth.retention_applied",
        target_type="organization",
        target_id=str(org_id),
        metadata={"at": stamp.isoformat(), **counts},
    )


async def sweep_org(org_id: UUID, engine: AsyncEngine, *, now: datetime | None = None) -> int:
    """One organization's sweep; how many rows went. First `APART_STEPS`,
    each in transactions of its own, holding nothing once done; then
    `STEPS` and, when something went, the audit row counting both -- the
    chain last -- in one transaction.

    EVERY ROW THAT WENT IS IN AN AUDIT ROW, the apps too (review of Task 13,
    fix round 1). They are gone before the sweep's transaction opens, so if
    that transaction fails -- a step, the chain, a dropped connection -- it
    rolls back its own deletions and their count with them, but not the
    apps'. One `auth.retention_applied` then records the apps alone, every
    `STEPS` count 0, in a transaction that takes nothing but the chain, and
    the sweep's own error propagates; if that row cannot be written either,
    a static ERROR line says so, and the sweep's error still propagates.
    RESIDUAL: a purge error `clients._purge_one` does not give way on (a
    database error other than 55P03 and 40P01) propagates out of
    `APART_STEPS` itself, after the apps before it in its batch committed,
    and nothing records those."""
    stamp = now or datetime.now(UTC)
    apart = {name: await step(engine, org_id, stamp) for name, step in APART_STEPS}
    try:
        async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
            swept = Swept(counts={**(await sweep(session, org_id, now=stamp)).counts, **apart})
            if swept.total:
                await _record(session, org_id, stamp, swept.counts)
    except Exception:
        purged = sum(apart.values())
        if purged:
            alone = {**{name: 0 for name, _step in STEPS}, **apart}
            try:
                async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
                    await _record(session, org_id, stamp, alone)
            except Exception as unrecorded:
                logger.error(
                    "auth retention: idle AI apps were purged but the sweep failed, and their "
                    "audit row could not be written",
                    extra={
                        "org_id": org_id,
                        "apps": purged,
                        "error": type(unrecorded).__name__,
                    },
                )
        raise
    return swept.total
