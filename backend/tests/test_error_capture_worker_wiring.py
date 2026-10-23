"""Task 3, worker side: `_OrgCycleFailures.contain` and Celery's `task_failure`
signal both call `capture_exception(..., where="worker")`, and the worker's
own drain (`drain_platform_errors_all`) writes to Postgres.
"""

import gc
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings, override_settings
from app.core.errors.capture import drained
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


def test_contain_captures_the_org_failure_alongside_logging_it() -> None:
    """`_OrgCycleFailures.contain` is the seam every `*_all_orgs` shell in
    `app/worker.py` already routes a CONTAINED per-org failure through --
    the sweep continues for every other org, so nothing else ever sees this
    exception. Without a capture call here, every contained worker failure
    in the fleet would be invisible to the error monitor entirely. Pure unit
    test -- no DB, no Celery app -- `contain` only logs and enqueues."""
    from app import worker

    drained()  # start empty
    failures = worker._OrgCycleFailures("test.sweep")
    org_id = uuid4()
    try:
        raise RuntimeError("openbao: connection refused")
    except RuntimeError as exc:
        failures.contain(org_id, exc)

    captured = drained()
    assert len(captured) == 1
    assert captured[0].where == "worker"
    assert captured[0].exc_type == "RuntimeError"


def test_task_failure_signal_captures_the_exception(
    pg_cluster: PgCluster, tmp_path: Path
) -> None:
    """The OTHER worker capture seam: a task that raised all the way out
    (every `*_all_orgs` shell when EVERY org failed, or any task with no
    per-org containment at all) is about to be logged and marked failed by
    Celery's own machinery -- `task_failure` is what turns THAT into a
    capture too.

    Same behavioural-proof pattern as `test_worker_sweep_containment.py`'s
    `test_the_setup_logging_receiver_still_configures_the_worker_after_gc`:
    build the real Celery app, force a GC pass (a receiver connected without
    `weak=False` does not survive one), then fire the signal the way Celery
    itself does (`celery/app/trace.py::TraceInfo.handle_failure`) and assert
    the capture landed -- not merely that the source connects a receiver.
    """
    from celery.signals import task_failure as celery_task_failure

    from app import worker

    drained()  # start empty

    with override_settings(_settings(pg_cluster, tmp_path)):
        worker.create_celery()

    gc.collect()

    try:
        raise ValueError("boom: worker task failed")
    except ValueError as exc:
        celery_task_failure.send(
            sender=None,
            task_id="t-1",
            exception=exc,
            args=(),
            kwargs={},
            traceback=exc.__traceback__,
            einfo=None,
        )

    captured = drained()
    assert len(captured) == 1
    assert captured[0].where == "worker"
    assert captured[0].exc_type == "ValueError"


@pytest.fixture()
async def db_session(app_engine: AsyncEngine):  # noqa: ANN201
    async with AsyncSession(app_engine, expire_on_commit=False) as session:
        yield session


async def test_worker_drain_writes_the_capture_queue_to_postgres(
    pg_cluster: PgCluster, tmp_path: Path, db_session: AsyncSession
) -> None:
    """`app.worker.drain_platform_errors_all` is the worker-side half of the
    `platform-error-drain` beat entry -- it opens its own app-role engine
    (matching `sample_health_all`'s rationale: `platform_error_group`/
    `_event`'s RLS grants `app_user` only) and calls the same `drain_once`
    the API's background loop calls."""
    from sqlalchemy import select

    from app import worker
    from app.core.errors.capture import capture_exception
    from app.models import PlatformErrorGroup

    drained()  # start empty
    try:
        raise KeyError("boom: worker drain fixture")
    except KeyError as exc:
        captured = capture_exception(exc, where="worker")
    assert captured is not None

    with override_settings(_settings(pg_cluster, tmp_path)):
        written = await worker.drain_platform_errors_all()

    assert written == 1
    # BY FINGERPRINT, NOT BY `exc_type` -- same reason as
    # `test_error_drain_wiring.py`'s drain test. Every test file in this suite
    # shares one database, so selecting on a bare type name matches this
    # test's group row and every other file's, and `scalar_one()` raises
    # `MultipleResultsFound` as soon as another file that captures the same
    # exception type runs first. Under pytest's default RANDOM ordering --
    # which CI uses -- that is an order-dependent failure. The fingerprint is
    # this capture's own identity, so the assertion asks about this test's
    # row and nobody else's.
    group = (
        await db_session.execute(
            select(PlatformErrorGroup).where(
                PlatformErrorGroup.fingerprint == captured.fingerprint
            )
        )
    ).scalar_one()
    assert group.where_ == "worker"
