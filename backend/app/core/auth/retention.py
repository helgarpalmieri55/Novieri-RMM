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
  whoever does not).

WHAT STAYS: everything live, the failure counters (one row per person and
factor), passwords, factors, Microsoft links, and the audit chain.

THE SWEEP IS AUDITED (plan decision 2; invariant 4): deleting evidence is a
privileged act, as `ipam.retention` and `security_logs.retention` hold. One
`auth.retention_applied` per organization, only when something went, with a
count per step.

EVERY STEP CARRIES AN EXPLICIT `org_id` PREDICATE, in addition to RLS (plan
decision 3; `app/core/ai/retention.py`'s rule): `sweep` takes any session,
and on one RLS does not filter, the predicate is what keeps one
organization's sweep off another's rows.

THE REGISTRY (`STEPS`) IS WHERE A NEW TABLE JOINS (plan decision 4): a
`(name, step)` pair, appended in order; each step deletes in the session it
is given, with its own `org_id` predicate, and returns how many rows went.
Phase 3 adds its authorization codes, its grants (refresh-token families,
whose tokens cascade) and its idle apps (`oauth_clients`) here.

ONE ORGANIZATION, EVERY CLIENT. A technician's rows have no client and only
an every-client scope deletes them (migration 0231), so `sweep_org` holds
`ClientScope.all()` (`ALL_SCOPE_ALLOWLIST`), over these tables only. The
worker runs it daily (`app/worker.py`, `auth-retention`); no person is behind
it. No index helps these scans (`auth_sessions` has none on `ended_at` or
`expires_at`): one sequential scan per organization per day is fine at this
size, and a step on a large table decides for itself.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.auth.action_tokens import INVITE
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import User
from app.models.auth import AuthActionToken, AuthSession, AuthTrustedDevice, AuthWebauthnChallenge

#: How long an ended or expired session is kept, with its address and browser (C7).
SESSION_RETENTION = timedelta(days=90)
#: How long a forgotten device or a spent link is kept (C7).
RETENTION = timedelta(days=30)
#: How long a used or expired WebAuthn challenge is kept (C7).
CHALLENGE_RETENTION = timedelta(days=1)
#: The beat interval (`app/worker.py`).
SWEEP_INTERVAL_S = 86400.0
#: The audit action of a sweep that deleted something (plan decision 2).
AUDIT_ACTION = "auth.retention_applied"

#: One step of the sweep: delete in `session`, for `org_id`, as of `now`; how many went.
Step = Callable[[AsyncSession, UUID, datetime], Awaitable[int]]


@dataclass(frozen=True)
class Swept:
    #: Rows deleted, per step name, in `STEPS` order.
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
            or_(
                AuthSession.ended_at < cutoff,
                and_(AuthSession.ended_at.is_(None), AuthSession.expires_at < cutoff),
            ),
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
                and_(expired_unused, AuthActionToken.purpose != INVITE),
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


#: The sweep, in order: sessions first, because a device is kept while a
#: remaining session names it. A new table joins by appending a pair here
#: (plan decision 4; phase 3).
STEPS: tuple[tuple[str, Step], ...] = (
    ("sessions", _sessions),
    ("devices", _devices),
    ("links", _links),
    ("challenges", _challenges),
)


async def sweep(session: AsyncSession, org_id: UUID, *, now: datetime) -> Swept:
    """Every step of `STEPS`, in the caller's session, for `org_id` only."""
    counts: dict[str, int] = {}
    for name, step in STEPS:
        counts[name] = await step(session, org_id, now)
    return Swept(counts=counts)


async def sweep_org(org_id: UUID, engine: AsyncEngine, *, now: datetime | None = None) -> int:
    """One organization's sweep, and its audit row when something went, in
    one transaction; how many rows went."""
    stamp = now or datetime.now(UTC)
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        swept = await sweep(session, org_id, now=stamp)
        if swept.total:
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
                metadata={"at": stamp.isoformat(), **swept.counts},
            )
    return swept.total
