"""Grandfather-Father-Son (GFS) retention selection (docs/modules/backup/spec.md §8).

Pure function, no DB/IO: given a device+repository's `backup_points` and a
`retention_gfs` dict (`{"daily": D, "weekly": W, "monthly": M, "yearly": Y}`,
any missing key treated as 0), decide which points to keep and which to
prune.

For each bucket we look at the most-recent N *distinct periods* that have at
least one point, and keep the single newest point within each of those
periods:

- **daily**: period = calendar date (`taken_at.date()`); the D most recent
  distinct dates, newest point per date.
- **weekly**: period = ISO (year, week) (`taken_at.isocalendar()[:2]`); the W
  most recent distinct ISO weeks, newest point per week.
- **monthly**: period = (year, month); the M most recent distinct months,
  newest point per month.
- **yearly**: period = year; the Y most recent distinct years, newest point
  per year.

A point kept by *any* bucket is kept overall (the buckets overlap by design —
e.g. the newest point in an otherwise-untouched older ISO week can be kept by
the weekly bucket even though it falls outside the daily window). Every other
point is pruned. Only points with `state != "pruned"` are considered at all
(already-pruned points are excluded from both the keep and prune sets, since
they are not being reconsidered here).

Selection is deterministic: within a bucket, points are ordered newest-first
by `taken_at`, and the first (newest) point encountered for a given period is
the one kept for that period.
"""

from collections import OrderedDict
from datetime import datetime
from typing import Any
from uuid import UUID

from app.modules.backup.models import BackupPoint


def _bucket_keep(
    points: list[BackupPoint], period_of: Any, count: int
) -> set[UUID]:
    if count <= 0:
        return set()
    # newest-first, so the first point seen for a period is that period's newest
    ordered = sorted(points, key=lambda p: p.taken_at, reverse=True)
    periods: OrderedDict[Any, UUID] = OrderedDict()
    for point in ordered:
        period = period_of(point.taken_at)
        if period not in periods:
            periods[period] = point.id
        if len(periods) >= count:
            break
    return set(periods.values())


def select_gfs(
    points: list[BackupPoint], gfs: dict[str, int], now: datetime
) -> tuple[set[UUID], set[UUID]]:
    """Return (keep_ids, prune_ids) for the given (non-pruned) backup points."""
    live = [p for p in points if p.state != "pruned"]

    keep_ids: set[UUID] = set()
    keep_ids |= _bucket_keep(live, lambda ts: ts.date(), gfs.get("daily", 0))
    keep_ids |= _bucket_keep(live, lambda ts: ts.isocalendar()[:2], gfs.get("weekly", 0))
    keep_ids |= _bucket_keep(live, lambda ts: (ts.year, ts.month), gfs.get("monthly", 0))
    keep_ids |= _bucket_keep(live, lambda ts: ts.year, gfs.get("yearly", 0))

    prune_ids = {p.id for p in live if p.id not in keep_ids}
    return keep_ids, prune_ids
