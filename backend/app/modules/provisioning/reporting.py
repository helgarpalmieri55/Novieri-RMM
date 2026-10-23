"""Provisioning engine reporting (docs/modules/provisioning/spec.md).

Pure read-only aggregation over the provisioning module's tables. This module
performs **no writes** and dispatches **no jobs** — it exists solely to
answer "what happened on this one provisioning run," "what does provisioning
throughput/success look like across the fleet," and "which profile/image
provisioned this device, and what's its latest run state."

State vocabulary (see `app/modules/provisioning/models.py`):

- `ProvisionRun.state`: pending | awaiting_enrollment | enrolled | complete |
  failed

Only `complete` and `failed` are terminal (decided) states; `pending`,
`awaiting_enrollment`, and `enrolled` are all still in flight.

`fleet_provisioning_report`'s `success_rate` is a FRACTION in `[0.0, 1.0]`
(not a percentage — the frontend multiplies by 100 for display):

- **success_rate** = `complete` runs / (`complete` + `failed`) runs — i.e. of
  terminal (decided) runs only. `0.0` on divide-by-zero, mirroring
  `backup/reporting.py`'s "0.0 when no rows" convention, so a fleet with only
  `pending`/`awaiting_enrollment`/`enrolled` runs also reports `0.0`; callers
  distinguish "not yet decided" from "decided and unsuccessful" via
  `run_counts_by_state`, not the rate alone.
- **machines_provisioned** = count of runs in state `complete`. This counts
  runs, not distinct devices — a device re-provisioned more than once
  contributes one `complete` run per successful provision.
- **per_profile** = `{profile_id: {"complete": n, "failed": n, "total": n}}`,
  one entry per profile that has at least one run in the org. `total` counts
  *every* run for that profile regardless of state (including still-in-flight
  runs); `complete`/`failed` count only those two states.

`device_provisioning`'s `enrolled_at` is the most recent run bound to the
device's `finished_at` — the best-available proxy for that run's terminal
transition timestamp (there is no separate per-transition timestamp column on
`ProvisionRun`). It is `None` if that run hasn't finished yet.

Every query below filters on `org_id` for every table involved (ProvisionRun,
ProvisionProfile) per the tenancy invariant — RLS is the backstop, not the
only line of defense.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProvisionProfile, ProvisionRun

RUN_STATES = ("pending", "awaiting_enrollment", "enrolled", "complete", "failed")


async def _image_id_for_profile(
    session: AsyncSession, org_id: UUID, profile_id: UUID
) -> UUID | None:
    """Resolves a run's `image_id` via its (org-scoped) profile. Returns
    `None` if the profile can't be resolved in this org."""
    profile = (
        await session.execute(
            select(ProvisionProfile).where(
                ProvisionProfile.org_id == org_id, ProvisionProfile.id == profile_id
            )
        )
    ).scalar_one_or_none()
    return profile.image_id if profile is not None else None


async def provision_run_status(
    session: AsyncSession, org_id: UUID, run_id: UUID
) -> dict[str, Any]:
    """One provisioning run's status, org-scoped.

    Returns a dict with:
    - `run_id`: the requested id, always echoed back.
    - `state`: the run's `ProvisionRun.state`, or `None` if no such run
      exists in this org.
    - `profile_id`, `image_id` (resolved via the run's `ProvisionProfile`,
      `None` if it can't be resolved), `device_id`, `target_ref`,
      `started_at`, `finished_at`, `error`, `log_ref` — all `None` when the
      run doesn't exist in this org (mirrors `backup.reporting.
      device_backup_status`'s "no history" shape: the requested identifier is
      always echoed back, everything else is `None`).
    """
    run = (
        await session.execute(
            select(ProvisionRun).where(ProvisionRun.org_id == org_id, ProvisionRun.id == run_id)
        )
    ).scalar_one_or_none()

    if run is None:
        return {
            "run_id": run_id,
            "state": None,
            "profile_id": None,
            "image_id": None,
            "device_id": None,
            "target_ref": None,
            "started_at": None,
            "finished_at": None,
            "error": None,
            "log_ref": None,
        }

    image_id = await _image_id_for_profile(session, org_id, run.profile_id)

    return {
        "run_id": run.id,
        "state": run.state,
        "profile_id": run.profile_id,
        "image_id": image_id,
        "device_id": run.device_id,
        "target_ref": run.target_ref,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "error": run.error,
        "log_ref": run.log_ref,
    }


async def fleet_provisioning_report(session: AsyncSession, org_id: UUID) -> dict[str, Any]:
    """Org-wide provisioning rollup. See the module docstring for the exact
    formula behind every computed field.

    Returns `run_counts_by_state` (dict keyed by every `ProvisionRun.state`),
    `success_rate`, `machines_provisioned`, and `per_profile`.
    """
    run_state_rows = (
        await session.execute(
            select(ProvisionRun.state, func.count())
            .where(ProvisionRun.org_id == org_id)
            .group_by(ProvisionRun.state)
        )
    ).all()
    run_counts_by_state = dict.fromkeys(RUN_STATES, 0)
    for state, count in run_state_rows:
        run_counts_by_state[state] = count

    complete_count = run_counts_by_state["complete"]
    failed_count = run_counts_by_state["failed"]
    terminal_total = complete_count + failed_count
    success_rate = (complete_count / terminal_total) if terminal_total else 0.0

    machines_provisioned = complete_count

    profile_rows = (
        await session.execute(
            select(ProvisionRun.profile_id, ProvisionRun.state, func.count())
            .where(ProvisionRun.org_id == org_id)
            .group_by(ProvisionRun.profile_id, ProvisionRun.state)
        )
    ).all()
    per_profile: dict[UUID, dict[str, int]] = {}
    for profile_id, state, count in profile_rows:
        bucket = per_profile.setdefault(profile_id, {"complete": 0, "failed": 0, "total": 0})
        if state == "complete":
            bucket["complete"] = count
        elif state == "failed":
            bucket["failed"] = count
        bucket["total"] += count

    return {
        "run_counts_by_state": run_counts_by_state,
        "success_rate": success_rate,
        "machines_provisioned": machines_provisioned,
        "per_profile": per_profile,
    }


async def device_provisioning(
    session: AsyncSession, org_id: UUID, device_id: UUID
) -> dict[str, Any]:
    """Which profile/image provisioned this device, and its latest run
    state.

    Returns `device_id` (echoed back), `profile_id`, `image_id` (resolved via
    the run's profile), `enrolled_at` (the most recent bound run's
    `finished_at` — see module docstring), and `latest_run_state` — all taken
    from the most recent `ProvisionRun` bound to this device (by
    `started_at` desc, `id` desc as a tie-break, mirroring
    `backup.reporting`'s "most recent" convention).

    A device with no provisioning run bound to it in this org returns the
    "no history" shape: `device_id` echoed back, everything else `None`
    (mirrors `backup.reporting.device_backup_status`'s uncovered-device
    shape).
    """
    run = (
        await session.execute(
            select(ProvisionRun)
            .where(ProvisionRun.org_id == org_id, ProvisionRun.device_id == device_id)
            .order_by(ProvisionRun.started_at.desc(), ProvisionRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if run is None:
        return {
            "device_id": device_id,
            "profile_id": None,
            "image_id": None,
            "enrolled_at": None,
            "latest_run_state": None,
        }

    image_id = await _image_id_for_profile(session, org_id, run.profile_id)

    return {
        "device_id": device_id,
        "profile_id": run.profile_id,
        "image_id": image_id,
        "enrolled_at": run.finished_at,
        "latest_run_state": run.state,
    }
