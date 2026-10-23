"""Approve all (GPO MSI spec 2026-09-30 §2.5): only the LISTED machines that
are still pending, one audit record each under a shared bulk_id, at most
1,000, and only with the digest of the list that was shown."""

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Device
from app.services.device_review import (
    APPROVE_ALL_MAX,
    ApproveAllRefused,
    approve_all,
    review_list_digest,
)
from tests.ipam_fixtures import seed_device, seed_org

URL = "/api/v1/devices/review/approve-all"


async def _pending(api_env, org_id: UUID, n: int) -> list[UUID]:
    return [
        await seed_device(
            api_env.admin_engine,
            org_id,
            hostname=f"DESKTOP-{i:04d}",
            review_state="pending",
            link_joined_from="10.50.0.9",
            link_joined_at=datetime.now(UTC),
        )
        for i in range(n)
    ]


async def _tech(api_env, org_id: UUID) -> dict[str, str]:
    return await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))


async def _states(api_env, ids: list[UUID]) -> dict[UUID, str]:
    async with AsyncSession(api_env.admin_engine) as session:
        rows = (
            await session.execute(select(Device.id, Device.review_state).where(Device.id.in_(ids)))
        ).all()
    return {row[0]: row[1] for row in rows}


def _body(ids: list[UUID]) -> dict[str, object]:
    return {"device_ids": [str(i) for i in ids], "list_digest": review_list_digest(ids)}


def test_the_digest_is_order_and_duplicate_blind_and_is_the_consoles() -> None:
    a, b = UUID(int=2), UUID(int=1)
    assert review_list_digest([a, b, a]) == review_list_digest([b, a])
    assert review_list_digest([a, b]) == hashlib.sha256(f"{b}\n{a}".encode()).hexdigest()
    # frontend/src/modules/devices/DevicesApproveAll.test.tsx pins the same value.
    assert review_list_digest([a, b]) == (
        "7a70c782c5d30f61a8f57b905eaa11c41ec8ffeafaa3a0e99c4fca60044a28d4"
    )


async def test_it_approves_the_listed_pending_machines_one_audit_each(api_env) -> None:
    org = await seed_org(api_env.admin_engine, "aa")
    listed = await _pending(api_env, org.org_id, 3)
    response = await api_env.client.post(
        URL, json=_body(listed), headers=await _tech(api_env, org.org_id)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["approved"] == 3 and body["not_pending"] == 0 and body["still_waiting"] == 0
    assert set((await _states(api_env, listed)).values()) == {"approved"}
    async with AsyncSession(api_env.admin_engine) as session:
        audits = list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org.org_id,
                        AuditEvent.action == "device.review_approved",
                    )
                )
            ).scalars()
        )
    assert sorted(a.target_id for a in audits) == sorted(str(i) for i in listed)
    assert {a.meta["bulk_id"] for a in audits} == {body["bulk_id"]}


async def test_a_machine_that_joined_after_the_list_stays_pending_and_is_counted(api_env) -> None:
    org = await seed_org(api_env.admin_engine, "aa")
    listed = await _pending(api_env, org.org_id, 2)
    [late] = await _pending(api_env, org.org_id, 1)
    body = (
        await api_env.client.post(URL, json=_body(listed), headers=await _tech(api_env, org.org_id))
    ).json()
    assert body["approved"] == 2 and body["still_waiting"] == 1
    assert (await _states(api_env, [late]))[late] == "pending"


async def test_a_listed_machine_no_longer_pending_is_left_alone_and_counted(api_env) -> None:
    org = await seed_org(api_env.admin_engine, "aa")
    listed = await _pending(api_env, org.org_id, 2)
    headers = await _tech(api_env, org.org_id)
    rejected = await api_env.client.post(
        f"/api/v1/devices/{listed[0]}/review/reject", headers=headers
    )
    assert rejected.status_code == 200
    body = (await api_env.client.post(URL, json=_body(listed), headers=headers)).json()
    assert body["approved"] == 1 and body["not_pending"] == 1
    assert (await _states(api_env, listed))[listed[0]] == "rejected"


