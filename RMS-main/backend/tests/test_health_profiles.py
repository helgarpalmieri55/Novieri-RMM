"""Profile / check / assignment configuration
(docs/modules/endpoint-health-alerting/spec.md §2.1-§2.3, §0.2, §0.3, §10).

READ THIS BEFORE ADDING A TEST HERE.

1. `policy_version` IS SERVER-OWNED AND BUMPS ON EVERY WRITE PATH. A test that
   only writes checks passes against an implementation that bumps in one place,
   so the bump is asserted separately for a profile rename, a check replacement,
   a clone's source (it must NOT bump) and a check retire.

2. THE VALUE-KIND VALIDATION IS THE ONLY THING SQL CANNOT DO.
   `ck_health_check_one_threshold` enforces "exactly one of the two columns";
   pairing the RIGHT one with the check type needs the registry, which SQL
   cannot consult. The test for it reduces both worlds to a comparable string,
   so deleting the validation prints the STORED ROW -- `threshold_num=95` on a
   boolean check -- rather than "DID NOT RAISE".

3. THE THREE ASSIGNMENT UPSERTS ARE EXECUTED AGAINST THE REAL DATABASE. They
   target PARTIAL indexes, so `ON CONFLICT ON CONSTRAINT` fails at RUNTIME with
   `InvalidObjectDefinition` -- a unit test that never runs the statement cannot
   see that, which is why every one of the three has a test that commits.

4. THE `org_id` PREDICATE TESTS RUN ON THE ADMIN ENGINE. Through `org_session`
   they pass with the predicate deleted, because RLS rejects first.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy import text as sqltext
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import AuditEvent, HealthAlert, HealthCheck, HealthProfile
from app.modules.health.errors import (
    HealthConflictError,
    HealthNotFoundError,
    HealthValidationError,
)
from app.modules.health.profiles import (
    clone_profile,
    create_assignment,
    create_profile,
    delete_assignment,
    delete_profile,
    effective_for_seconds,
    get_profile,
    list_assignments,
    list_checks,
    list_profiles,
    replace_checks,
    update_profile,
)
from tests.health_fixtures import seed_alert, seed_device, seed_group, seed_org

CPU = {
    "check_type": "cpu.busy_pct",
    "comparator": "gt",
    "threshold_num": 95,
    "severity": "warning",
}
MEM = {
    "check_type": "memory.used_pct",
    "comparator": "gt",
    "threshold_num": 90,
    "severity": "warning",
}
ENCRYPTION = {
    "check_type": "posture.disk_encryption",
    "comparator": "is",
    "threshold_bool": True,
    "severity": "critical",
}


async def _audit(admin_engine: AsyncEngine, org_id: UUID) -> list[AuditEvent]:
    async with AsyncSession(admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == org_id).order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )


async def _policy_version(admin_engine: AsyncEngine, profile_id: UUID) -> int:
    async with admin_engine.connect() as conn:
        return (
            await conn.execute(
                sqltext("SELECT policy_version FROM health_profile WHERE id = :p"),
                {"p": profile_id},
            )
        ).scalar_one()


async def _make_profile(api_env, org, **kw: Any) -> HealthProfile:
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        return await create_profile(
            session, org.org_id, org.user_id, name=kw.pop("name", f"p-{uuid4().hex[:8]}"), **kw
        )


# ---------------------------------------------------------------------------
# Profile CRUD
# ---------------------------------------------------------------------------


async def test_create_profile_stores_every_field_and_audits(api_env) -> None:
    """Every non-default column is set to a non-default value: a create that
    hardcoded `tick_interval_s=300` would pass a test that only asserts the
    name."""
    org = await seed_org(api_env, "prof-c")
    profile = await _make_profile(
        api_env,
        org,
        name="Servers",
        description="the noisy ones",
        tick_interval_s=120,
        posture_interval_s=1800,
        summary_interval_s=900,
        active=False,
    )
    assert (profile.name, profile.description) == ("Servers", "the noisy ones")
    assert (profile.tick_interval_s, profile.posture_interval_s) == (120, 1800)
    assert profile.summary_interval_s == 900
    assert profile.active is False
    assert profile.policy_version == 1
    assert profile.created_by == org.user_id

    events = await _audit(api_env.admin_engine, org.org_id)
    assert [(e.action, e.target_id) for e in events] == [
        ("health.profile_created", str(profile.id))
    ]


async def test_a_duplicate_profile_name_is_a_typed_error(api_env) -> None:
    org = await seed_org(api_env, "prof-dup")
    await _make_profile(api_env, org, name="Servers")
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        with pytest.raises(HealthValidationError):
            await create_profile(session, org.org_id, org.user_id, name="Servers")


async def test_out_of_range_intervals_are_typed_errors_not_500s(api_env) -> None:
    org = await seed_org(api_env, "prof-rng")
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        with pytest.raises(HealthValidationError):
            await create_profile(session, org.org_id, org.user_id, name="a", tick_interval_s=30)
        with pytest.raises(HealthValidationError):
            await create_profile(
                session, org.org_id, org.user_id, name="b", posture_interval_s=600
            )
        with pytest.raises(HealthValidationError):
            await create_profile(
                session,
                org.org_id,
                org.user_id,
                name="c",
                tick_interval_s=3000,
                posture_interval_s=900,
            )


async def test_getting_another_orgs_profile_raises_not_found(api_env) -> None:
    """404, never 403 -- a 403 confirms the id exists, which is a cross-tenant
    existence oracle."""
    a = await seed_org(api_env, "prof-xa")
    b = await seed_org(api_env, "prof-xb")
    profile = await _make_profile(api_env, b)
    async with org_session(a.org_id, engine=api_env.app_engine) as session:
        with pytest.raises(HealthNotFoundError):
            await get_profile(session, a.org_id, profile.id)


# ---------------------------------------------------------------------------
# policy_version -- server-owned, bumps on EVERY write path
# ---------------------------------------------------------------------------


async def test_renaming_a_profile_bumps_policy_version(api_env) -> None:
    """THE WRITE PATH AN IMPLEMENTATION FORGETS. §2.1: `policy_version`
    increments on ANY write to the profile OR its checks. A test that only
    replaces checks passes against a bump that lives in `replace_checks`
    alone -- and a renamed profile whose version did not move is a profile
    §12's reconciliation sweep believes every device has already
    acknowledged."""
    org = await seed_org(api_env, "pv-name")
    profile = await _make_profile(api_env, org)
    assert profile.policy_version == 1

    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        updated = await update_profile(
            session, org.org_id, org.user_id, profile.id, {"name": "Renamed"}
        )
    assert updated.name == "Renamed"
    assert updated.policy_version == 2
    assert await _policy_version(api_env.admin_engine, profile.id) == 2


async def test_replacing_checks_bumps_policy_version(api_env) -> None:
    org = await seed_org(api_env, "pv-checks")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await replace_checks(session, org.org_id, org.user_id, profile.id, [CPU])
    assert await _policy_version(api_env.admin_engine, profile.id) == 2
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await replace_checks(session, org.org_id, org.user_id, profile.id, [CPU, MEM])
    assert await _policy_version(api_env.admin_engine, profile.id) == 3


async def test_no_write_path_accepts_a_client_supplied_policy_version(api_env) -> None:
    """§2.1: it is server-owned and no API accepts it. A client-supplied
    version would let a caller silence a fleet by claiming convergence -- §12
    re-pushes only where the acknowledged version is BEHIND."""
    org = await seed_org(api_env, "pv-client")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        with pytest.raises(HealthValidationError):
            await update_profile(
                session, org.org_id, org.user_id, profile.id, {"policy_version": 99}
            )
    assert await _policy_version(api_env.admin_engine, profile.id) == 1


async def test_an_unknown_profile_field_is_refused_not_ignored(api_env) -> None:
    org = await seed_org(api_env, "pv-unk")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        with pytest.raises(HealthValidationError):
            await update_profile(session, org.org_id, org.user_id, profile.id, {"tick": 300})


# ---------------------------------------------------------------------------
# replace_checks -- validation against the registry
# ---------------------------------------------------------------------------


async def _replace_outcome(api_env, org, profile_id: UUID, specs: list[dict[str, Any]]) -> str:
    """Reduce both worlds to a comparable string: the typed error, or the rows
    that were actually stored. A `pytest.raises` here would print "DID NOT
    RAISE" and hide the fact that the database now holds a boolean check with a
    numeric threshold."""
    try:
        async with org_session(org.org_id, engine=api_env.app_engine) as session:
            await replace_checks(session, org.org_id, org.user_id, profile_id, specs)
    except HealthValidationError as exc:
        return f"refused: {exc}"
    async with api_env.admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                sqltext(
                    "SELECT check_type, comparator, threshold_num, threshold_bool, "
                    "for_ticks, os_family FROM health_check WHERE profile_id = :p "
                    "AND retired_at IS NULL ORDER BY check_type"
                ),
                {"p": profile_id},
            )
        ).all()
    return f"STORED: {[tuple(r) for r in rows]}"


async def test_a_boolean_check_with_a_numeric_threshold_is_refused(api_env) -> None:
    """THE VALIDATION SQL CANNOT DO. `ck_health_check_one_threshold` is happy
    with `threshold_num=95` on `posture.disk_encryption` -- exactly one column
    is set. Only the registry knows that column is the wrong one. Delete the
    check and this prints
    `STORED: [('posture.disk_encryption', 'is', Decimal('95.0000'), None, 3, None)]`."""
    org = await seed_org(api_env, "chk-kind")
    profile = await _make_profile(api_env, org)
    outcome = await _replace_outcome(
        api_env,
        org,
        profile.id,
        [
            {
                "check_type": "posture.disk_encryption",
                "comparator": "is",
                "threshold_num": 95,
                "severity": "critical",
            }
        ],
    )
    assert outcome.startswith("refused: "), outcome
    assert "threshold_bool" in outcome


async def test_a_numeric_check_with_a_boolean_threshold_is_refused(api_env) -> None:
    """The mirror image, so a validation that only ever looked at one direction
    cannot pass."""
    org = await seed_org(api_env, "chk-kind2")
    profile = await _make_profile(api_env, org)
    outcome = await _replace_outcome(
        api_env,
        org,
        profile.id,
        [
            {
                "check_type": "cpu.busy_pct",
                "comparator": "gt",
                "threshold_bool": True,
                "severity": "warning",
            }
        ],
    )
    assert outcome.startswith("refused: "), outcome
    assert "threshold_num" in outcome


async def test_both_thresholds_at_once_is_refused(api_env) -> None:
    org = await seed_org(api_env, "chk-both")
    profile = await _make_profile(api_env, org)
    outcome = await _replace_outcome(
        api_env, org, profile.id, [{**CPU, "threshold_bool": True}]
    )
    assert outcome.startswith("refused: "), outcome


async def test_a_numeric_comparator_on_a_boolean_check_is_refused(api_env) -> None:
    """`gt` is a legal member of `ck_health_check_comparator`, so the database
    cannot see this: only the registry knows `posture.disk_encryption` takes
    `is` and nothing else. Delete the check and this prints a stored
    `('posture.disk_encryption', 'gt', ...)`."""
    org = await seed_org(api_env, "chk-cmp")
    profile = await _make_profile(api_env, org)
    outcome = await _replace_outcome(
        api_env, org, profile.id, [{**ENCRYPTION, "comparator": "gt"}]
    )
    assert outcome.startswith("refused: "), outcome
    assert "comparator" in outcome


async def test_an_os_family_the_check_cannot_answer_is_refused(api_env) -> None:
    """§4.1: AV health is Windows-only. A check pinned to `linux` would be
    stored, pushed to nothing (resolution filters by OS), and appear in the
    console forever as a rule that never evaluates."""
    org = await seed_org(api_env, "chk-os")
    profile = await _make_profile(api_env, org)
    outcome = await _replace_outcome(
        api_env,
        org,
        profile.id,
        [
            {
                "check_type": "posture.antivirus_healthy",
                "comparator": "is",
                "threshold_bool": True,
                "severity": "critical",
                "os_family": "linux",
            }
        ],
    )
    assert outcome.startswith("refused: "), outcome
    assert "os_family" in outcome


async def test_a_windows_only_check_pinned_to_windows_is_accepted(api_env) -> None:
    """The positive control: a validator that refused every `os_family` would
    pass the test above."""
    org = await seed_org(api_env, "chk-os-ok")
    profile = await _make_profile(api_env, org)
    outcome = await _replace_outcome(
        api_env,
        org,
        profile.id,
        [
            {
                "check_type": "posture.antivirus_healthy",
                "comparator": "is",
                "threshold_bool": True,
                "severity": "critical",
                "os_family": "windows",
            }
        ],
    )
    assert outcome.startswith("STORED: "), outcome


async def test_an_unregistered_check_type_is_refused(api_env) -> None:
    org = await seed_org(api_env, "chk-unreg")
    profile = await _make_profile(api_env, org)
    outcome = await _replace_outcome(
        api_env, org, profile.id, [{**CPU, "check_type": "cpu.temperature"}]
    )
    assert outcome.startswith("refused: "), outcome


async def test_a_selector_on_a_selectorless_check_is_refused(api_env) -> None:
    """`cpu.busy_pct` takes no selector (§4.1). Allowing one lets the same
    check be stored twice in a profile past `uq_health_check_profile_type_
    selector_os`, and pushed twice to every device."""
    org = await seed_org(api_env, "chk-sel")
    profile = await _make_profile(api_env, org)
    outcome = await _replace_outcome(api_env, org, profile.id, [{**CPU, "selector": "C:"}])
    assert outcome.startswith("refused: "), outcome


@pytest.mark.parametrize("for_ticks", [0, 61])
async def test_for_ticks_out_of_range_is_refused(api_env, for_ticks: int) -> None:
    org = await seed_org(api_env, f"chk-ft{for_ticks}")
    profile = await _make_profile(api_env, org)
    outcome = await _replace_outcome(
        api_env, org, profile.id, [{**CPU, "for_ticks": for_ticks}]
    )
    assert outcome.startswith("refused: "), outcome


async def test_a_duplicate_key_inside_one_replacement_is_refused(api_env) -> None:
    """Two rows with the same `(check_type, selector, os_family)` in ONE
    request. The index would refuse it too, but as an `IntegrityError` that
    aborts the transaction and surfaces as a 500."""
    org = await seed_org(api_env, "chk-dupe")
    profile = await _make_profile(api_env, org)
    outcome = await _replace_outcome(api_env, org, profile.id, [CPU, {**CPU, "threshold_num": 80}])
    assert outcome.startswith("refused: "), outcome


async def test_more_than_thirty_two_checks_is_a_typed_error(api_env) -> None:
    """The trigger would refuse it as well (§0.11), but as a raw exception the
    API cannot translate into "you asked for 40, the cap is 32"."""
    org = await seed_org(api_env, "chk-cap")
    profile = await _make_profile(api_env, org)
    specs = [
        {**CPU, "check_type": "disk.free_pct", "selector": f"/mnt/{i}"} for i in range(33)
    ]
    outcome = await _replace_outcome(api_env, org, profile.id, specs)
    assert outcome.startswith("refused: "), outcome
    assert "32" in outcome


async def test_thirty_two_checks_are_accepted(api_env) -> None:
    """The positive control for the cap."""
    org = await seed_org(api_env, "chk-cap32")
    profile = await _make_profile(api_env, org)
    specs = [
        {**CPU, "check_type": "disk.free_pct", "selector": f"/mnt/{i}"} for i in range(32)
    ]
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        rows = await replace_checks(session, org.org_id, org.user_id, profile.id, specs)
    assert len(rows) == 32


async def test_the_enum_check_type_stores_its_threshold_in_the_boolean_column(
    api_env,
) -> None:
    """§4.1's amendment. `service.state` is an enum over exactly two members
    and `threshold_bool = true` means the first one (`running`)."""
    org = await seed_org(api_env, "chk-enum")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        rows = await replace_checks(
            session,
            org.org_id,
            org.user_id,
            profile.id,
            [
                {
                    "check_type": "service.state",
                    "selector": "Spooler",
                    "comparator": "is",
                    "threshold_bool": True,
                    "severity": "warning",
                }
            ],
        )
    assert (rows[0].threshold_bool, rows[0].threshold_num) == (True, None)


# ---------------------------------------------------------------------------
# for_seconds -> for_ticks, against the PROFILE's own tick interval (§0.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tick_interval_s", "for_seconds", "expected_ticks"),
    [(300, 900, 3), (60, 900, 15), (300, 901, 4), (120, 60, 1)],
)
async def test_for_seconds_converts_against_this_profiles_tick_interval(
    api_env, tick_interval_s: int, for_seconds: int, expected_ticks: int
) -> None:
    """§0.3, and the reason the parametrisation carries TWO tick intervals: a
    test with one interval cannot see a hardcoded divisor. 900 s is 3 ticks at
    a 5-minute tick and 15 at a 1-minute tick, and those two profiles behave
    completely differently on a spiky workload -- which is the whole reason
    duration is stored in ticks and not seconds."""
    org = await seed_org(api_env, "fs")
    profile = await _make_profile(
        api_env, org, tick_interval_s=tick_interval_s, posture_interval_s=3600
    )
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        rows = await replace_checks(
            session, org.org_id, org.user_id, profile.id, [{**CPU, "for_seconds": for_seconds}]
        )
    assert rows[0].for_ticks == expected_ticks
    assert effective_for_seconds(rows[0], profile) == expected_ticks * tick_interval_s


async def test_for_ticks_and_for_seconds_together_are_refused(api_env) -> None:
    """Two answers to one question. Accepting both means the stored duration
    depends on which branch the implementation happened to check first."""
    org = await seed_org(api_env, "fs-both")
    profile = await _make_profile(api_env, org)
    outcome = await _replace_outcome(
        api_env, org, profile.id, [{**CPU, "for_ticks": 3, "for_seconds": 900}]
    )
    assert outcome.startswith("refused: "), outcome


async def test_a_for_seconds_below_one_tick_still_stores_at_least_one_tick(api_env) -> None:
    """`ceil` on a sub-tick duration, not `0` -- and `for_ticks BETWEEN 1 AND
    60` would otherwise make the request a 500."""
    org = await seed_org(api_env, "fs-sub")
    profile = await _make_profile(api_env, org, tick_interval_s=300)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        rows = await replace_checks(
            session, org.org_id, org.user_id, profile.id, [{**CPU, "for_seconds": 10}]
        )
    assert rows[0].for_ticks == 1


# ---------------------------------------------------------------------------
# replace_checks -- the set replacement itself, and the retire path
# ---------------------------------------------------------------------------


async def test_replace_checks_is_a_set_replacement(api_env) -> None:
    org = await seed_org(api_env, "rc-set")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await replace_checks(session, org.org_id, org.user_id, profile.id, [CPU, MEM])
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await replace_checks(session, org.org_id, org.user_id, profile.id, [MEM, ENCRYPTION])
        live = await list_checks(session, org.org_id, profile.id)
    assert sorted(c.check_type for c in live) == ["memory.used_pct", "posture.disk_encryption"]


async def test_a_surviving_check_keeps_its_id_across_a_replacement(api_env) -> None:
    """An alert's dedup key is `(org, device, check_id, selector)`. Deleting and
    re-inserting an unchanged check on every PUT would mint a new `check_id`
    and therefore re-open every alert the profile carries, once per edit."""
    org = await seed_org(api_env, "rc-id")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        before = await replace_checks(session, org.org_id, org.user_id, profile.id, [CPU])
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        after = await replace_checks(
            session, org.org_id, org.user_id, profile.id, [{**CPU, "threshold_num": 80}, MEM]
        )
    cpu_after = next(c for c in after if c.check_type == "cpu.busy_pct")
    assert cpu_after.id == before[0].id
    assert float(cpu_after.threshold_num) == 80.0


async def test_removing_a_check_retires_it_and_clears_its_alerts(api_env) -> None:
    """§2.4's amendment and §0.8 clause 4. The row stays (an alert's `check_id`
    is `ON DELETE RESTRICT` and part of the dedup key); the POLICY does not.
    Both halves are asserted: the check is retired AND the alert is cleared with
    `policy_removed`, because clearing without retiring leaves the check
    running and retiring without clearing leaves a permanent alert."""
    org = await seed_org(api_env, "rc-retire")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        rows = await replace_checks(session, org.org_id, org.user_id, profile.id, [CPU, MEM])
    cpu_id = next(c.id for c in rows if c.check_type == "cpu.busy_pct")
    alert_id = await seed_alert(api_env, org, profile_id=profile.id, check_id=cpu_id)

    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await replace_checks(session, org.org_id, org.user_id, profile.id, [MEM])

    async with AsyncSession(api_env.admin_engine) as session:
        check = await session.get(HealthCheck, cpu_id)
        alert = await session.get(HealthAlert, alert_id)
    assert check is not None and check.retired_at is not None
    assert alert is not None
    assert (alert.state, alert.cleared_reason) == ("cleared", "policy_removed")
    assert alert.cleared_at is not None


async def test_a_check_that_never_alarmed_is_hard_deleted_not_retired(api_env) -> None:
    """The other half of §2.4's decision: RESTRICT only fires where an alert
    actually references the row, so the ordinary "added by mistake, removed the
    same minute" path leaves no permanent retired row consuming a slot."""
    org = await seed_org(api_env, "rc-hard")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        rows = await replace_checks(session, org.org_id, org.user_id, profile.id, [CPU, MEM])
    cpu_id = next(c.id for c in rows if c.check_type == "cpu.busy_pct")
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await replace_checks(session, org.org_id, org.user_id, profile.id, [MEM])

    async with AsyncSession(api_env.admin_engine) as session:
        assert await session.get(HealthCheck, cpu_id) is None


