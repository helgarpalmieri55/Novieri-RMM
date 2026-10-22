"""The retention pass that bounds the three append-mostly IPAM tables
(`docs/modules/ipam/spec.md` §4.2, §2.8, §2.10, §11.3).

**WHAT WAS MISSING WAS NOT A PREDICATE, IT WAS THE PASS.** `observations.py`
told readers that ageing rows "stop being current, and the 365-day retention
pass is what bounds the table", and §4.2 said the same in one line. There was no
retention pass: `ipam_observation` had no delete path anywhere in the
repository, `ipam_drift` kept every resolved finding forever, and a
`sweep_enabled` subnet with no authorization recorded a `refused`
`ipam_sweep_run` on every hourly beat, forever -- 8,760 rows per subnet per
year. Every unit test passed throughout.

WHAT THESE FIXTURES DELIBERATELY VARY:

* **Every deletion test seeds a SURVIVOR alongside the doomed row**, so the
  observable is a list of what is left rather than "the count went down". A
  fixture holding only doomed rows cannot tell "the cutoff is right" from
  "the DELETE has no WHERE clause" -- which is the one bug in a retention pass
  that is catastrophic rather than merely wrong.
* **The boundary is tested from both sides at once**: one row a day older than
  the window and one a day younger, in the same org, in the same call.
* **The drift test pairs a resolved row with an OPEN row of the SAME age.** An
  open finding is a finding, not a record; a pass that aged findings out would
  be the module forgetting a problem nobody fixed, and only the pairing observes
  it.
* **The sweep-run tests always seed at least two runs per subnet**, because the
  rule under test is "never delete the newest for a subnet" and a single-run
  fixture cannot distinguish that from "never delete anything".
* **The cross-org assertions run on the ADMIN engine.** Through `org_session`
  RLS rejects first, so a cross-org test driven that way passes with every
  `org_id` predicate in `retention.py` deleted.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import AuditEvent, IpamDrift, IpamObservation, IpamSweepRun
from app.modules.ipam.retention import (
    DRIFT_RETENTION_DAYS,
    OBSERVATION_RETENTION_DAYS,
    RESTATED_SWEEP_RUN_RETENTION_DAYS,
    RETENTION_INTERVAL_S,
    SWEEP_RUN_RETENTION_DAYS,
    sweep_all_orgs,
    sweep_org,
)
from tests.ipam_fixtures import (
    seed_org,
    seed_subnet,
    seed_sweep_run,
    seed_vrf,
    upsert_observation_row,
)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


class _Fx:
    def __init__(self, org_id: UUID, vrf_id: UUID, subnet_id: UUID) -> None:
        self.org_id = org_id
        self.vrf_id = vrf_id
        self.subnet_id = subnet_id


async def _fx(engine: AsyncEngine, tag: str, prefix: str = "10.70.0.0/24") -> _Fx:
    org = await seed_org(engine, tag)
    vrf_id = await seed_vrf(engine, org.org_id)
    subnet_id = await seed_subnet(
        engine, org.org_id, vrf_id=vrf_id, prefix=prefix
    )
    return _Fx(org.org_id, vrf_id, subnet_id)


async def _seed_drift(
    engine: AsyncEngine,
    fx: _Fx,
    address: str,
    *,
    drift_class: str = "documented_absent",
    resolved_at: datetime | None = None,
) -> UUID:
    """An `ipam_drift` row, seeded directly rather than through `evaluate_org` so
    a retention test is never set up by the reconciler whose output it bounds."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        row = IpamDrift(
            org_id=fx.org_id,
            vrf_id=fx.vrf_id,
            subnet_id=fx.subnet_id,
            address=address,
            drift_class=drift_class,
            severity="medium",
            confidence="medium",
            first_detected_at=NOW - timedelta(days=500),
            last_evaluated_at=resolved_at or NOW,
            resolved_at=resolved_at,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _addresses(engine: AsyncEngine, org_id: UUID) -> list[str]:
    async with AsyncSession(engine) as session:
        rows = (
            await session.execute(
                select(IpamObservation.address)
                .where(IpamObservation.org_id == org_id)
                .order_by(IpamObservation.address)
            )
        ).scalars()
        return [str(row) for row in rows]