async def test_another_organisations_machine_is_never_approved(api_env) -> None:
    mine = await seed_org(api_env.admin_engine, "aa")
    theirs = await seed_org(api_env.admin_engine, "bb")
    [own] = await _pending(api_env, mine.org_id, 1)
    [foreign] = await _pending(api_env, theirs.org_id, 1)
    body = (
        await api_env.client.post(
            URL, json=_body([own, foreign]), headers=await _tech(api_env, mine.org_id)
        )
    ).json()
    assert body["approved"] == 1 and body["not_pending"] == 1
    assert (await _states(api_env, [foreign]))[foreign] == "pending"


async def test_a_digest_that_is_not_the_lists_is_refused_before_anything_changes(api_env) -> None:
    org = await seed_org(api_env.admin_engine, "aa")
    listed = await _pending(api_env, org.org_id, 2)
    body = {"device_ids": [str(i) for i in listed], "list_digest": review_list_digest(listed[:1])}
    response = await api_env.client.post(URL, json=body, headers=await _tech(api_env, org.org_id))
    assert response.status_code == 422
    assert "not the list that was shown" in response.json()["detail"]
    assert set((await _states(api_env, listed)).values()) == {"pending"}


async def test_the_service_refuses_more_than_the_limit_on_its_own(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The schema refuses 1,001 ids first (below); the service's own check
    stays, for any caller that is not the route."""
    org = await seed_org(admin_engine, "aa")
    ids = [uuid4() for _ in range(APPROVE_ALL_MAX + 1)]
    with pytest.raises(ApproveAllRefused, match="at most 1000"):
        async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
            await approve_all(
                session,
                org_id=org.org_id,
                actor_id=uuid4(),
                device_ids=ids,
                list_digest=review_list_digest(ids),
                now=datetime.now(UTC),
            )


async def test_it_needs_devices_review(api_env) -> None:
    org = await seed_org(api_env.admin_engine, "aa")
    listed = await _pending(api_env, org.org_id, 1)
    reader = await api_env.headers(org.org_id, await api_env.create_user(org.org_id, "read-only"))
    assert (await api_env.client.post(URL, json=_body(listed), headers=reader)).status_code == 403
    assert (await _states(api_env, listed))[listed[0]] == "pending"


async def test_more_than_the_limit_is_a_422_from_the_schema(api_env) -> None:
    """1,001 ids never reach the service: the body itself is refused."""
    org = await seed_org(api_env.admin_engine, "aa")
    ids = [uuid4() for _ in range(APPROVE_ALL_MAX + 1)]
    response = await api_env.client.post(
        URL, json=_body(ids), headers=await _tech(api_env, org.org_id)
    )
    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "device_ids"]
    assert error["type"] == "too_long"


async def test_exactly_the_limit_is_accepted(api_env) -> None:
    org = await seed_org(api_env.admin_engine, "aa")
    ids = [uuid4() for _ in range(APPROVE_ALL_MAX)]
    response = await api_env.client.post(
        URL, json=_body(ids), headers=await _tech(api_env, org.org_id)
    )
    assert response.status_code == 200, response.text
    assert response.json()["not_pending"] == APPROVE_ALL_MAX


class _LockCountingSession:
    """Counts `pg_advisory_xact_lock` statements actually sent (as
    tests/test_audit_batch.py does)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.locks = 0

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if "pg_advisory_xact_lock" in str(statement):
            self.locks += 1
        return await self._session.execute(statement, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


async def test_the_bulk_approval_takes_the_audit_lock_once_and_keeps_one_chain(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "aa")
    listed = [
        await seed_device(admin_engine, org.org_id, hostname=f"PC-{i}", review_state="pending")
        for i in range(12)
    ]
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        counter = _LockCountingSession(session)
        outcome = await approve_all(
            counter,  # type: ignore[arg-type]
            org_id=org.org_id,
            actor_id=uuid4(),
            device_ids=listed,
            list_digest=review_list_digest(listed),
            now=datetime.now(UTC),
        )
    assert outcome.approved == 12
    assert counter.locks == 1
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        audits = list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.action == "device.review_approved")
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )
        verified = await audit_service.verify(session, org.org_id)
    assert verified.ok and verified.broken_at is None
    # Still one record per machine, all carrying the one bulk_id.
    assert sorted(a.target_id for a in audits) == sorted(str(i) for i in listed)
    assert {a.meta["bulk_id"] for a in audits} == {str(outcome.bulk_id)}
    assert {a.meta["hostname"] for a in audits} == {f"PC-{i}" for i in range(12)}


