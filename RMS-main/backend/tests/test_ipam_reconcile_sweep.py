"""The daily reconcile sweep and its beat entry
(`docs/modules/ipam/spec.md` §4.4, §0.8, §11.1).

**THIS IS THE PRODUCTION PATH, NOT A STAND-IN FOR IT.** `evaluate_org` is tested
exhaustively in `tests/test_ipam_reconcile.py`; what is tested here is the thing
that actually runs on a timer -- the per-org loop, the transaction boundary, the
containment of one bad org, and the beat entry that causes any of it to happen at
all. A module whose predicates are perfect and whose beat entry is missing
reconciles nothing, and every unit test still passes.

WHAT THESE FIXTURES DELIBERATELY VARY:

* **Two orgs, each with a drift-producing fixture**, so "the sweep visited every
  org" is a count of TWO rather than the presence of one. A single-org fixture
  cannot tell a loop from a call.
* **The containment test makes the FIRST org fail**, so the observable is that
  the second org was still reconciled. Failing the last org would pass against
  an implementation with no containment at all.
* **The tenancy assertion runs on the admin engine** and compares per-org row
  counts, because through `org_session` RLS rejects first and a cross-org test
  driven that way passes with every `org_id` predicate in `reconcile.py`
  deleted.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import IpamDrift
from app.modules.ipam.reconcile import (
    RECONCILE_INTERVAL_SECONDS,
    reconcile_sweep_all_orgs,
)
from tests.ipam_fixtures import (
    seed_address,
    seed_agent,
    seed_device,
    seed_org,
    seed_subnet,
    seed_sweep_run,
    seed_vrf,
)

# `reconcile_sweep_all_orgs` walks EVERY organization in the database, and the
# test database is shared by the whole run, so these tests cost time in
# proportion to every org any earlier test created -- not to anything here. Run
# alone the slowest takes under a second; late in a full run the three-pass case
# crossed the project's 120 s default as the suite grew (2026-10-14). A longer
# ceiling keeps a hang red without making the suite's size a failure of IPAM.
pytestmark = pytest.mark.timeout(600)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
RECENT = NOW - timedelta(hours=2)


async def _absent_org(engine: AsyncEngine, tag: str, prefix: str = "10.70.0.0/24"):
    """An org holding exactly one `documented_absent`: an assigned address, a
    live claimant, and a successful sweep inside `W_obs` proving somebody
    looked."""
    org = await seed_org(engine, tag)
    vrf_id = await seed_vrf(engine, org.org_id)
    subnet_id = await seed_subnet(engine, org.org_id, vrf_id=vrf_id, prefix=prefix)
    device_id = await seed_device(engine, org.org_id)
    await seed_agent(engine, org.org_id, device_id, status="online", last_seen_at=RECENT)
    await seed_address(
        engine, org.org_id, vrf_id=vrf_id, subnet_id=subnet_id,
        address=str(prefix).rsplit(".", 1)[0] + ".42", device_id=device_id,
    )
    await seed_sweep_run(
        engine, org.org_id, subnet_id=subnet_id, state="succeeded", started_at=RECENT
    )
    return org


async def _drift_count(engine: AsyncEngine, org_id: UUID) -> int:
    async with AsyncSession(engine) as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(IpamDrift).where(
                        IpamDrift.org_id == org_id
                    )
                )
            ).scalar_one()
        )


async def test_the_sweep_reconciles_every_org_and_scopes_each_ones_findings(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """One `org_session` per org, orgs enumerated on the admin engine (§4.4).

    Both orgs hold an identical, independently-detectable finding, so the
    assertion is `1 and 1` rather than "at least one somewhere": *if the loop
    reconciled only the first org* the second observes 0, and *if org A's pass
    leaked across the tenant boundary* org B observes 2.
    """
    a = await _absent_org(admin_engine, "sw-a", "10.70.0.0/24")
    b = await _absent_org(admin_engine, "sw-b", "10.71.0.0/24")

    total = await reconcile_sweep_all_orgs(
        engine=app_engine, admin_engine=admin_engine, now=NOW
    )
    assert total >= 2
    assert await _drift_count(admin_engine, a.org_id) == 1
    assert await _drift_count(admin_engine, b.org_id) == 1

    async with AsyncSession(admin_engine) as session:
        rows = (
            (
                await session.execute(
                    select(IpamDrift).where(IpamDrift.org_id == a.org_id)
                )
            )
            .scalars()
            .all()
        )
    assert [(str(r.address), r.drift_class) for r in rows] == [
        ("10.70.0.42", "documented_absent")
    ]


async def test_the_sweep_commits_per_org_so_the_second_pass_publishes(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """§0.8's two-detection rule survives the process boundary.

    The counter only reaches 2 on the second sweep if the FIRST sweep's writes
    were committed. *If the per-org transaction were rolled back* -- or if the
    sweep held one transaction across orgs and lost it -- this observes
    `consecutive_detections == 1` on the second pass and an event that never
    fires, which is the silent-forever failure this module would otherwise ship.
    """
    from app.models import DomainEvent

    org = await _absent_org(admin_engine, "sw-commit", "10.72.0.0/24")

    await reconcile_sweep_all_orgs(engine=app_engine, admin_engine=admin_engine, now=NOW)
    await reconcile_sweep_all_orgs(
        engine=app_engine, admin_engine=admin_engine, now=NOW + timedelta(days=1)
    )

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(IpamDrift).where(IpamDrift.org_id == org.org_id)
            )
        ).scalar_one()
        events = (
            (
                await session.execute(
                    select(DomainEvent).where(
                        DomainEvent.org_id == org.org_id,
                        DomainEvent.event_type == "ipam.drift_detected",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert row.consecutive_detections == 2
    assert len(events) == 1


async def test_one_unreconcilable_org_does_not_skip_every_org_behind_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, monkeypatch
) -> None:
    """On a fleet where org #2 of 300 has a row that breaks evaluation, the
    alternative is that 298 orgs are never reconciled again and the only symptom
    is a log line.

    THE FAILING ORG IS THE FIRST ONE VISITED, so the observable is that the
    LATER org was still reconciled -- an implementation with no containment at
    all would pass a test that failed the last org.

    *If the `except` were removed* this test observes the raise escaping
    `reconcile_sweep_all_orgs` and the healthy org carrying zero drift rows.
    """
    from app.modules.ipam import reconcile as reconcile_module

    broken = await _absent_org(admin_engine, "sw-broken", "10.73.0.0/24")
    healthy = await _absent_org(admin_engine, "sw-healthy", "10.74.0.0/24")

    real = reconcile_module.evaluate_org
    seen: list[UUID] = []

    async def flaky(session, org_id, *, now=None):
        seen.append(org_id)
        if org_id == broken.org_id:
            raise RuntimeError("a row this org holds breaks evaluation")
        return await real(session, org_id, now=now)

    monkeypatch.setattr(reconcile_module, "evaluate_org", flaky)
    total = await reconcile_sweep_all_orgs(
        engine=app_engine, admin_engine=admin_engine, now=NOW
    )

    assert broken.org_id in seen and healthy.org_id in seen
    assert await _drift_count(admin_engine, broken.org_id) == 0
    assert await _drift_count(admin_engine, healthy.org_id) == 1
    assert total >= 1


def test_the_reconcile_beat_entry_is_registered(pg_cluster, tmp_path: Path) -> None:
    """The entry that causes any of this to run.

    The interval is read from the module rather than repeated as a literal: a
    test carrying its own copy of 86400.0 passes when the constant changes and
    the beat entry does not. *If the entry were missing* this observes a
    `KeyError` -- which is exactly what production would show as "the drift page
    never updates", with nothing failing anywhere.
    """
    from app.core.config import Settings, override_settings
    from app.worker import create_celery

    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
    )
    with override_settings(settings):
        schedule = create_celery().conf.beat_schedule

    entry = schedule["ipam-reconcile-sweep"]
    assert entry["task"] == "app.worker.ipam_reconcile_sweep"
    assert entry["schedule"] == RECONCILE_INTERVAL_SECONDS
    # `worker._with_expiry` gives EVERY beat entry an `options.expires` so an
    # overrunning sweep cannot backlog without bound. Asserted per-key rather
    # than as a whole-dict equality, which would break again the next time a
    # cross-cutting option is added; the property itself is pinned over the
    # whole schedule in tests/test_worker_beat_expiry.py.
    assert entry["options"]["expires"] == RECONCILE_INTERVAL_SECONDS
    assert RECONCILE_INTERVAL_SECONDS == 86400.0


async def test_the_sweep_is_a_no_op_on_an_org_with_nothing_to_reconcile(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """§0.11 at the sweep level: an org with a registered subnet, an assigned
    address and NO evidence source produces no rows at all, pass after pass.

    Asserted twice, because the failure mode worth catching is a sweep that
    writes a row the second time round -- a `dormant` bucket that fills up on
    day two would be indistinguishable from working until somebody opened the
    tab.
    """
    org = await seed_org(admin_engine, "sw-quiet")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    subnet_id = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.75.0.0/24"
    )
    device_id = await seed_device(admin_engine, org.org_id)
    await seed_agent(admin_engine, org.org_id, device_id, status="online",
                     last_seen_at=RECENT)
    await seed_address(admin_engine, org.org_id, vrf_id=vrf_id, subnet_id=subnet_id,
                       address="10.75.0.42", device_id=device_id)

    for stamp in (NOW, NOW + timedelta(days=1)):
        await reconcile_sweep_all_orgs(
            engine=app_engine, admin_engine=admin_engine, now=stamp
        )
        assert await _drift_count(admin_engine, org.org_id) == 0


async def test_the_sweep_runs_under_rls_not_on_the_admin_engine(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """§11.1: all service work runs inside `org_session`, on the RLS-constrained
    role. The sweep is handed the APP engine and only uses the admin engine to
    enumerate org ids.

    Proven by the writes succeeding at all: `ipam_drift` carries `FORCE ROW
    LEVEL SECURITY` with a `WITH CHECK` clause, so a row written under the wrong
    tenant GUC is refused by Postgres. *If the sweep wrote on the admin engine*
    this test still passes -- which is why it also asserts the app engine's
    non-BYPASSRLS role can READ back exactly the rows it wrote, through a tenant
    session, which an admin-engine write with a mismatched `org_id` could not
    satisfy.
    """
    from app.core.tenancy import org_session

    org = await _absent_org(admin_engine, "sw-rls", "10.76.0.0/24")
    await reconcile_sweep_all_orgs(engine=app_engine, admin_engine=admin_engine, now=NOW)

    async with org_session(org.org_id, engine=app_engine) as session:
        rows = (await session.execute(select(IpamDrift))).scalars().all()
    assert [(str(r.address), r.drift_class) for r in rows] == [
        ("10.76.0.42", "documented_absent")
    ]


@pytest.mark.parametrize("passes", [1, 2, 3])
async def test_the_sweep_is_idempotent_in_row_count(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, passes: int
) -> None:
    """§0.8's stable identity, at the sweep level: N passes produce ONE row,
    whatever N is. The parameter is the point -- a fixture that always ran the
    sweep once could not tell an upsert from an insert."""
    org = await _absent_org(admin_engine, f"sw-idem{passes}", "10.77.0.0/24")
    for i in range(passes):
        await reconcile_sweep_all_orgs(
            engine=app_engine, admin_engine=admin_engine, now=NOW + timedelta(days=i)
        )
    assert await _drift_count(admin_engine, org.org_id) == 1
