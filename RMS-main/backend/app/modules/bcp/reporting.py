"""BCP/DR reporting (docs/modules/bcp/spec.md): read-only SOC 2 evidence for
one DR test, an org-wide period rollup, and a plan-status dashboard with an
overdue-review flag.

Pure read-only aggregation over the BCP/DR module's tables. This module
performs **no writes**, dispatches **no jobs**, and records **no audit
events** -- it exists solely to answer "what happened on this one DR test,
end to end" (`bcp_test_report`, the per-test SOC 2 evidence bundle), "what
does DR-test/finding activity look like across the whole org over a given
window" (`bcp_period_report`), and "what is the current state of every plan
right now" (`bcp_plan_status`, the dashboard view). Mirrors
`app.modules.change_mgmt.reporting`'s style and its no-audit-on-view
invariant: viewing a report must never be indistinguishable from mutating
anything, so nothing here ever calls `audit_service`.

Every query below filters on `org_id` for every table involved (`BcpPlan`,
`BcpTest`, `BcpTestStep`, `BcpFinding`, `Device`) per the tenancy invariant
(#5) -- RLS is the backstop, not the only line of defense.

`bcp_test_report`'s `evidence` field is the test's FROZEN `evidence` snapshot
column, captured once at `record_test` time (see
`app.modules.bcp.service.record_test`) -- it is passed through as-is, never
recomputed. `bcp_plan_status`'s `restore_proven_coverage`, by contrast, is
computed LIVE against the plan's covered devices' CURRENT backup evidence
(`app.modules.backup.reporting.backup_evidence_rows`) each time the dashboard
is read -- it is deliberately the "current state" view, distinct from any
one test's point-in-time snapshot.
"""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BcpFinding, BcpPlan, BcpTest, BcpTestStep
from app.modules.backup.reporting import backup_evidence_rows
from app.modules.bcp.service import resolve_scope_devices

_OUTCOME_KEYS = ("pass", "partial", "fail", "pending")
_SEVERITY_KEYS = ("low", "medium", "high", "critical")


def _is_review_overdue(
    last_reviewed_at: datetime | None, review_interval_days: int, now: datetime
) -> bool:
    """Pure, unit-testable overdue formula -- reused for both
    `bcp_plan_status`'s `review_overdue` field and `bcp_period_report`'s
    `review_overdue_plans` list, never inlined twice.

    A plan that has never been reviewed (`last_reviewed_at is None`) is
    always overdue. Otherwise a plan is overdue once its last review is more
    than `review_interval_days` in the past relative to `now` -- the
    boundary itself (`last_reviewed_at + review_interval_days == now`) is
    NOT overdue (strict `<`).
    """
    if last_reviewed_at is None:
        return True
    return last_reviewed_at + timedelta(days=review_interval_days) < now


def _empty_test_report() -> dict[str, Any]:
    """The "no such test in this org" shape -- mirrors
    `change_mgmt.reporting._empty_evidence`'s missing convention: the
    requested test simply isn't there (nonexistent or cross-org,
    indistinguishable by design per invariant #5), so everything else is
    empty."""
    return {"test": None, "plan": None, "steps": [], "findings": [], "evidence": {}}


def _plan_dict(plan: BcpPlan) -> dict[str, Any]:
    """Shape matching `schema.PlanOut` exactly."""
    return {
        "id": plan.id,
        "name": plan.name,
        "description": plan.description,
        "scope": plan.scope,
        "rto_minutes": plan.rto_minutes,
        "rpo_minutes": plan.rpo_minutes,
        "procedure_refs": plan.procedure_refs,
        "dependencies": plan.dependencies,
        "review_interval_days": plan.review_interval_days,
        "last_reviewed_at": plan.last_reviewed_at,
        "approved_by": plan.approved_by,
        "state": plan.state,
        "created_by": plan.created_by,
        "created_at": plan.created_at,
    }


def _test_dict(test: BcpTest) -> dict[str, Any]:
    """Shape matching `schema.TestOut` exactly."""
    return {
        "id": test.id,
        "plan_id": test.plan_id,
        "type": test.type,
        "scheduled_for": test.scheduled_for,
        "executed_at": test.executed_at,
        "executed_by": test.executed_by,
        "outcome": test.outcome,
        "rto_met": test.rto_met,
        "rpo_met": test.rpo_met,
        "evidence": test.evidence,
        "notes": test.notes,
        "created_at": test.created_at,
    }


