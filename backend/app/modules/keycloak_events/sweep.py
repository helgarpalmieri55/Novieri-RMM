"""Pull Keycloak's credential events into the ITMS audit chain.

`docs/modules/console-auth/spec.md` §4 and §7.1.

WHY A SWEEP AND NOT A PUSH. §7.1 left both open. The push option means shipping
a Java event-listener SPI into a Keycloak image this project pins and does not
build, and handing Keycloak a credential to call back into ITMS -- a new
inbound path authenticated by a secret Keycloak stores. The sweep needs no
plugin, no image change and no inbound path. Its cost is latency bounded by the
beat interval, which is stated rather than hidden.

WHAT THIS DOES NOT MAKE TRUE. Keycloak's own event log remains the source. A
Keycloak compromised before a sweep runs can drop an event this will never see,
and no amount of forwarding makes that absence detectable from here. Forwarding
puts the events that ARE emitted into a tamper-evident chain; it does not make
the emitter trustworthy.

### The three rules this module is built around

1. **An unconfigured deployment must be VISIBLY unconfigured.** No credential
   is a supported state and does not raise -- but it is reported by the
   `keycloak_events` probe, because an audit feature that silently does nothing
   is worse than one that does not exist: somebody reads the chain and believes
   it is complete.
2. **Never file an event under a guessed org.** The Keycloak realm is shared by
   every tenant. A `userId` that resolves to no ITMS user -- or, legally, to
   users in two orgs -- is counted and reported. Filing it under a default org
   would put one tenant's credential change into another's evidence, in an
   append-only log where it cannot be corrected.
3. **Counts are RETURNED, never merely logged.** "The sweep ran without
   raising" is satisfied by a sweep that wrote nothing, so the caller gets
   numbers it can act on and the probe records them.
"""

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.audit import audit_service
from app.core.client_scope import ClientScope
from app.core.tenancy import admin_session, org_session
from app.models import KeycloakEventIngest, User
from app.modules.keycloak_events.client import KeycloakEventsConfig, fetch_events
from app.modules.keycloak_events.mapping import event_fingerprint, map_event

logger = logging.getLogger(__name__)

#: Seconds between sweeps. The lag this feature costs, and the number the probe
#: measures staleness against. Hourly rather than by-the-minute because
#: Keycloak's own filters are day-granular -- a faster beat would re-read the
#: same day more often for no additional freshness.
SWEEP_INTERVAL_S = 3600.0

#: The furthest back any single fetch reaches. Two jobs at once: a first run on
#: a fresh deployment backfills the events already in the realm, and a worker
#: that has been down catches up rather than losing the outage's events -- but
#: a worker down for six months catches up thirty days and no more, because an
#: unbounded fetch after a long outage becomes an incident of its own.
LOOKBACK_CEILING_DAYS = 30


@dataclass(frozen=True, slots=True)
class SweepCounts:
    """What one run did. `written` is the only number that means events reached
    the chain; the other three are the ways a run can be busy and empty."""

    fetched: int = 0
    written: int = 0
    #: A forwardable event whose Keycloak user resolves to no ITMS org, or to
    #: more than one. NOT written anywhere -- see rule 2.
    unresolved: int = 0
    #: Fetched and deliberately not written: a type this does not forward (the
    #: overwhelming majority -- every login in the realm), or one already
    #: forwarded by an earlier sweep.
    skipped: int = 0


