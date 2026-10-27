"""Tests for the BCP/DR test-recording service (docs/modules/bcp/spec.md):
manual DR-test recording plus the FROZEN backup restore-test evidence
snapshot -- the differentiating feature of this module.

Mirrors `tests/test_bcp_plan_service.py`'s fixture style (throwaway
org/user/device seeded via `admin_engine`, service calls through the
RLS-constrained `org_session`) and `tests/test_backup_reporting.py`'s
org/device/repository/run/point seeding for backup evidence.
"""

import datetime as _dt
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    BackupPoint,
    BackupRepository,
    BackupRun,
    BcpTest,
    BcpTestStep,
    Device,
    Organization,
    User,
)
from app.modules.backup.reporting import backup_evidence_rows
from app.modules.bcp.errors import BcpNotFoundError
from app.modules.bcp.service import (
    create_plan,
    get_test,
    list_plan_tests,
    record_test,
    resolve_scope_devices,
    upsert_step,
)
from app.modules.bcp.service import create_test as bcp_create_test


async def _org_with_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"bcptest-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        s.add(user)
        await s.commit()
        return org.id, user.id


async def _seed_device(admin_engine: AsyncEngine, org_id: UUID, hostname: str) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        device = Device(
            org_id=org_id, hostname=hostname, os_family="linux", client_id=org_id, site_id=org_id
        )
        s.add(device)
        await s.commit()
        return device.id


async def _seed_repo(admin_engine: AsyncEngine, org_id: UUID) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        repo = BackupRepository(
            org_id=org_id,
            name="repo-1",
            storage_ref="s3://bucket/repo-1",
            encryption_key_ref="vault://key-1",
        )
        s.add(repo)
        await s.commit()
        return repo.id


async def _seed_complete_point(
    admin_engine: AsyncEngine,
    org_id: UUID,
    device_id: UUID,
    repo_id: UUID,
    *,
    restore_tested: bool,
) -> None:
    now = _dt.datetime.now(_dt.UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        run = BackupRun(
            org_id=org_id,
            device_id=device_id,
            repository_id=repo_id,
            state="success",
            bytes_protected=1000,
            bytes_stored=400,
            chunk_count=5,
            started_at=now - _dt.timedelta(minutes=10),
            finished_at=now - _dt.timedelta(minutes=9),
            actor_type="system",
        )
        s.add(run)
        await s.flush()

        point = BackupPoint(
            org_id=org_id,
            device_id=device_id,
            repository_id=repo_id,
            backup_run_id=run.id,
            taken_at=now - _dt.timedelta(minutes=9),
            manifest_ref=f"s3://bucket/repo-1/manifests/{device_id}",
            size=1000,
            state="complete",
            verified_at=now - _dt.timedelta(minutes=8),
            restore_tested_at=(now - _dt.timedelta(minutes=7)) if restore_tested else None,
        )
        s.add(point)
        await s.commit()


async def _seed_plan_with_scope(
    app_engine: AsyncEngine, org_id: UUID, actor_id: UUID, device_ids: list[UUID]
) -> UUID:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        plan = await create_plan(
            s,
            org_id=org_id,
            client_id=org_id,
            actor_id=actor_id,
            name="DR Plan",
            description="Recover the core stack",
            scope=[{"kind": "device", "id": str(d)} for d in device_ids],
            rto_minutes=60,
            rpo_minutes=15,
            procedure_refs=[],
            dependencies=[],
            review_interval_days=180,
        )
        await s.commit()
        return plan.id


async def _audit_actions(
    app_engine: AsyncEngine, org_id: UUID, target_type: str, target_id: str
) -> list[str]:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        events = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.target_type == target_type,
                    AuditEvent.target_id == target_id,
                )
                .order_by(AuditEvent.id)
            )
        ).scalars().all()
        return [e.action for e in events]


# ---------------------------------------------------------------------------
# create_test
# ---------------------------------------------------------------------------


