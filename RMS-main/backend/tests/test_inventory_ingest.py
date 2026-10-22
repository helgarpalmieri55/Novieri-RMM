import contextlib
import copy
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.models import (
    Device,
    HardwareComponent,
    InstalledSoftware,
    InventoryChange,
    InventorySnapshot,
    IpamObservation,
    JobStatus,
    Organization,
    Policy,
)
from app.modules.inventory.handler import register_job_type
from app.modules.inventory.hooks import (
    PostIngestHook,
    clear_post_ingest_hooks,
    post_ingest_hooks,
    register_post_ingest_hook,
)
from app.modules.inventory.ingest import ingest_inventory
from app.modules.inventory.schema import InventoryDocument
from app.modules.ipam import register_inventory_hook
from tests.test_inventory_schema import FULL_DOC


async def _org_device(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"inv-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        device = Device(org_id=org.id, hostname="pre-enroll-name", os_family="windows")
        session.add(device)
        await session.commit()
        return org.id, device.id


async def _ingest(app_engine: AsyncEngine, org_id: UUID, device_id: UUID, doc_dict: dict):
    async with org_session(org_id, engine=app_engine) as session:
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one()
        stats = await ingest_inventory(session, device, InventoryDocument(**doc_dict))
    return stats


async def test_first_sweep_populates_everything(admin_engine, app_engine) -> None:
    org_id, device_id = await _org_device(admin_engine)
    stats = await _ingest(app_engine, org_id, device_id, FULL_DOC)
    assert stats.software_added == 2
    assert stats.changes == 0  # first sweep is a baseline, not a change

    async with org_session(org_id, engine=app_engine) as session:
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one()
        assert device.hostname == "wks-042"
        assert device.os_version == "Windows 11 Pro 23H2"
        assert device.serial == "5CG1234ABC"
        assert device.ram_bytes == 17179869184
        software = (
            (await session.execute(select(InstalledSoftware))).scalars().all()
        )
        assert {s.name for s in software} == {"7-Zip", "Google Chrome"}
        components = (
            (await session.execute(select(HardwareComponent))).scalars().all()
        )
        assert {c.type for c in components} == {"disk", "nic"}
        snapshots = (
            (await session.execute(select(InventorySnapshot))).scalars().all()
        )
        assert len(snapshots) == 1
        assert snapshots[0].software_count == 2
        # default sweep policy seeded
        policy = (
            await session.execute(select(Policy).where(Policy.module == "inventory"))
        ).scalar_one()
        assert policy.config == {"interval_hours": 24}


async def test_second_sweep_records_exact_changes(admin_engine, app_engine) -> None:
    org_id, device_id = await _org_device(admin_engine)
    await _ingest(app_engine, org_id, device_id, FULL_DOC)

    second = {
        **FULL_DOC,
        "os_version": "Windows 11 Pro 24H2",
        "facts": {**FULL_DOC["facts"], "disk_encryption": False},
        "software": [
            # 7-Zip removed; Chrome upgraded; Slack installed
            {"name": "Google Chrome", "version": "127.0.6533.72"},
            {"name": "Slack", "version": "4.39.95", "publisher": "Slack Technologies"},
        ],
    }
    stats = await _ingest(app_engine, org_id, device_id, second)
    assert (stats.software_added, stats.software_removed, stats.software_updated) == (1, 1, 1)
    assert stats.changes == 5  # install + removal + update + os_updated + fact_changed

    async with org_session(org_id, engine=app_engine) as session:
        changes = (
            (await session.execute(select(InventoryChange).order_by(InventoryChange.id)))
            .scalars()
            .all()
        )
        by_type = {(c.change_type, c.subject): c for c in changes}
        assert by_type[("software_installed", "Slack")].to_value == "4.39.95"
        assert by_type[("software_removed", "7-Zip")].from_value == "23.01"
        updated = by_type[("software_updated", "Google Chrome")]
        assert (updated.from_value, updated.to_value) == ("126.0.6478.127", "127.0.6533.72")
        os_change = by_type[("os_updated", "os_version")]
        assert (os_change.from_value, os_change.to_value) == (
            "Windows 11 Pro 23H2",
            "Windows 11 Pro 24H2",
        )
        fact = by_type[("fact_changed", "disk_encryption")]
        assert (fact.from_value, fact.to_value) == ("True", "False")
        # second identical policy not duplicated
        count = (
            await session.execute(
                select(func.count()).select_from(Policy).where(Policy.module == "inventory")
            )
        ).scalar_one()
        assert count == 1


async def test_first_software_list_after_softwareless_sweeps_is_a_baseline(
    admin_engine, app_engine
) -> None:
    """Agents before 2026-09-23 sent `software: []`. The first sweep from an
    upgraded agent must not record every package on the machine as
    `software_installed` today; later differences are still changes."""
    org_id, device_id = await _org_device(admin_engine)
    await _ingest(app_engine, org_id, device_id, {**FULL_DOC, "software": []})

    stats = await _ingest(app_engine, org_id, device_id, FULL_DOC)
    assert stats.software_added == 2
    assert stats.changes == 0

    third = {
        **FULL_DOC,
        "software": [*FULL_DOC["software"], {"name": "Slack", "version": "4.39.95"}],
    }
    stats = await _ingest(app_engine, org_id, device_id, third)
    assert (stats.software_added, stats.changes) == (1, 1)
    async with org_session(org_id, engine=app_engine) as session:
        changes = (await session.execute(select(InventoryChange))).scalars().all()
        assert [(c.change_type, c.subject) for c in changes] == [("software_installed", "Slack")]


async def test_snapshot_pruning_keeps_ten(admin_engine, app_engine) -> None:
    org_id, device_id = await _org_device(admin_engine)
    for n in range(11):
        doc = {**FULL_DOC, "os_build": f"build-{n}"}
        await _ingest(app_engine, org_id, device_id, doc)
    async with org_session(org_id, engine=app_engine) as session:
        snapshots = (
            (await session.execute(select(InventorySnapshot).order_by(InventorySnapshot.id)))
            .scalars()
            .all()
        )
    assert len(snapshots) == 10
    assert snapshots[-1].raw["os_build"] == "build-10"


async def test_ingest_inventory_concurrent_same_device_sweeps_do_not_race_software_insert(
    admin_engine: AsyncEngine,
) -> None:
    """Two overlapping sweeps of the SAME device -- e.g. a scheduled sweep
    racing a manual "resync now", or an agent retry after a network
    timeout -- both diff against a stale `existing` snapshot that has never
    seen this device's software before. `InstalledSoftware` is backed by
    `UniqueConstraint("device_id", "name")`; naively, the second sweep's
    plain insert would 500 on a raw `IntegrityError`.

    Ingestion is agent-driven and high-frequency, and both sweeps are
    reporting the SAME device's actual current state -- this is the
    idempotent-upsert shape the task called out, not a
    duplicate-caller-error shape (unlike `add_link`/`approve_patch`). The
    fix is `ON CONFLICT (device_id, name) DO UPDATE`, so the race loser's
    insert becomes a harmless update instead of a 500.

    Deterministic interleave: session A's existence-check SELECT against
    `installed_software` is intercepted; right after it misses (nothing
    tracked for this device yet), session B inserts and commits the exact
    competing `("Google Chrome", device_id)` row -- the same shape the
    other sweep would also be about to write -- before A gets to write its
    own rows. (Session B deliberately does NOT re-run the whole
    `ingest_inventory` here: that also unconditionally updates the shared
    `Device` row, so two full concurrent sweeps of the same device
    genuinely serialize on that row's lock -- a separate, real, but
    out-of-scope contention point. This test isolates the one race this
    task closes: the `InstalledSoftware` insert.)
    """
    org_id, device_id = await _org_device(admin_engine)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session_a:
        original_execute = session_a.execute
        precheck_seen = False

        async def interleaving_execute(statement, *args, **kwargs):
            nonlocal precheck_seen
            result = await original_execute(statement, *args, **kwargs)
            if (
                not precheck_seen
                and getattr(statement, "is_select", False)
                and "installed_software" in str(statement).lower()
            ):
                precheck_seen = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as session_b:
                    session_b.add(
                        InstalledSoftware(
                            org_id=org_id,
                            device_id=device_id,
                            name="Google Chrome",
                            version="126.0.6478.127",
                            publisher="",
                            install_date=None,
                        )
                    )
                    await session_b.commit()
            return result

        session_a.execute = interleaving_execute  # type: ignore[method-assign]
        device_a = (
            await session_a.execute(select(Device).where(Device.id == device_id))
        ).scalar_one()
        stats = await ingest_inventory(session_a, device_a, InventoryDocument(**FULL_DOC))
        await session_a.commit()

    # A's own view: both software items looked new to it (its `existing`
    # snapshot was taken before B committed).
    assert stats.software_added == 2

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(
                select(InstalledSoftware).where(InstalledSoftware.device_id == device_id)
            )
        ).scalars().all()
        # No IntegrityError-induced duplicate rows -- exactly one row per
        # (device_id, name), not two -- and A's own conflicting insert
        # landed as an UPDATE (the DO UPDATE fix), not a swallowed no-op:
        # the row reflects A's version, matching what a real second sweep
        # reporting the same current state would do.
        assert {r.name for r in rows} == {"7-Zip", "Google Chrome"}
        assert len(rows) == 2
        chrome = next(r for r in rows if r.name == "Google Chrome")
        assert chrome.version == "126.0.6478.127"