async def test_re_adding_a_retired_check_mints_a_new_id(api_env) -> None:
    """§2.4: "an alert history that silently spans a removal and a re-add is a
    history that lies about continuity"."""
    org = await seed_org(api_env, "rc-readd")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        rows = await replace_checks(session, org.org_id, org.user_id, profile.id, [CPU])
    cpu_id = rows[0].id
    await seed_alert(api_env, org, profile_id=profile.id, check_id=cpu_id)

    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await replace_checks(session, org.org_id, org.user_id, profile.id, [])
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        again = await replace_checks(session, org.org_id, org.user_id, profile.id, [CPU])
    assert again[0].id != cpu_id


async def test_an_empty_check_set_is_legal(api_env) -> None:
    """A profile with no checks is a disarm, not an error (§5.2)."""
    org = await seed_org(api_env, "rc-empty")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await replace_checks(session, org.org_id, org.user_id, profile.id, [CPU])
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        rows = await replace_checks(session, org.org_id, org.user_id, profile.id, [])
    assert rows == []


async def test_replace_checks_audits_what_it_changed(api_env) -> None:
    org = await seed_org(api_env, "rc-audit")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await replace_checks(session, org.org_id, org.user_id, profile.id, [CPU, MEM])
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await replace_checks(session, org.org_id, org.user_id, profile.id, [MEM, ENCRYPTION])

    events = [e for e in await _audit(api_env.admin_engine, org.org_id) if "check" in e.action]
    assert [e.action for e in events] == ["health.checks_replaced", "health.checks_replaced"]
    assert events[1].meta["added"] == ["posture.disk_encryption"]
    assert events[1].meta["removed"] == ["cpu.busy_pct"]
    assert events[1].meta["policy_version"] == 3


