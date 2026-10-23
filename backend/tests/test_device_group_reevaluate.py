"""The membership re-evaluation sweep -- cycle 2 §5.3, §8.2, §8.3.

WHY A SWEEP AT ALL, since the ingest path already evaluates rules. Because
NOTHING SCHEDULES AN INGEST. `inventory.collect` is registered, directly
dispatchable and gated, and no worker beat dispatches it; the agent's scheduler
wheel registers no inventory task; `DEFAULT_SWEEP_POLICY = {"interval_hours":
24}` is seeded into `policies` and read by nothing. So "it takes effect on the
next ingest" means "whenever a human next presses collect", which for an
unattended fleet is never -- and a rules editor whose group stays at zero
devices with no explanation is worse than no rules editor.

WHY NOT IMMEDIATELY, INSIDE THE WRITE. One `POST` would re-read every device's
latest snapshot, re-evaluate, and move an arbitrary number of machines onto
different patch, backup and remote policy inside one HTTP transaction. On a
5,000-device org that is a fleet-wide policy change delivered by a form submit.
The sweep costs a window -- bounded by the beat interval -- in which the console
shows a membership one write behind. Every consumer resolves policy AT USE, so
that window is a display lag rather than a wrong action, with the single
exception of health, which the sweep reconciles itself.

THE TENANCY TEST HERE IS THE ONE THAT MATTERS IN THIS WHOLE CYCLE. The sweep is
the second caller of `evaluate_rules`, and it runs in the worker, where four
sibling sweeps hold a BYPASSRLS admin engine. `docs/superpowers/audits/
2026-08-21-tenancy-test-efficacy.md` §0: everything served through `org_session`
runs as `app_user` (NOBYPASSRLS), so a missing `org_id` predicate turns no
`org_session`-driven test red. The test below drives the sweep body on the ADMIN
engine, where the predicate is the only thing between one tenant's rule and
another tenant's device.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    Device,
    DeviceGroup,
    InventorySnapshot,
    Organization,
)
from app.modules.inventory.rules import evaluate_rules
from app.modules.inventory.schema import InventoryDocument
from app.services.device_groups import (
    MOVES_RECORDED,
    reevaluate_org_membership,
)
from tests.test_inventory_schema import FULL_DOC

WINDOWS = {"all": [{"fact": "os_family", "op": "eq", "value": "windows"}]}
LINUX = {"all": [{"fact": "os_family", "op": "eq", "value": "linux"}]}


async def _org(admin_engine: AsyncEngine, slug: str = "dgs") -> UUID:
    name = f"{slug}-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=name, slug=name)
        session.add(org)
        await session.commit()
        return org.id


async def _group(
    admin_engine: AsyncEngine,
    org_id: UUID,
    name: str,
    rules: dict[str, Any] | None,
    *,
    precedence: int = 0,
) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        group = DeviceGroup(
            org_id=org_id, name=name, rules=rules, precedence=precedence
        )
        session.add(group)
        await session.commit()
        return group.id


async def _device(
    admin_engine: AsyncEngine,
    org_id: UUID,
    hostname: str,
    *,
    doc: dict[str, Any] | None = None,
    group_id: UUID | None = None,
    status: str = "active",
) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = Device(
            org_id=org_id,
            hostname=hostname,
            os_family="windows",
            device_group_id=group_id,
            status=status,
        )
        session.add(device)
        await session.flush()
        if doc is not None:
            session.add(
                InventorySnapshot(
                    org_id=org_id,
                    device_id=device.id,
                    taken_at=datetime.now(UTC),
                    raw={**doc, "hostname": hostname},
                )
            )
        await session.commit()
        return device.id


async def _placement(admin_engine: AsyncEngine, device_id: UUID) -> UUID | None:
    async with AsyncSession(admin_engine) as session:
        return (
            await session.execute(
                select(Device.device_group_id).where(Device.id == device_id)
            )
        ).scalar_one()


async def _sweep(app_engine: AsyncEngine, org_id: UUID) -> Any:
    """Run one org's re-evaluation exactly as the worker task does: inside that
    org's own `org_session`, never on the admin engine."""
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        return await reevaluate_org_membership(session, org_id=org_id)


# --------------------------------------------------------------------------- #
# §11.6-7 -- what it evaluates, and what it cannot
# --------------------------------------------------------------------------- #


