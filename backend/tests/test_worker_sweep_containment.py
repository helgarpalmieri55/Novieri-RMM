"""Every `*_all_orgs` sweep in `app/worker.py`: one org's failure must not end
the cycle -- and a cycle that failed for EVERY org must still be visible.

`3a1dece` fixed this for `ipam_sweep_dispatch_all_orgs` (see
`tests/test_worker_ipam_sweep_containment.py`) and reported that the other
shells in the file had the same shape. They did: thirteen of them, not twelve --
the security-log and remote-recording retention shells are two functions, not
one -- plus `deliver_pending_all_orgs`, which guarded but SILENTLY, with a bare
`continue` and no log line. A skip nobody can see is its own defect.

THE THREE SHAPES, and why one test each is enough:

  A. DELEGATING -- the loop body is one call to a per-org function that owns its
     own transaction (`sweep_all_orgs`, `drain_outbox_all_orgs`,
     `deliver_pending_all_orgs`, `patch_scan_sweep_all_orgs`,
     `retention_all_orgs`, `remote_recording_retention_all_orgs`,
     `ipam_sweep_dispatch_all_orgs`). The guard has to wrap one `await`.
  B. INLINE TRANSACTION + PUBLISH-AFTER-COMMIT -- the loop body opens
     `org_session` itself, then publishes signed jobs to Redis after it commits
     (`patch_apply`, `deploy_window`, `backup_window`,
     `provision_enroll_dispatch`). The guard has to span BOTH phases: a Redis
     failure after the commit is as fatal to the cycle as a database one.
  C. INLINE TRANSACTION, NO PUBLISH -- the loop body opens `org_session` and
     only reads/writes/deletes (`backup_retention_gc`, `backup_verification`,
     `provision_timeout`, `licenses_compliance`). These are the ones that also
     mutated the running total INSIDE the transaction, so containment alone
     would have started reporting work that rolled back as done.

WHY THE FAILING ORG IS THE FIRST ONE VISITED, RATHER THAN A NAMED ONE: the
`pg_cluster` database is shared session-wide and `select(Organization.id)` has
no ORDER BY, so "org A then org B" is not something a test can arrange here.
Raising on the FIRST call, whichever org that is, makes the assertion
order-independent and still falsifying -- without the guard the loop stops after
exactly one org, whatever the enumeration order turns out to be.
"""

import ast
import contextlib
import inspect
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings, override_settings
from app.core.tenancy import org_session
from app.jobs.registry import UnknownJobType
from app.models import LicenseComplianceSnapshot, Organization, User
from app.modules.licenses.reporting import compliance_summary, reported_clients
from app.modules.licenses.service import create_product
from tests.conftest import PgCluster

REDIS_URL = "redis://localhost:6379/9"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


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


async def _org_count(admin_engine: AsyncEngine) -> int:
    async with AsyncSession(admin_engine) as session:
        return len(list((await session.execute(select(Organization.id))).scalars()))


async def _make_org(admin_engine: AsyncEngine, tag: str) -> UUID:
    slug = f"{tag}-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


# ---------------------------------------------------------------------------
# Shape A: the delegating shells
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Delegating:
    """One shape-A shell: the sweep, the per-org callee to break, what a healthy
    org returns, and how to read the operator-visible number out of the result."""

    shell: str
    target_module: str  # where the callee is looked up at call time
    target_attr: str
    healthy: Any
    number: Callable[[Any], int]
    args: tuple[Any, ...] = ()
    #: not a `SQLAlchemyError` and not the module's own error base, on purpose:
    #: the point of the guard is the failures nobody enumerated in advance.
    boom: BaseException = RuntimeError("openbao: connection refused")


_DELEGATING_SHELLS = [
    _Delegating(
        shell="sweep_all_orgs",
        target_module="app.worker",
        target_attr="sweep_org",
        healthy=(1, 1),
        number=lambda result: int(result[0]),
        boom=ConnectionError("redis: connection reset by peer"),
    ),
    _Delegating(
        shell="drain_outbox_all_orgs",
        target_module="app.worker",
        target_attr="drain_outbox",
        healthy=1,
        number=int,
        boom=ValueError("routing rule config is not a mapping"),
    ),
    _Delegating(
        shell="deliver_pending_all_orgs",
        target_module="app.worker",
        target_attr="deliver_pending",
        healthy=(1, 0),
        number=lambda result: int(result[0]),
        boom=RuntimeError("openbao: connection refused"),
    ),
    _Delegating(
        shell="patch_scan_sweep_all_orgs",
        target_module="app.worker",
        target_attr="patch_scan_sweep_org",
        healthy=1,
        number=int,
        args=(NOW,),
        boom=UnknownJobType("patch.scan"),
    ),
    _Delegating(
        # imported INSIDE the function, so the name that has to be patched is
        # the module's, not the worker's.
        shell="retention_all_orgs",
        target_module="app.modules.security_logs.retention",
        target_attr="apply_retention",
        healthy=1,
        number=int,
        boom=KeyError("retention_days"),
    ),
]