async def _drift_addresses(engine: AsyncEngine, org_id: UUID) -> list[str]:
    async with AsyncSession(engine) as session:
        rows = (
            await session.execute(
                select(IpamDrift.address)
                .where(IpamDrift.org_id == org_id)
                .order_by(IpamDrift.address)
            )
        ).scalars()
        return [str(row) for row in rows]


async def _runs(engine: AsyncEngine, org_id: UUID) -> list[tuple[str, datetime]]:
    """`(state, started_at)` per surviving run, oldest first. VALUES rather than
    a count: "which runs survived" is the assertion, and a count cannot tell the
    newest-per-subnet rule from an off-by-one in the window."""
    async with AsyncSession(engine) as session:
        rows = (
            (
                await session.execute(
                    select(IpamSweepRun)
                    .where(IpamSweepRun.org_id == org_id)
                    .order_by(IpamSweepRun.started_at)
                )
            )
            .scalars()
            .all()
        )
        return [(row.state, row.started_at) for row in rows]


async def _sweep(engine: AsyncEngine, org_id: UUID, **kw: Any) -> dict[str, int]:
    """`sweep_org` on the ADMIN engine -- deliberately, so the explicit `org_id`
    predicates are what scopes it and not RLS. See this module's docstring."""
    async with AsyncSession(engine) as session:
        counts = await sweep_org(session, org_id, now=kw.pop("now", NOW), **kw)
        await session.commit()
        return counts


# --------------------------------------------------------------------------- #
# §4.2 -- observations
# --------------------------------------------------------------------------- #


async def test_observations_past_365_days_are_deleted_and_current_ones_are_not(
    admin_engine: AsyncEngine
) -> None:
    """§4.2, the sentence `observations.py` has always pointed at.

    THREE ROWS, ONE DOOMED. *If the pass did not exist* -- which is what shipped
    until now -- this observes all three addresses; *if the DELETE had no
    cutoff* it observes none. Only the middle answer is right, so the assertion
    is the surviving addresses rather than a count.
    """
    fx = await _fx(admin_engine, "obs")
    for address, age in (
        ("10.70.0.10", 400),  # doomed
        ("10.70.0.11", 366),  # doomed: strictly older than the window
        ("10.70.0.12", 3),  # current
    ):
        await upsert_observation_row(
            admin_engine,
            fx.org_id,
            vrf_id=fx.vrf_id,
            address=address,
            now=NOW - timedelta(days=age),
        )

    counts = await _sweep(admin_engine, fx.org_id)

    assert counts["observations"] == 2
    assert await _addresses(admin_engine, fx.org_id) == ["10.70.0.12"]


async def test_the_observation_boundary_is_strictly_older_than_the_cutoff(
    admin_engine: AsyncEngine
) -> None:
    """A row EXACTLY `OBSERVATION_RETENTION_DAYS` old survives; the next tick
    takes it. `<=` at the boundary is a day of evidence an operator was told
    they had.

    Both sides in one call, so the test cannot pass by having no cutoff in
    either direction.
    """
    fx = await _fx(admin_engine, "bound")
    await upsert_observation_row(
        admin_engine, fx.org_id, vrf_id=fx.vrf_id, address="10.70.0.20",
        now=NOW - timedelta(days=OBSERVATION_RETENTION_DAYS),
    )
    await upsert_observation_row(
        admin_engine, fx.org_id, vrf_id=fx.vrf_id, address="10.70.0.21",
        now=NOW - timedelta(days=OBSERVATION_RETENTION_DAYS, seconds=1),
    )

    counts = await _sweep(admin_engine, fx.org_id)

    assert counts["observations"] == 1
    assert await _addresses(admin_engine, fx.org_id) == ["10.70.0.20"]


