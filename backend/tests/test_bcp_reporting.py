"""Tests for BCP/DR reporting (docs/modules/bcp/spec.md): the per-test SOC 2
evidence report, the period rollup, and the plan-status dashboard with its
overdue-review flag.

Mirrors `tests/test_bcp_test_service.py`'s fixture style (throwaway org/user
seeded via `admin_engine`, service calls through the RLS-constrained
`org_session`, backup-evidence seeding via `_seed_repo`/`_seed_device`/
`_seed_complete_point`) and `tests/test_change_mgmt_reporting.py`'s
reporting-test structure: drive real service calls to produce genuine rows,
then assert what the pure reporting layer surfaces, hand-computing every
expected aggregate rather than re-deriving the formula under test.
"""

import datetime as _dt
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    BackupPoint,
    BackupRepository,
    BackupRun,
    BcpPlan,
    Device,
    Organization,
    User,
)
from app.modules.bcp.reporting import (
    _is_review_overdue,
    bcp_period_report,
    bcp_plan_status,
    bcp_test_report,
)
from app.modules.bcp.service import (
    create_finding,
    create_plan,
    record_test,
    update_finding,
    upsert_step,
)
from app.modules.bcp.service import create_test as bcp_create_test


async def _org_with_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"bcprpt-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        s.add(user)
        await s.commit()
        return org.id, user.id


async def _seed_device(admin_engine: AsyncEngine, org_id: UUID, hostname: str) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        device = Device(
            org_id=org_id, hostname=hostname, os_family="linux", client_id=org_id, site_id=org_id
        )
        s.add(device)
        await s.commit()
        return device.id


async def _seed_repo(admin_engine: AsyncEngine, org_id: UUID) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        repo = BackupRepository(
            org_id=org_id,
            name="repo-1",
            storage_ref="s3://bucket/repo-1",
            encryption_key_ref="vault://key-1",
        )
        s.add(repo)
        await s.commit()
        return repo.id


