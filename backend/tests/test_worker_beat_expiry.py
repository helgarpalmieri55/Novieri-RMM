"""Every Celery beat entry must expire, so an overrunning sweep cannot backlog
without bound.

Beat publishes on the clock no matter how far behind the workers are: an entry
whose run takes longer than its own schedule enqueues a second copy while the
first is still going, then a third. The intervals also cluster hard -- eight
entries at 86400s, three at 30s, three at 60s, four at 900s -- and Celery fires
same-interval entries together from beat start.

CONCURRENT RUNS ARE SAFE (`SELECT ... FOR UPDATE SKIP LOCKED` plus the relevant
UNIQUE constraints), so this is a CAPACITY bound and not a correctness one.
`health-alert-actions` at 30s is the shortest-interval sweep that signs jobs --
an OpenBao round trip each -- and creates tickets, which is what a queue of
superseded copies actually costs.

WHY NOT ALSO STAGGER THE DAILY ENTRIES: every daily entry deliberately reads its
schedule from its own module's constant (`health_sweep_module.
RETENTION_INTERVAL_S`, `ipam_reconcile_module.RECONCILE_INTERVAL_SECONDS`,
`ai_retention_module.RETENTION_INTERVAL_S`, ...) precisely so the beat entry and
the module cannot drift apart, and several module docstrings say so. Celery
cannot offset an interval schedule -- the only way to stagger is `crontab(hour=)`,
which cannot express those constants at all. Staggering would buy a smoother
midnight at the cost of the anti-drift binding, on the cheapest sweeps in the
file. `expires` is the half that is free.
"""

from pathlib import Path

from app.core.config import Settings, override_settings
from tests.conftest import PgCluster

REDIS_URL = "redis://localhost:6379/9"


def _settings(pg_cluster: PgCluster, tmp_path: Path) -> Settings:
    return Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url=REDIS_URL,
    )


def test_every_beat_entry_expires_after_one_interval(
    pg_cluster: PgCluster, tmp_path: Path
) -> None:
    """ASSERTED OVER THE WHOLE SCHEDULE, not over a sampled entry: the entry
    that backlogs is the one whose `options` someone forgot, and a test naming
    three entries cannot see the fourth. `_with_expiry` exists for the same
    reason.

    One interval is the right window because these are all sweeps over "what is
    due now": a message still queued when its successor is published has been
    superseded, and the successor does strictly more work than the stale copy."""
    from app import worker

    with override_settings(_settings(pg_cluster, tmp_path)):
        schedule = worker.create_celery().conf.beat_schedule

    assert schedule, "no beat entries at all"
    missing = [name for name, e in schedule.items() if "expires" not in e.get("options", {})]
    assert missing == [], f"beat entries with an unbounded backlog: {missing}"

    wrong = {
        name: (e["options"]["expires"], e["schedule"])
        for name, e in schedule.items()
        if e["options"]["expires"] != e["schedule"]
    }
    assert wrong == {}, f"expires does not match the entry's own interval: {wrong}"


def test_with_expiry_leaves_an_explicit_window_alone() -> None:
    """`setdefault`, not assignment: an entry that ever needs a window other
    than its interval must be able to say so and keep it."""
    from app.worker import _with_expiry

    out = _with_expiry(
        {
            "explicit": {"task": "t", "schedule": 900.0, "options": {"expires": 60.0}},
            "default": {"task": "t", "schedule": 900.0},
        }
    )
    assert out["explicit"]["options"]["expires"] == 60.0
    assert out["default"]["options"]["expires"] == 900.0