@pytest.mark.parametrize("case", _DELEGATING_SHELLS, ids=lambda c: c.shell)
async def test_delegating_shell_one_orgs_failure_does_not_abort_the_cycle(
    admin_engine: AsyncEngine, pg_cluster: PgCluster, tmp_path: Path, case: _Delegating
) -> None:
    """THE OBSERVABLE VALUE IS THE NUMBER OF ORGS VISITED. With the guard, the
    loop calls the per-org function once per org in the database; without it,
    the first raise ends the cycle and the count is 1 -- so this fails on a
    number, not on an exception type."""
    import importlib

    from app import worker

    await _make_org(admin_engine, "guard-a")
    await _make_org(admin_engine, "guard-b")
    org_total = await _org_count(admin_engine)
    assert org_total >= 2, "the fixture needs at least two orgs to say anything"

    calls: list[UUID] = []

    async def fake(org_id: UUID, *args: Any, **kwargs: Any) -> Any:
        calls.append(org_id)
        if len(calls) == 1:
            raise case.boom
        return case.healthy

    # The escape is CAPTURED rather than allowed to fail the test, so the
    # assertion that fires is the one about how many orgs were swept -- the work
    # an operator would actually be missing -- and not "an exception was
    # raised", which says nothing about the blast radius.
    result: Any = None
    escaped: BaseException | None = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            importlib.import_module(case.target_module), case.target_attr, fake, raising=True
        )
        with override_settings(_settings(pg_cluster, tmp_path)):
            try:
                result = await getattr(worker, case.shell)(*case.args)
            except Exception as exc:  # noqa: BLE001 - this IS the defect under test
                escaped = exc

    assert len(calls) == org_total, (
        f"{case.shell} swept {len(calls)} of {org_total} orgs; {escaped!r} escaped the "
        f"org loop, so every org after it went un-swept this cycle"
    )
    assert escaped is None, f"{escaped!r} escaped the org loop"
    assert case.number(result) == org_total - 1, (
        "the surviving orgs' counts were not summed into the returned total"
    )
    assert len(set(calls)) == len(calls), "an org was swept twice"


# ---------------------------------------------------------------------------
# Shape B: inline transaction + publish-after-commit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shell",
    [
        "patch_apply_sweep_all_orgs",
        "deploy_window_sweep_all_orgs",
        "provision_enroll_dispatch_sweep_all_orgs",
    ],
)
async def test_inline_transaction_shell_one_orgs_failure_does_not_abort_the_cycle(
    admin_engine: AsyncEngine, pg_cluster: PgCluster, tmp_path: Path, shell: str
) -> None:
    """These shells open the tenant transaction themselves, so the failure that
    has to be contained is the one `org_session` itself raises -- a connection
    the pool cannot hand out, a `SET LOCAL` the tenant role is refused. Every
    healthy org runs the REAL `org_session` and the real body underneath it.

    The operator-visible number here is how many orgs got a tenant transaction
    at all, i.e. how many were swept; without the guard it is 1.

    `backup_window_sweep_all_orgs` is the fourth shell of this shape and is
    covered structurally below -- it additionally builds an `ObjectStore`, which
    would need `mock_aws` to say nothing new.
    """
    from app import worker

    await _make_org(admin_engine, "txnguard-a")
    await _make_org(admin_engine, "txnguard-b")
    org_total = await _org_count(admin_engine)
    assert org_total >= 2

    visited: list[UUID] = []
    # The real one, by its own name: `worker.org_session` is the same object,
    # but reading it back off the module after monkeypatching would be circular.
    real_org_session = org_session

    def fake_org_session(org_id: UUID, *, clients: ClientScope, engine: AsyncEngine) -> Any:
        visited.append(org_id)
        if len(visited) == 1:
            raise ConnectionError("asyncpg: connection was closed in the middle of operation")
        return real_org_session(org_id, clients=clients, engine=engine)

    escaped: BaseException | None = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker, "org_session", fake_org_session)
        with override_settings(_settings(pg_cluster, tmp_path)):
            try:
                await getattr(worker, shell)(NOW)
            except Exception as exc:  # noqa: BLE001 - this IS the defect under test
                escaped = exc

    assert len(visited) == org_total, (
        f"{shell} swept {len(visited)} of {org_total} orgs; {escaped!r} escaped the org "
        f"loop, so every org after it went un-swept this cycle"
    )
    assert escaped is None, f"{escaped!r} escaped the org loop"


