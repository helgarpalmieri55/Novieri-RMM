"""The Celery broker is Redis, so the backend asks for Celery's `redis` extra
(the first-boot rehearsal of plan 2026-11-23, Task 12, finding F7).

kombu's Redis transport imports `packaging`, which kombu declares only under
its `redis` extra (kombu 5.7.0's METADATA: `Requires-Dist: packaging==26.3;
extra == "redis"`). Without the extra, `pip install .` in
deploy/backend.Dockerfile installs no `packaging`, and the `worker` and
`backup-replicator` services die at start with `ModuleNotFoundError: No module
named 'packaging'` and restart for ever -- no beat schedule, no backup
replication. The backend suite cannot see it at run time: its venv has
`packaging` through pytest. Measured on 2026-11-24, with the images the
rehearsal built from backend/pyproject.toml.
"""

import re
import tomllib
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def test_the_broker_is_redis() -> None:
    """The premise: the broker URL is the Redis URL (`app/worker.py`)."""
    worker = (BACKEND / "app" / "worker.py").read_text()
    assert 'Celery("its", broker=settings.redis_url, backend=settings.redis_url)' in worker


def test_celery_is_declared_with_its_redis_extra() -> None:
    dependencies = tomllib.loads((BACKEND / "pyproject.toml").read_text())["project"][
        "dependencies"
    ]
    celery = [d for d in dependencies if re.match(r"celery\b", d)]
    assert celery == ["celery[redis]>=5.4"], celery