async def test_malformed_result_fails_job_and_ingests_nothing(
    admin_engine, app_engine, tmp_path: Path
) -> None:
    register_job_type()  # ensure the handler is bound to inventory.collect
    org_id, device_id = await _org_device(admin_engine)
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    async with org_session(org_id, engine=app_engine) as session:
        job = await engine.create(
            session, org_id=org_id, type="inventory.collect", target_device_id=device_id, params={}
        )
        await engine.mark_dispatched(session, job)
        await engine.complete(
            session, job, state="succeeded", result={"hostname": "", "os_family": "beos"}
        )
        assert job.status is JobStatus.failed
        assert "error" in (job.result or {})
    async with org_session(org_id, engine=app_engine) as session:
        count = (
            await session.execute(select(func.count()).select_from(InventorySnapshot))
        ).scalar_one()
    assert count == 0


async def test_valid_result_through_job_engine_ingests(
    admin_engine, app_engine, tmp_path: Path
) -> None:
    register_job_type()
    org_id, device_id = await _org_device(admin_engine)
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    async with org_session(org_id, engine=app_engine) as session:
        job = await engine.create(
            session, org_id=org_id, type="inventory.collect", target_device_id=device_id, params={}
        )
        await engine.mark_dispatched(session, job)
        await engine.complete(session, job, state="succeeded", result=FULL_DOC)
        assert job.status is JobStatus.succeeded
        # the module queued a post-commit UI event on the session
        queued = session.info.get("ui_events", [])
        assert [e["kind"] for _, e in queued] == ["inventory_updated"]
        assert queued[0][1]["device_id"] == str(device_id)
    async with org_session(org_id, engine=app_engine) as session:
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one()
    assert device.hostname == "wks-042"