# ---------------------------------------------------------------------------
# Shape C: inline transaction, no publish -- and the count must be the COMMITTED
# count, not the attempted one
# ---------------------------------------------------------------------------


async def _org_with_product(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> UUID:
    """An org with exactly one `SoftwareProduct`, so one healthy sweep of it
    writes exactly one `license_compliance_snapshot` row."""
    slug = f"cmplguard-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        session.add(user)
        await session.commit()
        org_id, user_id = org.id, user.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await create_product(
            session,
            org_id=org_id,
            actor_id=user_id,
            name=f"Product {slug}",
            publisher="Acme Inc",
            match_rules=[{"type": "exact", "pattern": f"Product {slug}"}],
        )
        await session.commit()
    return org_id


async def _snapshot_count(
    app_engine: AsyncEngine, org_id: UUID, stamp: datetime = NOW
) -> int:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        rows = (
            await session.execute(
                select(LicenseComplianceSnapshot.id).where(
                    LicenseComplianceSnapshot.org_id == org_id,
                    LicenseComplianceSnapshot.computed_at == stamp,
                )
            )
        ).scalars().all()
    return len(rows)


async def _fleet_snapshot_count(admin_engine: AsyncEngine, stamp: datetime) -> int:
    """Every org's snapshots at one `computed_at`, on the RLS-bypassing engine --
    the number the sweep's return value claims to be."""
    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(
                select(LicenseComplianceSnapshot.id).where(
                    LicenseComplianceSnapshot.computed_at == stamp
                )
            )
        ).scalars().all()
    return len(rows)


async def test_licenses_compliance_one_orgs_failure_does_not_abort_the_cycle(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster: PgCluster, tmp_path: Path
) -> None:
    """Shape C, with real rows: two orgs each holding one product, the first org
    whose position the sweep computes has its `compliance_summary` blow up, and
    the other org's snapshot must still be ON DISK afterwards.

    A compliance series is `computed_at`-stamped, so a day an org was skipped is
    a permanent hole -- the next run does not backfill it. That makes the row
    count, not the return value, the thing worth asserting."""
    from app import worker

    seeded = [
        await _org_with_product(admin_engine, app_engine),
        await _org_with_product(admin_engine, app_engine),
    ]
    org_total = await _org_count(admin_engine)

    real_summary = compliance_summary
    real_clients = reported_clients
    visited: list[UUID] = []
    failed: list[UUID] = []

    # Per client since plan 2026-10-26 Task 6: the sweep asks each org for the
    # clients it reports on (once per org -- that is the visit), then computes
    # each client's position. An org with no active client has no position to
    # record and is still visited. The first org whose position is computed
    # fails there, at its first client.
    async def fake_clients(session: Any, org_id: UUID) -> Any:
        visited.append(org_id)
        return await real_clients(session, org_id)

    async def fake_summary(session: Any, org_id: UUID, *, client_id: UUID) -> Any:
        if not failed:
            failed.append(org_id)
        if org_id == failed[0]:
            raise ConnectionError("asyncpg: server closed the connection unexpectedly")
        return await real_summary(session, org_id, client_id=client_id)

    escaped: BaseException | None = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker, "reported_clients", fake_clients)
        mp.setattr(worker, "compliance_summary", fake_summary)
        with override_settings(_settings(pg_cluster, tmp_path)):
            try:
                await worker.licenses_compliance_sweep_all_orgs(NOW)
            except Exception as exc:  # noqa: BLE001 - this IS the defect under test
                escaped = exc

    assert len(visited) == org_total, (
        f"the cycle swept {len(visited)} of {org_total} orgs; {escaped!r} escaped the org "
        f"loop, so every org after it has no compliance position recorded for this day"
    )
    assert escaped is None, f"{escaped!r} escaped the org loop"
    assert failed, "no org's position was computed, so no failure was injected"

    doomed = failed[0]
    for org_id in seeded:
        written = await _snapshot_count(app_engine, org_id)
        if org_id == doomed:
            assert written == 0, "the failed org committed a snapshot anyway"
        else:
            assert written == 1, (
                f"org {org_id} holds one product but got {written} snapshots; its cycle "
                f"was skipped by the failure in org {doomed}"
            )