async def test_the_sweep_replays_the_snapshot_and_agrees_with_the_ingest_path(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """§11.6. The sweep reads the LATEST `InventorySnapshot` and rebuilds
    `InventoryDocument(**raw)` -- the ingest writes `doc.model_dump(mode=
    "json")`, so this round-trips exactly.

    THE ASSERTION IS AGAINST THE INGEST PATH ITSELF, not against a hand-written
    expectation. Two code paths that decide membership must agree, or the
    console shows one answer and the fleet gets another; comparing the sweep's
    result to `evaluate_rules`'s own is the only assertion that stays true when
    either is changed.
    """
    org_id = await _org(admin_engine)
    windows = await _group(admin_engine, org_id, "windows", WINDOWS)
    await _group(admin_engine, org_id, "linux", LINUX, precedence=5)
    device_id = await _device(admin_engine, org_id, "wks", doc=FULL_DOC)

    result = await _sweep(app_engine, org_id)
    assert result.devices_moved == 1
    assert await _placement(admin_engine, device_id) == windows

    # The ingest path, on the same document, reaches the same group.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one()
        claimed = await evaluate_rules(
            session, device, InventoryDocument(**FULL_DOC), org_id=org_id
        )
    assert claimed == windows

    # AND IT IS IDEMPOTENT: a second sweep moves nobody, which is what keeps a
    # 300-second beat from being a 300-second write storm.
    again = await _sweep(app_engine, org_id)
    assert again.devices_moved == 0
    assert again.devices_evaluated == 1


async def test_a_device_with_no_snapshot_is_untouched_and_counted(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """§11.7. The evaluator reads DOCUMENTS, never device rows, so a
    manually-created asset, a discovered switch and an enrolled machine that has
    not yet collected are all unmatchable -- and saying so is the difference
    between a rule that looks broken and a fleet that has not reported yet."""
    org_id = await _org(admin_engine)
    home = await _group(admin_engine, org_id, "home", None)
    await _group(admin_engine, org_id, "windows", WINDOWS)
    bare = await _device(admin_engine, org_id, "bare", doc=None, group_id=home)

    result = await _sweep(app_engine, org_id)
    assert result.devices_without_inventory == 1
    assert result.devices_moved == 0
    assert await _placement(admin_engine, bare) == home


async def test_no_rule_bearing_group_means_no_work_and_no_audit_event(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """§5.3's short circuit: ONE `COUNT`, which is every org on a deployment
    that uses only static groups.

    It matters twice. It keeps the beat from reading a whole fleet's inventory
    documents every five minutes for nothing, and it keeps the audit log from
    accruing an event per org per run saying that nothing happened.
    """
    org_id = await _org(admin_engine)
    home = await _group(admin_engine, org_id, "home", None)
    device_id = await _device(admin_engine, org_id, "wks", doc=FULL_DOC, group_id=home)

    result = await _sweep(app_engine, org_id)
    assert result.groups_evaluated == 0
    assert result.devices_evaluated == 0
    assert await _placement(admin_engine, device_id) == home

    async with AsyncSession(admin_engine) as session:
        actions = (
            (
                await session.execute(
                    select(AuditEvent.action).where(AuditEvent.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )
    assert actions == []


async def test_an_archived_rule_group_is_not_swept_into(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """§7.1's guarantee holds through the sweep, or six modules are wrong about
    `archived_at`.

    An archived group is empty and unreferenced BY CONSTRUCTION -- that sentence
    is why deploy, patch, backup, remote, health and bcp are allowed not to know
    the column exists. A rule-bearing group with no members has no live
    references, so it CAN be archived; a sweep that then repopulated it would
    leave a group that is archived, occupied and unarchivable.
    """
    org_id = await _org(admin_engine)
    archived = await _group(admin_engine, org_id, "gone", WINDOWS, precedence=-5)
    async with AsyncSession(admin_engine) as session:
        group = (
            await session.execute(select(DeviceGroup).where(DeviceGroup.id == archived))
        ).scalar_one()
        group.archived_at = datetime.now(UTC)
        await session.commit()
    device_id = await _device(admin_engine, org_id, "wks", doc=FULL_DOC)

    result = await _sweep(app_engine, org_id)
    assert result.groups_evaluated == 0
    assert await _placement(admin_engine, device_id) is None


async def test_a_decommissioned_device_is_not_swept(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Active devices only, matching `health/sweep.py`'s reconcile. A retired
    machine is not one any job should reach, and re-homing it would change what
    a group-targeted deployment's device count says."""
    org_id = await _org(admin_engine)
    await _group(admin_engine, org_id, "windows", WINDOWS)
    retired = await _device(
        admin_engine, org_id, "old", doc=FULL_DOC, status="decommissioned"
    )
    result = await _sweep(app_engine, org_id)
    assert result.devices_evaluated == 0
    assert await _placement(admin_engine, retired) is None


async def test_precedence_decides_the_sweep_exactly_as_it_decides_the_ingest(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`(precedence, name)`, and the sweep gets it from the same walk. A sweep
    that ordered differently from the ingest would move machines back and forth
    between two rule groups on alternate beats."""
    org_id = await _org(admin_engine)
    loser = await _group(admin_engine, org_id, "aaa", WINDOWS, precedence=10)
    winner = await _group(admin_engine, org_id, "zzz", WINDOWS, precedence=0)
    device_id = await _device(admin_engine, org_id, "wks", doc=FULL_DOC)

    await _sweep(app_engine, org_id)
    assert await _placement(admin_engine, device_id) == winner
    assert await _placement(admin_engine, device_id) != loser


async def test_the_sweep_never_unassigns_a_device_no_rule_claims(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`evaluate_rules` only ever ASSIGNS on a match; the no-match case
    preserves whatever was there. §7.2 depends on this exactly -- clearing a
    group's rules has to leave its members in place, or a rule-bearing group
    cannot be emptied and therefore cannot be archived."""
    org_id = await _org(admin_engine)
    manual = await _group(admin_engine, org_id, "manual", None)
    await _group(admin_engine, org_id, "linux", LINUX)
    device_id = await _device(admin_engine, org_id, "wks", doc=FULL_DOC, group_id=manual)

    result = await _sweep(app_engine, org_id)
    assert result.devices_moved == 0
    assert await _placement(admin_engine, device_id) == manual


# --------------------------------------------------------------------------- #
# §8.3 -- tenancy, on the engine where it can fail
# --------------------------------------------------------------------------- #


async def test_the_sweep_moves_only_its_own_orgs_devices_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """§11.8, and it is the reason `evaluate_rules` took an `org_id` argument.

    Two orgs, each with a rule-bearing group whose rule matches the other's
    devices. The sweep body is driven HERE ON THE ADMIN ENGINE -- BYPASSRLS,
    the shape four sibling sweeps in `app/worker.py` already hold -- so the
    application predicate is the only thing scoping it.

    FALSIFIED: delete `DeviceGroup.org_id == org_id` from `evaluate_rules`'s
    group walk, or `Device.org_id == org_id` from the sweep's device walk, and
    this turns red. Both verified by doing it.
    """
    theirs = await _org(admin_engine, "dgs-theirs")
    mine = await _org(admin_engine, "dgs-mine")
    # The neighbour's rule sorts FIRST on precedence, so an unscoped walk picks
    # it and cross-assigns.
    their_group = await _group(
        admin_engine, theirs, "aaa-theirs", WINDOWS, precedence=-100
    )
    my_group = await _group(admin_engine, mine, "zzz-mine", WINDOWS, precedence=0)
    their_device = await _device(admin_engine, theirs, "their-wks", doc=FULL_DOC)
    my_device = await _device(admin_engine, mine, "my-wks", doc=FULL_DOC)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        result = await reevaluate_org_membership(session, org_id=mine)
        await session.commit()

    assert result.devices_evaluated == 1, "the sweep walked a neighbour's fleet"
    assert result.devices_moved == 1
    assert await _placement(admin_engine, my_device) == my_group
    assert await _placement(admin_engine, their_device) is None, (
        "the sweep moved a neighbour's device"
    )
    assert await _placement(admin_engine, my_device) != their_group


# --------------------------------------------------------------------------- #
# §8.2 -- the sweep's audit event
# --------------------------------------------------------------------------- #


async def test_the_sweep_writes_one_system_event_naming_what_moved(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """§8.2. `actor_type="system"` because no user pressed anything at the
    moment it ran, and `target_type="organization"` because the subject is the
    org's whole membership rather than any one group.

    ONE EVENT PER RUN, NOT ONE PER DEVICE, and the reason is mechanical:
    `audit_service.record` takes a per-org `pg_advisory_xact_lock` and reads the
    chain head on every append, so 5,000 devices would be 5,000 serialized head
    reads inside one transaction.
    """
    org_id = await _org(admin_engine)
    home = await _group(admin_engine, org_id, "home", None)
    windows = await _group(admin_engine, org_id, "windows", WINDOWS)
    device_id = await _device(admin_engine, org_id, "wks", doc=FULL_DOC, group_id=home)
    await _device(admin_engine, org_id, "bare", doc=None)

    await _sweep(app_engine, org_id)

    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(
                select(
                    AuditEvent.action,
                    AuditEvent.actor_type,
                    AuditEvent.target_type,
                    AuditEvent.target_id,
                    AuditEvent.meta,
                ).where(AuditEvent.org_id == org_id)
            )
        ).all()
    assert len(rows) == 1
    action, actor_type, target_type, target_id, meta = rows[0]
    assert action == "device_group.membership_reevaluated"
    assert actor_type == "system"
    assert target_type == "organization"
    assert target_id == str(org_id)
    assert meta["groups_evaluated"] == 1
    # EVALUATED COUNTS EVERY ACTIVE DEVICE THE SWEEP LOOKED AT, and
    # `devices_without_inventory` is a subset of it rather than a sibling
    # bucket: "we considered 2 machines, 1 of them has never reported" is the
    # sentence an operator needs, and two disjoint counts that must be added
    # together to reach the fleet size is not it.
    assert meta["devices_evaluated"] == 2
    assert meta["devices_moved"] == 1
    assert meta["devices_without_inventory"] == 1
    assert meta["moves_truncated"] is False
    assert meta["moves"] == [
        {
            "device_id": str(device_id),
            "from_group_id": str(home),
            "to_group_id": str(windows),
        }
    ]


async def test_a_run_that_moves_more_than_the_bound_says_so_and_keeps_the_count(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """§11.25. The count is EXACT and the list is truncated, with
    `moves_truncated: true` stated rather than left to be inferred.

    An audit entry that implies a completeness it does not have is worse than
    one that admits its bound -- the investigator counting rows would conclude
    the sweep moved exactly `MOVES_RECORDED` machines.
    """
    org_id = await _org(admin_engine)
    await _group(admin_engine, org_id, "windows", WINDOWS)
    for index in range(MOVES_RECORDED + 3):
        await _device(admin_engine, org_id, f"wks-{index:04d}", doc=FULL_DOC)

    result = await _sweep(app_engine, org_id)
    assert result.devices_moved == MOVES_RECORDED + 3
    assert len(result.moves) == MOVES_RECORDED
    assert result.moves_truncated is True

    async with AsyncSession(admin_engine) as session:
        meta = (
            await session.execute(
                select(AuditEvent.meta).where(AuditEvent.org_id == org_id)
            )
        ).scalar_one()
    assert meta["devices_moved"] == MOVES_RECORDED + 3
    assert len(meta["moves"]) == MOVES_RECORDED
    assert meta["moves_truncated"] is True


# --------------------------------------------------------------------------- #
# §5.3 -- health is the one module that has to be pushed
# --------------------------------------------------------------------------- #


async def test_the_worker_task_reconciles_health_only_when_something_moved(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster: Any, tmp_path: Any
) -> None:
    """§11.9. `patch/policy.py`, `backup/policy.py` and `remote/policy.py` all
    resolve a policy AT POINT OF USE by reading `Device.device_group_id` at
    dispatch time, so a membership change needs no push. `health` is different:
    `resolve_profile_for_device` walks the parent chain and
    `sweep.run_policy_reconcile` pushes `health.policy.apply` where a device's
    acknowledged version is behind.

    AND NOT WHEN NOTHING MOVED, which is the half that matters at a 300-second
    beat: an unconditional reconcile would be a fleet-wide profile comparison
    every five minutes for every org, forever.
    """
    import pytest

    import app.worker as worker
    from app.core.config import Settings, override_settings

    org_id = await _org(admin_engine)
    await _group(admin_engine, org_id, "windows", WINDOWS)
    await _device(admin_engine, org_id, "wks", doc=FULL_DOC)

    calls: list[UUID] = []

    async def _fake_reconcile(
        session: Any, scoped_org: UUID, **kwargs: Any
    ) -> list[Any]:
        calls.append(scoped_org)
        return []

    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url="redis://localhost:6379/9",
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker, "run_policy_reconcile", _fake_reconcile)
        with override_settings(settings):
            moved = await worker.device_group_reevaluate_all_orgs()
            assert moved >= 1
            assert org_id in calls

            # Second run: nothing left to move, so nothing to push. THIS is the
            # half that matters at a 300-second beat -- an unconditional
            # reconcile is a fleet-wide profile comparison every five minutes
            # for every org, forever.
            calls.clear()
            assert await worker.device_group_reevaluate_all_orgs() == 0
            assert org_id not in calls


def test_the_beat_entry_exists_at_the_specified_cadence(pg_cluster, tmp_path) -> None:
    """§5.3 fixes the lag at 300 seconds and says the console must state it.

    A SWEEP WITH NO BEAT ENTRY IS A FEATURE THAT NEVER RUNS, which is exactly
    the defect this cycle exists to close: `DEFAULT_SWEEP_POLICY =
    {"interval_hours": 24}` has been seeded into `policies` since Phase 0 and
    is read by NOTHING, which is why "it takes effect on the next ingest" meant
    "never" for an unattended fleet.

    The interval is the SERVICE's constant rather than a literal here, for the
    reason the health and IPAM entries state: the lag this sweep costs is an
    operational property of the deployment, and a literal in `worker.py` can
    drift from the number the console tells the operator.
    """
    from app.core.config import Settings, override_settings
    from app.services.device_groups import REEVALUATE_INTERVAL_S
    from app.worker import create_celery

    assert REEVALUATE_INTERVAL_S == 300.0
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

    entry = schedule["device-group-reevaluate"]
    assert entry["task"] == "app.worker.device_group_reevaluate"
    assert entry["schedule"] == REEVALUATE_INTERVAL_S
    # `worker._with_expiry` gives every beat entry an `options.expires`, so an
    # overrunning sweep cannot backlog without bound.
    assert entry["options"]["expires"] == REEVALUATE_INTERVAL_S


async def test_a_group_whose_rules_column_holds_jsonb_null_is_not_rule_bearing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """THE ROWS CYCLE 1 ALREADY WROTE, and the reason there is no migration.

    SQLAlchemy's JSON types serialize a Python `None` into the JSON literal
    `null` unless told otherwise, so every static group created through
    `POST /device-groups` before this cycle landed in the database as
    `rules = 'null'::jsonb` -- a value for which `rules IS NOT NULL` is TRUE.
    It deserializes back to Python `None`, so `dynamic` was always right and
    nothing on any screen was ever wrong; what was wrong was every SQL
    predicate over the column.

    `DeviceGroup.rules` now stores SQL NULL (`none_as_null=True`), and
    `rule_bearing_groups_query` asks `jsonb_typeof(rules) = 'object'` so the
    rows already written behave too. This test writes the OLD shape by hand,
    because a fixture built through the fixed model could not reproduce it.

    WHY IT IS NOT COSMETIC. Without the fix the sweep's short circuit -- "does
    this org hold any rule-bearing group" -- answers yes for every org that has
    ever created a static group, so an org using only hand-curated groups would
    read its whole fleet's inventory documents every five minutes to decide
    nothing. The ingest path merely logged a warning per static group per
    collection, which is exactly the "`logger.warning` nobody reads" §5.1 warns
    about, being written for real.
    """
    org_id = await _org(admin_engine)
    group_id = await _group(admin_engine, org_id, "legacy-static", None)
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("UPDATE device_groups SET rules = 'null'::jsonb WHERE id = :id"),
            {"id": group_id},
        )
        await session.commit()
        assert (
            await session.execute(
                text("SELECT jsonb_typeof(rules) FROM device_groups WHERE id = :id"),
                {"id": group_id},
            )
        ).scalar_one() == "null", "the fixture did not reproduce the old shape"

    device_id = await _device(admin_engine, org_id, "wks", doc=FULL_DOC, group_id=group_id)
    result = await _sweep(app_engine, org_id)

    assert result.groups_evaluated == 0
    assert result.devices_evaluated == 0
    assert await _placement(admin_engine, device_id) == group_id


# --------------------------------------------------------------------------- #
# THE WATERMARK, AND THE LAST RUN THE CONSOLE CAN SEE (migration 0081).
#
# Two gaps the design named and cycle 2 could not close:
#
#   * §5.3's cost note -- the sweep re-read EVERY active device's latest
#     `InventoryDocument` every 300 seconds. A document is a machine's whole
#     collected estate, so a 10,000-device org moved a few hundred megabytes
#     per sweep, forever. `SNAPSHOT_BATCH` bounds the resident set, not the
#     transfer.
#   * §5.3/§9's "a timestamp, never alone" -- the sweep's only durable record
#     was an audit event, and reading it needs `audit.read`, a code the Groups
#     page's `devices.read` gate does not entitle. So the page could not tell
#     "swept four minutes ago" from "the worker has been down since Tuesday".
# --------------------------------------------------------------------------- #


async def _evaluation_row(app_engine: AsyncEngine, org_id: UUID) -> Any:
    from app.models import DeviceGroupEvaluation

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        return (
            await session.execute(
                select(DeviceGroupEvaluation).where(
                    DeviceGroupEvaluation.org_id == org_id
                )
            )
        ).scalar_one_or_none()


async def _snapshot_bytes(admin_engine: AsyncEngine, org_id: UUID) -> int:
    """What one full pass actually moves, measured by Postgres rather than
    estimated: `pg_column_size` is the on-disk size of the `raw` jsonb."""
    async with AsyncSession(admin_engine) as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT coalesce(sum(pg_column_size(raw)), 0) "
                        "FROM inventory_snapshots WHERE org_id = :org"
                    ).bindparams(org=org_id)
                )
            ).scalar_one()
        )


async def test_the_sweep_stops_re_reading_a_fleet_whose_inventory_did_not_move(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """THE MEASUREMENT, NOT THE ASSERTION. §5.3 said the fix was "re-evaluating
    only devices whose snapshot is newer than the last sweep"; this seeds a
    fleet, counts the documents that come off the wire on a full pass, and
    counts them again once the watermark is in effect.

    THREE RUNS BEFORE IT IS INCREMENTAL, AND THAT IS THE DESIGN RATHER THAN A
    WARM-UP. The boundary lags one run on purpose: run 1 has no state, run 2
    has a `next_watermark` but no `watermark` yet, and run 3 selects on the
    maximum run 1 started from. A single-column watermark would skip any
    snapshot whose uuid7 was assigned before the sweep read the maximum but
    whose transaction committed after it -- a device left holding a membership
    its inventory no longer justifies, silently and forever.
    """
    org_id = await _org(admin_engine, "dg-watermark")
    await _group(admin_engine, org_id, "windows", WINDOWS)
    fleet = 40
    for index in range(fleet):
        await _device(admin_engine, org_id, f"ws-{index:03d}", doc=FULL_DOC)

    full_bytes = await _snapshot_bytes(admin_engine, org_id)
    assert full_bytes > 0, "the measurement below would be vacuous"

    first = await _sweep(app_engine, org_id)
    assert first.full_pass is True
    assert first.devices_evaluated == fleet
    assert first.snapshots_read == fleet, "a full pass reads every device"

    second = await _sweep(app_engine, org_id)
    assert second.full_pass is True, "the boundary is not in force until run 3"

    third = await _sweep(app_engine, org_id)
    assert third.full_pass is False
    assert third.devices_evaluated == 0
    assert third.snapshots_read == 0, (
        "nothing ingested between runs, so nothing had to be re-read -- this "
        "number IS the transfer the watermark exists to remove"
    )

    # ONE MACHINE INGESTS. Exactly one document may come back off the wire.
    await _device(admin_engine, org_id, "ws-new", doc=FULL_DOC)
    fourth = await _sweep(app_engine, org_id)
    assert fourth.full_pass is False
    assert fourth.devices_evaluated == 1
    assert fourth.snapshots_read == 1

    # The measured claim, stated as a ratio rather than as prose: a steady
    # fleet in which one machine reported moves one document's worth of bytes
    # instead of forty-one.
    per_document = full_bytes // fleet
    assert per_document > 0
    assert fourth.snapshots_read * per_document < full_bytes // 10, (
        f"one changed device out of {fleet + 1} should move well under a tenth "
        "of a full pass"
    )


async def test_a_rules_change_forces_a_full_pass_without_waiting_for_an_ingest(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A RULE DECIDES WHERE A SIGNED JOB LANDS. If a rules edit had to wait for
    each device to ingest before it could claim anything, an author would watch
    a correct rule reach an arbitrary subset of the fleet it matches -- which
    is worse than the "membership is up to five minutes behind" the design
    accepted, because it has no bound at all on an unattended fleet.

    So the fingerprint is over what the evaluator READS: which groups compete,
    in which precedence order, with which documents.
    """
    org_id = await _org(admin_engine, "dg-fingerprint")
    await _group(admin_engine, org_id, "windows", WINDOWS)
    for index in range(4):
        await _device(admin_engine, org_id, f"ws-{index}", doc=FULL_DOC)

    await _sweep(app_engine, org_id)
    await _sweep(app_engine, org_id)
    settled = await _sweep(app_engine, org_id)
    assert settled.full_pass is False and settled.snapshots_read == 0

    # A SECOND RULE GROUP, and not one device has ingested since.
    await _group(admin_engine, org_id, "linux", LINUX, precedence=1)
    after_edit = await _sweep(app_engine, org_id)
    assert after_edit.full_pass is True
    assert after_edit.devices_evaluated == 4
    assert after_edit.snapshots_read == 4

    # And it settles again rather than staying full forever -- a fingerprint
    # that never matched would be a watermark that never applied.
    await _sweep(app_engine, org_id)
    assert (await _sweep(app_engine, org_id)).full_pass is False


async def test_a_stale_full_pass_is_forced_however_quiet_the_fleet_is(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """THE WATERMARK CANNOT SEE EVERYTHING, AND THIS IS THE BACKSTOP.

    An incremental pass asks "whose inventory changed", which is silent about a
    device whose `status` returns to active carrying an old snapshot, and about
    one hand-placed into a static group while a rule elsewhere claims it. The
    old full sweep re-homed both on its next run; an incremental one would
    never look at them again. `FULL_PASS_INTERVAL_S` bounds that drift to an
    hour instead of to nothing.

    Driven by rewinding `last_full_pass_at`, not by sleeping: the constant is
    an hour and a test may not be.
    """
    from datetime import timedelta

    from app.models import DeviceGroupEvaluation

    org_id = await _org(admin_engine, "dg-stale")
    await _group(admin_engine, org_id, "windows", WINDOWS)
    for index in range(3):
        await _device(admin_engine, org_id, f"ws-{index}", doc=FULL_DOC)

    await _sweep(app_engine, org_id)
    await _sweep(app_engine, org_id)
    assert (await _sweep(app_engine, org_id)).full_pass is False

    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text(
                "UPDATE device_group_evaluation SET last_full_pass_at = :then "
                "WHERE org_id = :org"
            ).bindparams(then=datetime.now(UTC) - timedelta(hours=2), org=org_id)
        )
        await session.commit()

    forced = await _sweep(app_engine, org_id)
    assert forced.full_pass is True
    assert forced.devices_evaluated == 3

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(DeviceGroupEvaluation).where(
                    DeviceGroupEvaluation.org_id == org_id
                )
            )
        ).scalar_one()
    assert row.last_full_pass_at is not None
    assert (datetime.now(UTC) - row.last_full_pass_at) < timedelta(minutes=5), (
        "the forced pass must reset the clock, or every run after it is full"
    )


async def test_an_org_with_no_rules_still_records_that_the_worker_ran(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """"THE WORKER IS ALIVE" IS WHAT AN ORG WITH NO RULES STILL NEEDS TO KNOW.

    The short circuit deliberately writes no AUDIT event -- §8.2 taken
    literally over every org is 288 append-only rows a day saying nothing
    happened. But it now writes the evaluation row, because the Groups page's
    whole complaint was that it could not distinguish "swept four minutes ago"
    from "the worker has been down since Tuesday", and an org that uses only
    static groups is exactly the org where the audit log will never answer.
    """
    org_id = await _org(admin_engine, "dg-norules")
    await _group(admin_engine, org_id, "static", None)
    await _device(admin_engine, org_id, "ws-0", doc=FULL_DOC)

    result = await _sweep(app_engine, org_id)
    assert result.groups_evaluated == 0
    assert result.devices_evaluated == 0

    row = await _evaluation_row(app_engine, org_id)
    assert row is not None, "the run must be recorded even when it did nothing"
    assert row.last_evaluated_at is not None
    assert row.devices_moved == 0

    # STILL NO AUDIT EVENT. The two records answer different questions and the
    # short circuit's reason for writing neither has not changed for one of
    # them.
    async with AsyncSession(admin_engine) as session:
        events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "device_group.membership_reevaluated",
                )
            )
        ).scalars().all()
    assert events == []


async def test_the_evaluation_row_carries_what_the_groups_page_renders(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`GET /device-groups` SERVES THIS UNDER `devices.read`, WHICH IS THE
    POINT. The same facts live in the audit event, behind `audit.read`, which
    the Groups page's gate does not entitle a caller to hold -- so the page
    stated the interval and could say nothing about the last run."""
    org_id = await _org(admin_engine, "dg-render")
    group_id = await _group(admin_engine, org_id, "windows", WINDOWS)
    await _device(admin_engine, org_id, "ws-0", doc=FULL_DOC)
    await _device(admin_engine, org_id, "ws-blind")  # no snapshot at all

    result = await _sweep(app_engine, org_id)
    assert result.devices_moved == 1
    assert result.devices_without_inventory == 1
    moved_id = await _placement_source(admin_engine, org_id, "ws-0")
    assert await _placement(admin_engine, moved_id) == group_id

    row = await _evaluation_row(app_engine, org_id)
    assert row is not None
    assert row.groups_evaluated == 1
    assert row.devices_evaluated == 2
    assert row.devices_moved == 1
    assert row.devices_without_inventory == 1
    assert row.snapshots_read == 1, "one of the two devices has a document"
    assert row.full_pass is True


async def _placement_source(
    admin_engine: AsyncEngine, org_id: UUID, hostname: str
) -> UUID:
    async with AsyncSession(admin_engine) as session:
        return (
            await session.execute(
                select(Device.id).where(
                    Device.org_id == org_id, Device.hostname == hostname
                )
            )
        ).scalar_one()


async def test_one_orgs_watermark_never_moves_another_orgs_boundary(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """THE FALSIFIER, PROVEN. Everything the sweep touches runs through
    `org_session` as `app_user` (NOBYPASSRLS), so deleting `org_id` from
    `_evaluation_state` or `_max_snapshot_id` turns no `org_session` test red
    -- RLS rejects first (`docs/superpowers/audits/
    2026-08-21-tenancy-test-efficacy.md` §0). This drives them on the BYPASSRLS
    ADMIN engine, where the application's predicate is the only thing scoping
    anything.

    AND THE CONSEQUENCE IS WORSE HERE THAN A DISCLOSURE. A watermark read from
    a busier neighbour is HIGHER than this org's, so every one of this org's
    devices falls below it and is never evaluated again -- a silent, permanent
    stop, with a `last_evaluated_at` on screen saying the sweep ran.
    """
    from app.models import DeviceGroupEvaluation
    from app.services.device_groups import _evaluation_state, _max_snapshot_id

    mine = await _org(admin_engine, "dg-wm-mine")
    theirs = await _org(admin_engine, "dg-wm-theirs")
    await _group(admin_engine, mine, "windows", WINDOWS)
    await _group(admin_engine, theirs, "windows", WINDOWS)
    await _device(admin_engine, mine, "mine-0", doc=FULL_DOC)

    await _sweep(app_engine, mine)
    await _sweep(app_engine, mine)
    await _sweep(app_engine, theirs)

    # The neighbour ingests AFTER mine, so its snapshot id is strictly higher:
    # a `_max_snapshot_id` with no tenant predicate returns THEIRS for me.
    await _device(admin_engine, theirs, "theirs-0", doc=FULL_DOC)

    async with AsyncSession(admin_engine) as session:
        mine_max = await _max_snapshot_id(session, mine)
        theirs_max = await _max_snapshot_id(session, theirs)
        assert mine_max is not None and theirs_max is not None
        assert theirs_max > mine_max, (
            "the test is vacuous unless the neighbour's boundary is the higher one"
        )

        mine_state = await _evaluation_state(session, mine)
        theirs_state = await _evaluation_state(session, theirs)
        assert mine_state is not None and theirs_state is not None
        assert mine_state.org_id == mine
        assert theirs_state.org_id == theirs
        assert mine_state.watermark_snapshot_id != theirs_state.watermark_snapshot_id

        rows = (
            (await session.execute(select(DeviceGroupEvaluation))).scalars().all()
        )
    assert {row.org_id for row in rows} >= {mine, theirs}
