"""The drain: `capture.drained()` -> Postgres, and retention
(docs/superpowers/specs/2026-08-15-error-monitoring-design.md §3.1, §4, §4.2).

FIXTURE PATTERN, same rationale as `test_platform_health_sampler.py`: no
`db_session` fixture exists in `tests/conftest.py`, so this file builds one
locally over `app_engine` -- deliberately, not `admin_engine`, since
`platform_error_group`/`platform_error_event`'s only RLS policy grants
`app_user` (migration 0056) and `admin_engine` is `BYPASSRLS`; writing
through it would pass even if the app-role policy were wrong.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.errors.capture import FINGERPRINT_VERSION, CapturedError, drained
from app.core.errors.store import EVENT_RETENTION_DAYS, drain_once, prune_error_events
from app.models import PlatformErrorEvent, PlatformErrorGroup


@pytest.fixture()
async def db_session(app_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(app_engine, expire_on_commit=False) as session:
        yield session


def _captured(
    *,
    fingerprint: str = "abc123",
    fingerprint_version: int = FINGERPRINT_VERSION,
    exc_type: str = "ValueError",
    module: str = "app.modules.inventory.rules",
    lineno: int = 94,
    qualname: str = "evaluate_rules",
    where: str = "api",
    request_id: str | None = None,
    org_id: str | None = None,
    user_id: str | None = None,
    job_id: str | None = None,
    device_id: str | None = None,
) -> CapturedError:
    return CapturedError(
        fingerprint=fingerprint,
        fingerprint_version=fingerprint_version,
        exc_type=exc_type,
        module=module,
        lineno=lineno,
        qualname=qualname,
        where=where,
        request_id=request_id,
        org_id=org_id,
        user_id=user_id,
        job_id=job_id,
        device_id=device_id,
    )


async def _enqueue_directly(*items: CapturedError) -> None:
    """Populate `capture`'s module-level queue without going through
    `capture_exception` -- these tests are about the drain, not fingerprinting,
    so it is `drain_once`'s only real input (`capture.drained()`) that
    matters here."""
    import app.core.errors.capture as capture_mod

    with capture_mod._lock:
        capture_mod._queue.extend(items)


async def test_drain_once_returns_zero_and_opens_no_transaction_for_an_empty_queue(
    db_session: AsyncSession,
) -> None:
    drained()  # ensure empty
    assert await drain_once(db_session) == 0


async def test_drain_once_writes_one_group_and_one_event(db_session: AsyncSession) -> None:
    drained()
    org = str(uuid4())
    await _enqueue_directly(_captured(fingerprint="fp-1", request_id="req-1", org_id=org))

    written = await drain_once(db_session)
    assert written == 1

    group = (
        await db_session.execute(
            select(PlatformErrorGroup).where(PlatformErrorGroup.fingerprint == "fp-1")
        )
    ).scalar_one()
    assert group.exc_type == "ValueError"
    assert group.module == "app.modules.inventory.rules"
    assert group.lineno == 94
    assert group.qualname == "evaluate_rules"
    assert group.where_ == "api"
    assert group.fingerprint_version == FINGERPRINT_VERSION
    assert group.event_count == 1
    assert group.first_seen == group.last_seen
    assert group.resolved_at is None

    event = (
        await db_session.execute(
            select(PlatformErrorEvent).where(PlatformErrorEvent.group_id == group.id)
        )
    ).scalar_one()
    assert event.request_id == "req-1"
    assert event.org_id == UUID(org)


async def test_a_burst_of_the_same_fingerprint_is_one_group_row_with_a_batched_count(
    db_session: AsyncSession,
) -> None:
    """spec §2.1 / falsifier #3: grouping must not split one failure into a
    group per occurrence."""
    drained()
    await _enqueue_directly(*[_captured(fingerprint="fp-burst") for _ in range(50)])

    written = await drain_once(db_session)
    assert written == 50

    groups = (
        await db_session.execute(
            select(PlatformErrorGroup).where(PlatformErrorGroup.fingerprint == "fp-burst")
        )
    ).scalars().all()
    assert len(groups) == 1
    assert groups[0].event_count == 50

    events = (
        await db_session.execute(
            select(PlatformErrorEvent).where(PlatformErrorEvent.group_id == groups[0].id)
        )
    ).scalars().all()
    assert len(events) == 50


async def test_a_second_occurrence_updates_the_group_in_place(db_session: AsyncSession) -> None:
    """THE LOAD-BEARING CASE (brief): the group row is UPDATEd, not just
    INSERTed -- `event_count` accumulates and `last_seen` moves, while
    `first_seen` does not."""
    drained()
    await _enqueue_directly(_captured(fingerprint="fp-repeat"))
    await drain_once(db_session)

    first = (
        await db_session.execute(
            select(PlatformErrorGroup).where(PlatformErrorGroup.fingerprint == "fp-repeat")
        )
    ).scalar_one()
    original_first_seen = first.first_seen
    original_id = first.id

    await _enqueue_directly(_captured(fingerprint="fp-repeat"), _captured(fingerprint="fp-repeat"))
    await drain_once(db_session)

    # `populate_existing`: `drain_once`'s upsert is a Core statement the
    # identity map knows nothing about, so the `first` object already loaded
    # above -- same session, same PK -- would otherwise come back unchanged
    # (see `app/modules/health/settings.py` for the same gotcha).
    second = (
        await db_session.execute(
            select(PlatformErrorGroup)
            .where(PlatformErrorGroup.fingerprint == "fp-repeat")
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert second.id == original_id, "same fingerprint must upsert the same row, not a new one"
    assert second.event_count == 3
    assert second.first_seen == original_first_seen
    assert second.last_seen >= original_first_seen

    all_events = (
        await db_session.execute(
            select(PlatformErrorEvent).where(PlatformErrorEvent.group_id == original_id)
        )
    ).scalars().all()
    assert len(all_events) == 3


async def test_a_group_written_without_a_version_reads_as_version_one(
    db_session: AsyncSession,
) -> None:
    """WHAT THE DEFAULT MEANS. Every row that predates migration 0069 was
    produced by the version-1 rule -- the traceback's last frame -- and there
    is no backfill that could say otherwise: this table keeps the derived site
    and never the traceback, so the application frame a v1 group should have
    been named after is gone. `1` is therefore a fact about those rows, not a
    placeholder, which is why the column is NOT NULL with a default rather
    than nullable."""
    group = PlatformErrorGroup(
        fingerprint="fp-legacy",
        exc_type="ValueError",
        module="asyncpg.connection",
        lineno=1401,
        qualname="Connection.fetch",
        where_="worker",
        first_seen=datetime.now(UTC),
        last_seen=datetime.now(UTC),
        event_count=1,
    )
    db_session.add(group)
    await db_session.commit()

    reloaded = (
        await db_session.execute(
            select(PlatformErrorGroup).where(PlatformErrorGroup.fingerprint == "fp-legacy")
        )
    ).scalar_one()
    assert reloaded.fingerprint_version == 1


async def test_distinct_fingerprints_produce_distinct_groups(db_session: AsyncSession) -> None:
    drained()
    await _enqueue_directly(_captured(fingerprint="fp-a"), _captured(fingerprint="fp-b"))
    await drain_once(db_session)

    fingerprints = set(
        (
            await db_session.execute(
                select(PlatformErrorGroup.fingerprint).where(
                    PlatformErrorGroup.fingerprint.in_(["fp-a", "fp-b"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert fingerprints == {"fp-a", "fp-b"}


async def test_an_unparseable_correlation_id_is_dropped_to_none_not_raised(
    db_session: AsyncSession,
) -> None:
    """A drain that could crash on a malformed correlation id would turn a
    monitoring detail into an outage -- the same property `capture_exception`
    itself guards (spec §3)."""
    drained()
    await _enqueue_directly(_captured(fingerprint="fp-bad-id", org_id="not-a-uuid"))

    written = await drain_once(db_session)
    assert written == 1

    event = (
        await db_session.execute(
            select(PlatformErrorEvent).join(PlatformErrorGroup).where(
                PlatformErrorGroup.fingerprint == "fp-bad-id"
            )
        )
    ).scalar_one()
    assert event.org_id is None


async def test_prune_deletes_events_beyond_retention_and_keeps_the_group(
    db_session: AsyncSession,
) -> None:
    """spec §4.2: events are pruned, groups never are -- `first_seen` and
    `event_count` are the history worth keeping."""
    drained()
    now = datetime.now(UTC)
    old = now - timedelta(days=EVENT_RETENTION_DAYS + 1)
    recent = now - timedelta(days=1)

    await _enqueue_directly(_captured(fingerprint="fp-retention"))
    await drain_once(db_session)
    group = (
        await db_session.execute(
            select(PlatformErrorGroup).where(PlatformErrorGroup.fingerprint == "fp-retention")
        )
    ).scalar_one()

    # `drain_once` already wrote one event stamped with its own `now()`
    # (recent, and slightly after this test's `now`). Add one more backdated
    # past the window and one more explicitly recent, so three events exist:
    # two recent survivors and one that must go.
    db_session.add(PlatformErrorEvent(group_id=group.id, occurred_at=old))
    db_session.add(PlatformErrorEvent(group_id=group.id, occurred_at=recent))
    await db_session.commit()

    deleted = await prune_error_events(db_session, now=now)
    assert deleted == 1  # only the backdated one

    remaining = (
        await db_session.execute(
            select(PlatformErrorEvent).where(PlatformErrorEvent.group_id == group.id)
        )
    ).scalars().all()
    assert len(remaining) == 2
    assert all(e.occurred_at >= now - timedelta(days=EVENT_RETENTION_DAYS) for e in remaining)

    survivor = (
        await db_session.execute(
            select(PlatformErrorGroup).where(PlatformErrorGroup.id == group.id)
        )
    ).scalar_one()
    assert survivor.event_count == 1, "the group row itself is never pruned"


async def test_a_row_exactly_at_the_retention_boundary_survives(db_session: AsyncSession) -> None:
    """Strictly older-than, matching `app/modules/ipam/retention.py`'s
    convention -- a row exactly `EVENT_RETENTION_DAYS` old is not yet taken."""
    drained()
    now = datetime.now(UTC)
    await _enqueue_directly(_captured(fingerprint="fp-boundary"))
    await drain_once(db_session)
    group = (
        await db_session.execute(
            select(PlatformErrorGroup).where(PlatformErrorGroup.fingerprint == "fp-boundary")
        )
    ).scalar_one()

    exactly_at_cutoff = now - timedelta(days=EVENT_RETENTION_DAYS)
    (await db_session.execute(select(PlatformErrorEvent))).scalars().all()
    event = (
        await db_session.execute(
            select(PlatformErrorEvent).where(PlatformErrorEvent.group_id == group.id)
        )
    ).scalar_one()
    event.occurred_at = exactly_at_cutoff
    await db_session.commit()

    deleted = await prune_error_events(db_session, now=now)
    assert deleted == 0
