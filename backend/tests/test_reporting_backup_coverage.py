"""`backup-coverage` (spec §5.3, §2, §6.4, §12.5, §12.13).

**THE LOAD-BEARING TEST IS `test_a_device_with_no_policy_is_never_covered`
(§12.13).** Four devices, one per `coverage` value, one of them with NO policy
row at all. Falsify by making the policy resolution an inner join: the device
vanishes, `unprotected` reads 0 while `covered` is unchanged -- an observably
different number rather than a different diagnosis.

**TWO ORGS WITH DELIBERATELY OVERLAPPING DATA, AND THE APPLICATION PREDICATE
PINNED ON THE ADMIN ENGINE.** Same hostnames, repositories of the same name,
policies at the same scope, points taken in the same window. A cross-org test
driven only through `org_session` passes even when `Device.org_id == org_id` has
been deleted, because RLS rejects first.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import admin_session, org_session
from app.modules.reporting.definitions.backup_coverage import (
    _FAILED_RUN_STATES,
    _RESTORABLE_POINT_STATE,
    COVERAGE_VALUES,
    BackupCoverageParams,
)
from app.modules.reporting.registry import _NEVER_A_COLUMN, REGISTRY
from app.modules.reporting.service import execute_report
from tests.client_scope_support import client_with_site
from tests.reporting_fixtures import (
    reporting_user,
    seed_backup_point,
    seed_backup_policy,
    seed_backup_repository,
    seed_backup_run,
    seed_device,
    seed_device_group,
)

KEY = "backup-coverage"
CODES = ("reporting.read", "devices.read", "backup.read")


def _definition() -> Any:
    return REGISTRY[KEY]


async def _run(api_env: Any, org_id: UUID, **params: Any) -> Any:
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        return await execute_report(
            session, _definition(), BackupCoverageParams(**params), org_id=org_id
        )


# --------------------------------------------------------------------------- #
# The restated backup-module constants are pinned copies with a drift alarm.
# --------------------------------------------------------------------------- #


async def test_the_restated_backup_module_states_still_exist() -> None:
    """`_RESTORABLE_POINT_STATE` and `_FAILED_RUN_STATES` are restated rather
    than imported (§1.3). A restatement without a drift alarm is a guess."""
    from app.modules.backup.reporting import RUN_STATES

    assert set(_FAILED_RUN_STATES) <= set(RUN_STATES)
    assert _RESTORABLE_POINT_STATE == "complete"
    # `app/modules/backup/reporting.py`'s module docstring: a point is
    # restorable only when its state is `complete`; `failed` and `pruned` are
    # excluded from every coverage calculation.
    assert set(COVERAGE_VALUES) == {"covered", "stale", "failing", "unprotected"}


async def test_the_definition_requires_devices_and_backup() -> None:
    assert _definition().requires == frozenset({"devices.read", "backup.read"})


async def test_the_repository_secrets_are_denylisted_and_absent(api_env: Any) -> None:
    """§5.3, §0.8. `storage_ref` and `encryption_key_ref` are on
    `_NEVER_A_COLUMN`, so §12.3's sweep enforces their absence from
    `export_columns` and the row model rather than a reviewer remembering.

    This adds the observation §12.3 cannot make: the seeded values are
    recognisable strings, and they do not appear ANYWHERE in a real response
    body -- not in a row, not in the meta, not in an error.
    """
    assert "backup_repositories.storage_ref" in _NEVER_A_COLUMN
    assert "backup_repositories.encryption_key_ref" in _NEVER_A_COLUMN

    org_id = await api_env.create_org("rep-backup-secret")
    repository = await seed_backup_repository(api_env.admin_engine, org_id, name="primary")
    await seed_backup_policy(api_env.admin_engine, org_id, repository)
    await seed_device(api_env.admin_engine, org_id, hostname="only")

    _, sub = await reporting_user(api_env, org_id, *CODES)
    response = await api_env.client.get(
        f"/api/v1/reports/{KEY}", headers=await api_env.headers(org_id, sub)
    )
    assert response.status_code == 200, response.text
    # The premise: the repository IS reported, by name -- so an empty response
    # is not what is passing this assertion.
    assert response.json()["rows"][0]["repository_name"] == "primary"
    assert "never-in-a-report" not in response.text


# --------------------------------------------------------------------------- #
# §12.13: a device with no policy is never covered.
# --------------------------------------------------------------------------- #


async def _one_per_coverage(api_env: Any, org_id: UUID) -> dict[str, UUID]:
    """One device per `coverage` value, differing ONLY in the backup facts that
    decide the verdict. The `unprotected` one has no policy row at all."""
    repository = await seed_backup_repository(api_env.admin_engine, org_id, name="repo")
    groups = {
        value: await seed_device_group(api_env.admin_engine, org_id, name=f"g-{value}")
        for value in ("covered", "stale", "failing")
    }
    for group_id in groups.values():
        await seed_backup_policy(
            api_env.admin_engine, org_id, repository, device_group_id=group_id
        )

    ids: dict[str, UUID] = {}

    covered = await seed_device(
        api_env.admin_engine, org_id, hostname="a-covered", device_group_id=groups["covered"]
    )
    ids["covered"] = covered.device_id
    run = await seed_backup_run(
        api_env.admin_engine, org_id, covered.device_id, repository,
        state="success", started_days_ago=1,
    )
    await seed_backup_point(
        api_env.admin_engine, org_id, covered.device_id, repository, run, taken_days_ago=1
    )

    stale = await seed_device(
        api_env.admin_engine, org_id, hostname="b-stale", device_group_id=groups["stale"]
    )
    ids["stale"] = stale.device_id
    old_run = await seed_backup_run(
        api_env.admin_engine, org_id, stale.device_id, repository,
        state="success", started_days_ago=60,
    )
    await seed_backup_point(
        api_env.admin_engine, org_id, stale.device_id, repository, old_run, taken_days_ago=60
    )

    failing = await seed_device(
        api_env.admin_engine, org_id, hostname="c-failing", device_group_id=groups["failing"]
    )
    ids["failing"] = failing.device_id
    fresh_run = await seed_backup_run(
        api_env.admin_engine, org_id, failing.device_id, repository,
        state="success", started_days_ago=2,
    )
    await seed_backup_point(
        api_env.admin_engine, org_id, failing.device_id, repository, fresh_run,
        taken_days_ago=2,
    )
    await seed_backup_run(
        api_env.admin_engine, org_id, failing.device_id, repository,
        state="failed", started_days_ago=0.5,
    )

    unprotected = await seed_device(
        api_env.admin_engine, org_id, hostname="d-unprotected"
    )
    ids["unprotected"] = unprotected.device_id
    return ids


async def test_a_device_with_no_policy_is_never_covered(api_env: Any) -> None:
    """§12.13. `unprotected: 1`, and that device is in NO other bucket."""
    org_id = await api_env.create_org("rep-backup-cov")
    ids = await _one_per_coverage(api_env, org_id)

    page = await _run(api_env, org_id)
    by_id = {row.device_id: row for row in page.rows}
    # Present at all -- an inner join anywhere would delete it.
    assert set(by_id) == set(ids.values())

    for expected, device_id in ids.items():
        assert by_id[device_id].coverage == expected, (expected, by_id[device_id])

    assert page.totals["unprotected"] == 1
    assert page.totals["covered"] == 1
    assert page.totals["stale"] == 1
    assert page.totals["failing"] == 1
    assert page.totals["devices"] == 4
    # Exhaustive and mutually exclusive by construction (a CASE returns one
    # value), asserted rather than trusted.
    assert sum(page.totals[value] for value in COVERAGE_VALUES) == page.totals["devices"]

    unprotected_row = by_id[ids["unprotected"]]
    assert unprotected_row.policy_present is False
    assert unprotected_row.policy_enabled is False
    assert unprotected_row.repository_name is None


async def test_a_disabled_policy_is_unprotected_with_its_cause_visible(
    api_env: Any,
) -> None:
    """`resolve_backup_policy` considers only ENABLED policies at either scope,
    so a device whose only policy is switched off has nothing scheduled.

    THE FIXTURE-BLINDNESS GUARD: the two devices differ ONLY in `enabled`. Same
    repository, same group shape, same point history -- so a `coverage` that
    ignored `enabled` would make both read `covered` and this test could not
    tell the predicate from a constant. `policy_present` stays TRUE for the
    disabled one, which is what distinguishes "switched off" from "never
    configured".
    """
    org_id = await api_env.create_org("rep-backup-disabled")
    repository = await seed_backup_repository(api_env.admin_engine, org_id, name="repo")
    on_group = await seed_device_group(api_env.admin_engine, org_id, name="on")
    off_group = await seed_device_group(api_env.admin_engine, org_id, name="off")
    await seed_backup_policy(
        api_env.admin_engine, org_id, repository, device_group_id=on_group, enabled=True
    )
    await seed_backup_policy(
        api_env.admin_engine, org_id, repository, device_group_id=off_group, enabled=False
    )

    rows: dict[str, UUID] = {}
    for label, group_id in (("a-on", on_group), ("b-off", off_group)):
        device = await seed_device(
            api_env.admin_engine, org_id, hostname=label, device_group_id=group_id
        )
        rows[label] = device.device_id
        run = await seed_backup_run(
            api_env.admin_engine, org_id, device.device_id, repository,
            state="success", started_days_ago=1,
        )
        await seed_backup_point(
            api_env.admin_engine, org_id, device.device_id, repository, run, taken_days_ago=1
        )

    page = await _run(api_env, org_id)
    by_id = {row.device_id: row for row in page.rows}
    assert by_id[rows["a-on"]].coverage == "covered"
    assert by_id[rows["b-off"]].coverage == "unprotected"
    assert by_id[rows["b-off"]].policy_present is True, "the cause must stay visible"
    assert by_id[rows["b-off"]].policy_enabled is False
    assert page.totals["unprotected"] == 1


# --------------------------------------------------------------------------- #
# The proven-restore evidence (§5.3, the roadmap gate).
# --------------------------------------------------------------------------- #


async def test_a_covered_device_that_has_never_been_restore_tested_reads_as_such(
    api_env: Any,
) -> None:
    """§5.3's reason for existing. Two devices, both `covered`, differing ONLY
    in whether a restore was ever exercised -- so `coverage` alone cannot answer
    the roadmap's "proven restores, not just schedules" question and
    `last_restore_tested_at` is what does.

    `verified_at` is deliberately set on the untested one: verification is
    weaker than a restore test, and a report that conflated them would read
    both devices as proven.
    """
    org_id = await api_env.create_org("rep-backup-proven")
    repository = await seed_backup_repository(api_env.admin_engine, org_id, name="repo")
    await seed_backup_policy(api_env.admin_engine, org_id, repository)

    proven = await seed_device(api_env.admin_engine, org_id, hostname="a-proven")
    unproven = await seed_device(api_env.admin_engine, org_id, hostname="b-unproven")
    for device_id, restore_tested in (
        (proven.device_id, 3.0),
        (unproven.device_id, None),
    ):
        run = await seed_backup_run(
            api_env.admin_engine, org_id, device_id, repository,
            state="success", started_days_ago=1,
        )
        await seed_backup_point(
            api_env.admin_engine, org_id, device_id, repository, run,
            taken_days_ago=1, verified_days_ago=1, restore_tested_days_ago=restore_tested,
        )

    page = await _run(api_env, org_id)
    by_id = {row.device_id: row for row in page.rows}
    assert by_id[proven.device_id].coverage == "covered"
    assert by_id[unproven.device_id].coverage == "covered"
    assert by_id[proven.device_id].last_restore_tested_at is not None
    assert by_id[unproven.device_id].last_restore_tested_at is None
    assert by_id[unproven.device_id].last_verified_at is not None, (
        "verified is not restore-tested"
    )
    assert page.totals["devices_never_restore_tested"] == 1


async def test_restore_evidence_is_the_maximum_over_every_point_not_the_newest_one(
    api_env: Any,
) -> None:
    """"Has a restore ever been proven for this device" is not answered by the
    most recent point, which may have been taken an hour ago and never tested.

    Two points: an OLD one that was restore-tested and a NEW one that was not.
    A report reading the evidence off the newest point returns null here.
    """
    org_id = await api_env.create_org("rep-backup-max")
    repository = await seed_backup_repository(api_env.admin_engine, org_id, name="repo")
    await seed_backup_policy(api_env.admin_engine, org_id, repository)
    device = await seed_device(api_env.admin_engine, org_id, hostname="only")
    run = await seed_backup_run(
        api_env.admin_engine, org_id, device.device_id, repository,
        state="success", started_days_ago=30,
    )
    await seed_backup_point(
        api_env.admin_engine, org_id, device.device_id, repository, run,
        taken_days_ago=30, restore_tested_days_ago=29,
    )
    await seed_backup_point(
        api_env.admin_engine, org_id, device.device_id, repository, run, taken_days_ago=1
    )

    page = await _run(api_env, org_id)
    (row,) = page.rows
    assert row.last_restore_tested_at is not None
    assert row.coverage == "covered"
    assert page.totals["devices_never_restore_tested"] == 0


async def test_only_a_complete_point_counts_as_restorable(api_env: Any) -> None:
    """A `failed` point is not a backup. The device's only point is `failed`, so
    it is `stale` -- and `last_point_state` reports `failed` rather than hiding
    it, which is the shape worth acting on.

    Falsify by dropping the `state == 'complete'` filter from the restorable
    maximum: this reads `covered`.
    """
    org_id = await api_env.create_org("rep-backup-failedpoint")
    repository = await seed_backup_repository(api_env.admin_engine, org_id, name="repo")
    await seed_backup_policy(api_env.admin_engine, org_id, repository)
    device = await seed_device(api_env.admin_engine, org_id, hostname="only")
    run = await seed_backup_run(
        api_env.admin_engine, org_id, device.device_id, repository,
        state="success", started_days_ago=1,
    )
    await seed_backup_point(
        api_env.admin_engine, org_id, device.device_id, repository, run,
        state="failed", taken_days_ago=1,
    )

    page = await _run(api_env, org_id)
    (row,) = page.rows
    assert row.coverage == "stale"
    assert row.last_point_state == "failed"
    assert row.days_since_last_point == 1


async def test_the_stale_window_is_a_parameter(api_env: Any) -> None:
    """The SAME device reads `stale` at 7 days and `covered` at 90. A
    single-value test could not tell the predicate from a constant."""
    org_id = await api_env.create_org("rep-backup-window")
    repository = await seed_backup_repository(api_env.admin_engine, org_id, name="repo")
    await seed_backup_policy(api_env.admin_engine, org_id, repository)
    device = await seed_device(api_env.admin_engine, org_id, hostname="drifting")
    run = await seed_backup_run(
        api_env.admin_engine, org_id, device.device_id, repository,
        state="success", started_days_ago=30,
    )
    await seed_backup_point(
        api_env.admin_engine, org_id, device.device_id, repository, run, taken_days_ago=30
    )

    default_window = await _run(api_env, org_id)
    assert default_window.rows[0].coverage == "stale"
    assert default_window.totals["stale"] == 1

    wide_window = await _run(api_env, org_id, stale_after_days=90)
    assert wide_window.rows[0].coverage == "covered"
    assert wide_window.totals["covered"] == 1


async def test_the_org_default_policy_applies_to_an_ungrouped_device(
    api_env: Any,
) -> None:
    """`resolve_backup_policy` falls back to the device's client's default row
    (`device_group_id IS NULL`, `client_id` the device's) before giving up --
    the organisation default until plan 2026-10-26 Task 13, the internal
    client's here, whose device this is. A device with no group must therefore
    be protected by it -- and the repository NAME is what proves the fallback
    resolved rather than merely that something did."""
    org_id = await api_env.create_org("rep-backup-orgdefault")
    repository = await seed_backup_repository(api_env.admin_engine, org_id, name="org-default")
    await seed_backup_policy(api_env.admin_engine, org_id, repository)
    device = await seed_device(api_env.admin_engine, org_id, hostname="ungrouped")
    run = await seed_backup_run(
        api_env.admin_engine, org_id, device.device_id, repository,
        state="success", started_days_ago=1,
    )
    await seed_backup_point(
        api_env.admin_engine, org_id, device.device_id, repository, run, taken_days_ago=1
    )

    page = await _run(api_env, org_id)
    (row,) = page.rows
    assert row.policy_present is True
    assert row.policy_enabled is True
    assert row.repository_name == "org-default"
    assert row.coverage == "covered"


async def test_a_group_policy_wins_over_the_org_default(api_env: Any) -> None:
    """Resolution order, made observable by giving the two repositories
    DIFFERENT names: with one repository, "the group policy won" and "the
    client's default won" produce the same output."""
    org_id = await api_env.create_org("rep-backup-order")
    org_repository = await seed_backup_repository(api_env.admin_engine, org_id, name="org")
    group_repository = await seed_backup_repository(api_env.admin_engine, org_id, name="group")
    group_id = await seed_device_group(api_env.admin_engine, org_id, name="grouped")
    await seed_backup_policy(api_env.admin_engine, org_id, org_repository)
    await seed_backup_policy(
        api_env.admin_engine, org_id, group_repository, device_group_id=group_id
    )

    grouped = await seed_device(
        api_env.admin_engine, org_id, hostname="a-grouped", device_group_id=group_id
    )
    ungrouped = await seed_device(api_env.admin_engine, org_id, hostname="b-ungrouped")

    page = await _run(api_env, org_id)
    by_id = {row.device_id: row for row in page.rows}
    assert by_id[grouped.device_id].repository_name == "group"
    assert by_id[ungrouped.device_id].repository_name == "org"


async def _customer_device(
    api_env: Any, org_id: UUID, client_id: UUID, site_id: UUID, hostname: str,
    device_group_id: UUID | None = None,
) -> UUID:
    from app.models import Device

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, client_id=client_id, site_id=site_id, hostname=hostname,
                        os_family="windows", device_group_id=device_group_id)
        session.add(device)
        await session.commit()
        return device.id


