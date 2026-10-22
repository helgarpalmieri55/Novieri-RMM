from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import Organization, OsImage, ProvisionProfile, ProvisionRun, User


async def _org_user(admin_engine: AsyncEngine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="prov", slug=f"prov-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        usr = User(org_id=org.id, external_subject=f"s{uuid4().hex[:6]}", email="a@b.c")
        s.add(usr)
        await s.commit()
        return org.id, usr.id


async def test_provisioning_models_roundtrip(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id = await _org_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        image = OsImage(
            org_id=org_id,
            os_family="windows",
            name="Windows 11 Pro",
            kind="image",
            params={"version": "21h2"},
        )
        s.add(image)
        await s.flush()
        assert image.state == "active"
        assert image.artifact_ref is None
        assert image.artifact_sha256 is None
        assert image.params == {"version": "21h2"}

        profile = ProvisionProfile(
            org_id=org_id,
            name="Standard Win11",
            image_id=image.id,
            post_config={"enable_updates": True},
            target_scope={"all": True},
        )
        s.add(profile)
        await s.flush()
        assert profile.enabled is True
        assert profile.post_config == {"enable_updates": True}
        assert profile.target_scope == {"all": True}

        run = ProvisionRun(
            org_id=org_id,
            profile_id=profile.id,
            requested_by=user_id,
            actor_type="system",
        )
        s.add(run)
        await s.flush()
        assert run.state == "pending"
        assert run.job_id is None
        assert run.device_id is None
        assert run.target_ref is None
        assert run.enrollment_token_id is None
        assert run.answer_ref is None
        assert run.log_ref is None
        assert run.started_at is None
        assert run.finished_at is None
        assert run.error is None

    async with org_session(org_id, engine=app_engine) as s:
        got_image = (
            await s.execute(select(OsImage).where(OsImage.id == image.id))
        ).scalar_one()
        assert got_image.state == "active"

        got_profile = (
            await s.execute(select(ProvisionProfile).where(ProvisionProfile.id == profile.id))
        ).scalar_one()
        assert got_profile.enabled is True

        got_run = (
            await s.execute(select(ProvisionRun).where(ProvisionRun.id == run.id))
        ).scalar_one()
        assert got_run.state == "pending"
        assert got_run.actor_type == "system"
