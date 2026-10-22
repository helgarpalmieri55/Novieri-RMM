"""Package registry service (docs/modules/app-deploy/spec.md).

Packages are the org-scoped catalog of installable software. This module
owns creation/validation of catalog rows, upload of the installer artifact
to object storage, and deletion. The artifact is stored under a
package_id-keyed object store key (`{org_id}/package-artifacts/{package_id}`,
see `ObjectStore.put_package_artifact`) — NOT content-addressed by its
sha256 — and the uploaded bytes' sha256 is separately recorded on the
`Package` row (`artifact_sha256`) purely as an integrity check on later
download/reconciliation, not as the storage key. Every write is audited;
every query/write carries an explicit `org_id` predicate.
"""

import hashlib
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.storage import ObjectStore
from app.modules.deploy.models import Package

OS_FAMILIES = {"windows", "linux", "macos"}
INSTALLER_TYPES = {"msi", "exe", "winget", "choco", "pkg", "dmg", "deb", "rpm", "script"}
RULE_TYPES = {"file_exists", "registry_value", "package_installed", "command_exit_zero"}


class DeployValidationError(Exception):
    pass


def validate_rule(rule: dict[str, Any]) -> None:
    """A rule is either `{}` (no rule) or a dict with a `type` in RULE_TYPES.
    Validation is deliberately minimal: only the discriminator is checked."""
    if not rule:
        return
    rule_type = rule.get("type")
    if rule_type not in RULE_TYPES:
        raise DeployValidationError(tr("unknown rule type: {rule_type}", rule_type=repr(rule_type)))


async def create_package(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    name: str,
    version: str,
    os_family: str,
    installer_type: str,
    detection_rule: dict[str, Any],
    uninstall_rule: dict[str, Any],
    install_args: str | None = None,
    package_identifier: str | None = None,
) -> Package:
    if os_family not in OS_FAMILIES:
        raise DeployValidationError(tr("unknown os_family: {os_family}", os_family=repr(os_family)))
    if installer_type not in INSTALLER_TYPES:
        raise DeployValidationError(
            tr("unknown installer_type: {installer_type}", installer_type=repr(installer_type))
        )
    validate_rule(detection_rule)
    validate_rule(uninstall_rule)

    package = Package(
        org_id=org_id,
        name=name,
        version=version,
        os_family=os_family,
        installer_type=installer_type,
        detection_rule=detection_rule,
        uninstall_rule=uninstall_rule,
        install_args=install_args,
        package_identifier=(package_identifier or None),
    )
    session.add(package)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="deploy.package_created",
        target_type="package",
        target_id=str(package.id),
        metadata={"name": name, "version": version, "os_family": os_family},
    )

    return package


async def store_artifact(
    session: AsyncSession,
    object_store: ObjectStore,
    *,
    org_id: UUID,
    package_id: UUID,
    data: bytes,
    actor_id: UUID | None = None,
    actor_type: Literal["user", "agent", "system"] = "system",
) -> Package:
    package = (
        await session.execute(
            select(Package).where(Package.org_id == org_id, Package.id == package_id)
        )
    ).scalar_one()

    artifact_sha256 = hashlib.sha256(data).hexdigest()
    artifact_ref = await object_store.put_package_artifact(org_id, package_id, data)

    package.artifact_ref = artifact_ref
    package.artifact_sha256 = artifact_sha256
    session.add(package)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action="deploy.package_artifact_uploaded",
        target_type="package",
        target_id=str(package_id),
        metadata={"artifact_sha256": artifact_sha256},
    )

    return package


async def delete_package(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    package_id: UUID,
) -> bool:
    package = (
        await session.execute(
            select(Package).where(Package.org_id == org_id, Package.id == package_id)
        )
    ).scalar_one_or_none()
    if package is None:
        return False

    await session.delete(package)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="deploy.package_deleted",
        target_type="package",
        target_id=str(package_id),
        metadata={},
    )

    return True