async def _forced_policy(
    api_env: Any, org_id: UUID, repository_id: UUID, *, device_group_id: UUID | None = None
) -> None:
    """A policy the guard of migration 0242 refuses -- one naming another
    client's repository, or one for every client naming any -- written with
    the guard off inside one committed transaction, as 0242 can leave one."""
    from sqlalchemy import text

    from app.models import BackupPolicy

    guard = "its_zz_backup_policy_repository_guard"
    async with AsyncSession(api_env.admin_engine) as session:
        await session.execute(text(f"ALTER TABLE backup_policies DISABLE TRIGGER {guard}"))
        session.add(BackupPolicy(org_id=org_id, repository_id=repository_id,
                                 device_group_id=device_group_id, enabled=True))
        await session.flush()
        await session.execute(text(f"ALTER TABLE backup_policies ENABLE TRIGGER {guard}"))
        await session.commit()


async def test_a_client_s_default_protects_only_that_client_s_devices(api_env: Any) -> None:
    """Plan 2026-10-26 Task 13: the default policy is ONE CLIENT's. The
    internal client's default does not protect a device of customer Y, which
    reads unprotected with no policy at all -- until Y has a default of its
    own, writing to its own repository."""
    org_id = await api_env.create_org("rep-backup-client-default")
    y, y_site = await client_with_site(api_env, org_id)
    internal_repository = await seed_backup_repository(
        api_env.admin_engine, org_id, name="internal"
    )
    await seed_backup_policy(api_env.admin_engine, org_id, internal_repository)
    internal = await seed_device(api_env.admin_engine, org_id, hostname="a-internal")
    customer = await _customer_device(api_env, org_id, y, y_site, "b-customer")

    page = await _run(api_env, org_id)
    by_id = {row.device_id: row for row in page.rows}
    assert by_id[internal.device_id].repository_name == "internal"
    assert (by_id[customer].policy_present, by_id[customer].coverage) == (False, "unprotected")
    assert by_id[customer].repository_name is None

    y_repository = await seed_backup_repository(
        api_env.admin_engine, org_id, name="customer", client_id=y
    )
    await seed_backup_policy(api_env.admin_engine, org_id, y_repository, client_id=y)
    page = await _run(api_env, org_id)
    by_id = {row.device_id: row for row in page.rows}
    assert by_id[customer].repository_name == "customer"
    assert (by_id[customer].policy_present, by_id[customer].policy_enabled) == (True, True)
    assert by_id[internal.device_id].repository_name == "internal"