# ---------------------------------------------------------------------------
# clone_profile (§0.2's stated mitigation)
# ---------------------------------------------------------------------------


async def test_clone_profile_deep_copies_the_checks_and_creates_no_assignment(
    api_env,
) -> None:
    """§0.2: the mitigation for "resolution by specificity means an operator who
    wants the org profile PLUS one extra check must clone it". A clone that
    shared check rows would make editing the copy edit the original; a clone
    that carried the assignment would silently retarget the source's devices."""
    org = await seed_org(api_env, "clone")
    source = await _make_profile(api_env, org, tick_interval_s=120, posture_interval_s=1800)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        original = await replace_checks(session, org.org_id, org.user_id, source.id, [CPU, MEM])
        await create_assignment(
            session, org.org_id, org.user_id, profile_id=source.id, device_id=org.device_id
        )

    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        clone = await clone_profile(session, org.org_id, org.user_id, source.id, name="Copy")
        cloned_checks = await list_checks(session, org.org_id, clone.id)
        assignments = await list_assignments(session, org.org_id, profile_id=clone.id)

    assert clone.id != source.id
    assert clone.name == "Copy"
    assert (clone.tick_interval_s, clone.posture_interval_s) == (120, 1800)
    assert clone.policy_version == 1
    assert sorted(c.check_type for c in cloned_checks) == ["cpu.busy_pct", "memory.used_pct"]
    assert {c.id for c in cloned_checks}.isdisjoint({c.id for c in original})
    assert assignments == []