async def test_the_window_is_on_last_seen_at_not_first_seen_at(
    admin_engine: AsyncEngine
) -> None:
    """§2.7 is a CURRENT-STATE table: `first_seen_at` is "how long this address
    has been known" and §4.3's detail payloads quote it, so an address known for
    three years and seen this morning must not age out.

    *If the cutoff read `first_seen_at`* this observes an empty table.
    """
    fx = await _fx(admin_engine, "firstseen")
    await upsert_observation_row(
        admin_engine, fx.org_id, vrf_id=fx.vrf_id, address="10.70.0.30",
        first_seen_at=NOW - timedelta(days=1000), now=NOW - timedelta(hours=2),
    )

    counts = await _sweep(admin_engine, fx.org_id)

    assert counts["observations"] == 0
    assert await _addresses(admin_engine, fx.org_id) == ["10.70.0.30"]


# --------------------------------------------------------------------------- #
# §2.8 -- drift
# --------------------------------------------------------------------------- #


async def test_resolved_drift_ages_out_and_an_open_finding_of_the_same_age_does_not(
    admin_engine: AsyncEngine
) -> None:
    """An OPEN finding is a finding, not a record. Both rows here were first
    detected 500 days ago; the only difference is whether anybody fixed the
    thing, and that is precisely the difference the pass must turn on.

    *If the predicate were `first_detected_at < cutoff`* this observes an empty
    drift list, and the module would silently forget an unresolved conflict it
    had been reporting for a year and a half.
    """
    fx = await _fx(admin_engine, "drift")
    await _seed_drift(
        admin_engine, fx, "10.70.0.40", resolved_at=NOW - timedelta(days=400)
    )
    await _seed_drift(admin_engine, fx, "10.70.0.41", resolved_at=None)
    await _seed_drift(
        admin_engine, fx, "10.70.0.42", resolved_at=NOW - timedelta(days=10)
    )

    counts = await _sweep(admin_engine, fx.org_id)

    assert counts["drift"] == 1
    assert await _drift_addresses(admin_engine, fx.org_id) == [
        "10.70.0.41",
        "10.70.0.42",
    ]


# --------------------------------------------------------------------------- #
# §2.10 -- sweep runs, and the refused-run growth this bounds
# --------------------------------------------------------------------------- #


async def test_hourly_refusals_age_out_after_thirty_days_and_the_newest_survives(
    admin_engine: AsyncEngine
) -> None:
    """THE UNBOUNDED CASE, at 1/24 scale.

    `handle_sweep_result` is the only writer of `last_swept_at`, so a
    `sweep_enabled` subnet nobody authorized is re-offered by `due_subnet_ids`
    on every beat and records a `refused` run every time -- 8,760 a year, for a
    subnet whose configuration never changes.

    Four hourly refusals here, three of them outside the 30-day window. The
    NEWEST survives whatever its age, which is what keeps the subnet page
    showing "refused -- not authorized" rather than going silent: an empty run
    list means "never swept", a refusal means "we tried and were told no", and
    those must not collapse into one answer.

    *If the newest-per-subnet rule were dropped*, the fifth entry below is not
    seeded and this observes an EMPTY run list for a subnet that is being
    refused once an hour.
    """
    fx = await _fx(admin_engine, "refusals")
    ages_days = [200, 100, 31, 29]
    for age in ages_days:
        await seed_sweep_run(
            admin_engine, fx.org_id, subnet_id=fx.subnet_id, state="refused",
            started_at=NOW - timedelta(days=age), error="not authorized",
        )

    counts = await _sweep(admin_engine, fx.org_id)

    assert counts["sweep_runs"] == 3
    assert await _runs(admin_engine, fx.org_id) == [
        ("refused", NOW - timedelta(days=29))
    ]