async def _waiting_on_locks(admin_engine: AsyncEngine) -> int:
    """Backends in this test database blocked by another's lock. Read through
    `pg_blocking_pids`, which, unlike pg_stat_activity's wait columns, is not
    hidden from a role that is not a superuser."""
    async with AsyncSession(admin_engine) as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND cardinality(pg_blocking_pids(pid)) > 0"
                    )
                )
            ).scalar_one()
        )


async def _until_waiting(admin_engine: AsyncEngine, n: int) -> None:
    for _ in range(500):
        if await _waiting_on_locks(admin_engine) >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{n} requests never queued on the device row")


@pytest.mark.parametrize("first", ["approve_all", "reject"])
async def test_approve_all_and_a_single_reject_racing_end_consistently(api_env, first) -> None:
    """Both requests queue on the same device row, held by a third
    transaction, then are let go together. The machine ends approved or
    rejected -- never both, never neither -- with exactly one matching audit
    record; Approve all always answers 200, and the reject 200 or, if the
    approval won, the ordinary 409 'not waiting for review'. No 500, no
    deadlock."""
    org = await seed_org(api_env.admin_engine, "aa")
    [target, other] = await _pending(api_env, org.org_id, 2)
    headers = await _tech(api_env, org.org_id)

    def approve() -> Any:
        return api_env.client.post(URL, json=_body([target, other]), headers=headers)

    def reject() -> Any:
        return api_env.client.post(f"/api/v1/devices/{target}/review/reject", headers=headers)

    order = [approve, reject] if first == "approve_all" else [reject, approve]
    async with AsyncSession(api_env.admin_engine) as blocker:
        async with blocker.begin():
            await blocker.execute(select(Device).where(Device.id == target).with_for_update())
            tasks = []
            for n, send in enumerate(order, start=1):
                tasks.append(asyncio.create_task(send()))
                await _until_waiting(api_env.admin_engine, n)
    responses = await asyncio.wait_for(asyncio.gather(*tasks), 30)
    by_name = dict(
        zip(
            ["approve_all", "reject"] if first == "approve_all" else ["reject", "approve_all"],
            responses,
            strict=True,
        )
    )

    approved_all, rejected = by_name["approve_all"], by_name["reject"]
    assert approved_all.status_code == 200, approved_all.text
    assert rejected.status_code in (200, 409), rejected.text
    state = (await _states(api_env, [target]))[target]
    async with AsyncSession(api_env.admin_engine) as session:
        actions = sorted(
            (
                await session.execute(
                    select(AuditEvent.action).where(
                        AuditEvent.org_id == org.org_id,
                        AuditEvent.target_id == str(target),
                        AuditEvent.action.in_(["device.review_approved", "device.review_rejected"]),
                    )
                )
            ).scalars()
        )
    if rejected.status_code == 200:
        assert state == "rejected"
        assert approved_all.json()["not_pending"] == 1
        assert actions == ["device.review_rejected"]
    else:
        assert state == "approved"
        assert "not waiting for review" in rejected.json()["detail"]
        assert approved_all.json()["approved"] == 2
        assert actions == ["device.review_approved"]
    # The other listed machine is approved either way.
    assert (await _states(api_env, [other]))[other] == "approved"