async def test_licenses_compliance_total_counts_only_committed_snapshots(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster: PgCluster, tmp_path: Path
) -> None:
    """The count used to be added to the running total INSIDE the org's
    transaction. That was invisible while an org could not fail part-way; with
    containment it would mean an org whose transaction ROLLED BACK still
    reported its snapshots as written. Fail at the commit boundary -- after the
    rows are staged and flushed -- and the total must not count them.

    THE RETURNED TOTAL IS CHECKED AGAINST THE ROWS THAT EXIST, not against a
    literal: `pg_cluster` is session-scoped and other test files leave their own
    orgs and products behind, so the only honest statement is "the number the
    sweep reported is the number of snapshots on disk". Its own `computed_at`
    stamp keeps that count to this one cycle."""
    from app import worker

    stamp = NOW + timedelta(days=1)
    org_id = await _org_with_product(admin_engine, app_engine)
    real_org_session = org_session

    class _FailingCommit:
        """The real `org_session`, but the exit -- the commit -- fails.

        The failure is pushed INTO the real context manager rather than raised
        after it, so `session.begin()` rolls the transaction back exactly as a
        genuine commit failure would; a raise layered on top of a successful
        commit would leave the rows durable and prove nothing."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        async def __aenter__(self) -> Any:
            return await self._inner.__aenter__()

        async def __aexit__(self, *exc: Any) -> bool:
            boom = ConnectionError("asyncpg: connection lost at COMMIT")
            await self._inner.__aexit__(type(boom), boom, boom.__traceback__)
            raise boom

    def fake_org_session(target: UUID, *, clients: ClientScope, engine: AsyncEngine) -> Any:
        inner = real_org_session(target, clients=clients, engine=engine)
        return _FailingCommit(inner) if target == org_id else inner

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker, "org_session", fake_org_session)
        with override_settings(_settings(pg_cluster, tmp_path)):
            total = await worker.licenses_compliance_sweep_all_orgs(stamp)

    written = await _snapshot_count(app_engine, org_id, stamp)
    assert written == 0, "the aborted transaction left rows behind"
    on_disk = await _fleet_snapshot_count(admin_engine, stamp)
    assert total == on_disk, (
        f"the sweep reported {total} snapshots written but {on_disk} exist; the org that "
        f"failed at COMMIT had its staged rows counted anyway"
    )


# ---------------------------------------------------------------------------
# What the operator sees
# ---------------------------------------------------------------------------


async def test_every_contained_failure_names_its_org_and_its_exception(
    admin_engine: AsyncEngine,
    pg_cluster: PgCluster,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A contained failure that logs nothing is a silent skip -- the sweep
    reports a plausible total forever and one org is simply never swept again.
    Every skipped org gets its own ERROR line naming the org and the
    exception.

    UPDATED, logging fix wave 5 (I-4): this used to assert the FIRST failure
    of a cycle carries `exc_info` and the rest do not -- deliberately, to
    save a duplicate traceback for what is almost certainly one shared cause.
    `_ContainmentSink.contain`'s second-and-later branch got there by passing
    the exception as a `%r` argument instead, which the logging-discipline
    AST gate now flags: `%r` renders `repr(exc)` (including `exc.args`) into
    the message text, the same credential-leak shape as `str(exc)` -- see
    `tests/test_logging_discipline.py`. The fix is `exc_info=exc` on EVERY
    branch, uniformly.

    UPDATED AGAIN, logging fix wave 6 (C-1): the claim above that no
    formatter this project ships to production ever rendered the "duplicate"
    traceback text was FALSE for two of this project's three processes.
    `JsonFormatter` (`app/core/logging.py`) never calls `formatException` and
    only ever reads `exc_info[0].__name__` for `exc_type` -- but that is a
    property of `JsonFormatter`, not of `exc_info=`, and `configure_logging()`
    was reachable from exactly one of the three processes that log
    (`app/main.py`). This worker process is a second: before C-1 wired
    `create_celery()` to Celery's `setup_logging` signal, this logger ran
    under Celery's own installed `Formatter`, which -- like `basicConfig`,
    the stdlib default -- DOES call `formatException` and prints str(exc) in
    the tail. `exc_info=exc` was safe here only once that wiring existed, not
    from the day fix wave 5 shipped it. Every skipped org's record now
    carries `exc_info`, and this process is now one of the ones actually
    configured to render only `exc_type` from it.

    NARROWED, logging fix wave 7 (C-1): "this process is now one of the ones
    actually configured to render only `exc_type`" was true of every record
    THIS test drives -- `contain`'s own `logger.error(..., exc_info=exc)`
    calls -- but installing `JsonFormatter` on this process's root logger
    also routes Celery's OWN task-failure record
    (`celery.app.trace.TraceInfo._log_error`) through the same handler, and
    that record carries a full traceback, `repr(exc)` and the task's raw
    args/kwargs under field names redaction-by-name does not flag. This test
    says nothing about that record -- it never fires one -- see
    `tests/test_logging_kernel.py::
    test_celery_task_failure_record_is_sanitised_by_the_filter` for the
    coverage that closes it, via a second, separate defence
    (`_CeleryTraceSanitiser`, `app/core/logging.py`)."""
    from app import worker

    doomed_a = await _make_org(admin_engine, "logguard-a")
    doomed_b = await _make_org(admin_engine, "logguard-b")
    await _make_org(admin_engine, "logguard-ok")

    async def fake(org_id: UUID, *args: Any, **kwargs: Any) -> int:
        if org_id in (doomed_a, doomed_b):
            raise RuntimeError("openbao: connection refused")
        return 0

    with caplog.at_level("ERROR", logger="its.worker"):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(worker, "patch_scan_sweep_org", fake)
            with override_settings(_settings(pg_cluster, tmp_path)):
                with contextlib.suppress(RuntimeError):
                    await worker.patch_scan_sweep_all_orgs(NOW)

    for doomed in (doomed_a, doomed_b):
        named = [r for r in caplog.records if str(doomed) in r.getMessage()]
        assert named, f"org {doomed} was skipped with no log line naming it"
        record = named[0]
        assert "patch.scan" in record.getMessage(), "the log line does not name the sweep"
        assert record.exc_info is not None, "the log line does not carry the exception"
        assert str(record.exc_info[1]) == "openbao: connection refused", (
            "the record's exc_info must carry THIS org's exception, not a shared "
            "or stale reference"
        )

    # M-12: after fix wave 6 collapsed `contain`'s if/else into one
    # unconditional call, "every skipped org carries exc_info" is no longer a
    # behaviour the two branches could disagree on -- there is only one
    # branch. `len(with_exc_info) == 2` stopped testing that distinction and
    # started restating what the per-org loop above already proved. What is
    # still worth guarding: that the collapse emits exactly one ERROR line
    # per skipped org -- neither a dropped line nor a duplicate from a bad
    # merge of the two former branches.
    patch_scan_errors = [r for r in caplog.records if "patch.scan" in r.getMessage()]
    assert len(patch_scan_errors) == 2, (
        f"expected exactly one ERROR line per skipped org (2 total), got "
        f"{len(patch_scan_errors)} -- collapsing contain()'s branches must not "
        "change how many lines are emitted"
    )