async def test_the_newest_run_survives_even_when_it_is_older_than_every_window(
    admin_engine: AsyncEngine
) -> None:
    """A subnet refused two years ago and never re-offered since keeps its last
    refusal. Both rows here are far outside the 30-day window; only the newer
    one survives, so the assertion observes the RULE rather than the window.

    *If the newest-per-subnet rule were dropped* this observes `[]` -- the
    module forgetting that it was ever refused at all.
    """
    fx = await _fx(admin_engine, "ancient")
    await seed_sweep_run(
        admin_engine, fx.org_id, subnet_id=fx.subnet_id, state="no_agent",
        started_at=NOW - timedelta(days=800),
    )
    await seed_sweep_run(
        admin_engine, fx.org_id, subnet_id=fx.subnet_id, state="no_agent",
        started_at=NOW - timedelta(days=700),
    )

    counts = await _sweep(admin_engine, fx.org_id)

    assert counts["sweep_runs"] == 1
    assert await _runs(admin_engine, fx.org_id) == [
        ("no_agent", NOW - timedelta(days=700))
    ]


async def test_a_real_sweep_keeps_a_full_year_while_a_restatement_keeps_thirty_days(
    admin_engine: AsyncEngine
) -> None:
    """THE TWO WINDOWS, observed in one pass on one subnet.

    `succeeded`/`failed` runs are work that happened and are worth paging back
    through; `refused`/`no_agent` are the same configuration fact restated
    hourly, and the hundredth restatement carries nothing the first did not.

    The two doomed rows and the two survivors are interleaved in age -- the
    60-day `succeeded` survives while the 31-day `refused` does not -- so a
    single-window implementation cannot pass this whichever window it picked.
    """
    fx = await _fx(admin_engine, "twowindow")
    seeded = [
        ("succeeded", 400),  # doomed: past the long window
        ("succeeded", 60),  # survives: inside the long window
        ("refused", 31),  # doomed: past the short window
        ("failed", 200),  # survives: inside the long window
        ("dispatched", 1),  # survives, and is the newest
    ]
    for state, age in seeded:
        await seed_sweep_run(
            admin_engine, fx.org_id, subnet_id=fx.subnet_id, state=state,
            started_at=NOW - timedelta(days=age),
        )

    counts = await _sweep(admin_engine, fx.org_id)

    assert counts["sweep_runs"] == 2
    assert await _runs(admin_engine, fx.org_id) == [
        ("failed", NOW - timedelta(days=200)),
        ("succeeded", NOW - timedelta(days=60)),
        ("dispatched", NOW - timedelta(days=1)),
    ]


async def test_a_run_a_surviving_observation_still_points_at_is_not_deleted(
    admin_engine: AsyncEngine
) -> None:
    """`ipam_observation.sweep_run_id` is a real FK with NO `ON DELETE`.

    *If the reference check were dropped* this raises `ForeignKeyViolationError`
    and takes the whole org's pass down -- not a wrong answer, an outage of the
    only thing bounding these tables. The observation here is CURRENT (seen this
    morning) and points at a two-year-old run, which is exactly what the upsert
    produces for an address that keeps answering.
    """
    fx = await _fx(admin_engine, "fk")
    old_run = await seed_sweep_run(
        admin_engine, fx.org_id, subnet_id=fx.subnet_id, state="succeeded",
        started_at=NOW - timedelta(days=700),
    )
    await seed_sweep_run(
        admin_engine, fx.org_id, subnet_id=fx.subnet_id, state="succeeded",
        started_at=NOW - timedelta(days=1),
    )
    await upsert_observation_row(
        admin_engine, fx.org_id, vrf_id=fx.vrf_id, address="10.70.0.50",
        sweep_run_id=old_run, now=NOW - timedelta(hours=4),
    )

    counts = await _sweep(admin_engine, fx.org_id)

    assert counts == {"observations": 0, "drift": 0, "sweep_runs": 0}
    assert len(await _runs(admin_engine, fx.org_id)) == 2


