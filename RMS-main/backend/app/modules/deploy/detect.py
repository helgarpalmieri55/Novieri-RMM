"""Server-side detection-rule evaluation against inventory
(docs/modules/app-deploy/spec.md).

Before dispatching an `app.deploy` job the server can sometimes tell, from the
inventory it already has on hand, that the device is already in the state the
deployment is trying to reach — and skip the job entirely. This module is that
check.

The detection-rule grammar is closed (`app.modules.deploy.packages.RULE_TYPES`):
`package_installed`, `file_exists`, `registry_value`, `command_exit_zero`. Of
those, only `package_installed` maps onto data the server actually has —
`InstalledSoftware` rows populated by `inventory.collect` sweeps. The other
three describe device-local filesystem/registry/process state that the server
has no copy of; they can only be evaluated by the agent at job-execution time.
For those (and for an empty `{}` rule, or any rule this server build doesn't
recognize), `device_in_desired_state` returns `False` — "not confirmed to be in
the desired state" — so the job dispatches and the agent makes the real call.
This is a deliberately permissive default: a rule that was already validated
at package-create time (`packages.validate_rule`) is never re-validated or
raised on here.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import InstalledSoftware


async def _package_installed(
    session: AsyncSession,
    *,
    org_id: UUID,
    device_id: UUID,
    name: str,
    version: str | None,
) -> bool:
    """True iff the device has an `InstalledSoftware` row with this exact
    `name` (and `version`, when the rule specifies one). Existence-checked via
    `.limit(1)`, never a full fetch. Always org-scoped (invariant #5)."""
    stmt = select(InstalledSoftware.id).where(
        InstalledSoftware.org_id == org_id,
        InstalledSoftware.device_id == device_id,
        InstalledSoftware.name == name,
    )
    if version is not None:
        stmt = stmt.where(InstalledSoftware.version == version)
    result = await session.execute(stmt.limit(1))
    return result.first() is not None


async def device_in_desired_state(
    session: AsyncSession,
    *,
    org_id: UUID,
    device_id: UUID,
    action: str,
    detection_rule: dict[str, Any],
) -> bool:
    """`True` means "skip the dispatch — the device is already where `action`
    wants it": for `install`/`update`, that's when the detection rule matches
    (the package is already there); for `uninstall`, that's when it does NOT
    match (the package is already gone).

    Only `rule["type"] == "package_installed"` is evaluable from inventory.
    Every other case — an empty rule, an unrecognized type, or one of the
    device-local types (`file_exists`, `registry_value`,
    `command_exit_zero`) — returns `False` unconditionally, regardless of
    `action`: the server cannot claim "already gone" any more than it can
    claim "already installed" when it has no data to check, so it always
    defers to the agent rather than guessing from the action alone.
    """
    if detection_rule.get("type") != "package_installed":
        return False

    name = detection_rule.get("name")
    if not name:
        return False

    matches = await _package_installed(
        session,
        org_id=org_id,
        device_id=device_id,
        name=name,
        version=detection_rule.get("version"),
    )
    if action == "uninstall":
        return not matches
    return matches