async def _window_start(admin_engine: AsyncEngine, *, now: datetime) -> date:
    """The `dateFrom` for this run's fetch.

    Derived from the dedupe ledger rather than from a stored cursor, because
    the ledger is already the record of "what this platform has forwarded" and
    a second piece of state could disagree with it.

    ONE DAY BEHIND the newest ingest, not from it. Keycloak filters by day, so
    an event emitted late on the watermark's own day -- after that day's last
    sweep -- would otherwise fall just outside every subsequent window and be
    lost permanently. Re-reading a day costs nothing: the fingerprint dedupe is
    what makes the overlap free.

    FLEET-WIDE ON PURPOSE, AND IT MUST STAY THAT WAY. This is a `max()` over
    the whole ledger with no `org_id` predicate, taken on the BYPASSRLS admin
    engine, so an ingest under org A advances the fetch window used for org B.
    That is correct because the thing being windowed is not a tenant's data: it
    is ONE Keycloak realm's event stream, fetched once per sweep and fanned out
    to orgs afterwards by `_resolve_org`. A per-org cursor would be a cursor
    over a stream no org owns.

    It would also be a regression. Per-org watermarks advance at different
    rates -- an org with no forwardable events never advances at all -- so the
    oldest cursor would drag `dateFrom` back over ground already swept, or, if
    seeded from "now" to avoid that, would lose exactly the late-in-the-day
    events the paragraph above exists to catch. Do not "fix" this into a
    per-org cursor; the audit at
    `docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md` §2.2 row 6
    records it as safe-by-design rather than as a missing tenant predicate.
    """
    ceiling = (now - timedelta(days=LOOKBACK_CEILING_DAYS)).date()
    async with admin_session(engine=admin_engine) as session:
        newest = (
            await session.execute(select(func.max(KeycloakEventIngest.ingested_at)))
        ).scalar_one_or_none()
    if newest is None:
        return ceiling
    return max(ceiling, (newest - timedelta(days=1)).date())


async def _resolve_org(admin_engine: AsyncEngine, subject: str) -> tuple[UUID, UUID] | None:
    """`(org_id, user_id)` for a Keycloak subject, or `None`.

    `users` is unique on `(org_id, external_subject)` and NOT on the subject
    alone, so two orgs holding one subject is a legal state with no answer this
    module is entitled to invent. Both "nobody" and "more than one" return
    `None` and are counted as unresolved -- see rule 2.

    Runs on the ADMIN engine because the whole point is that the org is not yet
    known; a tenant-scoped session would need the answer to ask the question.
    """
    async with admin_session(engine=admin_engine) as session:
        rows = (
            await session.execute(
                select(User.org_id, User.id).where(User.external_subject == subject).limit(2)
            )
        ).all()
    if len(rows) != 1:
        return None
    return rows[0][0], rows[0][1]


async def sweep_once(
    *,
    engine: AsyncEngine,
    admin_engine: AsyncEngine,
    settings: Any,
    now: datetime,
    fetch: Any = fetch_events,
) -> SweepCounts:
    """Fetch, map, resolve, dedupe, write. Returns what happened.

    `fetch` is injected so everything downstream of the HTTP call is testable
    without an HTTP server -- which is all of the judgement in this module. The
    client itself is deliberately thin for the same reason.

    RAISES on a Keycloak that is unreachable, refuses the credential, or is
    configured with a secret file that has gone missing. A forwarding feature
    that swallows its own failures is the exact shape of the audit gap this
    exists to close; the Celery task's failure capture is where that surfaces.
    """
    config = KeycloakEventsConfig.from_settings(settings)
    if config is None:
        return SweepCounts()

    since = await _window_start(admin_engine, now=now)
    events = await fetch(config, since=since)

    fetched = written = unresolved = skipped = 0
    for event in events:
        fetched += 1
        if not isinstance(event, dict):
            skipped += 1
            continue

        action = map_event(event)
        if action is None:
            # The common case by a wide margin, and the reason nothing is
            # recorded in the ledger here: a row per login in the realm would
            # make the dedupe table larger than the audit log it protects.
            skipped += 1
            continue

        subject = event.get("userId")
        if not isinstance(subject, str) or not subject:
            unresolved += 1
            continue
        resolved = await _resolve_org(admin_engine, subject)
        if resolved is None:
            unresolved += 1
            continue
        org_id, user_id = resolved

        if await _forward(
            engine=engine,
            org_id=org_id,
            user_id=user_id,
            realm=config.realm,
            action=action,
            event=event,
            now=now,
        ):
            written += 1
        else:
            skipped += 1

    return SweepCounts(
        fetched=fetched, written=written, unresolved=unresolved, skipped=skipped
    )


