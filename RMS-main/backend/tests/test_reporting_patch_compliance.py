"""`patch-compliance` (spec §5.2, §2, §6.4, §12.5, §12.13).

**THE LOAD-BEARING TEST IS `test_a_never_scanned_device_is_never_counted_as_
compliant` (§12.13).** Everything else here is ordinary coverage; that one is the
place §2's no-inference rule is most tempting to break, and it is written so the
two ways of breaking it produce a DIFFERENT NUMBER rather than a different
diagnosis:

* make the `device_patch_status` join INNER and the never-scanned device vanishes
  from the report entirely -- `totals.devices` reads 2 instead of 3;
* treat "no rows" as "nothing missing" (drop the `scan_state != 'never'` filter
  from `devices_with_zero_missing`) and it reads 2 instead of 1.

**THE OTHER LOAD-BEARING TEST IS `test_a_settled_device_is_dated_by_its_scan_
not_by_its_oldest_patch_row`.** `last_scan_at` was
`max(device_patch_status.detected_at)` until 2026-08-21, which is stamped when a
status row is INSERTED and never rewritten -- so a device whose patch set had
not changed since enrolment was reported as last scanned on the day it was
enrolled, however often it had been measured since. Its inverse sits beside it
(`test_patch_rows_are_not_a_scan_and_a_device_without_one_stays_never`), because
a fix that answered `now()` to everything would pass the first test alone.

**TWO ORGS WITH DELIBERATELY OVERLAPPING DATA, AND THE APPLICATION PREDICATE
PINNED ON THE ADMIN ENGINE.** Same hostnames, same OS family, the same patch
identifiers at the same severity, approvals in the same ring. A cross-org test
driven only through `org_session` passes even when `Device.org_id == org_id` has
been deleted, because RLS rejects first; the admin-engine test is the only place
that predicate is observable.
"""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.tenancy import admin_session, org_session
from app.modules.reporting.definitions.patch_compliance import (
    _DEFAULT_RING,
    _DEFAULT_SCAN_INTERVAL_HOURS,
    _OUTSTANDING_STATES,
    _SECURITY_SEVERITIES,
    PatchComplianceParams,
)
from app.modules.reporting.registry import REGISTRY
from app.modules.reporting.service import execute_report
from tests.reporting_fixtures import (
    reporting_user,
    seed_device,
    seed_device_group,
    seed_patch,
    seed_patch_approval,
    seed_patch_policy,
    seed_patch_scan_job,
    seed_patch_status,
)

KEY = "patch-compliance"
# `jobs.read` joined this list on 2026-08-21 with `last_scan_at`'s move onto
# the `jobs` table. It is not a widening of what a caller may see -- the jobs
# API gates the same rows on the same code -- and every seeded role holding
# `patch.read` already holds it (migration 0003 grants it to technician,
# auditor and read-only; 0011 grants them `patch.read`).
CODES = ("reporting.read", "devices.read", "patch.read", "jobs.read")


def _definition() -> Any:
    return REGISTRY[KEY]


async def _run(api_env: Any, org_id: UUID, **params: Any) -> Any:
    async with org_session(org_id, engine=api_env.app_engine) as session:
        return await execute_report(
            session, _definition(), PatchComplianceParams(**params), org_id=org_id
        )


# --------------------------------------------------------------------------- #
# The restated predicates are pinned copies with a drift alarm, not guesses.
# --------------------------------------------------------------------------- #


async def test_the_restated_patch_module_constants_still_match() -> None:
    """§1.3 has reporting read tables and not module code, so the outstanding
    states, the security severities and the policy defaults are RESTATED here.
    A restatement without a drift alarm is a guess; this is the alarm.

    If the patch module widens `NON_COMPLIANT_STATES` (say, a new `pending`
    state) and this file does not follow, `missing_approved` silently stops
    counting a whole class of unapplied patch -- a wrong compliance number,
    which is the worst defect this module can ship.
    """
    from app.modules.patch.ingest import AUTO_APPROVE_SEVERITIES
    from app.modules.patch.policy import ResolvedPatchPolicy
    from app.modules.patch.reporting import NON_COMPLIANT_STATES

    assert _OUTSTANDING_STATES == NON_COMPLIANT_STATES
    assert _SECURITY_SEVERITIES == AUTO_APPROVE_SEVERITIES
    assert _DEFAULT_RING == ResolvedPatchPolicy().ring_name
    assert _DEFAULT_SCAN_INTERVAL_HOURS == ResolvedPatchPolicy().scan_interval_hours


async def test_the_definition_requires_devices_and_patch_and_jobs() -> None:
    """§5.2 printed `{devices.read, patch.read}`, and the third code is a
    deliberate amendment recorded in the spec: `last_scan_at` is read from
    `jobs`, so `jobs.read` is the honest requirement rather than a table read
    under a gate that does not govern it (§0.4)."""
    assert _definition().requires == frozenset(
        {"devices.read", "patch.read", "jobs.read"}
    )