async def _seed_complete_point(
    admin_engine: AsyncEngine,
    org_id: UUID,
    device_id: UUID,
    repo_id: UUID,
    *,
    restore_tested: bool,
) -> None:
    now = _dt.datetime.now(_dt.UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        run = BackupRun(
            org_id=org_id,
            device_id=device_id,
            repository_id=repo_id,
            state="success",
            bytes_protected=1000,
            bytes_stored=400,
            chunk_count=5,
            started_at=now - _dt.timedelta(minutes=10),
            finished_at=now - _dt.timedelta(minutes=9),
            actor_type="system",
        )
        s.add(run)
        await s.flush()

        point = BackupPoint(
            org_id=org_id,
            device_id=device_id,
            repository_id=repo_id,
            backup_run_id=run.id,
            taken_at=now - _dt.timedelta(minutes=9),
            manifest_ref=f"s3://bucket/repo-1/manifests/{device_id}",
            size=1000,
            state="complete",
            verified_at=now - _dt.timedelta(minutes=8),
            restore_tested_at=(now - _dt.timedelta(minutes=7)) if restore_tested else None,
        )
        s.add(point)
        await s.commit()


async def _seed_plan(
    app_engine: AsyncEngine,
    org_id: UUID,
    actor_id: UUID,
    *,
    scope: list[dict[str, Any]] | None = None,
    review_interval_days: int = 180,
) -> UUID:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        plan = await create_plan(
            s,
            org_id=org_id,
            client_id=org_id,
            actor_id=actor_id,
            name="DR Plan",
            description="Recover the core stack",
            scope=scope or [],
            rto_minutes=60,
            rpo_minutes=15,
            procedure_refs=[],
            dependencies=[],
            review_interval_days=review_interval_days,
        )
        await s.commit()
        return plan.id


async def _set_last_reviewed_at(
    admin_engine: AsyncEngine, plan_id: UUID, when: _dt.datetime | None
) -> None:
    """Directly stamp `last_reviewed_at` to a specific point in time --
    `review_plan` only ever stamps `now()`, so tests that need a *stale*
    review date write it directly, mirroring `_seed_complete_point`'s
    direct-write style for test setup outside the service under test."""
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        plan = (await s.execute(select(BcpPlan).where(BcpPlan.id == plan_id))).scalar_one()
        plan.last_reviewed_at = when
        await s.commit()


async def _seed_test(app_engine: AsyncEngine, org_id: UUID, actor_id: UUID, plan_id: UUID) -> UUID:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        test = await bcp_create_test(
            s,
            org_id=org_id,
            actor_id=actor_id,
            plan_id=plan_id,
            type_="tabletop",
            scheduled_for=None,
            notes=None,
        )
        await s.commit()
        return test.id


async def _record(
    app_engine: AsyncEngine,
    org_id: UUID,
    actor_id: UUID,
    test_id: UUID,
    *,
    executed_at: _dt.datetime,
    outcome: str,
) -> None:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await record_test(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_id,
            executed_at=executed_at,
            rto_met=True,
            rpo_met=True,
            outcome=outcome,
            notes=None,
        )
        await s.commit()


# ---------------------------------------------------------------------------
# _is_review_overdue -- pure helper, no DB
# ---------------------------------------------------------------------------


def test_is_review_overdue_none_last_reviewed_is_overdue() -> None:
    now = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
    assert _is_review_overdue(None, 180, now) is True


def test_is_review_overdue_within_interval_is_not_overdue() -> None:
    now = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
    last_reviewed = now - _dt.timedelta(days=100)
    assert _is_review_overdue(last_reviewed, 180, now) is False


def test_is_review_overdue_past_interval_is_overdue() -> None:
    now = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
    last_reviewed = now - _dt.timedelta(days=200)
    assert _is_review_overdue(last_reviewed, 180, now) is True


def test_is_review_overdue_exact_boundary_is_not_overdue() -> None:
    now = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
    last_reviewed = now - _dt.timedelta(days=180)
    assert _is_review_overdue(last_reviewed, 180, now) is False


def test_is_review_overdue_just_past_boundary_is_overdue() -> None:
    now = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
    last_reviewed = now - _dt.timedelta(days=180, seconds=1)
    assert _is_review_overdue(last_reviewed, 180, now) is True


# ---------------------------------------------------------------------------
# bcp_test_report
# ---------------------------------------------------------------------------


async def test_bcp_test_report_returns_full_nested_record_with_frozen_evidence(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    repo_id = await _seed_repo(admin_engine, org_id)
    device_a = await _seed_device(admin_engine, org_id, "device-a")
    await _seed_complete_point(admin_engine, org_id, device_a, repo_id, restore_tested=True)

    plan_id = await _seed_plan(
        app_engine, org_id, actor_id, scope=[{"kind": "device", "id": str(device_a)}]
    )
    test_id = await _seed_test(app_engine, org_id, actor_id, plan_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await upsert_step(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_id,
            ordinal=1,
            description="Failover DB",
            result="pass",
            evidence_ref=None,
        )
        await s.commit()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await upsert_step(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_id,
            ordinal=2,
            description="Verify app",
            result="pending",
            evidence_ref=None,
        )
        await s.commit()

    executed_at = _dt.datetime.now(_dt.UTC)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        recorded = await record_test(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_id,
            executed_at=executed_at,
            rto_met=True,
            rpo_met=True,
            outcome="pass",
            notes="ok",
        )
        await s.commit()
        expected_evidence = recorded.evidence

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        finding1 = await create_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_id,
            description="Slow failover",
            severity="high",
        )
        await s.commit()
        finding1_id = finding1.id
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        finding2 = await create_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_id,
            description="Doc gap",
            severity="low",
        )
        await s.commit()
        finding2_id = finding2.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        report = await bcp_test_report(s, org_id, test_id)

    assert report["test"]["id"] == test_id
    assert report["test"]["plan_id"] == plan_id
    assert report["test"]["outcome"] == "pass"
    assert report["test"]["notes"] == "ok"
    assert report["test"]["evidence"] == expected_evidence

    assert report["plan"]["id"] == plan_id
    assert report["plan"]["name"] == "DR Plan"

    assert [step["ordinal"] for step in report["steps"]] == [1, 2]
    assert report["steps"][0]["result"] == "pass"
    assert report["steps"][1]["result"] == "pending"

    finding_ids = {f["id"] for f in report["findings"]}
    assert finding_ids == {finding1_id, finding2_id}
    for f in report["findings"]:
        if f["id"] == finding1_id:
            assert f["severity"] == "high"
        else:
            assert f["severity"] == "low"

    assert report["evidence"] == expected_evidence


async def test_bcp_test_report_missing_test_returns_empty_shape(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _actor_id = await _org_with_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        report = await bcp_test_report(s, org_id, uuid4())
    assert report == {"test": None, "plan": None, "steps": [], "findings": [], "evidence": {}}


async def test_bcp_test_report_cross_org_test_returns_empty_shape(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, _actor_b = await _org_with_user(admin_engine)
    plan_a = await _seed_plan(app_engine, org_a, actor_a)
    test_a = await _seed_test(app_engine, org_a, actor_a, plan_a)

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        report = await bcp_test_report(s, org_b, test_a)
    assert report == {"test": None, "plan": None, "steps": [], "findings": [], "evidence": {}}


# ---------------------------------------------------------------------------
# bcp_period_report
# ---------------------------------------------------------------------------


async def test_bcp_period_report_hand_computed_counts_and_overdue_plans(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    t_frm = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
    t_to = _dt.datetime(2026, 1, 31, tzinfo=_dt.UTC)

    plan_a = await _seed_plan(
        app_engine, org_id, actor_id, review_interval_days=180
    )  # never reviewed
    plan_b = await _seed_plan(app_engine, org_id, actor_id, review_interval_days=180)
    plan_c = await _seed_plan(app_engine, org_id, actor_id, review_interval_days=180)

    await _set_last_reviewed_at(admin_engine, plan_b, t_to - _dt.timedelta(days=10))  # fresh
    await _set_last_reviewed_at(admin_engine, plan_c, t_to - _dt.timedelta(days=400))  # stale

    # Plan A: 2 pass + 1 fail, all inside the window.
    test_a1 = await _seed_test(app_engine, org_id, actor_id, plan_a)
    await _record(
        app_engine,
        org_id,
        actor_id,
        test_a1,
        executed_at=t_frm + _dt.timedelta(days=5),
        outcome="pass",
    )
    test_a2 = await _seed_test(app_engine, org_id, actor_id, plan_a)
    await _record(
        app_engine,
        org_id,
        actor_id,
        test_a2,
        executed_at=t_frm + _dt.timedelta(days=10),
        outcome="pass",
    )
    test_a3 = await _seed_test(app_engine, org_id, actor_id, plan_a)
    await _record(
        app_engine,
        org_id,
        actor_id,
        test_a3,
        executed_at=t_frm + _dt.timedelta(days=15),
        outcome="fail",
    )

    # Plan A: a test executed AFTER the window -- must not count.
    test_a4 = await _seed_test(app_engine, org_id, actor_id, plan_a)
    await _record(
        app_engine,
        org_id,
        actor_id,
        test_a4,
        executed_at=t_to + _dt.timedelta(days=5),
        outcome="fail",
    )

    # Plan B: 1 partial inside the window.
    test_b1 = await _seed_test(app_engine, org_id, actor_id, plan_b)
    await _record(
        app_engine,
        org_id,
        actor_id,
        test_b1,
        executed_at=t_frm + _dt.timedelta(days=20),
        outcome="partial",
    )

    # Plan C: a test executed BEFORE the window -- must not count, and no
    # in-window test at all, so plan_c must be absent from plans_tested.
    test_c1 = await _seed_test(app_engine, org_id, actor_id, plan_c)
    await _record(
        app_engine,
        org_id,
        actor_id,
        test_c1,
        executed_at=t_frm - _dt.timedelta(days=5),
        outcome="pass",
    )

    # Findings: mix of open/in_progress/resolved across plans/tests.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await create_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_a1,
            description="slow",
            severity="high",
        )
        await s.commit()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        finding2 = await create_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_a2,
            description="doc gap",
            severity="low",
        )
        await s.commit()
        finding2_id = finding2.id
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await update_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            finding_id=finding2_id,
            remediation_state="resolved",
        )
        await s.commit()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await create_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_b1,
            description="config drift",
            severity="high",
        )
        await s.commit()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        finding4 = await create_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_c1,
            description="critical gap",
            severity="critical",
        )
        await s.commit()
        finding4_id = finding4.id
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await update_finding(
            s,
            org_id=org_id,
            actor_id=actor_id,
            finding_id=finding4_id,
            remediation_state="in_progress",
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        report = await bcp_period_report(s, org_id, frm=t_frm, to=t_to)

    assert report["outcome_counts"] == {"pass": 2, "partial": 1, "fail": 1, "pending": 0}
    assert set(report["plans_tested"]) == {plan_a, plan_b}
    assert plan_c not in report["plans_tested"]
    assert report["open_findings_by_severity"] == {"low": 0, "medium": 0, "high": 2, "critical": 1}
    assert set(report["review_overdue_plans"]) == {plan_a, plan_c}
    assert plan_b not in report["review_overdue_plans"]


# ---------------------------------------------------------------------------
# bcp_plan_status
# ---------------------------------------------------------------------------


async def test_bcp_plan_status_review_overdue_and_restore_proven_coverage(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    now = _dt.datetime(2026, 6, 1, tzinfo=_dt.UTC)

    repo_id = await _seed_repo(admin_engine, org_id)
    device_proven = await _seed_device(admin_engine, org_id, "device-proven")
    device_uncovered = await _seed_device(admin_engine, org_id, "device-uncovered")
    await _seed_complete_point(admin_engine, org_id, device_proven, repo_id, restore_tested=True)
    # device_uncovered: no backup points at all.

    plan_unreviewed = await _seed_plan(
        app_engine,
        org_id,
        actor_id,
        scope=[
            {"kind": "device", "id": str(device_proven)},
            {"kind": "device", "id": str(device_uncovered)},
        ],
        review_interval_days=180,
    )
    plan_stale = await _seed_plan(app_engine, org_id, actor_id, review_interval_days=180)
    plan_fresh = await _seed_plan(app_engine, org_id, actor_id, review_interval_days=180)

    await _set_last_reviewed_at(admin_engine, plan_stale, now - _dt.timedelta(days=400))
    await _set_last_reviewed_at(admin_engine, plan_fresh, now - _dt.timedelta(days=10))

    test_id = await _seed_test(app_engine, org_id, actor_id, plan_unreviewed)
    await _record(
        app_engine,
        org_id,
        actor_id,
        test_id,
        executed_at=now - _dt.timedelta(days=1),
        outcome="partial",
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        statuses = await bcp_plan_status(s, org_id, now=now)

    by_id = {row["id"]: row for row in statuses}
    assert set(by_id) == {plan_unreviewed, plan_stale, plan_fresh}

    assert by_id[plan_unreviewed]["review_overdue"] is True
    assert by_id[plan_unreviewed]["last_reviewed_at"] is None
    assert by_id[plan_unreviewed]["last_test_outcome"] == "partial"
    assert by_id[plan_unreviewed]["restore_proven_coverage"] == {"total": 2, "restore_proven": 1}

    assert by_id[plan_stale]["review_overdue"] is True
    assert by_id[plan_stale]["last_test_outcome"] is None
    assert by_id[plan_stale]["restore_proven_coverage"] == {"total": 0, "restore_proven": 0}

    assert by_id[plan_fresh]["review_overdue"] is False
    assert by_id[plan_fresh]["last_test_outcome"] is None


async def test_bcp_plan_status_open_findings_count(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    now = _dt.datetime(2026, 6, 1, tzinfo=_dt.UTC)
    plan_id = await _seed_plan(app_engine, org_id, actor_id)
    test_id = await _seed_test(app_engine, org_id, actor_id, plan_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        f1 = await create_finding(
            s, org_id=org_id, actor_id=actor_id, test_id=test_id, description="a", severity="low"
        )
        await s.commit()
        f1_id = f1.id
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await create_finding(
            s, org_id=org_id, actor_id=actor_id, test_id=test_id, description="b", severity="high"
        )
        await s.commit()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await update_finding(
            s, org_id=org_id, actor_id=actor_id, finding_id=f1_id, remediation_state="resolved"
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        statuses = await bcp_plan_status(s, org_id, now=now)

    row = next(r for r in statuses if r["id"] == plan_id)
    assert row["open_findings"] == 1


# ---------------------------------------------------------------------------
# tenancy: bcp_test_report / bcp_period_report / bcp_plan_status
# ---------------------------------------------------------------------------


async def test_another_orgs_rows_are_excluded_from_all_reports_by_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    plan_a = await _seed_plan(app_engine, org_a, actor_a)
    test_a = await _seed_test(app_engine, org_a, actor_a, plan_a)
    await _record(
        app_engine,
        org_a,
        actor_a,
        test_a,
        executed_at=_dt.datetime(2026, 1, 5, tzinfo=_dt.UTC),
        outcome="pass",
    )
    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        await create_finding(
            s, org_id=org_a, actor_id=actor_a, test_id=test_a, description="x", severity="critical"
        )
        await s.commit()

    plan_b = await _seed_plan(app_engine, org_b, actor_b)
    test_b = await _seed_test(app_engine, org_b, actor_b, plan_b)
    await _record(
        app_engine,
        org_b,
        actor_b,
        test_b,
        executed_at=_dt.datetime(2026, 1, 5, tzinfo=_dt.UTC),
        outcome="fail",
    )
    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        await create_finding(
            s, org_id=org_b, actor_id=actor_b, test_id=test_b, description="y", severity="critical"
        )
        await s.commit()

    frm = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
    to = _dt.datetime(2026, 1, 31, tzinfo=_dt.UTC)

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        period = await bcp_period_report(s, org_a, frm=frm, to=to)
        statuses = await bcp_plan_status(s, org_a, now=to)
        report_a = await bcp_test_report(s, org_a, test_a)
        report_cross = await bcp_test_report(s, org_a, test_b)

    assert period["outcome_counts"] == {"pass": 1, "partial": 0, "fail": 0, "pending": 0}
    assert set(period["plans_tested"]) == {plan_a}
    assert period["open_findings_by_severity"]["critical"] == 1

    assert {row["id"] for row in statuses} == {plan_a}

    assert report_a["test"]["id"] == test_a
    assert report_cross == {"test": None, "plan": None, "steps": [], "findings": [], "evidence": {}}


async def test_tenancy_report_org_filters_are_load_bearing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Isolate the explicit `org_id` predicates in the three report functions'
    own lookups so they are the SOLE tenant guard.

    The tenancy test above drives the reports through `org_session(org_a)`,
    where RLS already hides org B's rows -- so those assertions would still
    pass even if an app-level `org_id` predicate regressed. Here we run all
    three reports on an RLS-bypassing admin session (the `admin_engine` role
    bypasses RLS -- it is how the cross-org seed helpers insert), so org B's
    rows ARE visible on the connection and only each query's own `org_id`
    predicate can exclude them. Mutation-verified: deleting any of the
    per-function `org_id` predicates makes this fail. (The coverage-path
    predicates -- `resolve_scope_devices` / `backup_evidence_rows` -- are
    made load-bearing independently by test_bcp_test_service.py.)
    """
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    plan_a = await _seed_plan(app_engine, org_a, actor_a)
    test_a = await _seed_test(app_engine, org_a, actor_a, plan_a)
    await _record(
        app_engine,
        org_a,
        actor_a,
        test_a,
        executed_at=_dt.datetime(2026, 1, 5, tzinfo=_dt.UTC),
        outcome="pass",
    )
    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        await create_finding(
            s, org_id=org_a, actor_id=actor_a, test_id=test_a, description="x", severity="critical"
        )
        await s.commit()

    plan_b = await _seed_plan(app_engine, org_b, actor_b)
    test_b = await _seed_test(app_engine, org_b, actor_b, plan_b)
    await _record(
        app_engine,
        org_b,
        actor_b,
        test_b,
        executed_at=_dt.datetime(2026, 1, 5, tzinfo=_dt.UTC),
        outcome="fail",
    )
    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        await create_finding(
            s, org_id=org_b, actor_id=actor_b, test_id=test_b, description="y", severity="critical"
        )
        await s.commit()

    frm = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
    to = _dt.datetime(2026, 1, 31, tzinfo=_dt.UTC)

    # RLS-bypassing admin session: org B's rows are visible on this connection,
    # so only each report query's own org_id predicate can exclude them.
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        period = await bcp_period_report(s, org_a, frm=frm, to=to)
        statuses = await bcp_plan_status(s, org_a, now=to)
        report_cross = await bcp_test_report(s, org_a, test_b)

    # org B's failing test / plan / critical finding must be absent.
    assert period["outcome_counts"] == {"pass": 1, "partial": 0, "fail": 0, "pending": 0}
    assert set(period["plans_tested"]) == {plan_a}
    assert period["open_findings_by_severity"]["critical"] == 1
    assert {row["id"] for row in statuses} == {plan_a}
    assert report_cross == {"test": None, "plan": None, "steps": [], "findings": [], "evidence": {}}
