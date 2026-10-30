from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Device, DocVersion, Organization, User
from app.modules.documentation import service as svc
from app.modules.documentation.schemas import validate_record_fields


async def _org_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID, UUID]:
    """Returns (org_id, user_id, device_id)."""
    slug = f"doc-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=f"s-{slug}", email=f"{slug}@x.test")
        device = Device(
            org_id=org.id, hostname="doc-dev", os_family="linux", client_id=org.id, site_id=org.id
        )
        session.add_all([user, device])
        await session.commit()
        return org.id, user.id, device.id


async def test_create_and_update_version_and_audit(admin_engine, app_engine) -> None:
    org_id, user_id, _ = await _org_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        article = await svc.create_article(
            session,
            org_id=org_id,
            actor_id=user_id,
            title="Server build runbook",
            body="# Steps\n1. image\n2. join domain",
            category="Runbooks",
            client_id=None,
        )
        assert article.current_version == 1

        changed = await svc.update_article(
            session,
            article,
            actor_id=user_id,
            title="Server build runbook",
            body="# Steps\n1. image\n2. join domain\n3. verify",
            category="Runbooks",
        )
        assert changed is True
        assert article.current_version == 2

        # identical update is a no-op
        unchanged = await svc.update_article(
            session,
            article,
            actor_id=user_id,
            title="Server build runbook",
            body="# Steps\n1. image\n2. join domain\n3. verify",
            category="Runbooks",
        )
        assert unchanged is False
        assert article.current_version == 2

        versions = await svc.list_versions(session, article_id=article.id)
        assert [v.version for v in versions] == [2, 1]
        assert versions[1].snapshot["body"].endswith("join domain")
        assert versions[0].snapshot["body"].endswith("verify")

        actions = [
            row.action
            for row in (
                await session.execute(select(AuditEvent).order_by(AuditEvent.id))
            ).scalars()
        ]
    assert actions == ["docs.article_created", "docs.article_updated"]


async def test_archive_hides_but_keeps_history(admin_engine, app_engine) -> None:
    org_id, user_id, _ = await _org_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        article = await svc.create_article(
            session, org_id=org_id, actor_id=user_id, title="Old doc", body="x", category="G",
            client_id=None,
        )
        await svc.archive_article(session, article, actor_id=user_id)
        assert article.archived is True
        versions = (
            (
                await session.execute(
                    select(DocVersion).where(DocVersion.article_id == article.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(versions) == 1  # history survives archive


async def test_record_field_validation() -> None:
    fields = validate_record_fields(
        "network", {"cidr": "10.20.0.0/24", "vlan": 20, "site_notes": "third floor"}
    )
    assert fields["cidr"] == "10.20.0.0/24"
    assert fields["site_notes"] == "third floor"  # extras preserved
    with pytest.raises(ValueError):
        validate_record_fields("network", {"cidr": "not-a-network"})
    with pytest.raises(ValueError):
        validate_record_fields("nonsense_type", {})


async def test_record_lifecycle(admin_engine, app_engine) -> None:
    org_id, user_id, _ = await _org_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        record = await svc.create_record(
            session,
            org_id=org_id,
            actor_id=user_id,
            record_type="vendor",
            name="Dell ProSupport",
            fields={"contact_email": "support@dell.example", "contract_end": "2027-01-31"},
            client_id=None,
        )
        assert record.current_version == 1
        changed = await svc.update_record(
            session,
            record,
            actor_id=user_id,
            name="Dell ProSupport Plus",
            fields={"contact_email": "support@dell.example", "contract_end": "2028-01-31"},
        )
        assert changed and record.current_version == 2
        versions = await svc.list_versions(session, record_id=record.id)
        assert versions[0].snapshot["name"] == "Dell ProSupport Plus"


async def test_links_validate_in_org_targets(admin_engine, app_engine) -> None:
    org_id, user_id, device_id = await _org_user(admin_engine)
    foreign_org, _, foreign_device = await _org_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        article = await svc.create_article(
            session, org_id=org_id, actor_id=user_id, title="Linked", body="b", category="G",
            client_id=None,
        )
        link = await svc.add_link(
            session, article, actor_id=user_id, target_type="device", target_id=device_id
        )
        assert link.target_id == device_id

        with pytest.raises(svc.LinkTargetNotFound):
            await svc.add_link(
                session,
                article,
                actor_id=user_id,
                target_type="device",
                target_id=foreign_device,  # RLS hides it -> not found
            )
        with pytest.raises(svc.LinkTargetNotFound):
            await svc.add_link(
                session, article, actor_id=user_id, target_type="record", target_id=uuid4()
            )
        with pytest.raises(IntegrityError):
            await svc.add_link(
                session, article, actor_id=user_id, target_type="device", target_id=device_id
            )