def _step_dict(step: BcpTestStep) -> dict[str, Any]:
    """Shape matching `schema.StepOut` exactly."""
    return {
        "id": step.id,
        "test_id": step.test_id,
        "ordinal": step.ordinal,
        "description": step.description,
        "result": step.result,
        "evidence_ref": step.evidence_ref,
    }


def _finding_dict(finding: BcpFinding) -> dict[str, Any]:
    """Shape matching `schema.FindingOut` exactly."""
    return {
        "id": finding.id,
        "test_id": finding.test_id,
        "description": finding.description,
        "severity": finding.severity,
        "remediation_state": finding.remediation_state,
        "change_ref": finding.change_ref,
        "created_at": finding.created_at,
        "resolved_at": finding.resolved_at,
    }


async def bcp_test_report(session: AsyncSession, org_id: UUID, test_id: UUID) -> dict[str, Any]:
    """One DR test's full SOC 2 evidence bundle, org-scoped.

    Returns `test` (the test's core fields, or `None` if no such test exists
    in this org -- see `_empty_test_report`), `plan` (the owning `BcpPlan`,
    org-scoped-loaded), `steps` (every `BcpTestStep` for this test, ordered
    by `ordinal`), `findings` (every `BcpFinding` for this test, ordered by
    `created_at`), and `evidence` (the test's FROZEN `evidence` snapshot
    column, passed through as-is -- see the module docstring).
    """
    test = (
        await session.execute(
            select(BcpTest).where(BcpTest.org_id == org_id, BcpTest.id == test_id)
        )
    ).scalar_one_or_none()

    if test is None:
        return _empty_test_report()

    plan = (
        await session.execute(
            select(BcpPlan).where(BcpPlan.org_id == org_id, BcpPlan.id == test.plan_id)
        )
    ).scalar_one_or_none()

    steps = (
        (
            await session.execute(
                select(BcpTestStep)
                .where(BcpTestStep.org_id == org_id, BcpTestStep.test_id == test_id)
                .order_by(BcpTestStep.ordinal)
            )
        )
        .scalars()
        .all()
    )

    findings = (
        (
            await session.execute(
                select(BcpFinding)
                .where(BcpFinding.org_id == org_id, BcpFinding.test_id == test_id)
                .order_by(BcpFinding.created_at)
            )
        )
        .scalars()
        .all()
    )

    return {
        "test": _test_dict(test),
        "plan": _plan_dict(plan) if plan is not None else None,
        "steps": [_step_dict(step) for step in steps],
        "findings": [_finding_dict(finding) for finding in findings],
        "evidence": test.evidence,
    }


async def bcp_period_report(
    session: AsyncSession, org_id: UUID, *, frm: datetime, to: datetime
) -> dict[str, Any]:
    """Org-wide DR-test/finding rollup over `[frm, to]`, org-scoped.

    - `outcome_counts`: count of `BcpTest` rows whose `executed_at` falls in
      `[frm, to]` (inclusive both ends), grouped by `outcome`. Always carries
      the stable keys `pass`/`partial`/`fail`/`pending` (default `0` if
      absent) so callers/dashboards never have to guard a missing key --
      note that `pending` tests have no `executed_at` by construction (see
      `app.modules.bcp.service.create_test`/`record_test`), so this key is
      effectively always `0` in practice, but is kept for shape stability.
    - `plans_tested`: distinct `plan_id`s with at least one test whose
      `executed_at` falls in `[frm, to]`.
    - `open_findings_by_severity`: count of `BcpFinding` rows across the
      WHOLE org (not window-scoped -- a finding has no `executed_at` of its
      own; this is a point-in-time "what's still open right now" rollup)
      with `remediation_state != "resolved"` (i.e. `open` + `in_progress`),
      grouped by `severity`. Always carries the stable keys
      `low`/`medium`/`high`/`critical` (default `0` if absent).
    - `review_overdue_plans`: every plan id in the org where
      `_is_review_overdue(...)` is `True`, using `to` (the end of the
      reporting window) as the `now` reference -- so this list reflects
      "plans overdue as of the end of this reporting period."
    """
    outcome_rows = (
        await session.execute(
            select(BcpTest.outcome, func.count())
            .where(
                BcpTest.org_id == org_id,
                BcpTest.executed_at.is_not(None),
                BcpTest.executed_at >= frm,
                BcpTest.executed_at <= to,
            )
            .group_by(BcpTest.outcome)
        )
    ).all()
    outcome_counts = dict.fromkeys(_OUTCOME_KEYS, 0)
    for outcome, count in outcome_rows:
        outcome_counts[outcome] = count

    plans_tested = (
        (
            await session.execute(
                select(func.distinct(BcpTest.plan_id)).where(
                    BcpTest.org_id == org_id,
                    BcpTest.executed_at.is_not(None),
                    BcpTest.executed_at >= frm,
                    BcpTest.executed_at <= to,
                )
            )
        )
        .scalars()
        .all()
    )

    severity_rows = (
        await session.execute(
            select(BcpFinding.severity, func.count())
            .where(BcpFinding.org_id == org_id, BcpFinding.remediation_state != "resolved")
            .group_by(BcpFinding.severity)
        )
    ).all()
    open_findings_by_severity = dict.fromkeys(_SEVERITY_KEYS, 0)
    for severity, count in severity_rows:
        open_findings_by_severity[severity] = count

    plans = (await session.execute(select(BcpPlan).where(BcpPlan.org_id == org_id))).scalars().all()
    review_overdue_plans = [
        plan.id
        for plan in plans
        if _is_review_overdue(plan.last_reviewed_at, plan.review_interval_days, to)
    ]

    return {
        "outcome_counts": outcome_counts,
        "plans_tested": list(plans_tested),
        "open_findings_by_severity": open_findings_by_severity,
        "review_overdue_plans": review_overdue_plans,
    }