async def test_cloning_does_not_bump_the_sources_policy_version(api_env) -> None:
    """A clone is not a write to the source. Bumping it would make §12 re-push
    the source's whole device set for a copy nobody assigned."""
    org = await seed_org(api_env, "clone-pv")
    source = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await replace_checks(session, org.org_id, org.user_id, source.id, [CPU])
    before = await _policy_version(api_env.admin_engine, source.id)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await clone_profile(session, org.org_id, org.user_id, source.id, name="Copy")
    assert await _policy_version(api_env.admin_engine, source.id) == before


async def test_cloning_a_retired_check_does_not_copy_it(api_env) -> None:
    """A retired check is history, and history is never pushed to an endpoint
    (§3). A clone that copied it would resurrect a rule an operator removed."""
    org = await seed_org(api_env, "clone-ret")
    source = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        rows = await replace_checks(session, org.org_id, org.user_id, source.id, [CPU, MEM])
    await seed_alert(
        api_env,
        org,
        profile_id=source.id,
        check_id=next(c.id for c in rows if c.check_type == "cpu.busy_pct"),
    )
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await replace_checks(session, org.org_id, org.user_id, source.id, [MEM])
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        clone = await clone_profile(session, org.org_id, org.user_id, source.id, name="Copy")
        cloned = await list_checks(session, org.org_id, clone.id)
    assert [c.check_type for c in cloned] == ["memory.used_pct"]