async def test_the_restated_succeeded_scan_criteria_still_match() -> None:
    """The second drift alarm, and it guards a NUMBER AN AUDITOR READS.

    `_scan_criteria` is a restatement of the patch module's
    `_succeeded_scan_criteria` (§1.3 again: reporting reads tables, it does not
    import module code, and adding a reporting-shaped helper to the patch
    module would be reporting adding to a module, which §1.3 also forbids).
    Two statements answering "when was this machine last measured" are free to
    drift, and the console and this report disagreeing about that is exactly
    the failure the sweep shipped for a phase.

    COMPILED, WITH BINDS COMPARED, because the SQL text alone would not move if
    the patch module started matching `patch.rescan`: the shape would be
    identical and only the bound value would differ.
    """
    from sqlalchemy import and_
    from sqlalchemy.dialects import postgresql

    from app.models import Device
    from app.modules.patch.reporting import _succeeded_scan_criteria
    from app.modules.reporting.definitions.patch_compliance import _scan_criteria

    org_id = uuid4()

    def rendered(criteria: Any) -> tuple[str, Any]:
        compiled = and_(*criteria).compile(dialect=postgresql.dialect())
        return str(compiled), compiled.params

    assert rendered(_scan_criteria(org_id, Device)) == rendered(
        _succeeded_scan_criteria(org_id)
    )


# --------------------------------------------------------------------------- #
# WHERE `last_scan_at` COMES FROM. `max(device_patch_status.detected_at)` was
# the answer here until 2026-08-21 and it is not a scan timestamp at all.
# --------------------------------------------------------------------------- #


async def test_a_settled_device_is_dated_by_its_scan_not_by_its_oldest_patch_row(
    api_env: Any,
) -> None:
    """THE REGRESSION TEST. A device whose patch set has not changed since
    enrolment, scanned an hour ago, must report an hour ago.

    `detected_at` is stamped when a status ROW IS FIRST CREATED and no update
    path in `patch/ingest.py` ever rewrites it, so on this device -- the
    ordinary case of a machine that is fully patched and staying that way --
    `max(detected_at)` is frozen at enrolment. Put the old expression back and
    this reads ~90 days and `stale`: a settled fleet reported to an auditor as
    last measured on the day it was bought.
    """
    org_id = await api_env.create_org("rep-patch-settled")
    device = await seed_device(api_env.admin_engine, org_id, hostname="settled")
    patch_id = await seed_patch(api_env.admin_engine, org_id, identifier="KB-SETTLED")
    await seed_patch_status(
        api_env.admin_engine, org_id, device.device_id, patch_id,
        state="installed", detected_days_ago=90, applied_days_ago=90,
    )
    await seed_patch_scan_job(api_env.admin_engine, org_id, device.device_id, hours_ago=1)

    page = await _run(api_env, org_id)
    (row,) = page.rows
    assert row.last_scan_at is not None
    age_hours = (page.meta.generated_at - row.last_scan_at).total_seconds() / 3600
    assert 0.5 <= age_hours <= 2, f"{age_hours}h -- this is the patch row's age, not a scan"
    assert row.scan_state == "fresh"
    assert page.totals["devices_never_scanned"] == 0
    assert page.totals["devices_with_zero_missing"] == 1


async def test_patch_rows_are_not_a_scan_and_a_device_without_one_stays_never(
    api_env: Any,
) -> None:
    """THE INVERSE, so the fix cannot pass by answering `now()` to everything.

    A device carrying `device_patch_status` rows detected yesterday but with no
    succeeded `patch.scan` job has NOT been shown to have completed a scan, and
    must not acquire a date from the rows. It sits beside a device that HAS
    one, so this is a difference between two devices in one report rather than
    a query that returns nothing.
    """
    org_id = await api_env.create_org("rep-patch-rows-are-not-scans")
    patch_id = await seed_patch(api_env.admin_engine, org_id, identifier="KB-NOSCAN")
    rows_only = await seed_device(api_env.admin_engine, org_id, hostname="a-rows-only")
    scanned = await seed_device(api_env.admin_engine, org_id, hostname="b-scanned")
    for device_id in (rows_only.device_id, scanned.device_id):
        await seed_patch_status(
            api_env.admin_engine, org_id, device_id, patch_id,
            state="not_applicable", detected_days_ago=1,
        )
    await seed_patch_scan_job(api_env.admin_engine, org_id, scanned.device_id, hours_ago=1)

    page = await _run(api_env, org_id)
    by_id = {row.device_id: row for row in page.rows}
    assert by_id[rows_only.device_id].last_scan_at is None
    assert by_id[rows_only.device_id].scan_state == "never"
    assert by_id[scanned.device_id].last_scan_at is not None
    assert by_id[scanned.device_id].scan_state == "fresh"
    # And `never` is still excluded from the compliance numerator: both devices
    # have zero outstanding patches, and only the measured one counts.
    assert page.totals == {
        "devices": 2,
        "devices_with_zero_missing": 1,
        "devices_never_scanned": 1,
    }