async def test_a_policy_naming_another_client_s_repository_protects_nothing(
    api_env: Any,
) -> None:
    """A group policy from before migration 0242 can name a repository that
    is now another client's; the dispatcher refuses to back up into it
    (`backup.dispatch`), so the report reproduces that: present, not
    protecting, no repository named -- however fresh the device's last point
    is. And a policy for every client names no repository at all."""
    org_id = await api_env.create_org("rep-backup-foreign")
    y, y_site = await client_with_site(api_env, org_id)
    internal_repository = await seed_backup_repository(
        api_env.admin_engine, org_id, name="internal"
    )
    group_id = await _customer_group(api_env, org_id, y)
    await _forced_policy(api_env, org_id, internal_repository, device_group_id=group_id)
    grouped = await _customer_device(api_env, org_id, y, y_site, "a-grouped", group_id)
    own = await seed_backup_repository(api_env.admin_engine, org_id, name="own", client_id=y)
    run = await seed_backup_run(
        api_env.admin_engine, org_id, grouped, own, state="success", started_days_ago=0.1
    )
    await seed_backup_point(api_env.admin_engine, org_id, grouped, own, run, taken_days_ago=0.1)
    # The internal client's ungrouped device under a policy for every client.
    await _forced_policy(api_env, org_id, internal_repository)
    ungrouped = await seed_device(api_env.admin_engine, org_id, hostname="b-ungrouped")

    page = await _run(api_env, org_id)
    by_id = {row.device_id: row for row in page.rows}
    for device_id in (grouped, ungrouped.device_id):
        row = by_id[device_id]
        assert (row.policy_present, row.policy_enabled, row.coverage) == (
            True, False, "unprotected"
        ), row
        assert row.repository_name is None


