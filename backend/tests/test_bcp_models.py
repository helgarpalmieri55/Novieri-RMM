from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import (
    BcpFinding,
    BcpPlan,
    BcpTest,
    BcpTestStep,
    Organization,
    User,
)


async def _org_user(admin_engine: AsyncEngine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="bcp", slug=f"bcp-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        usr = User(org_id=org.id, external_subject=f"s{uuid4().hex[:6]}", email="a@b.c")
        s.add(usr)
        await s.commit()
        return org.id, usr.id


async def test_bcp_models_roundtrip(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id = await _org_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        # Test BcpPlan
        plan = BcpPlan(
            org_id=org_id,
            name="Test Plan",
            description="A test BCP plan",
            created_by=user_id,
        )
        s.add(plan)
        await s.flush()
        assert plan.state == "active"
        assert plan.scope == []
        assert plan.procedure_refs == []
        assert plan.dependencies == []
        assert plan.review_interval_days == 365
        assert plan.last_reviewed_at is None
        assert plan.approved_by is None
        assert plan.rto_minutes is None
        assert plan.rpo_minutes is None

        # Test BcpTest
        test = BcpTest(
            org_id=org_id,
            plan_id=plan.id,
            type="tabletop",
            created_by=user_id,
        )
        s.add(test)
        await s.flush()
        assert test.outcome == "pending"
        assert test.evidence == {}
        assert test.scheduled_for is None
        assert test.executed_at is None
        assert test.executed_by is None
        assert test.rto_met is None
        assert test.rpo_met is None
        assert test.notes is None

        # Test BcpTestStep
        step = BcpTestStep(
            org_id=org_id,
            test_id=test.id,
            ordinal=1,
            description="Verify backup availability",
        )
        s.add(step)
        await s.flush()
        assert step.result == "pending"
        assert step.evidence_ref is None

        # Test BcpFinding
        finding = BcpFinding(
            org_id=org_id,
            test_id=test.id,
            description="Finding during test",
            severity="high",
            created_by=user_id,
        )
        s.add(finding)
        await s.flush()
        assert finding.remediation_state == "open"
        assert finding.change_ref is None
        assert finding.resolved_at is None

    # Verify round-trip with fresh session
    async with org_session(org_id, engine=app_engine) as s:
        got_plan = (
            await s.execute(select(BcpPlan).where(BcpPlan.id == plan.id))
        ).scalar_one()
        assert got_plan.name == "Test Plan"
        assert got_plan.description == "A test BCP plan"
        assert got_plan.state == "active"
        assert got_plan.scope == []
        assert got_plan.review_interval_days == 365

        got_test = (
            await s.execute(select(BcpTest).where(BcpTest.id == test.id))
        ).scalar_one()
        assert got_test.type == "tabletop"
        assert got_test.outcome == "pending"
        assert got_test.evidence == {}

        got_step = (
            await s.execute(select(BcpTestStep).where(BcpTestStep.id == step.id))
        ).scalar_one()
        assert got_step.ordinal == 1
        assert got_step.result == "pending"

        got_finding = (
            await s.execute(select(BcpFinding).where(BcpFinding.id == finding.id))
        ).scalar_one()
        assert got_finding.severity == "high"
        assert got_finding.remediation_state == "open"