# ---------------------------------------------------------------------------
# The post-ingest extension point (`app/modules/inventory/hooks.py`, IPAM spec
# §0.17). LAYER 1 of the two guarantees: no hook -- any module's -- can fail an
# `inventory.collect` job or poison its transaction.
#
# LAYER 2 (IPAM's NIC parser being total) is tested in `tests/test_ipam_nic.py`,
# separately and on purpose: a test of either alone passes against an
# implementation missing the other. This file's hooks raise DELIBERATELY, which
# layer 2 would never do, so what is under test here is only the SAVEPOINT.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _hooks(*extra: PostIngestHook) -> Iterator[None]:
    """Register `extra` for the duration, then restore the process's own hooks.

    The hook list is module-global (registration happens once per process, in
    `register_all` and again in the gateway), so a test that leaves a raising
    hook behind fails whichever unrelated test ingests next.
    """
    original = post_ingest_hooks()
    try:
        for hook in extra:
            register_post_ingest_hook(hook)
        yield
    finally:
        clear_post_ingest_hooks()
        for hook in original:
            register_post_ingest_hook(hook)


async def _collect(
    app_engine: AsyncEngine, org_id: UUID, device_id: UUID, tmp_path: Path, doc: dict
) -> JobStatus:
    """One whole `inventory.collect` through `JobEngine.complete` -- the only
    path that runs result handlers, and therefore hooks."""
    register_job_type()
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    async with org_session(org_id, engine=app_engine) as session:
        job = await engine.create(
            session, org_id=org_id, type="inventory.collect", target_device_id=device_id, params={}
        )
        await engine.mark_dispatched(session, job)
        await engine.complete(session, job, state="succeeded", result=doc)
        return job.status


async def _ingested_rows(app_engine: AsyncEngine, org_id: UUID, device_id: UUID) -> tuple[int, int]:
    async with org_session(org_id, engine=app_engine) as session:
        software = (
            await session.execute(
                select(func.count())
                .select_from(InstalledSoftware)
                .where(InstalledSoftware.device_id == device_id)
            )
        ).scalar_one()
        components = (
            await session.execute(
                select(func.count())
                .select_from(HardwareComponent)
                .where(HardwareComponent.device_id == device_id)
            )
        ).scalar_one()
    return software, components