# ---------------------------------------------------------------------------
# Assignments -- three partial-index upserts, each executed for real
# ---------------------------------------------------------------------------


async def test_a_device_level_assignment_upsert_executes(api_env) -> None:
    """`ON CONFLICT ON CONSTRAINT uq_health_assignment_device` raises
    `InvalidObjectDefinition` at RUNTIME -- a partial unique index has no
    `pg_constraint` row to name. Only a test that commits the statement sees
    it, which is why all three levels get one."""
    org = await seed_org(api_env, "asg-d")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        row = await create_assignment(
            session, org.org_id, org.user_id, profile_id=profile.id, device_id=org.device_id
        )
    assert row.device_id == org.device_id
    assert row.device_group_id is None


async def test_a_group_level_assignment_upsert_executes(api_env) -> None:
    org = await seed_org(api_env, "asg-g")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        row = await create_assignment(
            session, org.org_id, org.user_id, profile_id=profile.id, device_group_id=org.group_id
        )
    assert row.device_group_id == org.group_id


async def test_an_org_default_assignment_upsert_executes(api_env) -> None:
    org = await seed_org(api_env, "asg-o")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        row = await create_assignment(session, org.org_id, org.user_id, profile_id=profile.id)
    assert (row.device_id, row.device_group_id) == (None, None)


