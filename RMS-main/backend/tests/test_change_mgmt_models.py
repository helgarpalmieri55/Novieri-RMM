from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import (
    ChangeApproval,
    ChangeExternalRef,
    ChangeLink,
    ChangeRequest,
    Organization,
    User,
)


async def _org_user(admin_engine: AsyncEngine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="change", slug=f"change-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        usr = User(org_id=org.id, external_subject=f"s{uuid4().hex[:6]}", email="a@b.c")
        s.add(usr)
        await s.commit()
        return org.id, usr.id


async def test_change_mgmt_models_roundtrip(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id = await _org_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        # Test ChangeRequest
        change_req = ChangeRequest(
            org_id=org_id,
            type="normal",
            title="Test Change",
            description="A test change request",
            risk="low",
            created_by=user_id,
        )
        s.add(change_req)
        await s.flush()
        assert change_req.state == "draft"
        assert change_req.affected == []
        assert change_req.window_start is None
        assert change_req.window_end is None
        assert change_req.rollback_plan is None

        # Test ChangeApproval
        approval = ChangeApproval(
            org_id=org_id,
            change_id=change_req.id,
            approver_id=user_id,
            decision="approved",
        )
        s.add(approval)
        await s.flush()
        assert approval.is_retrospective is False
        assert approval.comment is None

        # Test ChangeLink
        action_id = uuid4()
        link = ChangeLink(
            org_id=org_id,
            change_id=change_req.id,
            action_type="job",
            action_id=action_id,
            linked_by=user_id,
        )
        s.add(link)
        await s.flush()

        # Test ChangeExternalRef
        ext_ref = ChangeExternalRef(
            org_id=org_id,
            change_id=change_req.id,
            system="jira",
            external_id="JIRA-123",
        )
        s.add(ext_ref)
        await s.flush()
        assert ext_ref.synced_at is None

    # Verify round-trip with fresh session
    async with org_session(org_id, engine=app_engine) as s:
        got_req = (
            await s.execute(select(ChangeRequest).where(ChangeRequest.id == change_req.id))
        ).scalar_one()
        assert got_req.state == "draft"
        assert got_req.affected == []
        assert got_req.type == "normal"
        assert got_req.risk == "low"

        got_approval = (
            await s.execute(
                select(ChangeApproval).where(ChangeApproval.id == approval.id)
            )
        ).scalar_one()
        assert got_approval.decision == "approved"
        assert got_approval.is_retrospective is False

        got_link = (
            await s.execute(select(ChangeLink).where(ChangeLink.id == link.id))
        ).scalar_one()
        assert got_link.action_type == "job"
        assert got_link.action_id == action_id

        got_ext = (
            await s.execute(
                select(ChangeExternalRef).where(ChangeExternalRef.id == ext_ref.id)
            )
        ).scalar_one()
        assert got_ext.system == "jira"
        assert got_ext.external_id == "JIRA-123"