async def bcp_plan_status(
    session: AsyncSession, org_id: UUID, *, now: datetime
) -> list[dict[str, Any]]:
    """Per-plan dashboard status, org-scoped, `now` passed in (pure/testable
    against `_is_review_overdue`).

    For each plan, returns:
    - `id`, `name`, `state`, `last_reviewed_at`: the plan's own fields.
    - `review_overdue`: `_is_review_overdue(plan.last_reviewed_at,
      plan.review_interval_days, now)`.
    - `last_test_outcome`: the `outcome` of the plan's most recently
      EXECUTED test (by `executed_at` desc), or `None` if the plan has no
      executed test at all.
    - `open_findings`: count of this plan's tests' findings with
      `remediation_state != "resolved"`.
    - `restore_proven_coverage`: `{"total", "restore_proven"}` over the
      plan's covered devices' CURRENT (live) backup evidence -- see the
      module docstring; this is deliberately distinct from any one test's
      frozen `evidence` snapshot.
    """
    plans = (await session.execute(select(BcpPlan).where(BcpPlan.org_id == org_id))).scalars().all()

    result: list[dict[str, Any]] = []
    for plan in plans:
        last_test = (
            await session.execute(
                select(BcpTest)
                .where(
                    BcpTest.org_id == org_id,
                    BcpTest.plan_id == plan.id,
                    BcpTest.executed_at.is_not(None),
                )
                .order_by(BcpTest.executed_at.desc(), BcpTest.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        last_test_outcome = last_test.outcome if last_test is not None else None

        open_findings = (
            await session.execute(
                select(func.count())
                .select_from(BcpFinding)
                .join(BcpTest, BcpTest.id == BcpFinding.test_id)
                .where(
                    BcpFinding.org_id == org_id,
                    BcpTest.org_id == org_id,
                    BcpTest.plan_id == plan.id,
                    BcpFinding.remediation_state != "resolved",
                )
            )
        ).scalar_one()

        devices = await resolve_scope_devices(session, org_id, plan.scope)
        rows = await backup_evidence_rows(session, org_id)
        scoped = [row for row in rows if row["device_id"] in devices]
        restore_proven_coverage = {
            "total": len(scoped),
            "restore_proven": sum(1 for row in scoped if row["restore_proven"]),
        }

        result.append(
            {
                "id": plan.id,
                "name": plan.name,
                "state": plan.state,
                "review_overdue": _is_review_overdue(
                    plan.last_reviewed_at, plan.review_interval_days, now
                ),
                "last_reviewed_at": plan.last_reviewed_at,
                "last_test_outcome": last_test_outcome,
                "open_findings": open_findings,
                "restore_proven_coverage": restore_proven_coverage,
            }
        )

    return result