async def test_a_hook_raising_a_database_error_leaves_the_job_succeeded(
    admin_engine, app_engine, tmp_path: Path
) -> None:
    """THE SAVEPOINT, not the `except`, is what this test can see.

    The hook raises a DATABASE error, so Postgres has already put the enclosing
    transaction into an aborted state by the time the `except` runs. IF THE
    `session.begin_nested()` WERE DELETED and only the `try/except` kept, this
    observes the `inventory.collect` job NOT succeeded and **zero** software and
    hardware rows -- the ingest's own writes are lost with the poisoned
    transaction. Those are different VALUES, not a different exception, which is
    why the assertion is on the row counts and not on `pytest.raises`.
    """

    async def poison(session: AsyncSession, device: Device, doc) -> None:
        await session.execute(text("SELECT 1 / 0"))

    org_id, device_id = await _org_device(admin_engine)
    with _hooks(poison):
        status = await _collect(app_engine, org_id, device_id, tmp_path, FULL_DOC)

    assert status is JobStatus.succeeded
    assert await _ingested_rows(app_engine, org_id, device_id) == (2, 2)


async def test_a_hook_raising_a_python_error_leaves_the_job_succeeded(
    admin_engine, app_engine, tmp_path: Path
) -> None:
    """The other half of layer 1. A plain exception is contained by the `except`;
    a database error needs the SAVEPOINT. Both are tested because an
    implementation with only the `try/except` passes this one."""

    async def boom(session: AsyncSession, device: Device, doc) -> None:
        raise RuntimeError("hook is broken")

    org_id, device_id = await _org_device(admin_engine)
    with _hooks(boom):
        status = await _collect(app_engine, org_id, device_id, tmp_path, FULL_DOC)

    assert status is JobStatus.succeeded
    assert await _ingested_rows(app_engine, org_id, device_id) == (2, 2)


async def test_a_failing_hook_rolls_back_only_its_own_writes(
    admin_engine, app_engine, tmp_path: Path
) -> None:
    """The rollback is BOUNDED to the hook. The hook writes a row and then
    fails; that row is gone, and the ingest's rows are not.

    IF THE SAVEPOINT WERE OPENED BEFORE `ingest_inventory` RATHER THAN AFTER
    (or if the hook ran inside the ingest) this observes 0 software rows: a
    SAVEPOINT opened while inventory's writes are still pending would autoflush
    them INSIDE it, and rolling back would take them along.
    """

    async def write_then_fail(session: AsyncSession, device: Device, doc) -> None:
        session.add(
            Policy(org_id=device.org_id, module="hook-probe", config={"seen": True})
        )
        await session.flush()
        raise RuntimeError("after writing")

    org_id, device_id = await _org_device(admin_engine)
    with _hooks(write_then_fail):
        status = await _collect(app_engine, org_id, device_id, tmp_path, FULL_DOC)

    assert status is JobStatus.succeeded
    assert await _ingested_rows(app_engine, org_id, device_id) == (2, 2)
    async with org_session(org_id, engine=app_engine) as session:
        probes = (
            await session.execute(
                select(func.count()).select_from(Policy).where(Policy.module == "hook-probe")
            )
        ).scalar_one()
    assert probes == 0


async def test_one_failing_hook_does_not_stop_the_next(
    admin_engine, app_engine, tmp_path: Path
) -> None:
    """Each hook gets its OWN savepoint. IF ONE SAVEPOINT WRAPPED THE WHOLE LOOP
    the second hook's row would be rolled back with the first hook's failure --
    one broken module would silently disable every other one."""
    seen: list[str] = []

    async def first(session: AsyncSession, device: Device, doc) -> None:
        raise RuntimeError("first is broken")

    async def second(session: AsyncSession, device: Device, doc) -> None:
        seen.append(device.hostname)

    org_id, device_id = await _org_device(admin_engine)
    with _hooks(first, second):
        status = await _collect(app_engine, org_id, device_id, tmp_path, FULL_DOC)

    assert status is JobStatus.succeeded
    assert seen == ["wks-042"]


async def test_hooks_see_the_normalized_components_not_the_raw_document(
    admin_engine, app_engine, tmp_path: Path
) -> None:
    """The call sits AFTER `ingest_inventory`, so a hook reads what inventory
    stored. IF IT SAT BEFORE, this observes 0 components -- and IPAM's hook,
    which re-reads `hardware_components` by design (§4.1), would document
    nothing on a device's first sweep."""
    counts: list[int] = []

    async def count_components(session: AsyncSession, device: Device, doc) -> None:
        counts.append(
            (
                await session.execute(
                    select(func.count())
                    .select_from(HardwareComponent)
                    .where(HardwareComponent.device_id == device.id)
                )
            ).scalar_one()
        )

    org_id, device_id = await _org_device(admin_engine)
    with _hooks(count_components):
        await _collect(app_engine, org_id, device_id, tmp_path, FULL_DOC)
    assert counts == [2]