async def test_a_failed_scan_measured_nothing_and_dates_nothing(api_env: Any) -> None:
    """`Job.status == succeeded` pinned on its own. `JobEngine.complete` flips a
    job to `succeeded` only after the patch module's ingest handler returns
    without raising, so that status is what distinguishes "a scan ran and its
    document landed" from "a scan was attempted". Drop the status predicate and
    the failed device reads `fresh`.
    """
    org_id = await api_env.create_org("rep-patch-failed-scan")
    failed = await seed_device(api_env.admin_engine, org_id, hostname="a-failed")
    succeeded = await seed_device(api_env.admin_engine, org_id, hostname="b-succeeded")
    await seed_patch_scan_job(
        api_env.admin_engine, org_id, failed.device_id, hours_ago=1, succeeded=False
    )
    await seed_patch_scan_job(
        api_env.admin_engine, org_id, succeeded.device_id, hours_ago=1
    )

    page = await _run(api_env, org_id)
    by_id = {row.device_id: row for row in page.rows}
    assert by_id[failed.device_id].scan_state == "never"
    assert by_id[failed.device_id].last_scan_at is None
    assert by_id[succeeded.device_id].scan_state == "fresh"


async def test_the_newest_scan_wins_and_an_older_one_does_not_win_it_back(
    api_env: Any,
) -> None:
    """`max(finished_at)`, over a device with a history. Three succeeded scans;
    the report reads the newest. A `min` or a first-row read would report the
    oldest and still return a plausible timestamp.
    """
    org_id = await api_env.create_org("rep-patch-newest-scan")
    device = await seed_device(api_env.admin_engine, org_id, hostname="veteran")
    for hours_ago in (72, 2, 25):
        await seed_patch_scan_job(
            api_env.admin_engine, org_id, device.device_id, hours_ago=hours_ago
        )

    page = await _run(api_env, org_id)
    (row,) = page.rows
    assert row.last_scan_at is not None
    age_hours = (page.meta.generated_at - row.last_scan_at).total_seconds() / 3600
    assert 1.5 <= age_hours <= 3, age_hours