async def _forward(
    *,
    engine: AsyncEngine,
    org_id: UUID,
    user_id: UUID,
    realm: str,
    action: str,
    event: dict[str, Any],
    now: datetime,
) -> bool:
    """Record the fingerprint and write the audit event, or report a duplicate.

    ONE TRANSACTION, LEDGER FIRST. The insert is `ON CONFLICT DO NOTHING`, so a
    second sweep seeing the same event inserts nothing and this returns False
    before an audit row is written. Both statements share a transaction, so the
    reverse failure -- ledger written, audit write raises -- rolls the ledger
    back and the next sweep retries rather than believing it already forwarded
    an event that never landed.
    """
    fingerprint = event_fingerprint(realm, event)
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        result = await session.execute(
            pg_insert(KeycloakEventIngest)
            # `ingested_at` FROM THE SWEEP'S OWN CLOCK, not the column's
            # `now()` default: `_window_start` reads this column back as the
            # watermark, and a watermark set by the database's wall clock while
            # the window is computed from the caller's would be two clocks
            # deciding one boundary.
            .values(org_id=org_id, fingerprint=fingerprint, ingested_at=now)
            .on_conflict_do_nothing(constraint="uq_keycloak_event_fingerprint")
            # RETURNING rather than `rowcount`: an empty result set is the
            # unambiguous signal that the conflict clause fired, and it is
            # typed. `rowcount` on a DML `Result` is a driver-dependent int the
            # type checker does not believe in.
            .returning(KeycloakEventIngest.id)
        )
        if result.first() is None:
            return False

        await audit_service.record(
            session,
            org_id=org_id,
            # The Keycloak user is the actor: this platform reads USER events,
            # not admin events, so the account these describe is the account
            # that acted on its own credentials.
            actor_type="user",
            actor_id=user_id,
            action=action,
            target_type="user",
            target_id=str(user_id),
            metadata={
                "source": "keycloak",
                "realm": realm,
                "keycloak_user_id": event.get("userId"),
                "keycloak_event_type": event.get("type"),
                # WHEN KEYCLOAK OBSERVED IT, which is not when this row was
                # written. A sweep catching up after downtime writes yesterday's
                # events today, and the row must still say when the credential
                # actually changed.
                "event_time_ms": event.get("time"),
            },
        )
    return True


# --------------------------------------------------------------------------- #
# The status record the probe reads
# --------------------------------------------------------------------------- #

#: Through Redis, for the same reason `core/health/heartbeat.py` is: the sweep
#: runs in the Celery worker and the probe answers in the API process, and the
#: API must not reach into the worker by any other means.
_LAST_SWEEP_KEY = "platform:keycloak_events:last_sweep"

#: NO TTL, deliberately. If this key expired, a worker that has been dead for a
#: day would look identical to a deployment that has never swept -- and worse,
#: an operator reading "never run" would go looking at configuration instead of
#: at the worker. The timestamp inside the record is what makes staleness
#: visible, and it can only do that if the record survives the staleness.
def record_sweep(counts: SweepCounts, *, now: float) -> str:
    """The JSON one sweep leaves behind. Split from the write so the encoding
    is testable without a Redis."""
    return json.dumps(
        {
            "ts": now,
            "fetched": counts.fetched,
            "written": counts.written,
            "unresolved": counts.unresolved,
            "skipped": counts.skipped,
        }
    )


async def write_last_sweep(redis: Any, counts: SweepCounts, *, now: float) -> None:
    await redis.set(_LAST_SWEEP_KEY, record_sweep(counts, now=now))


async def read_last_sweep(redis: Any) -> dict[str, Any] | None:
    """The last run's record, or `None` if this deployment has never swept."""
    raw = await redis.get(_LAST_SWEEP_KEY)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        # A key somebody hand-edited, or a record written by a version that
        # framed it differently. Unreadable and never-written are the same
        # thing to a probe: no evidence the sweep has run.
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = [
    "LOOKBACK_CEILING_DAYS",
    "SWEEP_INTERVAL_S",
    "SweepCounts",
    "read_last_sweep",
    "record_sweep",
    "sweep_once",
    "write_last_sweep",
]