@pytest.mark.parametrize("level", ["device", "group", "org"])
async def test_a_second_assignment_at_one_level_names_the_existing_one(
    api_env, level: str
) -> None:
    """`ON CONFLICT DO NOTHING` + `RETURNING`, not check-then-insert: the race
    loser gets the same typed error a pre-check would raise and never the
    winner's row. The message names the profile already assigned, because
    "conflict" alone does not tell an operator which of their profiles is in
    the way."""
    org = await seed_org(api_env, f"asg2-{level}")
    first = await _make_profile(api_env, org, name="First")
    second = await _make_profile(api_env, org, name="Second")
    target: dict[str, Any] = {
        "device": {"device_id": org.device_id},
        "group": {"device_group_id": org.group_id},
        "org": {},
    }[level]

    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await create_assignment(session, org.org_id, org.user_id, profile_id=first.id, **target)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        with pytest.raises(HealthValidationError) as exc:
            await create_assignment(
                session, org.org_id, org.user_id, profile_id=second.id, **target
            )
    assert str(first.id) in str(exc.value)


async def test_one_assignment_at_each_level_coexists(api_env) -> None:
    """The other half. §3's precedence is unexpressible if the three levels
    cannot hold a row each."""
    org = await seed_org(api_env, "asg-all")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await create_assignment(
            session, org.org_id, org.user_id, profile_id=profile.id, device_id=org.device_id
        )
        await create_assignment(
            session, org.org_id, org.user_id, profile_id=profile.id, device_group_id=org.group_id
        )
        await create_assignment(session, org.org_id, org.user_id, profile_id=profile.id)
        rows = await list_assignments(session, org.org_id)
    assert len(rows) == 3


async def test_an_assignment_naming_both_a_device_and_a_group_is_refused(api_env) -> None:
    org = await seed_org(api_env, "asg-both")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        with pytest.raises(HealthValidationError):
            await create_assignment(
                session,
                org.org_id,
                org.user_id,
                profile_id=profile.id,
                device_id=org.device_id,
                device_group_id=org.group_id,
            )


