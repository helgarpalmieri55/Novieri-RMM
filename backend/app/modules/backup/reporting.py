"""Backup engine reporting + SOC 2/BCP evidence export
(docs/modules/backup/spec.md).

Pure read-only aggregation over the backup module's tables. This module
performs **no writes** and dispatches **no jobs** — it exists solely to
answer "is this device's data actually protected" for a single device, "what
does backup coverage/health look like across the fleet," and to produce the
flat, per-device export used as SOC 2 / business-continuity compliance
evidence.

State vocabularies (see `app/modules/backup/models.py`):

- `BackupRun.state`: pending | running | success | failed | missed
- `BackupPoint.state`: complete | failed | pruned

A backup point only ever represents a *restorable* snapshot when its state is
`"complete"` — `"failed"` and `"pruned"` points are excluded from every
coverage/last-good-point calculation below. A point being `verified_at` is
NOT NULL means an integrity check (e.g. manifest/checksum verification) has
been performed against it; `restore_tested_at` NOT NULL means a full restore
has actually been exercised against it (the stronger, restore-proven
guarantee used for BCP evidence).

`fleet_backup_report`'s two rate fields are both fractions in `[0.0, 1.0]`
(not percentages), computed as `0.0` on divide-by-zero, mirroring
`deploy/reporting.py`'s "0.0 when no rows" convention:

- **coverage_rate** = (devices with at least one `complete` `BackupPoint`) /
  (total devices in the org), `0.0` when the org has zero devices.
- **success_rate** = `success` runs / (`success` + `failed` + `missed` runs)
  — i.e. of terminal (decided) runs only; a missed schedule counts against
  it. `0.0` when there are zero terminal runs, so a fleet with only
  `pending`/`running` runs also reports `0.0`; callers
  distinguish "not yet decided" from "decided and unsuccessful" via
  `run_counts_by_state`, not the rate alone.

Dedup savings compare, over `success` runs only, the total bytes the backup
was asked to protect (`bytes_protected`, i.e. the logical/source size) against
the bytes actually written to the repository after dedup/compression
(`bytes_stored`):

- `dedup_ratio` = `bytes_stored_total / bytes_protected_total` (the fraction
  of original size actually stored — lower is better dedup), `0.0` when
  `bytes_protected_total` is `0`.
- `savings_pct` = `(1 - dedup_ratio) * 100` (percent of original size saved
  by dedup/compression), `0.0` when `bytes_protected_total` is `0`.

Every query below filters on `org_id` for every table involved (BackupRun,
BackupPoint, BackupRepository, Device) per the tenancy invariant — RLS is the
backstop, not the only line of defense.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BackupPoint, BackupRepository, BackupRun, Device

RUN_STATES = ("pending", "running", "success", "failed", "missed")


async def device_backup_status(
    session: AsyncSession, org_id: UUID, device_id: UUID
) -> dict[str, Any]:
    """Per-device backup posture.

    Returns:
    - `device_id`
    - `last_good_point`: the most recent `complete` `BackupPoint` (by
      `taken_at` desc) as `{"backup_point_id", "taken_at", "size",
      "repository_id"}`, or `None` if the device has never produced one.
    - `last_run_state` / `last_run_finished_at`: the most recent `BackupRun`
      (any state, by `started_at` desc) — `None`/`None` if the device has no
      runs at all.
    - `verified_at` / `restore_tested_at`: taken from `last_good_point`
      (`None` if there is no last-good point).
    - `repository`: `{"id", "name"}` of the last-good point's repository, or
      `None` if there is no last-good point.
    """
    point = (
        await session.execute(
            select(BackupPoint)
            .where(
                BackupPoint.org_id == org_id,
                BackupPoint.device_id == device_id,
                BackupPoint.state == "complete",
            )
            .order_by(BackupPoint.taken_at.desc(), BackupPoint.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    last_run = (
        await session.execute(
            select(BackupRun)
            .where(BackupRun.org_id == org_id, BackupRun.device_id == device_id)
            .order_by(BackupRun.started_at.desc(), BackupRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    repository: dict[str, Any] | None = None
    if point is not None:
        repo = (
            await session.execute(
                select(BackupRepository).where(
                    BackupRepository.org_id == org_id,
                    BackupRepository.id == point.repository_id,
                )
            )
        ).scalar_one_or_none()
        if repo is not None:
            repository = {"id": repo.id, "name": repo.name}

    last_good_point = (
        {
            "backup_point_id": point.id,
            "taken_at": point.taken_at,
            "size": point.size,
            "repository_id": point.repository_id,
        }
        if point is not None
        else None
    )

    return {
        "device_id": device_id,
        "last_good_point": last_good_point,
        "last_run_state": last_run.state if last_run is not None else None,
        "last_run_finished_at": last_run.finished_at if last_run is not None else None,
        "verified_at": point.verified_at if point is not None else None,
        "restore_tested_at": point.restore_tested_at if point is not None else None,
        "repository": repository,
    }


async def fleet_backup_report(session: AsyncSession, org_id: UUID) -> dict[str, Any]:
    """Org-wide backup rollup. See the module docstring for the exact
    formula behind every computed field.

    Returns `devices_total`, `devices_covered`, `coverage_rate`,
    `run_counts_by_state` (dict keyed by every `BackupRun.state`),
    `success_rate`, `points_needing_verification` (count of `complete` points
    with `verified_at IS NULL`), and `dedup` (`bytes_protected_total`,
    `bytes_stored_total`, `dedup_ratio`, `savings_pct`).
    """
    devices_total = (
        await session.execute(
            select(func.count()).select_from(Device).where(Device.org_id == org_id)
        )
    ).scalar_one()

    devices_covered = (
        await session.execute(
            select(func.count(func.distinct(BackupPoint.device_id)))
            .select_from(BackupPoint)
            .where(BackupPoint.org_id == org_id, BackupPoint.state == "complete")
        )
    ).scalar_one()

    coverage_rate = (devices_covered / devices_total) if devices_total else 0.0

    run_state_rows = (
        await session.execute(
            select(BackupRun.state, func.count())
            .where(BackupRun.org_id == org_id)
            .group_by(BackupRun.state)
        )
    ).all()
    run_counts_by_state = dict.fromkeys(RUN_STATES, 0)
    for state, count in run_state_rows:
        run_counts_by_state[state] = count

    success_count = run_counts_by_state["success"]
    failed_count = run_counts_by_state["failed"] + run_counts_by_state["missed"]
    terminal_total = success_count + failed_count
    success_rate = (success_count / terminal_total) if terminal_total else 0.0

    points_needing_verification = (
        await session.execute(
            select(func.count())
            .select_from(BackupPoint)
            .where(
                BackupPoint.org_id == org_id,
                BackupPoint.state == "complete",
                BackupPoint.verified_at.is_(None),
            )
        )
    ).scalar_one()

    raw_protected, raw_stored = (
        await session.execute(
            select(
                func.coalesce(func.sum(BackupRun.bytes_protected), 0),
                func.coalesce(func.sum(BackupRun.bytes_stored), 0),
            ).where(BackupRun.org_id == org_id, BackupRun.state == "success")
        )
    ).one()
    # Postgres SUM() over bigint yields numeric (Decimal); normalize to int
    # since these are exact byte counts, so downstream ratio math is plain
    # float division rather than Decimal/float comparisons.
    bytes_protected_total = int(raw_protected)
    bytes_stored_total = int(raw_stored)
    dedup_ratio = (bytes_stored_total / bytes_protected_total) if bytes_protected_total else 0.0
    savings_pct = ((1 - dedup_ratio) * 100) if bytes_protected_total else 0.0

    return {
        "devices_total": devices_total,
        "devices_covered": devices_covered,
        "coverage_rate": coverage_rate,
        "run_counts_by_state": run_counts_by_state,
        "success_rate": success_rate,
        "points_needing_verification": points_needing_verification,
        "dedup": {
            "bytes_protected_total": bytes_protected_total,
            "bytes_stored_total": bytes_stored_total,
            "dedup_ratio": dedup_ratio,
            "savings_pct": savings_pct,
        },
    }


async def backup_evidence_rows(session: AsyncSession, org_id: UUID) -> list[dict[str, Any]]:
    """Flat, one-row-per-device export for SOC 2 / business-continuity
    evidence: device identity, whether it is covered by at least one
    `complete` backup point, that point's `taken_at`/`verified_at`, and
    whether a restore has actually been proven (`restore_tested_at` set). A
    device with zero backups still gets a row with `covered=False` — that
    row IS the evidence of a coverage gap, which is the point of this
    export. Ordered deterministically by hostname, then `device_id` as a
    tie-break, so repeated exports are stable/diffable.
    """
    rn = (
        func.row_number()
        .over(
            partition_by=BackupPoint.device_id,
            order_by=[BackupPoint.taken_at.desc(), BackupPoint.id.desc()],
        )
        .label("rn")
    )
    latest_points_subq = (
        select(
            BackupPoint.device_id.label("device_id"),
            BackupPoint.taken_at.label("taken_at"),
            BackupPoint.verified_at.label("verified_at"),
            BackupPoint.restore_tested_at.label("restore_tested_at"),
            rn,
        )
        .where(BackupPoint.org_id == org_id, BackupPoint.state == "complete")
        .subquery()
    )
    latest_points = (
        select(
            latest_points_subq.c.device_id,
            latest_points_subq.c.taken_at,
            latest_points_subq.c.verified_at,
            latest_points_subq.c.restore_tested_at,
        )
        .where(latest_points_subq.c.rn == 1)
        .subquery()
    )

    rows = (
        await session.execute(
            select(
                Device.id.label("device_id"),
                Device.hostname.label("hostname"),
                latest_points.c.taken_at,
                latest_points.c.verified_at,
                latest_points.c.restore_tested_at,
            )
            .select_from(Device)
            .outerjoin(latest_points, latest_points.c.device_id == Device.id)
            .where(Device.org_id == org_id)
            .order_by(Device.hostname.asc(), Device.id.asc())
        )
    ).all()

    return [
        {
            "device_id": row.device_id,
            "hostname": row.hostname,
            "covered": row.taken_at is not None,
            "last_backup_taken_at": row.taken_at,
            "verified_at": row.verified_at,
            "restore_tested_at": row.restore_tested_at,
            "restore_proven": row.restore_tested_at is not None,
        }
        for row in rows
    ]
