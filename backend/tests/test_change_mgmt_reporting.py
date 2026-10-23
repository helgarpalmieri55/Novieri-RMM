"""Tests for SOC 2 evidence export + change calendar with conflict detection
(docs/modules/change_management/spec.md §4). Mirrors
`tests/test_provisioning_reporting.py`'s fixture style:
`app/modules/change_mgmt/reporting.py` is pure read-only aggregation -- these
tests drive real changes through `create_change`/`transition_change`/
`decide_change`/`add_link` so genuine `change.*` audit events land in
`audit_events`, then assert the reporting functions surface exactly those
rows, org-scoped.
"""

import datetime as _dt
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    Deployment,
    Device,
    Organization,
    Package,
    Patch,
    PatchRun,
    User,
)
from app.models import Job as JobModel
from app.modules.change_mgmt.links import add_link
from app.modules.change_mgmt.reporting import (
    _find_conflicts,
    change_calendar,
    change_evidence,
    fleet_change_evidence,
)
from app.modules.change_mgmt.service import create_change, decide_change, transition_change
from app.modules.change_mgmt.ticketing import NullChangeTicketAdapter


async def _org_with_two_users(admin_engine: AsyncEngine) -> tuple[UUID, UUID, UUID]:
    """Returns (org_id, requester_id, approver_id)."""
    slug = f"chrpt-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        requester = User(
            org_id=org.id, external_subject=f"req-{slug}", email=f"req-{slug}@example.com"
        )
        approver = User(
            org_id=org.id, external_subject=f"apr-{slug}", email=f"apr-{slug}@example.com"
        )
        s.add_all([requester, approver])
        await s.commit()
        return org.id, requester.id, approver.id


async def _seed_deployment(admin_engine: AsyncEngine, org_id: UUID) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        device = Device(org_id=org_id, hostname="h1", os_family="windows")
        s.add(device)
        await s.flush()
        package = Package(
            org_id=org_id, name="Acme", version="1.0", os_family="windows", installer_type="msi"
        )
        s.add(package)
        await s.flush()
        deployment = Deployment(
            org_id=org_id,
            package_id=package.id,
            action="install",
            target_type="device",
            target_id=device.id,
            state="pending",
        )
        s.add(deployment)
        await s.commit()
        return deployment.id


async def _seed_patch_run(admin_engine: AsyncEngine, org_id: UUID) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        device = Device(org_id=org_id, hostname="h2", os_family="windows")
        s.add(device)
        await s.flush()
        patch = Patch(
            org_id=org_id,
            os_family="windows",
            source="windows_update",
            identifier="KB1",
            title="Cum Update",
            severity="critical",
        )
        s.add(patch)
        await s.flush()
        job = JobModel(
            org_id=org_id,
            type="patch.apply",
            target_type="device",
            target_id=device.id,
            params={},
            nonce="n",
            issued_at=_dt.datetime.now(_dt.UTC),
            expires_at=_dt.datetime.now(_dt.UTC),
            signature="fake-signature",
        )
        s.add(job)
        await s.flush()
        run = PatchRun(org_id=org_id, job_id=job.id, device_id=device.id)
        s.add(run)
        await s.commit()
        return run.id


async def _audit_ids_for_change(
    app_engine: AsyncEngine, org_id: UUID, change_id: UUID
) -> set[UUID]:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = (
            await s.execute(
                select(AuditEvent.id).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.target_type == "change",
                    AuditEvent.target_id == str(change_id),
                )
            )
        ).scalars().all()
        return set(rows)


# ---------------------------------------------------------------------------
# change_evidence
# ---------------------------------------------------------------------------