async def test_a_neighbour_orgs_scan_cannot_date_this_orgs_device(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    """GLOBAL CONSTRAINT 4, on the new join. `jobs.target_id` is a bare UUID
    with NO foreign key, so a job in org B can legitimately carry org A's
    device id -- there is no constraint stopping it, which is why the org
    predicate is the only thing that can.

    Seeded on the admin (RLS-bypassing) engine, because RLS would reject org
    B's rows before `Job.org_id == org_id` was ever consulted, and this is the
    only place that predicate is observable. Org B's own device IS dated by its
    own scan of the same age, so the assertion is a difference between two
    tenants rather than a query returning nothing.
    """
    org_a = await api_env.create_org("rep-patch-scan-a")
    org_b = await api_env.create_org("rep-patch-scan-b")
    device_a = await seed_device(api_env.admin_engine, org_a, hostname="shared-host")
    device_b = await seed_device(api_env.admin_engine, org_b, hostname="shared-host")
    # Org B holds a succeeded scan naming ORG A's device, an hour old.
    await seed_patch_scan_job(api_env.admin_engine, org_b, device_a.device_id, hours_ago=1)
    await seed_patch_scan_job(api_env.admin_engine, org_b, device_b.device_id, hours_ago=1)

    async with admin_session(engine=admin_engine) as session:
        page_a = await execute_report(
            session, _definition(), PatchComplianceParams(), org_id=org_a
        )
        page_b = await execute_report(
            session, _definition(), PatchComplianceParams(), org_id=org_b
        )

    (row_a,) = page_a.rows
    assert row_a.device_id == device_a.device_id
    assert row_a.last_scan_at is None, "org B's job dated org A's device"
    assert row_a.scan_state == "never"
    assert page_a.totals["devices_never_scanned"] == 1

    (row_b,) = page_b.rows
    assert row_b.device_id == device_b.device_id
    assert row_b.scan_state == "fresh", "org B's own scan must still date org B's device"


# --------------------------------------------------------------------------- #
# §12.13: an unscanned device is never counted as compliant.
# --------------------------------------------------------------------------- #


async def test_a_never_scanned_device_is_never_counted_as_compliant(
    api_env: Any,
) -> None:
    """THREE devices: one compliant, one with an outstanding approved patch, one
    with NO `device_patch_status` row at all.

    `devices_with_zero_missing` must count EXACTLY the first. Falsify either way
    described in the module docstring and the number changes.
    """
    org_id = await api_env.create_org("rep-patch-never")
    patch_id = await seed_patch(api_env.admin_engine, org_id, identifier="KB-1")
    await seed_patch_approval(api_env.admin_engine, org_id, patch_id, ring_name=_DEFAULT_RING)

    compliant = await seed_device(api_env.admin_engine, org_id, hostname="a-compliant")
    await seed_patch_status(
        api_env.admin_engine, org_id, compliant.device_id, patch_id, state="installed",
        applied_days_ago=1,
    )
    exposed = await seed_device(api_env.admin_engine, org_id, hostname="b-exposed")
    await seed_patch_status(
        api_env.admin_engine, org_id, exposed.device_id, patch_id, state="missing"
    )
    never = await seed_device(api_env.admin_engine, org_id, hostname="c-never")
    # The two measured devices carry the succeeded scan that says so. Their
    # status rows never did: a `device_patch_status` row records that a PATCH
    # was found, and this test's subject is the difference between a device
    # that has been measured and one that has not.
    for device_id in (compliant.device_id, exposed.device_id):
        await seed_patch_scan_job(api_env.admin_engine, org_id, device_id, hours_ago=1)

    page = await _run(api_env, org_id)
    by_id = {row.device_id: row for row in page.rows}
    # The never-scanned device is PRESENT -- an inner join would drop it.
    assert set(by_id) == {compliant.device_id, exposed.device_id, never.device_id}

    assert by_id[never.device_id].scan_state == "never"
    assert by_id[never.device_id].last_scan_at is None
    assert by_id[never.device_id].missing_approved == 0
    assert by_id[compliant.device_id].scan_state == "fresh"
    assert by_id[compliant.device_id].missing_approved == 0
    assert by_id[exposed.device_id].missing_approved == 1

    assert page.totals == {
        "devices": 3,
        # ONE, not two. The never-scanned device's zero is absence of a record.
        "devices_with_zero_missing": 1,
        "devices_never_scanned": 1,
    }


# --------------------------------------------------------------------------- #
# The approval predicate -- the patch module's, reproduced.
# --------------------------------------------------------------------------- #


async def test_only_patches_approved_for_this_devices_ring_are_counted(
    api_env: Any,
) -> None:
    """The ring is per group, so two devices with the SAME outstanding patch get
    DIFFERENT numbers -- which is the only way the ring predicate is observable.

    Falsify by dropping the `ring_name` condition from the approval EXISTS: the
    pilot device's count goes from 0 to 1 and this reads 1/1 instead of 1/0.
    """
    org_id = await api_env.create_org("rep-patch-ring")
    broad_group = await seed_device_group(api_env.admin_engine, org_id, name="broad-ring")
    pilot_group = await seed_device_group(api_env.admin_engine, org_id, name="pilot-ring")
    await seed_patch_policy(
        api_env.admin_engine, org_id, device_group_id=broad_group, ring_name="broad"
    )
    await seed_patch_policy(
        api_env.admin_engine, org_id, device_group_id=pilot_group, ring_name="pilot"
    )

    patch_id = await seed_patch(api_env.admin_engine, org_id, identifier="KB-RING")
    await seed_patch_approval(api_env.admin_engine, org_id, patch_id, ring_name="broad")

    in_broad = await seed_device(
        api_env.admin_engine, org_id, hostname="a-broad", device_group_id=broad_group
    )
    in_pilot = await seed_device(
        api_env.admin_engine, org_id, hostname="b-pilot", device_group_id=pilot_group
    )
    for device_id in (in_broad.device_id, in_pilot.device_id):
        await seed_patch_status(
            api_env.admin_engine, org_id, device_id, patch_id, state="missing"
        )
        # Both measured, so `devices_with_zero_missing` below is about the ring
        # predicate and not about one of them being unscanned.
        await seed_patch_scan_job(api_env.admin_engine, org_id, device_id, hours_ago=1)

    page = await _run(api_env, org_id)
    by_id = {row.device_id: row for row in page.rows}
    assert by_id[in_broad.device_id].missing_approved == 1
    assert by_id[in_pilot.device_id].missing_approved == 0, (
        "the patch is approved for 'broad', and this device's ring is 'pilot'"
    )
    assert page.totals["devices_with_zero_missing"] == 1


async def test_a_revoked_approval_stops_counting(api_env: Any) -> None:
    """`revoke_approval` sets `revoked = True` in place rather than deleting the
    row, so a report that only checked for the row's EXISTENCE would keep
    counting a withdrawn approval forever."""
    org_id = await api_env.create_org("rep-patch-revoked")
    live = await seed_patch(api_env.admin_engine, org_id, identifier="KB-LIVE")
    withdrawn = await seed_patch(api_env.admin_engine, org_id, identifier="KB-GONE")
    await seed_patch_approval(api_env.admin_engine, org_id, live, ring_name=_DEFAULT_RING)
    await seed_patch_approval(
        api_env.admin_engine, org_id, withdrawn, ring_name=_DEFAULT_RING, revoked=True
    )

    device = await seed_device(api_env.admin_engine, org_id, hostname="only")
    for patch_id in (live, withdrawn):
        await seed_patch_status(
            api_env.admin_engine, org_id, device.device_id, patch_id, state="missing"
        )

    page = await _run(api_env, org_id)
    (row,) = page.rows
    assert row.missing_approved == 1, "the revoked approval must not count"


async def test_the_device_patch_status_approved_cache_is_not_what_is_counted(
    api_env: Any,
) -> None:
    """A device carrying `state = "missing"` for a patch that IS approved for its
    ring is counted, and a device carrying `state = "approved"` for a patch whose
    approval was never written is NOT.

    This is the divergence the module docstring records: `approve._devices_in_ring`
    flips the cache over the devices in the ring AT THAT MOMENT, so a device
    created or moved afterwards keeps `missing` while the patch is genuinely
    approved for it. Reading the cache would understate the exposure of exactly
    those devices. Falsify by counting `state == "approved"` instead: this reads
    0 and 1 rather than 1 and 0.
    """
    org_id = await api_env.create_org("rep-patch-cache")
    approved_patch = await seed_patch(api_env.admin_engine, org_id, identifier="KB-APPROVED")
    unapproved_patch = await seed_patch(api_env.admin_engine, org_id, identifier="KB-NONE")
    await seed_patch_approval(
        api_env.admin_engine, org_id, approved_patch, ring_name=_DEFAULT_RING
    )

    stale_cache = await seed_device(api_env.admin_engine, org_id, hostname="a-stale-cache")
    await seed_patch_status(
        api_env.admin_engine, org_id, stale_cache.device_id, approved_patch, state="missing"
    )
    lying_cache = await seed_device(api_env.admin_engine, org_id, hostname="b-lying-cache")
    await seed_patch_status(
        api_env.admin_engine, org_id, lying_cache.device_id, unapproved_patch, state="approved"
    )

    page = await _run(api_env, org_id)
    by_id = {row.device_id: row for row in page.rows}
    assert by_id[stale_cache.device_id].missing_approved == 1
    assert by_id[lying_cache.device_id].missing_approved == 0


async def test_every_outstanding_state_counts_and_installed_does_not(api_env: Any) -> None:
    """`missing`, `approved` and `failed` are all "applicable and unresolved";
    `installed` and `not_applicable` are not. One device per state, so a
    predicate that dropped one of the three reads a different total."""
    org_id = await api_env.create_org("rep-patch-states")
    states = ("missing", "approved", "failed", "installed", "not_applicable")
    patches = {
        state: await seed_patch(api_env.admin_engine, org_id, identifier=f"KB-{state}")
        for state in states
    }
    for patch_id in patches.values():
        await seed_patch_approval(
            api_env.admin_engine, org_id, patch_id, ring_name=_DEFAULT_RING
        )

    device = await seed_device(api_env.admin_engine, org_id, hostname="everything")
    for state, patch_id in patches.items():
        await seed_patch_status(
            api_env.admin_engine,
            org_id,
            device.device_id,
            patch_id,
            state=state,
            applied_days_ago=1 if state == "installed" else None,
        )

    page = await _run(api_env, org_id)
    (row,) = page.rows
    assert row.missing_approved == 3, "missing + approved + failed"
    assert row.applied_in_window == 1


async def test_security_severity_is_a_subset_and_uses_the_patch_modules_definition(
    api_env: Any,
) -> None:
    """`critical` and `important` count as security; `moderate` does not. Three
    patches of three severities, so `missing_approved_security` is a strict
    subset of `missing_approved` and neither number is the other by
    coincidence."""
    org_id = await api_env.create_org("rep-patch-sev")
    device = await seed_device(api_env.admin_engine, org_id, hostname="mixed")
    for severity in ("critical", "important", "moderate"):
        patch_id = await seed_patch(
            api_env.admin_engine, org_id, identifier=f"KB-{severity}", severity=severity
        )
        await seed_patch_approval(
            api_env.admin_engine, org_id, patch_id, ring_name=_DEFAULT_RING
        )
        await seed_patch_status(
            api_env.admin_engine, org_id, device.device_id, patch_id, state="missing"
        )

    page = await _run(api_env, org_id)
    (row,) = page.rows
    assert row.missing_approved == 3
    assert row.missing_approved_security == 2


async def test_oldest_missing_is_the_oldest_outstanding_approved_one(api_env: Any) -> None:
    """It is EXPOSURE age, so an older row that is installed or unapproved must
    not win. Three rows whose `detected_at` differ, only one of which is
    outstanding-and-approved."""
    org_id = await api_env.create_org("rep-patch-oldest")
    device = await seed_device(api_env.admin_engine, org_id, hostname="aged")

    ancient_installed = await seed_patch(api_env.admin_engine, org_id, identifier="KB-OLD-OK")
    await seed_patch_approval(
        api_env.admin_engine, org_id, ancient_installed, ring_name=_DEFAULT_RING
    )
    await seed_patch_status(
        api_env.admin_engine, org_id, device.device_id, ancient_installed,
        state="installed", detected_days_ago=100, applied_days_ago=90,
    )
    old_unapproved = await seed_patch(api_env.admin_engine, org_id, identifier="KB-OLD-NA")
    await seed_patch_status(
        api_env.admin_engine, org_id, device.device_id, old_unapproved,
        state="missing", detected_days_ago=50,
    )
    exposed = await seed_patch(api_env.admin_engine, org_id, identifier="KB-EXPOSED")
    await seed_patch_approval(api_env.admin_engine, org_id, exposed, ring_name=_DEFAULT_RING)
    await seed_patch_status(
        api_env.admin_engine, org_id, device.device_id, exposed,
        state="missing", detected_days_ago=10,
    )

    page = await _run(api_env, org_id)
    (row,) = page.rows
    assert row.missing_approved == 1
    assert row.oldest_missing_detected_at is not None
    age_days = (page.meta.generated_at - row.oldest_missing_detected_at).days
    assert 9 <= age_days <= 11, age_days


# --------------------------------------------------------------------------- #
# The window and the scan cadence are PARAMETERS, and therefore observable.
# --------------------------------------------------------------------------- #


async def test_the_window_is_a_parameter_for_both_applied_and_failed(
    api_env: Any,
) -> None:
    """The SAME device reads 0/0 at a 7-day window and 1/1 at a 90-day one. A
    single-value test could not tell the predicate from a constant."""
    org_id = await api_env.create_org("rep-patch-window")
    device = await seed_device(api_env.admin_engine, org_id, hostname="historic")
    installed = await seed_patch(api_env.admin_engine, org_id, identifier="KB-INS")
    failed = await seed_patch(api_env.admin_engine, org_id, identifier="KB-FAIL")
    await seed_patch_status(
        api_env.admin_engine, org_id, device.device_id, installed,
        state="installed", detected_days_ago=40, applied_days_ago=30,
    )
    await seed_patch_status(
        api_env.admin_engine, org_id, device.device_id, failed,
        state="failed", detected_days_ago=40, updated_days_ago=30,
    )

    narrow = await _run(api_env, org_id, window_days=7)
    assert narrow.rows[0].applied_in_window == 0
    assert narrow.rows[0].failed_in_window == 0

    wide = await _run(api_env, org_id, window_days=90)
    assert wide.rows[0].applied_in_window == 1
    assert wide.rows[0].failed_in_window == 1


async def test_scan_freshness_follows_the_devices_own_policy_interval(
    api_env: Any,
) -> None:
    """Two devices scanned at the SAME instant read differently because their
    groups' policies declare different scan intervals -- which is the only way
    the per-device interval is observable at all (a constant would make both
    agree).

    And the multiplier is a parameter: the six-hourly device reads `stale` at
    two intervals (12h) and `fresh` at ten (60h), on the same seeded data.
    """
    org_id = await api_env.create_org("rep-patch-scan")
    hourly = await seed_device_group(api_env.admin_engine, org_id, name="hourly")
    monthly = await seed_device_group(api_env.admin_engine, org_id, name="monthly")
    await seed_patch_policy(
        api_env.admin_engine, org_id, device_group_id=hourly, scan_interval_hours=6
    )
    await seed_patch_policy(
        api_env.admin_engine, org_id, device_group_id=monthly, scan_interval_hours=720
    )

    patch_id = await seed_patch(api_env.admin_engine, org_id, identifier="KB-SCAN")
    quick = await seed_device(
        api_env.admin_engine, org_id, hostname="a-quick", device_group_id=hourly
    )
    slow = await seed_device(
        api_env.admin_engine, org_id, hostname="b-slow", device_group_id=monthly
    )
    for device_id in (quick.device_id, slow.device_id):
        await seed_patch_status(
            api_env.admin_engine, org_id, device_id, patch_id,
            state="not_applicable", detected_days_ago=1,
        )
        # Scanned a day ago -- the fact the freshness window is judged against,
        # and the reason both devices are `never` without it.
        await seed_patch_scan_job(
            api_env.admin_engine, org_id, device_id, hours_ago=24
        )

    page = await _run(api_env, org_id)
    by_id = {row.device_id: row for row in page.rows}
    assert by_id[quick.device_id].scan_state == "stale", "6h interval, scanned a day ago"
    assert by_id[slow.device_id].scan_state == "fresh", "720h interval, scanned a day ago"

    lenient = await _run(api_env, org_id, scan_stale_after_intervals=10)
    by_id_lenient = {row.device_id: row for row in lenient.rows}
    assert by_id_lenient[quick.device_id].scan_state == "fresh", "10 x 6h = 60h"
    assert by_id_lenient[slow.device_id].scan_state == "fresh"
    # Neither device is `never` at any multiplier -- both HAVE been scanned.
    assert lenient.totals["devices_never_scanned"] == 0


async def test_the_org_default_policy_applies_to_an_ungrouped_device(
    api_env: Any,
) -> None:
    """`resolve_patch_policy` falls back to the org-default row
    (`device_group_id IS NULL`) before the hardcoded `broad`. A device with no
    group must therefore be judged against the ORG's ring, not against
    `broad` -- so an approval in the org-default ring counts and one in `broad`
    does not."""
    org_id = await api_env.create_org("rep-patch-orgdefault")
    await seed_patch_policy(api_env.admin_engine, org_id, ring_name="org-wide")

    org_ring = await seed_patch(api_env.admin_engine, org_id, identifier="KB-ORG")
    broad_ring = await seed_patch(api_env.admin_engine, org_id, identifier="KB-BROAD")
    await seed_patch_approval(api_env.admin_engine, org_id, org_ring, ring_name="org-wide")
    await seed_patch_approval(api_env.admin_engine, org_id, broad_ring, ring_name="broad")

    device = await seed_device(api_env.admin_engine, org_id, hostname="ungrouped")
    for patch_id in (org_ring, broad_ring):
        await seed_patch_status(
            api_env.admin_engine, org_id, device.device_id, patch_id, state="missing"
        )

    page = await _run(api_env, org_id)
    (row,) = page.rows
    assert row.missing_approved == 1, "only the org-default ring's approval applies"


# --------------------------------------------------------------------------- #
# Tenancy (§12.5, Global Constraint 4 and 5).
# --------------------------------------------------------------------------- #


async def _seed_two_overlapping_orgs(
    api_env: Any,
) -> tuple[UUID, UUID, dict[str, UUID], dict[str, UUID]]:
    """Org A and org B, seeded IDENTICALLY except for the org: same hostnames,
    same patch identifiers, same severity, approvals in the same ring, statuses
    in the same state. The ONLY thing that can exclude org B's rows from org A's
    report is an org predicate."""
    org_a = await api_env.create_org("rep-patch-a")
    org_b = await api_env.create_org("rep-patch-b")
    ids_a: dict[str, UUID] = {}
    ids_b: dict[str, UUID] = {}
    for org_id, ids in ((org_a, ids_a), (org_b, ids_b)):
        patch_id = await seed_patch(api_env.admin_engine, org_id, identifier="KB-SHARED")
        await seed_patch_approval(
            api_env.admin_engine, org_id, patch_id, ring_name=_DEFAULT_RING
        )
        for hostname in ("shared-host-1", "shared-host-2"):
            device = await seed_device(api_env.admin_engine, org_id, hostname=hostname)
            ids[hostname] = device.device_id
            await seed_patch_status(
                api_env.admin_engine, org_id, device.device_id, patch_id, state="missing"
            )
    return org_a, org_b, ids_a, ids_b


async def test_a_report_returns_only_the_callers_org(api_env: Any) -> None:
    """§12.5, through the API, exactly as a request runs."""
    org_a, _org_b, ids_a, ids_b = await _seed_two_overlapping_orgs(api_env)
    _, sub = await reporting_user(api_env, org_a, *CODES)
    response = await api_env.client.get(
        f"/api/v1/reports/{KEY}", headers=await api_env.headers(org_a, sub)
    )
    assert response.status_code == 200, response.text
    returned = {row["device_id"] for row in response.json()["rows"]}
    assert returned == {str(v) for v in ids_a.values()}
    assert not (returned & {str(v) for v in ids_b.values()})
    assert response.json()["totals"]["devices"] == 2


async def test_the_org_predicate_holds_where_rls_is_not_doing_the_rejecting(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    """GLOBAL CONSTRAINT 4. `admin_session` connects as the RLS-BYPASSING role,
    so this is the ONLY assertion in the file that can see
    `Device.org_id == org_id`. Delete that predicate and this returns four
    devices while the `org_session` test above stays green."""
    org_a, _org_b, ids_a, ids_b = await _seed_two_overlapping_orgs(api_env)

    async with admin_session(engine=admin_engine) as session:
        page = await execute_report(
            session, _definition(), PatchComplianceParams(), org_id=org_a
        )

    returned = {row.device_id for row in page.rows}
    assert returned == set(ids_a.values()), returned
    assert not (returned & set(ids_b.values()))
    assert page.meta.total == 2
    assert page.totals["devices"] == 2
    # And the COUNTS are org-scoped too, not merely the row list: org B seeded
    # the identical outstanding patch, so a missing predicate inside the stats
    # subquery would double this.
    assert {row.missing_approved for row in page.rows} == {1}


async def test_an_org_b_patch_status_cannot_inflate_org_as_counts(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    """The subquery's OWN org predicate, pinned separately.

    The row list can be right while the aggregate is wrong: a
    `device_patch_status` row belonging to another org, joined on `device_id`
    alone, would land on nobody's device -- but a `patch_approvals` row without
    its org predicate WOULD approve org A's patch through org B's approval.
    Seeded here so the assertion is not hypothetical.
    """
    org_a = await api_env.create_org("rep-patch-xa")
    org_b = await api_env.create_org("rep-patch-xb")
    patch_a = await seed_patch(api_env.admin_engine, org_a, identifier="KB-X")
    device = await seed_device(api_env.admin_engine, org_a, hostname="lonely")
    await seed_patch_status(
        api_env.admin_engine, org_a, device.device_id, patch_a, state="missing"
    )
    # Org B approves a patch of its own in the same ring. Nothing about it may
    # reach org A's numbers.
    patch_b = await seed_patch(api_env.admin_engine, org_b, identifier="KB-X")
    await seed_patch_approval(api_env.admin_engine, org_b, patch_b, ring_name=_DEFAULT_RING)

    async with admin_session(engine=admin_engine) as session:
        page = await execute_report(
            session, _definition(), PatchComplianceParams(), org_id=org_a
        )
    (row,) = page.rows
    assert row.missing_approved == 0, "org B's approval must not approve org A's patch"


# --------------------------------------------------------------------------- #
# The envelope (§6.3).
# --------------------------------------------------------------------------- #


async def test_totals_are_computed_over_the_whole_match_not_the_page(
    api_env: Any,
) -> None:
    """§12.10. Five devices whose values DIFFER, so a `totals` accidentally
    computed over the page produces a DIFFERENT number rather than the same one
    by coincidence: page 0 holds two exposed devices and the last page holds a
    never-scanned one."""
    org_id = await api_env.create_org("rep-patch-totals")
    patch_id = await seed_patch(api_env.admin_engine, org_id, identifier="KB-T")
    await seed_patch_approval(api_env.admin_engine, org_id, patch_id, ring_name=_DEFAULT_RING)
    for hostname, state in [
        ("a-exposed", "missing"),
        ("b-exposed", "failed"),
        ("c-clean", "installed"),
        ("d-clean", "not_applicable"),
        ("e-never", None),
    ]:
        device = await seed_device(api_env.admin_engine, org_id, hostname=hostname)
        if state is not None:
            await seed_patch_status(
                api_env.admin_engine, org_id, device.device_id, patch_id,
                state=state, applied_days_ago=1 if state == "installed" else None,
            )
            await seed_patch_scan_job(
                api_env.admin_engine, org_id, device.device_id, hours_ago=1
            )

    expected = {
        "devices": 5,
        "devices_with_zero_missing": 2,
        "devices_never_scanned": 1,
    }
    first = await _run(api_env, org_id, limit=2, offset=0)
    last = await _run(api_env, org_id, limit=2, offset=4)

    assert first.totals == expected, first.totals
    assert last.totals == expected, last.totals
    assert first.meta.total == 5 and last.meta.total == 5
    # The premise: the two pages really do hold different rows.
    assert {r.missing_approved for r in first.rows} == {1}
    assert [r.scan_state for r in last.rows] == ["never"]


async def test_ordering_is_stable_across_a_tie_on_the_sort_key(api_env: Any) -> None:
    """§12.11. Four devices SHARING ONE HOSTNAME. Without a deliberate tie an
    unstable sort is unobservable, because the sort key already orders the rows
    totally."""
    org_id = await api_env.create_org("rep-patch-tie")
    expected: set[UUID] = set()
    for _ in range(4):
        device = await seed_device(api_env.admin_engine, org_id, hostname="identical")
        expected.add(device.device_id)

    seen: list[UUID] = []
    for offset in range(4):
        page = await _run(api_env, org_id, limit=1, offset=offset)
        assert len(page.rows) == 1, page.rows
        seen.append(page.rows[0].device_id)

    assert len(seen) == len(set(seen)), seen
    assert set(seen) == expected


async def test_the_meta_envelope_names_all_three_source_modules(api_env: Any) -> None:
    """`jobs` is the third since `last_scan_at` moved onto it, and the envelope
    has to say so: `meta.sources` is where a reader learns which module each
    figure came from and what recency it inherits, and a report that read a
    module silently would be the staleness misreading §2 exists to prevent."""
    org_id = await api_env.create_org("rep-patch-meta")
    await seed_device(api_env.admin_engine, org_id, hostname="only")
    _, sub = await reporting_user(api_env, org_id, *CODES)
    response = await api_env.client.get(
        f"/api/v1/reports/{KEY}", headers=await api_env.headers(org_id, sub)
    )
    assert response.status_code == 200, response.text
    meta = response.json()["meta"]
    assert meta["report_key"] == KEY
    assert meta["reproducible"] is True
    modules = {source["module"]: source for source in meta["sources"]}
    assert set(modules) == {"devices", "patch", "jobs"}
    assert modules["patch"]["permission"] == "patch.read"
    assert modules["jobs"]["permission"] == "jobs.read"
    assert modules["patch"]["recency"].strip()
    assert modules["jobs"]["recency"].strip()
    # The patch module's recency line no longer claims `detected_at` is when a
    # device was last scanned. It is the sentence a reader trusts about how old
    # these numbers are, and it was false.
    assert "first detection" in modules["patch"]["recency"], modules["patch"]["recency"]


async def test_the_row_model_carries_a_description_on_every_field() -> None:
    """Property 5 as written, not through the class-docstring fallback."""
    undocumented = [
        name
        for name, field in _definition().row_model.model_fields.items()
        if not (field.description or "").strip()
    ]
    assert not undocumented, undocumented
