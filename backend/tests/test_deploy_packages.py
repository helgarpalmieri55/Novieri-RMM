"""Tests for the package registry service (docs/modules/app-deploy/spec.md).

Package creation validates the os_family/installer_type/rule-grammar closed
sets; artifact storage computes the sha256 content hash and uploads via
`ObjectStore.put_package_artifact`; every query/write is org-scoped.
"""

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.models import AuditEvent, Organization, User
from app.modules.deploy.models import Package
from app.modules.deploy.packages import (
    DeployValidationError,
    create_package,
    delete_package,
    store_artifact,
)


@pytest.fixture()
def store(tmp_path):
    from moto import mock_aws

    settings = Settings(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=tmp_path,
        s3_access_key="test",
        s3_secret_key="test",
        s3_bucket="it-suite-test",
    )
    with mock_aws():
        yield ObjectStore(settings)


async def _org_user(admin_engine: AsyncEngine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="dp", slug=f"dp-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        usr = User(org_id=org.id, external_subject=f"s{uuid4().hex[:6]}", email="a@b.c")
        s.add(usr)
        await s.commit()
        return org.id, usr.id


async def test_create_package_persists_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id = await _org_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        pkg = await create_package(
            s,
            org_id=org_id,
            actor_id=user_id,
            name="acrobat",
            version="24.1",
            os_family="windows",
            installer_type="msi",
            detection_rule={"type": "registry_value"},
            uninstall_rule={},
        )
        assert pkg.id is not None
        assert pkg.name == "acrobat"

        got = (await s.execute(select(Package).where(Package.id == pkg.id))).scalar_one()
        assert got.version == "24.1"

        event = (
            await s.execute(
                select(AuditEvent).where(AuditEvent.action == "deploy.package_created")
            )
        ).scalar_one()
        assert event.actor_id == user_id
        assert event.target_id == str(pkg.id)


async def test_create_package_rejects_invalid_detection_rule(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id = await _org_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(DeployValidationError):
            await create_package(
                s,
                org_id=org_id,
                actor_id=user_id,
                name="bad",
                version="1.0",
                os_family="windows",
                installer_type="msi",
                detection_rule={"type": "nonsense"},
                uninstall_rule={},
            )


async def test_create_package_rejects_invalid_os_family_and_installer_type(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id = await _org_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(DeployValidationError):
            await create_package(
                s,
                org_id=org_id,
                actor_id=user_id,
                name="bad",
                version="1.0",
                os_family="plan9",
                installer_type="msi",
                detection_rule={},
                uninstall_rule={},
            )
        with pytest.raises(DeployValidationError):
            await create_package(
                s,
                org_id=org_id,
                actor_id=user_id,
                name="bad",
                version="1.0",
                os_family="windows",
                installer_type="unknown_installer",
                detection_rule={},
                uninstall_rule={},
            )


async def test_store_artifact_computes_sha256_and_audits(
    store: ObjectStore, admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id = await _org_user(admin_engine)
    data = b"MZfake-installer"
    expected_sha = hashlib.sha256(data).hexdigest()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        pkg = await create_package(
            s,
            org_id=org_id,
            actor_id=user_id,
            name="acrobat",
            version="24.1",
            os_family="windows",
            installer_type="msi",
            detection_rule={},
            uninstall_rule={},
        )
        pkg_id = pkg.id

        updated = await store_artifact(
            s,
            store,
            org_id=org_id,
            package_id=pkg_id,
            data=data,
            actor_id=user_id,
            actor_type="user",
        )
        assert updated.artifact_sha256 == expected_sha
        assert updated.artifact_ref is not None
        assert updated.artifact_ref.endswith(f"package-artifacts/{pkg_id}")

        event = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "deploy.package_artifact_uploaded"
                )
            )
        ).scalar_one()
        assert event.target_id == str(pkg_id)
        assert event.meta["artifact_sha256"] == expected_sha
        assert event.actor_type == "user"
        assert event.actor_id == user_id

    # Round-trips through the object store.
    assert await store.get_package_artifact(updated.artifact_ref) == data


async def test_store_artifact_defaults_to_system_actor_when_unspecified(
    store: ObjectStore, admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id = await _org_user(admin_engine)
    data = b"MZfake-installer-2"

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        pkg = await create_package(
            s,
            org_id=org_id,
            actor_id=user_id,
            name="acrobat",
            version="24.2",
            os_family="windows",
            installer_type="msi",
            detection_rule={},
            uninstall_rule={},
        )
        pkg_id = pkg.id

        # No actor_id/actor_type passed: defaults must remain system/None.
        await store_artifact(s, store, org_id=org_id, package_id=pkg_id, data=data)

        event = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "deploy.package_artifact_uploaded",
                    AuditEvent.target_id == str(pkg_id),
                )
            )
        ).scalar_one()
        assert event.actor_type == "system"
        assert event.actor_id is None


async def test_a_second_org_cannot_delete_or_store_an_artifact_on_org_as_package_under_rls(
    store: ObjectStore, admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, user_a = await _org_user(admin_engine)
    org_b, user_b = await _org_user(admin_engine)

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        pkg = await create_package(
            s,
            org_id=org_a,
            actor_id=user_a,
            name="acrobat",
            version="24.1",
            os_family="windows",
            installer_type="msi",
            detection_rule={},
            uninstall_rule={},
        )
        pkg_id = pkg.id

    # org_b's delete against org_a's package returns False and does not delete it.
    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        deleted = await delete_package(
            s, org_id=org_b, actor_id=user_b, package_id=pkg_id
        )
        assert deleted is False

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        still_there = (
            await s.execute(select(Package).where(Package.id == pkg_id))
        ).scalar_one_or_none()
        assert still_there is not None

    # org_b's store_artifact against org_a's package should not find the row.
    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(NoResultFound):  # RLS + explicit predicate both exclude it
            await store_artifact(
                s, store, org_id=org_b, package_id=pkg_id, data=b"x"
            )

    # org_a can still delete its own package, and it is gone afterward + audited.
    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        deleted = await delete_package(
            s, org_id=org_a, actor_id=user_a, package_id=pkg_id
        )
        assert deleted is True

        event = (
            await s.execute(
                select(AuditEvent).where(AuditEvent.action == "deploy.package_deleted")
            )
        ).scalar_one()
        assert event.target_id == str(pkg_id)

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        gone = (
            await s.execute(select(Package).where(Package.id == pkg_id))
        ).scalar_one_or_none()
        assert gone is None