async def test_a_sweep_that_fails_for_every_org_still_fails_loudly(
    admin_engine: AsyncEngine, pg_cluster: PgCluster, tmp_path: Path
) -> None:
    """CONTAINMENT MUST NOT HIDE A GLOBALLY BROKEN SWEEP. If the signer is gone
    or Redis is unreachable, every org fails; returning a plausible `0` and a
    green Celery task would replace a loud crash with a silent one. When the
    whole cycle failed, the first exception is re-raised once the loop is done
    -- which is exactly what these shells did before containment, so nothing an
    operator relies on for a total outage has changed."""
    from app import worker

    await _make_org(admin_engine, "allfail-a")
    await _make_org(admin_engine, "allfail-b")

    attempts: list[UUID] = []

    async def always_fails(org_id: UUID, *args: Any, **kwargs: Any) -> int:
        attempts.append(org_id)
        raise RuntimeError(f"openbao: connection refused ({len(attempts)})")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker, "patch_scan_sweep_org", always_fails)
        with override_settings(_settings(pg_cluster, tmp_path)):
            with pytest.raises(RuntimeError) as caught:
                await worker.patch_scan_sweep_all_orgs(NOW)

    assert len(attempts) == await _org_count(admin_engine), (
        "the cycle gave up early; every org must still be attempted before it raises"
    )
    assert "(1)" in str(caught.value), (
        "the LAST failure was raised, not the first; the first is the one closest to the "
        "root cause"
    )


