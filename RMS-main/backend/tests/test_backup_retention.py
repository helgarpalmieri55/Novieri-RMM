from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.modules.backup.models import BackupPoint
from app.modules.backup.retention import select_gfs

ORG = uuid4()
DEVICE = uuid4()
REPO = uuid4()
RUN = uuid4()


def _point(taken_at: datetime, state: str = "complete") -> BackupPoint:
    return BackupPoint(
        id=uuid4(),
        org_id=ORG,
        device_id=DEVICE,
        repository_id=REPO,
        backup_run_id=RUN,
        taken_at=taken_at,
        manifest_ref="s3://x",
        size=1,
        state=state,
    )


def test_ten_daily_points_daily_7_keeps_7_newest_prunes_3() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    points = [_point(now - timedelta(days=i)) for i in range(10)]  # days 0..9 ago
    keep, prune = select_gfs(points, {"daily": 7}, now)
    # newest 7 distinct calendar days (offsets 0..6) are kept
    expected_keep_ids = {p.id for p in points if (now - p.taken_at).days <= 6}
    expected_prune_ids = {p.id for p in points} - expected_keep_ids
    assert keep == expected_keep_ids
    assert prune == expected_prune_ids
    assert len(keep) == 7
    assert len(prune) == 3


def test_weekly_bucket_keeps_point_outside_daily_window() -> None:
    """A single point that is the newest (only) point of an ISO week 10 days
    ago is outside a daily=7 window but must still be kept by weekly=4."""
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)  # Tuesday, ISO week (2026, 30)
    recent = [_point(now - timedelta(days=i)) for i in range(7)]  # last 7 days, ISO week 29/30
    old_week_point = _point(now - timedelta(days=10))  # 10 days ago -> outside daily=7 window
    points = recent + [old_week_point]

    keep, prune = select_gfs(points, {"daily": 7, "weekly": 4}, now)

    assert old_week_point.id in keep
    assert old_week_point.id not in prune


def test_empty_gfs_prunes_everything() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    points = [_point(now - timedelta(days=i)) for i in range(5)]
    keep, prune = select_gfs(points, {}, now)
    assert keep == set()
    assert prune == {p.id for p in points}


def test_pruned_points_excluded_from_both_sets() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    live = [_point(now - timedelta(days=i)) for i in range(3)]
    pruned = _point(now - timedelta(days=100), state="pruned")
    keep, prune = select_gfs(live + [pruned], {"daily": 7}, now)
    assert pruned.id not in keep
    assert pruned.id not in prune
    assert keep == {p.id for p in live}