async def test_create_test_succeeds_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    plan_id = await _seed_plan_with_scope(app_engine, org_id, actor_id, [])

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        test = await bcp_create_test(
            s,
            org_id=org_id,
            actor_id=actor_id,
            plan_id=plan_id,
            type_="tabletop",
            scheduled_for=None,
            notes="Q3 DR test",
        )
        await s.commit()
        test_id = test.id

    assert test.outcome == "pending"
    assert test.plan_id == plan_id

    actions = await _audit_actions(app_engine, org_id, "bcp_test", str(test_id))
    assert actions == ["bcp.test_created"]


async def test_create_test_missing_plan_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpNotFoundError):
            await bcp_create_test(
                s,
                org_id=org_id,
                actor_id=actor_id,
                plan_id=uuid4(),
                type_="tabletop",
                scheduled_for=None,
                notes=None,
            )


# ---------------------------------------------------------------------------
# record_test -- evidence snapshot
# ---------------------------------------------------------------------------


async def test_record_test_captures_frozen_evidence_snapshot(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    repo_id = await _seed_repo(admin_engine, org_id)

    device_a = await _seed_device(admin_engine, org_id, "device-a")
    device_b = await _seed_device(admin_engine, org_id, "device-b")
    await _seed_complete_point(admin_engine, org_id, device_a, repo_id, restore_tested=True)
    # device_b: no backups at all -> covered=False, restore_proven=False

    # foreign-org device with a backup -- must never leak into the snapshot
    other_org_id, _other_actor = await _org_with_user(admin_engine)
    other_repo_id = await _seed_repo(admin_engine, other_org_id)
    foreign_device = await _seed_device(admin_engine, other_org_id, "foreign-host")
    await _seed_complete_point(
        admin_engine, other_org_id, foreign_device, other_repo_id, restore_tested=True
    )

    plan_id = await _seed_plan_with_scope(app_engine, org_id, actor_id, [device_a, device_b])

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        test = await bcp_create_test(
            s,
            org_id=org_id,
            actor_id=actor_id,
            plan_id=plan_id,
            type_="tabletop",
            scheduled_for=None,
            notes=None,
        )
        await s.commit()
        test_id = test.id

    executed_at = _dt.datetime.now(_dt.UTC)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        recorded = await record_test(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_id,
            executed_at=executed_at,
            rto_met=True,
            rpo_met=True,
            outcome="pass",
            notes="Ran fine",
        )
        await s.commit()

    assert recorded.outcome == "pass"
    assert recorded.executed_by == actor_id
    assert recorded.rto_met is True
    assert recorded.rpo_met is True

    evidence = recorded.evidence
    assert evidence["captured_at"] == executed_at.isoformat()
    assert isinstance(evidence["captured_at"], str)

    devices_by_id = {d["device_id"]: d for d in evidence["devices"]}
    assert set(devices_by_id) == {str(device_a), str(device_b)}
    assert foreign_device not in devices_by_id
    assert str(foreign_device) not in devices_by_id

    a_row = devices_by_id[str(device_a)]
    assert a_row["covered"] is True
    assert a_row["restore_proven"] is True
    assert isinstance(a_row["last_backup_taken_at"], str)
    assert isinstance(a_row["verified_at"], str)
    assert isinstance(a_row["restore_tested_at"], str)

    b_row = devices_by_id[str(device_b)]
    assert b_row["covered"] is False
    assert b_row["restore_proven"] is False
    assert b_row["last_backup_taken_at"] is None
    assert b_row["verified_at"] is None
    assert b_row["restore_tested_at"] is None

    summary = evidence["summary"]
    assert summary["total_n"] == 2
    assert summary["covered_n"] == 1
    assert summary["restore_proven_n"] == 1

    # Round-trip: JSONB column values are plain JSON-safe types, no
    # datetime/UUID objects leaked through.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        fresh = (await s.execute(select(BcpTest).where(BcpTest.id == test_id))).scalar_one()
        for row in fresh.evidence["devices"]:
            assert isinstance(row["device_id"], str)
            for field in ("last_backup_taken_at", "verified_at", "restore_tested_at"):
                assert row[field] is None or isinstance(row[field], str)

    actions = await _audit_actions(app_engine, org_id, "bcp_test", str(test_id))
    assert actions == ["bcp.test_created", "bcp.test_recorded"]

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        event = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "bcp.test_recorded"
                )
            )
        ).scalar_one()
        assert event.meta == {
            "outcome": "pass",
            "total_n": 2,
            "covered_n": 1,
            "restore_proven_n": 1,
        }

    # --- FROZEN-SNAPSHOT PROOF ---
    # Mutate the backup world after the test was recorded: give device_b a
    # brand-new, fully-proven backup point. The already-recorded evidence
    # must NOT change -- it's a stored snapshot, not a live query.
    await _seed_complete_point(admin_engine, org_id, device_b, repo_id, restore_tested=True)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        refetched = (
            await s.execute(select(BcpTest).where(BcpTest.id == test_id))
        ).scalar_one()
        assert refetched.evidence == evidence  # byte-for-byte unchanged
        assert refetched.evidence["summary"]["restore_proven_n"] == 1
        assert refetched.evidence["summary"]["covered_n"] == 1