async def test_an_assignment_to_another_orgs_device_is_refused(api_env) -> None:
    """A plain FK does not enforce tenancy. Without this check, org A could
    assign a health profile -- and therefore a remediation trigger -- to org B's
    machine."""
    a = await seed_org(api_env, "asg-xa")
    b = await seed_org(api_env, "asg-xb")
    profile = await _make_profile(api_env, a)
    async with org_session(a.org_id, engine=api_env.app_engine) as session:
        with pytest.raises(HealthValidationError):
            await create_assignment(
                session, a.org_id, a.user_id, profile_id=profile.id, device_id=b.device_id
            )


async def test_deleting_an_assignment_audits_and_removes_it(api_env) -> None:
    org = await seed_org(api_env, "asg-del")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        row = await create_assignment(
            session, org.org_id, org.user_id, profile_id=profile.id, device_id=org.device_id
        )
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await delete_assignment(session, org.org_id, org.user_id, row.id)
        assert await list_assignments(session, org.org_id) == []

    actions = [e.action for e in await _audit(api_env.admin_engine, org.org_id)]
    assert "health.assignment_deleted" in actions


# ---------------------------------------------------------------------------
# delete_profile
# ---------------------------------------------------------------------------


async def test_a_profile_with_an_assignment_cannot_be_deleted(api_env) -> None:
    """§10, and the error names the assignments: "in use" alone leaves the
    operator to find which of a hundred devices still points at it."""
    org = await seed_org(api_env, "del-asg")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        assignment = await create_assignment(
            session, org.org_id, org.user_id, profile_id=profile.id, device_id=org.device_id
        )
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        with pytest.raises(HealthConflictError) as exc:
            await delete_profile(session, org.org_id, org.user_id, profile.id)
    assert str(assignment.id) in str(exc.value)


async def test_a_profile_with_alerts_cannot_be_deleted(api_env) -> None:
    """`health_alert.profile_id` and `health_alert.check_id` both reference the
    profile's rows, and an alert is an incident record §12's retention sweep
    owns. Deleting the profile would either destroy that history or fail with a
    bare `IntegrityError`; it does neither."""
    org = await seed_org(api_env, "del-alert")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        rows = await replace_checks(session, org.org_id, org.user_id, profile.id, [CPU])
    alert_id = await seed_alert(api_env, org, profile_id=profile.id, check_id=rows[0].id)

    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        with pytest.raises(HealthConflictError) as exc:
            await delete_profile(session, org.org_id, org.user_id, profile.id)
    assert str(alert_id) in str(exc.value)


async def test_deleting_a_profile_removes_its_checks_and_audits(api_env) -> None:
    org = await seed_org(api_env, "del-ok")
    profile = await _make_profile(api_env, org)
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await replace_checks(session, org.org_id, org.user_id, profile.id, [CPU, MEM])
    async with org_session(org.org_id, engine=api_env.app_engine) as session:
        await delete_profile(session, org.org_id, org.user_id, profile.id)

    async with api_env.admin_engine.connect() as conn:
        remaining = (
            await conn.execute(
                sqltext("SELECT count(*) FROM health_check WHERE profile_id = :p"),
                {"p": profile.id},
            )
        ).scalar_one()
        profiles = (
            await conn.execute(
                sqltext("SELECT count(*) FROM health_profile WHERE id = :p"), {"p": profile.id}
            )
        ).scalar_one()
    assert (remaining, profiles) == (0, 0)
    actions = [e.action for e in await _audit(api_env.admin_engine, org.org_id)]
    assert "health.profile_deleted" in actions


# ---------------------------------------------------------------------------
# Tenancy -- load-bearing, and only visible on the admin engine
# ---------------------------------------------------------------------------


async def test_list_and_get_carry_their_own_org_predicate(admin_engine) -> None:
    """THE ONE RLS CANNOT MASK. Driven through `org_session` this passes with
    the `org_id` predicate deleted from `list_profiles`, because RLS removes the
    other org's rows before the query runs. On the admin engine the predicate is
    the only thing separating them: delete it and org A's list contains org B's
    profile, and `get_profile` returns it instead of raising."""
    a = await seed_org(admin_engine, "pred-a")
    b = await seed_org(admin_engine, "pred-b")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(HealthProfile(org_id=a.org_id, name="A's profile", description=""))
        session.add(HealthProfile(org_id=b.org_id, name="B's profile", description=""))
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        names_a = [p.name for p in await list_profiles(session, a.org_id)]
        names_b = [p.name for p in await list_profiles(session, b.org_id)]
        b_profiles = await list_profiles(session, b.org_id)
        b_profile_id = next(p.id for p in b_profiles if p.name == "B's profile")
        try:
            leaked = (await get_profile(session, a.org_id, b_profile_id)).name
        except HealthNotFoundError:
            leaked = "refused"

    # Asserted before the `get_profile` outcome, because the leaked LIST is the
    # more informative failure: it names every other tenant's profile.
    assert names_a == ["A's profile"], names_a
    assert names_b == ["B's profile"], names_b
    assert leaked == "refused"