async def test_one_orgs_failure_does_not_make_the_sweep_look_broken(
    admin_engine: AsyncEngine, pg_cluster: PgCluster, tmp_path: Path
) -> None:
    """The converse of the test above: a PARTIAL failure must not raise, or the
    containment buys nothing -- the Celery task would still go red and the beat
    would still look like it stopped working."""
    from app import worker

    doomed = await _make_org(admin_engine, "partial-a")
    await _make_org(admin_engine, "partial-b")

    async def fake(org_id: UUID, *args: Any, **kwargs: Any) -> int:
        if org_id == doomed:
            raise RuntimeError("openbao: connection refused")
        return 0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker, "patch_scan_sweep_org", fake)
        with override_settings(_settings(pg_cluster, tmp_path)):
            assert await worker.patch_scan_sweep_all_orgs(NOW) == 0


# ---------------------------------------------------------------------------
# Structural: no shell gets to be added back without the guard
# ---------------------------------------------------------------------------

#: Every `*_all_orgs` in `app/worker.py` that enumerates orgs and loops over
#: them ITSELF. The shells not listed here delegate their org loop to a module
#: that already contains per-org failures (`health/sweep.py::_for_each_org`,
#: `ipam/reconcile.py`, `ipam/retention.py`, `ai_admin/retention.py`,
#: `sla/sweep.py`, `email_intake/poller.py`) and have no `for org_id` of their
#: own, which is why the discovery below finds exactly this set.
_GUARDED_SHELLS = {
    "sweep_all_orgs",
    "drain_outbox_all_orgs",
    "deliver_pending_all_orgs",
    "retention_all_orgs",
    "remote_recording_retention_all_orgs",
    "patch_scan_sweep_all_orgs",
    "ipam_sweep_dispatch_all_orgs",
    "patch_apply_sweep_all_orgs",
    "deploy_window_sweep_all_orgs",
    "backup_window_sweep_all_orgs",
    "backup_retention_gc_all_orgs",
    "backup_verification_sweep_all_orgs",
    "backup_reconcile_all_orgs",
    "backup_replication_all_orgs",
    "backup_offsite_test_all_orgs",
    "backup_offsite_gc_all_orgs",
    "backup_test_restore_all_orgs",
    "backup_health_all_orgs",
    "provision_enroll_dispatch_sweep_all_orgs",
    "provision_timeout_sweep_all_orgs",
    "licenses_compliance_sweep_all_orgs",
    # Patch restarts and scheduled scripts (2026-09-23): both dispatch signed
    # jobs per org, so one org's failure must not skip the rest.
    "patch_reboot_sweep_all_orgs",
    "script_policy_sweep_all_orgs",
    # Push runs (design 2026-09-30 §1.4): the pacer's backstop signs install
    # jobs per org and decrypts a credential for each, so one org's failure
    # (an OpenBao hiccup, a signer outage) must not skip every other org's run.
    # Contained per RUN since Task 12's review (`_NARROWER_UNIT`).
    "push_run_tick_all_orgs",
    # docs/modules/ai-automation.md §9.1 names this sweep specifically: "a
    # failure in one org must not abort the others -- the pattern the worker
    # already had to be corrected into". A stale AI action preview that never
    # expires is a confirmation token that outlived the situation justifying it.
    "ai_action_expiry_all_orgs",
    # The device-group re-evaluation beat. It belongs here more than most:
    # `evaluate_rules` is the one function in this codebase whose missing
    # tenant predicate would cross-ASSIGN devices between tenants rather than
    # merely disclose them, so an uncontained failure part-way through the org
    # list would leave the fleet half re-homed with no record of where it
    # stopped. The guard is inside its `for org_id` loop, which is what the
    # assertion below actually checks.
    "device_group_reevaluate_all_orgs",
    # The shared shell behind the scheduled `inventory.collect` and `logs.ship`
    # sweeps (`inventory_collect_sweep_all_orgs`, `logs_ship_sweep_all_orgs`).
    "_job_sweep_all_orgs",
    "platform_health_notify_all_orgs",
    "subscription_alerts_all_orgs",
    # The install link's two beats (spec 2026-09-28 §1.1, §1.5): recording an
    # automatic switch-off, and completing rejections (revoking certificates).
    # One org's failure must not leave every later org's rejected machine
    # holding a live certificate.
    "install_link_auto_off_all_orgs",
    "review_rejections_all_orgs",
}