async def test_a_run_and_the_sightings_it_produced_age_out_together(
    admin_engine: AsyncEngine
) -> None:
    """The other half of the FK rule: once the observation is gone the run it
    came from is collectable, in the SAME pass and the same transaction.

    Observations are deleted first for exactly this reason. *If the order were
    reversed* the delete raises against the foreign key.
    """
    fx = await _fx(admin_engine, "together")
    old_run = await seed_sweep_run(
        admin_engine, fx.org_id, subnet_id=fx.subnet_id, state="succeeded",
        started_at=NOW - timedelta(days=700),
    )
    await seed_sweep_run(
        admin_engine, fx.org_id, subnet_id=fx.subnet_id, state="succeeded",
        started_at=NOW - timedelta(days=1),
    )
    await upsert_observation_row(
        admin_engine, fx.org_id, vrf_id=fx.vrf_id, address="10.70.0.51",
        sweep_run_id=old_run, now=NOW - timedelta(days=700),
    )

    counts = await _sweep(admin_engine, fx.org_id)

    assert counts == {"observations": 1, "drift": 0, "sweep_runs": 1}
    assert await _runs(admin_engine, fx.org_id) == [("succeeded", NOW - timedelta(days=1))]


async def test_each_subnet_keeps_its_own_newest_run(
    admin_engine: AsyncEngine
) -> None:
    """"Newest" is PER SUBNET, not per org. Two subnets, each with an ancient
    pair; a rule written per org would leave one subnet with nothing.

    *If the correlation on `subnet_id` were dropped* this observes one surviving
    run instead of two, and one of the two segments would show "never swept".
    """
    fx = await _fx(admin_engine, "persubnet")
    other_subnet = await seed_subnet(
        admin_engine, fx.org_id, vrf_id=fx.vrf_id, prefix="10.71.0.0/24"
    )
    for subnet_id in (fx.subnet_id, other_subnet):
        for age in (800, 600):
            await seed_sweep_run(
                admin_engine, fx.org_id, subnet_id=subnet_id, state="refused",
                started_at=NOW - timedelta(days=age),
            )

    counts = await _sweep(admin_engine, fx.org_id)

    assert counts["sweep_runs"] == 2
    async with AsyncSession(admin_engine) as session:
        rows = (
            (
                await session.execute(
                    select(IpamSweepRun).where(IpamSweepRun.org_id == fx.org_id)
                )
            )
            .scalars()
            .all()
        )
    assert sorted(str(r.subnet_id) for r in rows) == sorted(
        [str(fx.subnet_id), str(other_subnet)]
    )
    assert {r.started_at for r in rows} == {NOW - timedelta(days=600)}


# --------------------------------------------------------------------------- #
# Tenancy (invariant #5)
# --------------------------------------------------------------------------- #


async def test_the_pass_deletes_only_the_named_orgs_rows(
    admin_engine: AsyncEngine
) -> None:
    """ON THE ADMIN ENGINE, where RLS is not doing the work.

    Two orgs, identical fixtures, one call. *If any `org_id` predicate in
    `retention.py` were deleted* the other org's ancient rows go with it -- and
    a test driven through `org_session` would still pass, because RLS rejects
    first. That is why this one is not.
    """
    mine = await _fx(admin_engine, "mine")
    theirs = await _fx(admin_engine, "theirs", prefix="10.90.0.0/24")
    for fx, address in ((mine, "10.70.0.60"), (theirs, "10.90.0.60")):
        await upsert_observation_row(
            admin_engine, fx.org_id, vrf_id=fx.vrf_id, address=address,
            now=NOW - timedelta(days=500),
        )
        await _seed_drift(
            admin_engine, fx, address, resolved_at=NOW - timedelta(days=500)
        )
        for age in (800, 600):
            await seed_sweep_run(
                admin_engine, fx.org_id, subnet_id=fx.subnet_id, state="refused",
                started_at=NOW - timedelta(days=age),
            )

    counts = await _sweep(admin_engine, mine.org_id)

    assert counts == {"observations": 1, "drift": 1, "sweep_runs": 1}
    assert await _addresses(admin_engine, theirs.org_id) == ["10.90.0.60"]
    assert await _drift_addresses(admin_engine, theirs.org_id) == ["10.90.0.60"]
    assert len(await _runs(admin_engine, theirs.org_id)) == 2