async def test_registration_is_idempotent(admin_engine, app_engine, tmp_path: Path) -> None:
    """The API app and the gateway each register their modules, and a test
    session does both repeatedly. IF REGISTRATION APPENDED UNCONDITIONALLY this
    observes 3 runs per ingest -- which for an upsert-based hook like IPAM's is
    invisible until it is a `sighting_count` nobody can explain."""
    runs: list[int] = []

    async def once(session: AsyncSession, device: Device, doc) -> None:
        runs.append(1)

    org_id, device_id = await _org_device(admin_engine)
    with _hooks(once, once, once):
        await _collect(app_engine, org_id, device_id, tmp_path, FULL_DOC)
    assert runs == [1]


# ---------------------------------------------------------------------------
# IPAM's hook specifically (spec §4.1) -- the module registers it in
# `ipam.register(app)` and, because results complete in the GATEWAY process,
# again in `app/gateway/main.py`.
# ---------------------------------------------------------------------------


async def test_a_collect_writes_ipam_observations(
    admin_engine, app_engine, tmp_path: Path
) -> None:
    """FULL_DOC's NIC carries §14.1's `addresses[]` block and nothing else --
    the only IP-bearing NIC shape in this repository, on either side, since
    `attributes.ip` was removed from the agent and the parser (OQ-2, closed
    2026-08-05)."""
    register_inventory_hook()
    org_id, device_id = await _org_device(admin_engine)
    status = await _collect(app_engine, org_id, device_id, tmp_path, FULL_DOC)

    assert status is JobStatus.succeeded
    async with org_session(org_id, engine=app_engine) as session:
        rows = (
            (await session.execute(select(IpamObservation))).scalars().all()
        )
    assert [str(r.address) for r in rows] == ["10.1.2.42"]
    assert rows[0].source == "inventory"
    assert rows[0].device_id == device_id


async def test_a_malformed_nic_leaves_the_job_succeeded_with_zero_observations(
    admin_engine, app_engine, tmp_path: Path
) -> None:
    """THE WHOLE POINT OF THE TASK, as one assertion set: the job succeeds, the
    device's software and `hardware_components` rows are written, and IPAM
    writes nothing.

    This is layer 2 doing the work (`nic.observations_from_components` returns
    `[]`), which is why the job never needed layer 1 here -- and why layer 1 is
    proven separately, above, with a hook that really does raise.

    THE MALFORMATION IS INSIDE `addresses[]`, the key the parser actually reads.
    These two NICs used to carry `{"ip": [...]}` and `{"ip": "169.254.9.9"}`;
    after `ip` stopped being read, that fixture still produced zero observations
    and this test still passed -- but it would have been observing an unread key
    rather than layer 2's tolerance, and it would have gone on passing with
    every `try`/`except` in `nic.py` deleted.
    """
    register_inventory_hook()
    doc = copy.deepcopy(FULL_DOC)
    doc["hardware"]["components"] = [
        # Ethernet0: structurally broken -- `addresses` is not a list, and its
        # one entry is not a dict.
        {"type": "nic", "identifier": "Ethernet0", "attributes": {"addresses": "10.1.2.42/24"}},
        # Ethernet1: well-formed, but every address is undocumentable (§4.1).
        # Two different routes to "no observation", because a parser can be
        # total against one and not the other.
        {
            "type": "nic",
            "identifier": "Ethernet1",
            "attributes": {
                "addresses": [
                    {"address": "169.254.9.9/16", "family": "ipv4", "origin": "unknown"},
                    {"address": ["10.1.2.42/24"], "family": "ipv4", "origin": "dhcp"},
                ]
            },
        },
    ]
    org_id, device_id = await _org_device(admin_engine)
    status = await _collect(app_engine, org_id, device_id, tmp_path, doc)

    assert status is JobStatus.succeeded
    assert await _ingested_rows(app_engine, org_id, device_id) == (2, 2)
    async with org_session(org_id, engine=app_engine) as session:
        observations = (
            await session.execute(select(func.count()).select_from(IpamObservation))
        ).scalar_one()
    assert observations == 0