#: Shells whose unit of containment is NARROWER than an org, by the name of
#: the loop variable that unit is iterated under. The push-run beat contains
#: each RUN (MSP clients plan 2026-10-26, Task 12 review): with one active run
#: per client, one client's failing run must not starve another client's run
#: in the same org, so the guard sits inside the per-run loop and that is the
#: loop checked below.
_NARROWER_UNIT = {"push_run_tick_all_orgs": "run"}


def _org_loops() -> dict[str, ast.For]:
    """Every worker function with a `for org_id in ...` loop -- or, for a
    shell in `_NARROWER_UNIT`, its per-unit loop -- by name."""
    from app import worker

    source = textwrap.dedent(inspect.getsource(worker))
    found: dict[str, ast.For] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        unit = _NARROWER_UNIT.get(node.name, "org_id")
        for loop in ast.walk(node):
            if (
                isinstance(loop, ast.For)
                and isinstance(loop.target, ast.Name)
                and loop.target.id == unit
            ):
                found[node.name] = loop
    return found


def test_every_shell_with_its_own_org_loop_guards_inside_that_loop() -> None:
    """Pin the guard structurally, for all seventeen at once. A `try` HOISTED
    OUTSIDE the `for` reads almost identically and contains nothing, and an
    eighteenth shell added later with the old shape would otherwise ship
    unnoticed."""
    loops = _org_loops()
    assert set(loops) == _GUARDED_SHELLS, (
        "the set of worker shells that run their own org loop changed; a new one must "
        "carry the guard (and be listed here), and a removed one must be delisted"
    )
    unguarded = [
        name
        for name, loop in loops.items()
        if not any(isinstance(stmt, ast.Try) for stmt in loop.body)
    ]
    assert not unguarded, (
        f"{unguarded} call their per-org body outside any `try` INSIDE the org loop; one "
        f"org's failure skips every org enumerated after it"
    )


def test_every_shell_reports_a_cycle_that_failed_for_every_org() -> None:
    """The other half of the contract: `raise_if_total` is what stops a globally
    broken sweep from returning a plausible zero forever."""
    from app import worker

    missing = [
        name
        for name in _GUARDED_SHELLS
        if "raise_if_total" not in inspect.getsource(getattr(worker, name))
    ]
    assert not missing, (
        f"{missing} contain every org's failure but never report a cycle in which EVERY "
        f"org failed; a dead signer would look like an idle fleet"
    )


# ---------------------------------------------------------------------------
# I-3/I-4/M-6: the mechanism the worker's own logging fix depends on
# ---------------------------------------------------------------------------


def test_the_setup_logging_receiver_still_configures_the_worker_after_gc(
    pg_cluster: PgCluster, tmp_path: Path
) -> None:
    """I-3: `tests/test_logging_kernel.py::
    test_configure_logging_is_wired_into_every_process_entrypoint` only
    proves `create_celery()`'s SOURCE calls `configure_logging` -- it parses
    `app/worker.py` with `ast` and never runs it, so it cannot see that the
    call only happens inside a receiver connected with `weak=False`, nor that
    the connection depends on that flag outliving `create_celery()`'s own
    stack frame. Delete `weak=False` and this receiver is exactly as
    reachable by source inspection and precisely as dead at runtime: celery's
    default WEAK reference to a signal receiver is collected the instant
    `create_celery()` returns and nothing else holds a strong reference to
    the closure -- verified below -- so the worker would silently revert to
    Celery's own formatter (str(exc) in the traceback tail, the leak C-1
    exists to close) while the AST check kept passing and the rest of this
    file's suite stayed green. This is the same "green suite, uncovered
    process" shape that let the last Critical live for a whole phase (see
    `docs/superpowers/specs/2026-08-14-logging-kernel-design.md` §0) -- so
    this is reproduced BEHAVIOURALLY, not by reading source.

    Build the real Celery app, force a GC pass (a closure without
    `weak=False` does not survive one), then fire `setup_logging` the way
    `celery.app.log.Logging.setup_logging_subsystem` actually does, and
    assert the root handler carries `JsonFormatter` afterward."""
    import gc
    import logging

    from celery.signals import setup_logging as celery_setup_logging

    from app import worker
    from app.core.logging import JsonFormatter

    with override_settings(_settings(pg_cluster, tmp_path)):
        worker.create_celery()

    gc.collect()
    root = logging.getLogger()
    # F-8: SAVE, not just clear -- this test's `finally` used to hardcode
    # `root.handlers[:] = []` and `root.setLevel(logging.WARNING)`, which
    # throws away whatever state the root logger had BEFORE this test ran
    # (another test's handler, a different level) and replaces it with a
    # fixed guess. That guess happened to match pytest's usual baseline, so
    # the bug was invisible -- until a test ran after this one expecting its
    # OWN prior root state (e.g. a caplog-installed handler) and silently
    # got a bare, handler-less root instead. Restoring exactly what was here
    # before is correct regardless of what that turns out to be.
    prev_root_handlers = root.handlers[:]
    prev_root_level = root.level
    trace_logger = logging.getLogger("celery.app.trace")
    prev_trace_handlers = trace_logger.handlers[:]
    prev_trace_propagate = trace_logger.propagate
    root.handlers[:] = []
    try:
        celery_setup_logging.send(sender=None, loglevel=None)
        assert len(root.handlers) == 1, (
            "setup_logging never reached configure_logging -- the receiver did not fire"
        )
        assert isinstance(root.handlers[0].formatter, JsonFormatter), (
            "the worker's root handler does not carry JsonFormatter after a GC pass -- "
            "the setup_logging receiver was garbage-collected"
        )
    finally:
        root.handlers[:] = prev_root_handlers
        root.setLevel(prev_root_level)
        trace_logger.handlers[:] = prev_trace_handlers
        trace_logger.propagate = prev_trace_propagate