async def _customer_group(api_env: Any, org_id: UUID, client_id: UUID) -> UUID:
    from app.models import DeviceGroup

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        group = DeviceGroup(org_id=org_id, client_id=client_id, name="customer servers")
        session.add(group)
        await session.commit()
        return group.id


# --------------------------------------------------------------------------- #
# Tenancy (§12.5, Global Constraint 4 and 5).
# --------------------------------------------------------------------------- #


async def _seed_two_overlapping_orgs(
    api_env: Any,
) -> tuple[UUID, UUID, dict[str, UUID], dict[str, UUID]]:
    """Org A and org B, seeded IDENTICALLY except for the org: same hostnames,
    a repository of the same name, an org-default policy in both, points taken
    in the same window."""
    org_a = await api_env.create_org("rep-backup-a")
    org_b = await api_env.create_org("rep-backup-b")
    ids_a: dict[str, UUID] = {}
    ids_b: dict[str, UUID] = {}
    for org_id, ids in ((org_a, ids_a), (org_b, ids_b)):
        repository = await seed_backup_repository(api_env.admin_engine, org_id, name="shared")
        await seed_backup_policy(api_env.admin_engine, org_id, repository)
        for hostname in ("shared-host-1", "shared-host-2"):
            device = await seed_device(api_env.admin_engine, org_id, hostname=hostname)
            ids[hostname] = device.device_id
            run = await seed_backup_run(
                api_env.admin_engine, org_id, device.device_id, repository,
                state="success", started_days_ago=1,
            )
            await seed_backup_point(
                api_env.admin_engine, org_id, device.device_id, repository, run,
                taken_days_ago=1,
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
            session, _definition(), BackupCoverageParams(), org_id=org_a
        )

    returned = {row.device_id for row in page.rows}
    assert returned == set(ids_a.values()), returned
    assert not (returned & set(ids_b.values()))
    assert page.meta.total == 2
    assert page.totals["covered"] == 2


