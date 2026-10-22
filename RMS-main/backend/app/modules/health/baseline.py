"""The default "Workstation baseline" health profile (platform review 2026-09-23).

A fresh org monitors nothing until someone writes a profile, and §3 rightly
refuses to fabricate one at resolution time. This seeds a REAL profile instead:
an ordinary row, visible and editable in the console, assigned as the org
default, written through the same service functions and audit chain as one an
operator created.

ONCE PER ORG. `health_baseline_seed` records the decision -- `seeded`, or
`skipped` because the org already had profiles -- and the seed never runs for
that org again, so an org's own profiles are never touched and a deleted
baseline stays deleted. The marker insert is `ON CONFLICT DO NOTHING` on the
org's primary key, which also makes two concurrent callers safe.

WHAT THE CHECKS ARE, AND WHAT IS LEFT OUT:

- disk free < 10% on the worst volume, all OSes;
- offline > 30 min (server-evaluated; the threshold is the org setting
  `offline_after_s`, set to 1800 here only if the org has no settings row);
- disk encryption off and firewall off, Windows and macOS only -- the Linux
  servers in this fleet are not expected to run LUKS or a host firewall, and a
  check that fires on every server is noise;
- the Check Point Harmony Endpoint watchdog process (`EPWD.exe`) absent,
  Windows only, via `process.present`. `service.state` would be the better
  check but no agent sampler answers it yet (it reports `unavailable`
  everywhere). There is no macOS row: the Harmony daemon's process name on
  macOS has not been confirmed, and a wrong name would open a critical alert
  on every Mac.

Critical checks declare `open_ticket`; the ticket is opened once the org sets
`default_alert_queue_id` and `default_ticket_category_id` (declared_actions.py).
The offline check is a warning and opens no ticket: laptops go offline nightly.
"""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.modules.health.models import HealthBaselineSeed, HealthProfile
from app.modules.health.profiles import create_assignment, create_profile, replace_checks
from app.modules.health.settings import get_settings_row, update_settings

logger = logging.getLogger("its.health.baseline")

BASELINE_NAME = "Workstation baseline"
BASELINE_DESCRIPTION = (
    "Seeded by the platform as this organization's default. Edit or replace it "
    "freely; it is not re-created if deleted."
)

# How often the worker looks for orgs that have never been decided.
RECONCILE_INTERVAL_S = 3600.0

BASELINE_OFFLINE_AFTER_S = 1800

HARMONY_WINDOWS_PROCESS = "EPWD.exe"


def _critical(spec: dict[str, Any]) -> dict[str, Any]:
    return {"severity": "critical", "declares_open_ticket": True, **spec}


BASELINE_CHECKS: tuple[dict[str, Any], ...] = (
    _critical(
        {
            "check_type": "disk.free_pct",
            "selector": "*",
            "comparator": "lt",
            "threshold_num": 10,
            "for_ticks": 3,
        }
    ),
    {
        "check_type": "agent.offline",
        "comparator": "gt",
        "threshold_num": BASELINE_OFFLINE_AFTER_S,
        "for_ticks": 1,
        "severity": "warning",
        "declares_open_ticket": False,
    },
    *(
        _critical(
            {
                "check_type": check_type,
                "comparator": "is",
                "threshold_bool": True,
                "for_ticks": 1,
                "os_family": os_family,
            }
        )
        for check_type in ("posture.disk_encryption", "posture.firewall_enabled")
        for os_family in ("windows", "macos")
    ),
    _critical(
        {
            "check_type": "process.present",
            "selector": HARMONY_WINDOWS_PROCESS,
            "comparator": "is",
            "threshold_bool": True,
            "for_ticks": 3,
            "os_family": "windows",
        }
    ),
    # Server-evaluated checks from backup and scheduled scripts. Migrations
    # 0091 and 0121 only add these to profiles that already existed, so a
    # baseline seeded later has to carry them itself. Each one only fires
    # for devices that have a backup policy or a scheduled script.
    _critical(
        {
            "check_type": "backup.success_age_h",
            "comparator": "gt",
            "threshold_num": 50,
            "for_ticks": 1,
        }
    ),
    _critical(
        {
            "check_type": "backup.last_run_failed",
            "comparator": "is",
            "threshold_bool": True,
            "for_ticks": 1,
        }
    ),
    {
        "check_type": "backup.offsite_lag_h",
        "comparator": "gt",
        "threshold_num": 24,
        "for_ticks": 1,
        "severity": "warning",
        "declares_open_ticket": False,
    },
    {
        "check_type": "backup.restore_test_age_d",
        "comparator": "gt",
        "threshold_num": 35,
        "for_ticks": 1,
        "severity": "warning",
        "declares_open_ticket": False,
    },
    {
        "check_type": "script.policy_failed",
        "selector": "",
        "comparator": "is",
        "threshold_bool": True,
        "for_ticks": 1,
        "severity": "warning",
        "declares_open_ticket": False,
    },
)


async def ensure_baseline(session: AsyncSession, org_id: UUID) -> str:
    """Decide this org's baseline once. Returns `seeded`, `skipped` (the org
    already had profiles) or `already` (decided before; nothing done)."""
    claimed = (
        await session.execute(
            pg_insert(HealthBaselineSeed)
            .values(org_id=org_id, outcome="skipped", recorded_at=func.now())
            .on_conflict_do_nothing(index_elements=["org_id"])
            .returning(HealthBaselineSeed.org_id)
        )
    ).scalar_one_or_none()
    if claimed is None:
        return "already"

    existing = (
        await session.execute(
            select(func.count()).select_from(HealthProfile).where(HealthProfile.org_id == org_id)
        )
    ).scalar_one()
    if existing:
        return "skipped"

    profile = await create_profile(
        session, org_id, None, name=BASELINE_NAME, description=BASELINE_DESCRIPTION
    )
    await replace_checks(session, org_id, None, profile.id, [dict(c) for c in BASELINE_CHECKS])
    await create_assignment(session, org_id, None, profile_id=profile.id)
    offline_set = False
    if await get_settings_row(session, org_id) is None:
        await update_settings(
            session, org_id, None, {"offline_after_s": BASELINE_OFFLINE_AFTER_S}
        )
        offline_set = True

    await session.execute(
        update(HealthBaselineSeed)
        .where(HealthBaselineSeed.org_id == org_id)
        .values(outcome="seeded", profile_id=profile.id)
    )
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action="health.baseline_seeded",
        target_type="health_profile",
        target_id=str(profile.id),
        metadata={
            "name": BASELINE_NAME,
            "checks": len(BASELINE_CHECKS),
            "offline_after_s_set": offline_set,
        },
    )
    await session.flush()
    return "seeded"


async def ensure_baseline_contained(session: AsyncSession, org_id: UUID) -> str | None:
    """`ensure_baseline` inside a SAVEPOINT, for a caller whose own work must
    not fail with it (org creation). A failure is logged and left for the
    worker's reconcile to retry. Returns the outcome, or None on failure."""
    try:
        async with session.begin_nested():
            return await ensure_baseline(session, org_id)
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("health baseline not seeded for org %s; the reconcile will retry", org_id)
        return None