async def test_change_evidence_returns_change_approvals_links_and_scoped_audit_refs(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, requester_id, approver_id = await _org_with_two_users(admin_engine)
    ticket = NullChangeTicketAdapter()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        change = await create_change(
            s,
            org_id=org_id,
            actor_id=requester_id,
            type_="emergency",
            title="Emergency patch",
            description="Sev1 fix",
            risk="high",
            affected=[{"kind": "device", "id": "dev-1"}],
            window_start=_dt.datetime(2026, 8, 1, 10, 0, tzinfo=_dt.UTC),
            window_end=_dt.datetime(2026, 8, 1, 12, 0, tzinfo=_dt.UTC),
            rollback_plan="Revert",
        )
        await s.commit()
        change_id = change.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await transition_change(
            s, ticket, org_id=org_id, actor_id=requester_id, change_id=change_id, action="submit"
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await transition_change(
            s, ticket, org_id=org_id, actor_id=approver_id, change_id=change_id, action="begin"
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        approval1 = await decide_change(
            s,
            ticket,
            org_id=org_id,
            actor_id=approver_id,
            change_id=change_id,
            decision="approved",
            comment="first look",
        )
        await s.commit()
        approval1_id = approval1.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        approval2 = await decide_change(
            s,
            ticket,
            org_id=org_id,
            actor_id=approver_id,
            change_id=change_id,
            decision="approved",
            comment="second look",
        )
        await s.commit()
        approval2_id = approval2.id

    deployment_id = await _seed_deployment(admin_engine, org_id)
    patch_run_id = await _seed_patch_run(admin_engine, org_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        link1 = await add_link(
            s,
            org_id=org_id,
            actor_id=approver_id,
            change_id=change_id,
            action_type="deployment",
            action_id=deployment_id,
        )
        await s.commit()
        link1_id = link1.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        link2 = await add_link(
            s,
            org_id=org_id,
            actor_id=approver_id,
            change_id=change_id,
            action_type="patch_run",
            action_id=patch_run_id,
        )
        await s.commit()
        link2_id = link2.id

    # A second, unrelated change in the same org -- its audit events must
    # never leak into the first change's evidence.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        other_change = await create_change(
            s,
            org_id=org_id,
            actor_id=requester_id,
            type_="normal",
            title="Unrelated",
            description="D",
            risk="low",
            affected=[],
            window_start=None,
            window_end=None,
            rollback_plan=None,
        )
        await s.commit()
        other_change_id = other_change.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await transition_change(
            s,
            ticket,
            org_id=org_id,
            actor_id=requester_id,
            change_id=other_change_id,
            action="submit",
        )
        await s.commit()

    expected_audit_ids = await _audit_ids_for_change(app_engine, org_id, change_id)
    other_audit_ids = await _audit_ids_for_change(app_engine, org_id, other_change_id)
    assert expected_audit_ids  # create/submit/begin/approve/approve/link/link
    assert not (expected_audit_ids & other_audit_ids)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        evidence = await change_evidence(s, org_id, change_id)

    assert evidence["change"]["id"] == change_id
    assert evidence["change"]["type"] == "emergency"
    assert evidence["change"]["title"] == "Emergency patch"
    assert evidence["change"]["risk"] == "high"
    assert evidence["change"]["window_start"] == _dt.datetime(2026, 8, 1, 10, 0, tzinfo=_dt.UTC)
    assert evidence["change"]["window_end"] == _dt.datetime(2026, 8, 1, 12, 0, tzinfo=_dt.UTC)
    assert evidence["change"]["state"] == "implementing"
    assert evidence["change"]["created_by"] == requester_id
    assert evidence["change"]["created_at"] is not None

    assert [a["id"] for a in evidence["approvals"]] == [approval1_id, approval2_id]
    assert evidence["approvals"][0]["approver_id"] == approver_id
    assert evidence["approvals"][0]["decision"] == "approved"
    assert evidence["approvals"][0]["comment"] == "first look"
    assert evidence["approvals"][0]["is_retrospective"] is True
    assert evidence["approvals"][1]["comment"] == "second look"

    link_ids = {link["id"] for link in evidence["links"]}
    assert link_ids == {link1_id, link2_id}
    for link in evidence["links"]:
        assert link["linked_by"] == approver_id
        if link["id"] == link1_id:
            assert link["action_type"] == "deployment"
            assert link["action_id"] == deployment_id
        else:
            assert link["action_type"] == "patch_run"
            assert link["action_id"] == patch_run_id

    assert set(evidence["audit_refs"]) == expected_audit_ids
    assert not (set(evidence["audit_refs"]) & other_audit_ids)


async def test_change_evidence_missing_change_returns_minimal_shape(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _requester_id, _approver_id = await _org_with_two_users(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        evidence = await change_evidence(s, org_id, uuid4())

    assert evidence["change"] is None
    assert evidence["approvals"] == []
    assert evidence["links"] == []
    assert evidence["audit_refs"] == []


# ---------------------------------------------------------------------------
# fleet_change_evidence
# ---------------------------------------------------------------------------


async def test_fleet_change_evidence_returns_one_record_per_change(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, requester_id, _approver_id = await _org_with_two_users(admin_engine)

    change_ids = []
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        for i in range(3):
            change = await create_change(
                s,
                org_id=org_id,
                actor_id=requester_id,
                type_="normal",
                title=f"Change {i}",
                description="D",
                risk="low",
                affected=[],
                window_start=None,
                window_end=None,
                rollback_plan=None,
            )
            change_ids.append(change.id)
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        fleet = await fleet_change_evidence(s, org_id)

    assert len(fleet) == 3
    assert {record["change"]["id"] for record in fleet} == set(change_ids)


# ---------------------------------------------------------------------------
# change_calendar: conflict detection
# ---------------------------------------------------------------------------


async def test_change_calendar_conflicts_and_range_filtering(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, requester_id, _approver_id = await _org_with_two_users(admin_engine)
    t0 = _dt.datetime(2026, 9, 1, 10, 0, tzinfo=_dt.UTC)

    async def _make(
        title: str, start: _dt.datetime, end: _dt.datetime, affected: list
    ) -> UUID:
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
            change = await create_change(
                s,
                org_id=org_id,
                actor_id=requester_id,
                type_="normal",
                title=title,
                description="D",
                risk="low",
                affected=affected,
                window_start=start,
                window_end=end,
                rollback_plan=None,
            )
            await s.commit()
            return change.id

    # A, B overlap in time and share dev-1 -> conflict.
    a_id = await _make(
        "A", t0, t0 + _dt.timedelta(hours=2), [{"kind": "device", "id": "dev-1"}]
    )
    b_id = await _make(
        "B",
        t0 + _dt.timedelta(hours=1),
        t0 + _dt.timedelta(hours=3),
        [{"kind": "device", "id": "dev-1"}],
    )
    # C overlaps A/B in time but shares no asset -> no conflict.
    c_id = await _make(
        "C",
        t0 + _dt.timedelta(hours=1),
        t0 + _dt.timedelta(hours=3),
        [{"kind": "device", "id": "dev-2"}],
    )
    # D shares dev-1 with A but its window is disjoint in time -> no conflict.
    d_id = await _make(
        "D",
        t0 + _dt.timedelta(hours=10),
        t0 + _dt.timedelta(hours=12),
        [{"kind": "device", "id": "dev-1"}],
    )
    # E is entirely outside the queried range -> excluded from entries.
    await _make(
        "E",
        t0 - _dt.timedelta(hours=10),
        t0 - _dt.timedelta(hours=8),
        [{"kind": "device", "id": "dev-1"}],
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        calendar = await change_calendar(
            s,
            org_id,
            frm=t0 - _dt.timedelta(hours=1),
            to=t0 + _dt.timedelta(hours=13),
        )

    entry_ids = {entry["change_id"] for entry in calendar["entries"]}
    assert entry_ids == {a_id, b_id, c_id, d_id}

    assert calendar["conflicts"] == [{"change_a": a_id, "change_b": b_id}]


async def test_change_calendar_excludes_another_orgs_change_under_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, requester_a, _ = await _org_with_two_users(admin_engine)
    org_b, requester_b, _ = await _org_with_two_users(admin_engine)
    t0 = _dt.datetime(2026, 9, 1, 10, 0, tzinfo=_dt.UTC)

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        change_a = await create_change(
            s,
            org_id=org_a,
            actor_id=requester_a,
            type_="normal",
            title="A",
            description="D",
            risk="low",
            affected=[],
            window_start=t0,
            window_end=t0 + _dt.timedelta(hours=1),
            rollback_plan=None,
        )
        await s.commit()

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        change_b = await create_change(
            s,
            org_id=org_b,
            actor_id=requester_b,
            type_="normal",
            title="B",
            description="D",
            risk="low",
            affected=[],
            window_start=t0,
            window_end=t0 + _dt.timedelta(hours=1),
            rollback_plan=None,
        )
        await s.commit()

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        calendar_b = await change_calendar(
            s,
            org_b,
            frm=t0 - _dt.timedelta(hours=1),
            to=t0 + _dt.timedelta(hours=2),
        )

    entry_ids_b = {entry["change_id"] for entry in calendar_b["entries"]}
    assert entry_ids_b == {change_b.id}
    assert change_a.id not in entry_ids_b


# ---------------------------------------------------------------------------
# tenancy: change_evidence / fleet_change_evidence
# ---------------------------------------------------------------------------


async def test_change_evidence_is_empty_for_another_orgs_change_under_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, requester_a, _ = await _org_with_two_users(admin_engine)
    org_b, _requester_b, _ = await _org_with_two_users(admin_engine)
    ticket = NullChangeTicketAdapter()

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        change = await create_change(
            s,
            org_id=org_a,
            actor_id=requester_a,
            type_="normal",
            title="A-only",
            description="D",
            risk="low",
            affected=[],
            window_start=None,
            window_end=None,
            rollback_plan=None,
        )
        await s.commit()
        change_id = change.id

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        await transition_change(
            s, ticket, org_id=org_a, actor_id=requester_a, change_id=change_id, action="submit"
        )
        await s.commit()

    # From org_b's perspective the change simply doesn't exist.
    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        evidence = await change_evidence(s, org_b, change_id)
    assert evidence["change"] is None
    assert evidence["approvals"] == []
    assert evidence["links"] == []
    assert evidence["audit_refs"] == []

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        fleet = await fleet_change_evidence(s, org_b)
    assert fleet == []


# ---------------------------------------------------------------------------
# _find_conflicts: pure helper, no DB
# ---------------------------------------------------------------------------


def test_find_conflicts_pure_helper() -> None:
    id1, id2, id3, id4 = uuid4(), uuid4(), uuid4(), uuid4()
    entries = [
        (id1, _dt.datetime(2026, 1, 1, 10, 0), _dt.datetime(2026, 1, 1, 12, 0), {("device", "d1")}),
        # overlaps id1 in time, shares d1 -> conflict
        (id2, _dt.datetime(2026, 1, 1, 11, 0), _dt.datetime(2026, 1, 1, 13, 0), {("device", "d1")}),
        # overlaps id1/id2 in time but different asset -> no conflict
        (id3, _dt.datetime(2026, 1, 1, 11, 0), _dt.datetime(2026, 1, 1, 13, 0), {("device", "d2")}),
        # shares d1 with id1 but disjoint in time -> no conflict
        (id4, _dt.datetime(2026, 1, 1, 20, 0), _dt.datetime(2026, 1, 1, 22, 0), {("device", "d1")}),
    ]

    assert _find_conflicts(entries) == [(id1, id2)]


def test_find_conflicts_no_entries_no_conflicts() -> None:
    assert _find_conflicts([]) == []


def test_find_conflicts_touching_windows_do_not_overlap() -> None:
    id1, id2 = uuid4(), uuid4()
    entries = [
        (id1, _dt.datetime(2026, 1, 1, 10, 0), _dt.datetime(2026, 1, 1, 11, 0), {("device", "d1")}),
        # starts exactly when id1 ends -> not overlapping (strict inequality)
        (id2, _dt.datetime(2026, 1, 1, 11, 0), _dt.datetime(2026, 1, 1, 12, 0), {("device", "d1")}),
    ]
    assert _find_conflicts(entries) == []
