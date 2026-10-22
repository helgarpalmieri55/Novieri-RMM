"""The drain: `capture.drained()` -> Postgres, and retention
(docs/superpowers/specs/2026-08-15-error-monitoring-design.md §3.1, §4, §4.2).

Recording is asynchronous and bounded (spec §3.1): capture writes to the
bounded in-process queue in `app.core.errors.capture`; this module is what a
periodic background task (Task 3) calls to empty it. ONE TRANSACTION PER
DRAIN, NOT PER ERROR -- that is the whole point of the queue: a burst of ten
thousand errors must not open ten thousand transactions, and an error in the
request path must not add a database round trip to a request that is already
failing.

Grouping happens twice, for different reasons. `capture_exception`
fingerprints the RAISE site so two `ValueError`s from the same line are one
group (spec §2.1) no matter how many callers capture concurrently. This
module groups the DRAINED BATCH by that same fingerprint again, in Python,
before any of it reaches SQL -- so a burst of a thousand identical failures
becomes one `INSERT ... ON CONFLICT DO UPDATE` per distinct fingerprint, each
bumping `event_count` by however many of that fingerprint the batch held,
rather than a thousand statements serializing against the same row.

Neither table stores exception text -- see `app.core.errors.capture` for the
invariant this drain inherits and does not touch.
"""

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors.capture import CapturedError, drained
from app.models.platform_error import PlatformErrorEvent, PlatformErrorGroup

logger = logging.getLogger(__name__)

# spec §4.2. Events beyond this are pruned; groups never are -- `first_seen`
# and `event_count` are the history worth having, and a group carries no
# payload to reclaim.
EVENT_RETENTION_DAYS = 30

# Task 3: how often the API's background task and the worker's beat entry
# each call `drain_once`. A MODULE CONSTANT, not a literal repeated at both
# call sites, for the same reason every other sweep interval in this
# codebase is (`health_sweep_module.ACTION_INTERVAL_S`, `ipam_sweep_module.
# SWEEP_DISPATCH_INTERVAL_S`, ...): a literal in `app/main.py` and a literal
# in `app/worker.py` can drift from each other with nothing to notice.
# `capture`'s queue is bounded at `QUEUE_MAX` (1000) and drops the newest
# arrival on overflow, so this needs to be short enough that a burst does not
# sit queued for long -- "a few seconds", not the 30s-86400s range the other
# sweeps in `app/worker.py` use, none of which are draining a bounded,
# drop-on-overflow queue.
DRAIN_INTERVAL_S = 5.0


def _parse_uuid(value: str | None) -> UUID | None:
    """Best-effort parse of a correlation contextvar into the `uuid` column
    spec §4 declares for `org_id`/`user_id`/`job_id`/`device_id`. Returns
    `None` for anything that is not a UUID rather than raising -- a drain
    that could crash on a malformed correlation id would turn a monitoring
    detail into an outage, the same property `capture_exception` itself
    guards (spec §3). `request_id` needs no such parse: its column is `text`
    (it is `uuid.uuid4().hex`, not a value anything joins on structurally).
    """
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError:
        logger.warning("dropping unparseable correlation id on drain")
        return None


async def drain_once(session: AsyncSession) -> int:
    """Pop everything `capture`'s queue holds, upsert one group row per
    distinct fingerprint in the batch, bulk-insert one event row per capture,
    and commit once.

    Returns the number of events written -- 0 if the queue was empty, and no
    transaction is opened in that case.
    """
    items = drained()
    if not items:
        return 0

    now = datetime.now(UTC)
    by_fingerprint: dict[str, list[CapturedError]] = defaultdict(list)
    for item in items:
        by_fingerprint[item.fingerprint].append(item)

    group_values = [
        {
            "fingerprint": fingerprint,
            "fingerprint_version": batch[0].fingerprint_version,
            "exc_type": batch[0].exc_type,
            "module": batch[0].module,
            "lineno": batch[0].lineno,
            "qualname": batch[0].qualname,
            "where_": batch[0].where,
            "first_seen": now,
            "last_seen": now,
            "event_count": len(batch),
        }
        for fingerprint, batch in by_fingerprint.items()
    ]
    insert_stmt = pg_insert(PlatformErrorGroup).values(group_values)
    upsert = insert_stmt.on_conflict_do_update(
        index_elements=["fingerprint"],
        set_={
            "last_seen": insert_stmt.excluded.last_seen,
            # ADDS to the existing count; does not replace it. `first_seen`
            # is deliberately absent from `set_` -- an existing group's first
            # occurrence must not move just because a later batch touches
            # its row. So is `fingerprint_version`, and for a stronger reason:
            # a conflict here means an existing row already carries this exact
            # digest, and the version is part of what was hashed to produce it
            # (`fingerprint_of`), so the versions necessarily agree. Putting
            # it in `set_` could only ever relabel a row -- claim a v1 group
            # was computed by the v2 rule -- which is the one lie this column
            # exists to prevent.
            "event_count": PlatformErrorGroup.event_count + insert_stmt.excluded.event_count,
        },
    ).returning(PlatformErrorGroup.id, PlatformErrorGroup.fingerprint)
    result = await session.execute(upsert)
    group_id_by_fingerprint = {row.fingerprint: row.id for row in result}

    events = [
        PlatformErrorEvent(
            group_id=group_id_by_fingerprint[item.fingerprint],
            occurred_at=now,
            request_id=item.request_id,
            org_id=_parse_uuid(item.org_id),
            user_id=_parse_uuid(item.user_id),
            job_id=_parse_uuid(item.job_id),
            device_id=_parse_uuid(item.device_id),
        )
        for item in items
    ]
    session.add_all(events)
    await session.commit()
    return len(items)


async def prune_error_events(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Delete `platform_error_event` rows older than `EVENT_RETENTION_DAYS`
    (spec §4.2).

    GROUPS ARE NEVER DELETED HERE. `first_seen`/`event_count` are the history
    worth keeping, and a group carries no payload -- pruning it would erase a
    resolved incident's record for no storage saved. A row exactly
    `EVENT_RETENTION_DAYS` old survives; the boundary is strictly
    older-than, matching `app/modules/ipam/retention.py`'s convention.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=EVENT_RETENTION_DAYS)
    result = await session.execute(
        delete(PlatformErrorEvent).where(PlatformErrorEvent.occurred_at < cutoff)
    )
    await session.commit()
    # `rowcount` off a DELETE -- see `app/modules/ipam/retention.py::_deleted`
    # for why this is narrowed with `getattr` rather than typed directly.
    return int(getattr(result, "rowcount", 0) or 0)