def test_the_setup_logging_receiver_forwards_loglevel(
    pg_cluster: PgCluster, tmp_path: Path
) -> None:
    """I-4: `configure_logging()` used to be called with no arguments, so the
    `loglevel` Celery's `setup_logging_subsystem` sends on this signal (itself
    resolved from `--loglevel`, `deploy/docker-compose.yml`'s
    `--loglevel=info`) was silently discarded -- changing that flag to `debug`
    during an incident did nothing, with no warning. The signal sends an
    already-numeric level (`celery.utils.log.mlevel`), not a string."""
    import logging

    from celery.signals import setup_logging as celery_setup_logging

    from app import worker

    with override_settings(_settings(pg_cluster, tmp_path)):
        worker.create_celery()

    root = logging.getLogger()
    # F-8: SAVE, not just clear -- see the twin test above for why hardcoding
    # "empty and WARNING" in `finally` is itself a bug, not a cleanup.
    prev_root_handlers = root.handlers[:]
    prev_root_level = root.level
    trace_logger = logging.getLogger("celery.app.trace")
    prev_trace_handlers = trace_logger.handlers[:]
    prev_trace_propagate = trace_logger.propagate
    root.handlers[:] = []
    try:
        celery_setup_logging.send(sender=None, loglevel=logging.DEBUG)
        assert root.level == logging.DEBUG, (
            "the worker's root logger did not pick up the loglevel the setup_logging "
            "signal carried"
        )
    finally:
        root.handlers[:] = prev_root_handlers
        root.setLevel(prev_root_level)
        trace_logger.handlers[:] = prev_trace_handlers
        trace_logger.propagate = prev_trace_propagate


def test_create_celery_does_not_leak_a_receiver_per_call(
    pg_cluster: PgCluster, tmp_path: Path
) -> None:
    """M-6: every call to `create_celery()` used to leave a NEW strong
    reference connected to Celery's `setup_logging` signal forever (celery
    signals are never disconnected automatically) -- one call per process in
    production, but many per test session, each from a different test module
    that happens to call `create_celery()`. Each firing does
    `root.handlers[:] = [h]`, so a signal firing mid-suite would evict
    pytest's `caplog` handler once per leaked receiver. `dispatch_uid` makes
    a repeat `.connect()` with the same id a no-op; this asserts the
    receiver COUNT stays at exactly one no matter how many times this
    process calls `create_celery()`."""
    from celery.signals import setup_logging as celery_setup_logging

    from app import worker

    settings = _settings(pg_cluster, tmp_path)
    with override_settings(settings):
        worker.create_celery()
        worker.create_celery()
        worker.create_celery()

    matching = [
        r
        for r in celery_setup_logging.receivers
        if r[0][0] == "app.worker.configure_worker_logging"
    ]
    assert len(matching) == 1, (
        f"expected exactly one connected receiver for app.worker's setup_logging "
        f"wiring after three calls to create_celery(), found {len(matching)} -- the "
        "connection is not idempotent"
    )