async def test_list_checks_and_assignments_carry_their_own_org_predicate(
    admin_engine,
) -> None:
    """Same shape, for the two list endpoints an operator actually reads from.
    `list_checks` additionally excludes retired rows -- a retired check leaking
    into the effective-profile view is §2.2's soft delete leaking."""
    from app.models import HealthCheck as CheckModel
    from app.models import HealthProfileAssignment

    a = await seed_org(admin_engine, "pred2-a")
    b = await seed_org(admin_engine, "pred2-b")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        profile_a = HealthProfile(org_id=a.org_id, name="A", description="")
        profile_b = HealthProfile(org_id=b.org_id, name="B", description="")
        session.add_all([profile_a, profile_b])
        await session.flush()
        session.add_all(
            [
                CheckModel(
                    org_id=a.org_id,
                    profile_id=profile_a.id,
                    check_type="cpu.busy_pct",
                    comparator="gt",
                    threshold_num=95,
                    severity="warning",
                ),
                CheckModel(
                    org_id=b.org_id,
                    profile_id=profile_b.id,
                    check_type="memory.used_pct",
                    comparator="gt",
                    threshold_num=90,
                    severity="warning",
                ),
                HealthProfileAssignment(org_id=a.org_id, profile_id=profile_a.id),
                HealthProfileAssignment(org_id=b.org_id, profile_id=profile_b.id),
            ]
        )
        await session.commit()
        profile_a_id, profile_b_id = profile_a.id, profile_b.id

    async with AsyncSession(admin_engine) as session:
        assert [c.check_type for c in await list_checks(session, a.org_id, profile_a_id)] == [
            "cpu.busy_pct"
        ]
        # B's profile id, read in A's org, resolves to nothing rather than to
        # B's checks.
        assert await list_checks(session, a.org_id, profile_b_id) == []
        assert [r.profile_id for r in await list_assignments(session, a.org_id)] == [profile_a_id]


async def test_a_cross_org_profile_id_cannot_be_written_through(api_env) -> None:
    """Every mutating entry point takes `org_id` separately from the row id, so
    each must re-scope. Without it, knowing another tenant's profile UUID is
    enough to retire its checks."""
    a = await seed_org(api_env, "wx-a")
    b = await seed_org(api_env, "wx-b")
    victim = await _make_profile(api_env, b)
    async with org_session(a.org_id, engine=api_env.app_engine) as session:
        for call in (
            lambda: update_profile(session, a.org_id, a.user_id, victim.id, {"name": "x"}),
            lambda: replace_checks(session, a.org_id, a.user_id, victim.id, [CPU]),
            lambda: clone_profile(session, a.org_id, a.user_id, victim.id, name="x"),
            lambda: delete_profile(session, a.org_id, a.user_id, victim.id),
        ):
            with pytest.raises(HealthNotFoundError):
                await call()


async def test_an_assignment_id_from_another_org_is_not_deletable(api_env) -> None:
    a = await seed_org(api_env, "wd-a")
    b = await seed_org(api_env, "wd-b")
    profile = await _make_profile(api_env, b)
    async with org_session(b.org_id, engine=api_env.app_engine) as session:
        row = await create_assignment(
            session, b.org_id, b.user_id, profile_id=profile.id, device_id=b.device_id
        )
    async with org_session(a.org_id, engine=api_env.app_engine) as session:
        with pytest.raises(HealthNotFoundError):
            await delete_assignment(session, a.org_id, a.user_id, row.id)


async def test_a_group_from_another_org_cannot_be_assigned(api_env) -> None:
    a = await seed_org(api_env, "wg-a")
    b = await seed_org(api_env, "wg-b")
    profile = await _make_profile(api_env, a)
    foreign_group = await seed_group(api_env, b.org_id)
    async with org_session(a.org_id, engine=api_env.app_engine) as session:
        with pytest.raises(HealthValidationError):
            await create_assignment(
                session, a.org_id, a.user_id, profile_id=profile.id, device_group_id=foreign_group
            )


async def test_two_orgs_may_hold_a_profile_of_the_same_name(api_env) -> None:
    """Uniqueness is `(org_id, name)`. Without the `org_id` in the constraint
    the first tenant to name a profile "Servers" would take the name globally."""
    a = await seed_org(api_env, "nm-a")
    b = await seed_org(api_env, "nm-b")
    await _make_profile(api_env, a, name="Servers")
    await _make_profile(api_env, b, name="Servers")
    async with org_session(a.org_id, engine=api_env.app_engine) as session:
        assert [p.name for p in await list_profiles(session, a.org_id)] == ["Servers"]


async def test_a_device_from_another_org_is_invisible_to_assignment_listing(
    api_env,
) -> None:
    """A sanity check on the fixture itself: `seed_device` in another org must
    not appear anywhere in this org's assignment view."""
    a = await seed_org(api_env, "dv-a")
    b = await seed_org(api_env, "dv-b")
    other_device = await seed_device(api_env, b.org_id)
    profile = await _make_profile(api_env, a)
    async with org_session(a.org_id, engine=api_env.app_engine) as session:
        await create_assignment(
            session, a.org_id, a.user_id, profile_id=profile.id, device_id=a.device_id
        )
        rows = await list_assignments(session, a.org_id)
    assert [r.device_id for r in rows] == [a.device_id]
    assert other_device not in [r.device_id for r in rows]