async def test_evidence_source_queries_org_filter_is_load_bearing(
    admin_engine: AsyncEngine,
) -> None:
    """Isolate the explicit `org_id` predicate in the snapshot's two evidence-
    source queries (`backup_evidence_rows`, `resolve_scope_devices`) so it is
    the SOLE tenant guard.

    The frozen-snapshot test above also seeds a foreign-org device, but there
    the scope-membership filter (`device_id in devices`, over a scope of only
    own-org ids) dominates -- that assertion would still pass even if an
    `org_id` predicate regressed. Here we run both queries directly on an
    RLS-bypassing admin session (the `admin_engine` role bypasses RLS -- it is
    how the cross-org seed helpers insert), removing RLS as a backstop. If
    either query's `Device.org_id == org_id` filter were deleted, the foreign
    device would leak and this test would fail.
    """
    org_a, _actor_a = await _org_with_user(admin_engine)
    org_b, _actor_b = await _org_with_user(admin_engine)

    repo_a = await _seed_repo(admin_engine, org_a)
    device_a = await _seed_device(admin_engine, org_a, "own-host")
    await _seed_complete_point(admin_engine, org_a, device_a, repo_a, restore_tested=True)

    repo_b = await _seed_repo(admin_engine, org_b)
    foreign_device = await _seed_device(admin_engine, org_b, "foreign-host")
    await _seed_complete_point(admin_engine, org_b, foreign_device, repo_b, restore_tested=True)

    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        # Evidence source, org-scoped head-on: org_b's device must be absent
        # even though RLS is not filtering here.
        rows = await backup_evidence_rows(s, org_a)
        row_device_ids = {r["device_id"] for r in rows}
        assert device_a in row_device_ids
        assert foreign_device not in row_device_ids

        # Scope resolution defensively drops a foreign device id...
        resolved_foreign = await resolve_scope_devices(
            s, org_a, [{"kind": "device", "id": str(foreign_device)}]
        )
        assert resolved_foreign == set()

        # ...while keeping an own-org device id.
        resolved_own = await resolve_scope_devices(
            s, org_a, [{"kind": "device", "id": str(device_a)}]
        )
        assert resolved_own == {device_a}


async def test_record_test_missing_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpNotFoundError):
            await record_test(
                s,
                org_id=org_id,
                actor_id=actor_id,
                test_id=uuid4(),
                executed_at=None,
                rto_met=None,
                rpo_met=None,
                outcome="pass",
                notes=None,
            )