async def test_another_orgs_policy_cannot_protect_this_orgs_device(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    """The POLICY subquery's own org predicate, pinned separately.

    Org A has a device and no policy at all; org B has an enabled org-default
    policy. Drop `BackupPolicy.org_id == org_id` from the resolution and org A's
    device reads `stale` (it has a policy but no points) instead of
    `unprotected` -- a different bucket, from another tenant's configuration.
    """
    org_a = await api_env.create_org("rep-backup-xa")
    org_b = await api_env.create_org("rep-backup-xb")
    device = await seed_device(api_env.admin_engine, org_a, hostname="lonely")
    repository_b = await seed_backup_repository(api_env.admin_engine, org_b, name="theirs")
    await seed_backup_policy(api_env.admin_engine, org_b, repository_b)

    async with admin_session(engine=admin_engine) as session:
        page = await execute_report(
            session, _definition(), BackupCoverageParams(), org_id=org_a
        )
    (row,) = page.rows
    assert row.device_id == device.device_id
    assert row.coverage == "unprotected"
    assert row.policy_present is False


# --------------------------------------------------------------------------- #
# The envelope (§6.3).
# --------------------------------------------------------------------------- #


async def test_totals_are_computed_over_the_whole_match_not_the_page(
    api_env: Any,
) -> None:
    """§12.10. The four devices land in four DIFFERENT buckets, so a `totals`
    computed over the page reads a different number on every page rather than
    the same one by coincidence."""
    org_id = await api_env.create_org("rep-backup-totals")
    await _one_per_coverage(api_env, org_id)

    expected = {
        "devices": 4,
        "covered": 1,
        "stale": 1,
        "failing": 1,
        "unprotected": 1,
        "devices_never_restore_tested": 4,
    }
    first = await _run(api_env, org_id, limit=2, offset=0)
    second = await _run(api_env, org_id, limit=2, offset=2)

    assert first.totals == expected, first.totals
    assert second.totals == expected, second.totals
    assert first.meta.total == 4 and second.meta.total == 4
    # The premise: the two pages really do hold different rows.
    assert [r.coverage for r in first.rows] == ["covered", "stale"]
    assert [r.coverage for r in second.rows] == ["failing", "unprotected"]


async def test_ordering_is_stable_across_a_tie_on_the_sort_key(api_env: Any) -> None:
    """§12.11. Four devices SHARING ONE HOSTNAME."""
    org_id = await api_env.create_org("rep-backup-tie")
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


async def test_the_meta_envelope_names_both_source_modules(api_env: Any) -> None:
    org_id = await api_env.create_org("rep-backup-meta")
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
    assert set(modules) == {"devices", "backup"}
    assert modules["backup"]["permission"] == "backup.read"
    assert modules["backup"]["recency"].strip()


async def test_the_row_model_carries_a_description_on_every_field() -> None:
    """Property 5 as written, not through the class-docstring fallback."""
    undocumented = [
        name
        for name, field in _definition().row_model.model_fields.items()
        if not (field.description or "").strip()
    ]
    assert not undocumented, undocumented


# --------------------------------------------------------------------------- #
# Missed runs and the off-site copy (spec-server-backup.md §8.3).
# --------------------------------------------------------------------------- #


async def test_a_missed_scheduled_run_reads_as_failing(api_env: Any) -> None:
    org_id = await api_env.create_org("rep-backup-missed")
    repository = await seed_backup_repository(api_env.admin_engine, org_id, name="repo")
    await seed_backup_policy(api_env.admin_engine, org_id, repository)
    device = await seed_device(api_env.admin_engine, org_id, hostname="a-missed")
    run = await seed_backup_run(api_env.admin_engine, org_id, device.device_id, repository,
                                state="success", started_days_ago=2)
    await seed_backup_point(api_env.admin_engine, org_id, device.device_id, repository, run,
                            taken_days_ago=2)
    await seed_backup_run(api_env.admin_engine, org_id, device.device_id, repository,
                          state="missed", started_days_ago=0.5)
    page = await _run(api_env, org_id)
    assert page.rows[0].coverage == "failing"
    assert page.rows[0].offsite == "off"


async def test_with_off_site_on_a_device_is_covered_only_once_a_point_is_copied(
    api_env: Any,
) -> None:
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import BackupOffsiteTarget, BackupPointCopy, BackupRepository, Credential

    org_id = await api_env.create_org("rep-backup-offsite")
    repository = await seed_backup_repository(api_env.admin_engine, org_id, name="repo")
    await seed_backup_policy(api_env.admin_engine, org_id, repository)
    copied = await seed_device(api_env.admin_engine, org_id, hostname="a-copied")
    behind = await seed_device(api_env.admin_engine, org_id, hostname="b-behind")
    points = {}
    for dev in (copied, behind):
        run = await seed_backup_run(api_env.admin_engine, org_id, dev.device_id, repository,
                                    state="success", started_days_ago=1)
        points[dev.device_id] = await seed_backup_point(
            api_env.admin_engine, org_id, dev.device_id, repository, run, taken_days_ago=1)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        cred = Credential(org_id=org_id, name="off", type="s3_offsite", vault_ref="x")
        s.add(cred)
        await s.flush()
        target = BackupOffsiteTarget(org_id=org_id, name="t", bucket="b", credential_id=cred.id,
                                     state="ok")
        s.add(target)
        await s.flush()
        await s.execute(update(BackupRepository).where(BackupRepository.id == repository)
                        .values(offsite_target_id=target.id))
        s.add(BackupPointCopy(org_id=org_id, point_id=points[copied.device_id],
                              target_id=target.id, state="complete",
                              copied_at=datetime.now(UTC)))
        s.add(BackupPointCopy(org_id=org_id, point_id=points[behind.device_id],
                              target_id=target.id, state="copying"))
        await s.commit()

    page = await _run(api_env, org_id)
    by_id = {row.device_id: row for row in page.rows}
    assert (by_id[copied.device_id].coverage, by_id[copied.device_id].offsite) == (
        "covered", "current")
    assert by_id[copied.device_id].last_offsite_at is not None
    assert (by_id[behind.device_id].coverage, by_id[behind.device_id].offsite) == (
        "stale", "behind")


async def test_a_live_newest_point_is_reported_not_crash_consistent(api_env: Any) -> None:
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import BackupPoint

    org_id = await api_env.create_org("rep-backup-live")
    repository = await seed_backup_repository(api_env.admin_engine, org_id, name="repo")
    await seed_backup_policy(api_env.admin_engine, org_id, repository)
    device = await seed_device(api_env.admin_engine, org_id, hostname="a-live")
    run = await seed_backup_run(api_env.admin_engine, org_id, device.device_id, repository,
                                state="success", started_days_ago=1)
    point = await seed_backup_point(api_env.admin_engine, org_id, device.device_id, repository,
                                    run, taken_days_ago=1)
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(update(BackupPoint).where(BackupPoint.id == point)
                        .values(consistency_level="live", crash_consistent=False))
        await s.commit()
    row = (await _run(api_env, org_id)).rows[0]
    assert (row.consistency_level, row.crash_consistent) == ("live", False)
    # Still covered: the backup is recent; the column says how good it is.
    assert row.coverage == "covered"
