"""`ipam_sweep_dispatch_all_orgs`: one org's failure must not end the cycle.

The sweep had no `try` around its per-org call. `IpamError` is caught per SUBNET
inside `ipam_sweep_dispatch_org`, which contains a bad subnet and nothing else:
an unavailable signer (OpenBao down), a Redis publish failure, a database error
or an `UnknownJobType` propagates out of the org loop, and **every org after it
in enumeration order is not swept that hour**. On a fleet where org #2 of 300
has a broken row, 298 orgs stop being swept and the only symptom is one
traceback in the worker log.

Every sibling already has the guard and says why in its own docstring --
`health/sweep.py::_for_each_org`, `ipam/reconcile.py::reconcile_sweep_all_orgs`
(which quotes health's reasoning verbatim), `ai_admin/retention.py::
sweep_all_orgs`. This one copied the older unguarded
`patch_apply_sweep_all_orgs` shape instead.

WHY THE FAILING ORG IS THE FIRST ONE VISITED, RATHER THAN A NAMED ONE: the
`pg_cluster` database is shared session-wide and `select(Organization.id)` has
no ORDER BY, so "org A then org B" is not something a test can arrange here.
Raising on the FIRST call, whichever org that is, makes the assertion
order-independent and still falsifying -- without the guard the loop stops after
exactly one call, whatever the enumeration order turns out to be.
"""

import ast
import contextlib
import inspect
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings, override_settings
from app.jobs.registry import UnknownJobType
from app.models import Organization
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


@pytest.mark.parametrize(
    "boom",
    [
        # The signer is gone: OpenBao unreachable when `JobEngine.create` asks
        # for a signature. Not a subclass of anything the inner guard names.
        RuntimeError("openbao: connection refused"),
        # The job type was never registered in this process -- the failure mode
        # `register_worker_job_types` exists to prevent, raised from inside
        # `JobEngine.create` rather than from the subnet loop.
        UnknownJobType("net.sweep"),
        # A publish that fails after the tenant transaction committed.
        ConnectionError("redis: connection reset by peer"),
    ],
    ids=["signer-unavailable", "unknown-job-type", "redis-publish-failure"],
)
async def test_ipam_dispatch_one_orgs_failure_does_not_abort_the_cycle(
    admin_engine: AsyncEngine, pg_cluster: PgCluster, tmp_path: Path, boom: Exception
) -> None:
    """THE OBSERVABLE VALUE IS THE NUMBER OF ORGS VISITED. With the guard, the
    loop calls the per-org sweep once per org in the database; without it, the
    first raise ends the cycle and the count is 1 -- so this fails on a number,
    not on an exception type.

    Three exception classes, none of them `IpamError` and none of them a
    `SQLAlchemyError`, because the point of the guard is precisely the failures
    nobody enumerated in advance."""
    from app import worker

    await _make_org(admin_engine, "ipamguard-a")
    await _make_org(admin_engine, "ipamguard-b")
    org_total = await _org_count(admin_engine)
    assert org_total >= 2, "the fixture needs at least two orgs to say anything"

    calls: list[UUID] = []

    async def fake_dispatch_org(
        org_id: UUID, job_engine: Any, app_engine: Any, redis: Any, *, now: datetime
    ) -> int:
        calls.append(org_id)
        if len(calls) == 1:
            raise boom
        return 1

    # The escape is CAPTURED rather than allowed to fail the test, so the
    # assertion that fires is the one about how many orgs were swept -- the
    # value an operator would actually be missing -- and not "an exception was
    # raised", which says nothing about the blast radius.
    total = 0
    escaped: BaseException | None = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker, "ipam_sweep_dispatch_org", fake_dispatch_org)
        with override_settings(_settings(pg_cluster, tmp_path)):
            try:
                total = await worker.ipam_sweep_dispatch_all_orgs(NOW)
            except Exception as exc:  # noqa: BLE001 - this IS the defect under test
                escaped = exc

    assert len(calls) == org_total, (
        f"the cycle swept {len(calls)} of {org_total} orgs; {escaped!r} escaped the "
        f"org loop, so every org after it went un-swept this hour"
    )
    assert escaped is None, f"{escaped!r} escaped the org loop"
    assert total == org_total - 1, "the surviving orgs' dispatch counts were not summed"
    assert len(set(calls)) == len(calls), "an org was swept twice"


async def test_ipam_dispatch_logs_the_contained_failure_with_the_org_id(
    admin_engine: AsyncEngine,
    pg_cluster: PgCluster,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A swallowed exception with no log line is worse than the crash it
    replaced: the sweep reports a plausible total forever and one org is simply
    never swept again. The org id must be in the record, or the operator cannot
    tell which."""
    from app import worker

    doomed = await _make_org(admin_engine, "ipamlog")
    await _make_org(admin_engine, "ipamlog-other")

    async def fake_dispatch_org(
        org_id: UUID, job_engine: Any, app_engine: Any, redis: Any, *, now: datetime
    ) -> int:
        if org_id == doomed:
            raise RuntimeError("openbao: connection refused")
        return 0

    with caplog.at_level("ERROR", logger="its.worker"):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(worker, "ipam_sweep_dispatch_org", fake_dispatch_org)
            with override_settings(_settings(pg_cluster, tmp_path)):
                with contextlib.suppress(RuntimeError):
                    await worker.ipam_sweep_dispatch_all_orgs(NOW)

    records = [r for r in caplog.records if str(doomed) in r.getMessage()]
    assert records, "the contained failure was not logged with its org id"
    assert records[0].exc_info is not None, "logged without the traceback"


def test_ipam_dispatch_guard_is_the_shape_its_siblings_use() -> None:
    """`ipam_sweep_dispatch_all_orgs` copied the older unguarded
    `patch_apply_sweep_all_orgs` pattern. Pin the guard structurally so a future
    refactor that hoists the `try` outside the `for` -- which reads identically
    and contains nothing -- fails here."""
    from app import worker

    source = textwrap.dedent(inspect.getsource(worker.ipam_sweep_dispatch_all_orgs))
    fn = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef)
    )
    org_loop = next(
        loop
        for loop in ast.walk(fn)
        if isinstance(loop, ast.For)
        and isinstance(loop.target, ast.Name)
        and loop.target.id == "org_id"
    )
    assert any(isinstance(stmt, ast.Try) for stmt in org_loop.body), (
        "the per-org call is not inside a try INSIDE the org loop"
    )