async def test_record_test_defaults_executed_at_to_now(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    plan_id = await _seed_plan_with_scope(app_engine, org_id, actor_id, [])

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        test = await bcp_create_test(
            s,
            org_id=org_id,
            actor_id=actor_id,
            plan_id=plan_id,
            type_="tabletop",
            scheduled_for=None,
            notes=None,
        )
        await s.commit()
        test_id = test.id

    before = _dt.datetime.now(_dt.UTC)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        recorded = await record_test(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_id,
            executed_at=None,
            rto_met=None,
            rpo_met=None,
            outcome="pass",
            notes=None,
        )
        await s.commit()
    after = _dt.datetime.now(_dt.UTC)

    assert recorded.executed_at is not None
    assert before <= recorded.executed_at <= after
    assert recorded.evidence["captured_at"] == recorded.executed_at.isoformat()


# ---------------------------------------------------------------------------
# upsert_step
# ---------------------------------------------------------------------------


async def test_upsert_step_inserts_then_updates(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    plan_id = await _seed_plan_with_scope(app_engine, org_id, actor_id, [])

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        test = await bcp_create_test(
            s,
            org_id=org_id,
            actor_id=actor_id,
            plan_id=plan_id,
            type_="tabletop",
            scheduled_for=None,
            notes=None,
        )
        await s.commit()
        test_id = test.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        step = await upsert_step(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_id,
            ordinal=1,
            description="Failover DB",
            result="pending",
            evidence_ref=None,
        )
        await s.commit()
        step_id = step.id

    assert step.ordinal == 1
    assert step.result == "pending"

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        updated = await upsert_step(
            s,
            org_id=org_id,
            actor_id=actor_id,
            test_id=test_id,
            ordinal=1,
            description="Failover DB",
            result="pass",
            evidence_ref="s3://evidence/1",
        )
        await s.commit()

    assert updated.id == step_id  # same row, updated in place
    assert updated.result == "pass"
    assert updated.evidence_ref == "s3://evidence/1"

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = (
            await s.execute(select(BcpTestStep).where(BcpTestStep.test_id == test_id))
        ).scalars().all()
        assert len(rows) == 1

    # Each upsert (insert + in-place update) audits `bcp.test_step_recorded`
    # against the parent test, alongside the test's own `bcp.test_created`
    # (invariant #4: every privileged mutation is audited).
    actions = await _audit_actions(app_engine, org_id, "bcp_test", str(test_id))
    assert actions == [
        "bcp.test_created",
        "bcp.test_step_recorded",
        "bcp.test_step_recorded",
    ]


async def test_upsert_step_missing_test_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpNotFoundError):
            await upsert_step(
                s,
                org_id=org_id,
                actor_id=actor_id,
                test_id=uuid4(),
                ordinal=1,
                description="x",
                result="pending",
                evidence_ref=None,
            )


# ---------------------------------------------------------------------------
# get_test / list_plan_tests + tenancy
# ---------------------------------------------------------------------------


async def test_get_test_and_list_plan_tests(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    plan_id = await _seed_plan_with_scope(app_engine, org_id, actor_id, [])

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        test = await bcp_create_test(
            s,
            org_id=org_id,
            actor_id=actor_id,
            plan_id=plan_id,
            type_="tabletop",
            scheduled_for=None,
            notes=None,
        )
        await s.commit()
        test_id = test.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        fetched = await get_test(s, org_id, test_id)
        assert fetched is not None
        assert fetched.id == test_id

        all_tests = await list_plan_tests(s, org_id, plan_id)
        assert [t.id for t in all_tests] == [test_id]


async def test_get_test_missing_returns_none(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _actor_id = await _org_with_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await get_test(s, org_id, uuid4())
        assert result is None


async def test_another_orgs_test_is_hidden_by_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, _actor_b = await _org_with_user(admin_engine)
    plan_a = await _seed_plan_with_scope(app_engine, org_a, actor_a, [])

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        test = await bcp_create_test(
            s,
            org_id=org_a,
            actor_id=actor_a,
            plan_id=plan_a,
            type_="tabletop",
            scheduled_for=None,
            notes=None,
        )
        await s.commit()
        test_id = test.id

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        fetched = await get_test(s, org_b, test_id)
        assert fetched is None

        with pytest.raises(BcpNotFoundError):
            await record_test(
                s,
                org_id=org_b,
                actor_id=actor_a,
                test_id=test_id,
                executed_at=None,
                rto_met=None,
                rpo_met=None,
                outcome="pass",
                notes=None,
            )