# --------------------------------------------------------------------------- #
# The all-orgs shell
# --------------------------------------------------------------------------- #


async def test_the_sweep_visits_every_org_and_returns_the_total(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """TWO orgs, each with something to delete, so "the sweep visited every org"
    is a count of two rather than the presence of one. A single-org fixture
    cannot tell a loop from a call."""
    first = await _fx(admin_engine, "all1")
    second = await _fx(admin_engine, "all2", prefix="10.91.0.0/24")
    for fx, address in ((first, "10.70.0.70"), (second, "10.91.0.70")):
        await upsert_observation_row(
            admin_engine, fx.org_id, vrf_id=fx.vrf_id, address=address,
            now=NOW - timedelta(days=500),
        )

    deleted = await sweep_all_orgs(
        engine=app_engine, admin_engine=admin_engine, now=NOW
    )

    assert deleted >= 2
    assert await _addresses(admin_engine, first.org_id) == []
    assert await _addresses(admin_engine, second.org_id) == []


async def test_the_pass_runs_on_the_app_engine_under_the_tenant_guc(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`ipam_observation` carries FORCE ROW LEVEL SECURITY, so a delete issued
    under the wrong tenant GUC removes nothing. Proven by the row actually
    disappearing through `sweep_all_orgs`, which is the production path and runs
    on the non-BYPASSRLS role."""
    fx = await _fx(admin_engine, "rls")
    await upsert_observation_row(
        admin_engine, fx.org_id, vrf_id=fx.vrf_id, address="10.70.0.80",
        now=NOW - timedelta(days=500),
    )

    await sweep_all_orgs(engine=app_engine, admin_engine=admin_engine, now=NOW)

    assert await _addresses(admin_engine, fx.org_id) == []


async def test_one_bad_org_does_not_stop_the_orgs_behind_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§0.9's containment. The FIRST org visited fails, so the observable is that
    a later one was still swept -- failing the last org would pass against an
    implementation with no containment at all.

    Ordering is not guaranteed by `select(Organization.id)`, so the stub fails
    whichever org it sees first and the assertion is "the other one was cleaned".
    """
    from sqlalchemy.exc import OperationalError

    from app.modules.ipam import retention as module

    first = await _fx(admin_engine, "bad1")
    second = await _fx(admin_engine, "bad2", prefix="10.92.0.0/24")
    for fx, address in ((first, "10.70.0.90"), (second, "10.92.0.90")):
        await upsert_observation_row(
            admin_engine, fx.org_id, vrf_id=fx.vrf_id, address=address,
            now=NOW - timedelta(days=500),
        )

    real = module.sweep_org
    seen: list[UUID] = []

    async def _flaky(session: Any, org_id: UUID, **kw: Any) -> dict[str, int]:
        if org_id in (first.org_id, second.org_id) and not seen:
            seen.append(org_id)
            raise OperationalError("boom", {}, Exception("boom"))
        return await real(session, org_id, **kw)

    monkeypatch.setattr(module, "sweep_org", _flaky)
    await module.sweep_all_orgs(
        engine=app_engine, admin_engine=admin_engine, now=NOW
    )

    assert len(seen) == 1
    survivor = second if seen[0] == first.org_id else first
    assert await _addresses(admin_engine, survivor.org_id) == []


# --------------------------------------------------------------------------- #
# Audit (invariant #4)
# --------------------------------------------------------------------------- #


async def test_a_pass_that_deleted_something_is_audited_with_the_counts(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Deleting evidence is a privileged action. The counts go in the metadata so
    the audit log alone answers "what did retention remove and when"."""
    fx = await _fx(admin_engine, "audit")
    await upsert_observation_row(
        admin_engine, fx.org_id, vrf_id=fx.vrf_id, address="10.70.0.100",
        now=NOW - timedelta(days=500),
    )
    await _seed_drift(
        admin_engine, fx, "10.70.0.101", resolved_at=NOW - timedelta(days=500)
    )

    await sweep_all_orgs(engine=app_engine, admin_engine=admin_engine, now=NOW)

    async with AsyncSession(admin_engine) as session:
        rows = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == fx.org_id)
                )
            )
            .scalars()
            .all()
        )
    assert [row.action for row in rows] == ["ipam.retention_applied"]
    assert rows[0].actor_type == "system"
    assert rows[0].meta["observations"] == 1
    assert rows[0].meta["drift"] == 1
    assert rows[0].meta["sweep_runs"] == 0


async def test_a_pass_that_deleted_nothing_writes_no_audit_row(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Daily, per org, forever. An audit row saying "0" every day for every org
    is the volume §11.3 excludes routine ingestion for, and it would bury the
    passes that did remove something."""
    fx = await _fx(admin_engine, "quiet")
    await upsert_observation_row(
        admin_engine, fx.org_id, vrf_id=fx.vrf_id, address="10.70.0.110",
        now=NOW - timedelta(hours=1),
    )

    await sweep_all_orgs(engine=app_engine, admin_engine=admin_engine, now=NOW)

    async with AsyncSession(admin_engine) as session:
        rows = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == fx.org_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


async def test_an_org_with_no_ipam_rows_is_a_no_op(admin_engine: AsyncEngine) -> None:
    """Most orgs on most days. Three zeros, not an exception."""
    assert await _sweep(admin_engine, uuid4()) == {
        "observations": 0,
        "drift": 0,
        "sweep_runs": 0,
    }


# --------------------------------------------------------------------------- #
# The beat entry -- the thing that causes any of this to run
# --------------------------------------------------------------------------- #


def test_the_retention_beat_entry_is_registered(pg_cluster: Any, tmp_path: Any) -> None:
    """THE ENTRY IS THE FINDING. Every predicate above can be perfect and, with
    no beat entry, nothing is ever deleted and no test fails -- which is exactly
    the state this module shipped in. *If the entry were missing* this observes
    a `KeyError`.

    The interval is read from the module rather than repeated as a literal: a
    test carrying its own copy of 86400.0 passes when the constant changes and
    the beat entry does not.
    """
    from app.core.config import Settings, override_settings
    from app.worker import create_celery

    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
    )
    with override_settings(settings):
        schedule = create_celery().conf.beat_schedule

    entry = schedule["ipam-retention"]
    assert entry["task"] == "app.worker.ipam_retention"
    assert entry["schedule"] == RETENTION_INTERVAL_S
    assert RETENTION_INTERVAL_S == 86400.0


def test_the_worker_exposes_the_retention_shell() -> None:
    from app import worker

    assert callable(worker.ipam_retention_all_orgs)


def test_the_windows_are_the_ones_the_spec_and_the_docstrings_name() -> None:
    """§4.2 says 365 days for observations, in one line, and `observations.py`
    quotes it. The two shorter/longer numbers are this module's decisions and are
    pinned here so a change to either is a change somebody made on purpose."""
    assert OBSERVATION_RETENTION_DAYS == 365
    assert DRIFT_RETENTION_DAYS == 365
    assert SWEEP_RUN_RETENTION_DAYS == 365
    assert RESTATED_SWEEP_RUN_RETENTION_DAYS == 30
