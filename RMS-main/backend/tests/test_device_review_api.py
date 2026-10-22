"""Approve and Reject (install-link spec §1.5, §4; plan D1, D2)."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, AuditEvent, Device, DomainEvent, Job
from tests.ipam_fixtures import seed_agent, seed_device, seed_org


async def _pending(api_env, *, capabilities: dict | None = None, agent: bool = True):
    org = await seed_org(api_env.admin_engine, "rv")
    device_id = await seed_device(
        api_env.admin_engine,
        org.org_id,
        hostname="DESKTOP-7Q2M4KD",
        review_state="pending",
        link_joined_from="10.50.0.9",
        link_joined_at=datetime.now(UTC),
    )
    if agent:
        await seed_agent(
            api_env.admin_engine,
            org.org_id,
            device_id,
            capabilities=capabilities if capabilities is not None else {"agent.uninstall": True},
        )
    return org.org_id, device_id


async def _as(api_env, org_id, role: str) -> dict[str, str]:
    return await api_env.headers(org_id, await api_env.create_user(org_id, role))


async def _actions(api_env, org_id) -> list[AuditEvent]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org_id, AuditEvent.action.like("device.%"))
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )


async def test_review_is_technician_tier(api_env) -> None:
    org_id, device_id = await _pending(api_env)
    for verb in ("approve", "reject"):
        response = await api_env.client.post(
            f"/api/v1/devices/{device_id}/review/{verb}",
            headers=await _as(api_env, org_id, "read-only"),
        )
        assert response.status_code == 403, verb
    approved = await api_env.client.post(
        f"/api/v1/devices/{device_id}/review/approve",
        headers=await _as(api_env, org_id, "technician"),
    )
    assert approved.status_code == 200, approved.text


async def test_approve_makes_it_an_ordinary_device_and_is_audited(api_env) -> None:
    org_id, device_id = await _pending(api_env)
    headers = await _as(api_env, org_id, "technician")
    body = (
        await api_env.client.post(f"/api/v1/devices/{device_id}/review/approve", headers=headers)
    ).json()
    assert body["id"] == str(device_id) and body["review_state"] == "approved"
    # The history of how it joined survives the approval.
    assert body["link_joined_from"] == "10.50.0.9"
    [row] = await _actions(api_env, org_id)
    assert row.action == "device.review_approved" and row.target_id == str(device_id)

    again = await api_env.client.post(
        f"/api/v1/devices/{device_id}/review/approve", headers=headers
    )
    assert again.status_code == 409
    assert again.json()["detail"] == "this computer is not waiting for review"

    # And the gate now lets anything through: `hello` was refused a moment ago.
    admin = await _as(api_env, org_id, "org-admin")
    job = await api_env.client.post(
        "/api/v1/jobs",
        json={"type": "hello", "device_id": str(device_id), "params": {}},
        headers=admin,
    )
    assert job.status_code == 201, job.text


async def test_reject_dispatches_a_signed_uninstall_and_revokes_nothing_yet(api_env) -> None:
    org_id, device_id = await _pending(api_env)
    response = await api_env.client.post(
        f"/api/v1/devices/{device_id}/review/reject",
        headers=await _as(api_env, org_id, "technician"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["uninstall"] == "dispatched" and body["job_id"] is not None
    assert "uninstall itself" in body["message"]

    async with AsyncSession(api_env.admin_engine) as session:
        device = await session.get(Device, device_id)
        agent = (
            await session.execute(select(Agent).where(Agent.device_id == device_id))
        ).scalar_one()
        job = await session.get(Job, UUID(body["job_id"]))
    assert (
        device is not None and device.review_state == "rejected" and device.rejected_at is not None
    )
    assert device.status != "decommissioned"
    # D2: revoking now would cut the connection the uninstall travels over.
    assert agent.cert_revoked is False
    assert job is not None and job.type == "agent.uninstall" and job.params == {}
    assert job.signature and job.target_id == device_id
    # 24 hours to receive it: an agent that is off tonight still gets it tomorrow.
    assert (job.expires_at - job.issued_at).total_seconds() == 24 * 3600

    [row] = await _actions(api_env, org_id)
    assert row.action == "device.review_rejected"
    assert row.meta["uninstall"] == "dispatched" and row.meta["job_id"] == body["job_id"]

    # Gone from the list at once (Task 8), and nothing but the uninstall may
    # reach it any more.
    listing = await api_env.client.get(
        "/api/v1/devices", headers=await _as(api_env, org_id, "read-only")
    )
    assert str(device_id) not in listing.text
    admin = await _as(api_env, org_id, "org-admin")
    job_refused = await api_env.client.post(
        "/api/v1/jobs",
        json={"type": "inventory.collect", "device_id": str(device_id), "params": {}},
        headers=admin,
    )
    assert job_refused.status_code == 409


async def test_an_agent_too_old_to_uninstall_is_revoked_now_and_the_tech_is_told(api_env) -> None:
    org_id, device_id = await _pending(api_env, capabilities={"inventory.collect": True})
    response = await api_env.client.post(
        f"/api/v1/devices/{device_id}/review/reject",
        headers=await _as(api_env, org_id, "technician"),
    )
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["uninstall"] == "manual" and body["job_id"] is None
    assert "remove the agent from the computer by hand" in body["message"]

    async with AsyncSession(api_env.admin_engine) as session:
        device = await session.get(Device, device_id)
        agent = (
            await session.execute(select(Agent).where(Agent.device_id == device_id))
        ).scalar_one()
        jobs = (
            (await session.execute(select(Job).where(Job.target_id == device_id))).scalars().all()
        )
    assert device is not None and device.status == "decommissioned"
    assert device.rejection_outcome == "agent_cannot_uninstall"
    assert agent.cert_revoked is True and jobs == []
    assert api_env.app.state.ca.is_revoked(agent.cert_serial)
    assert [r.action for r in await _actions(api_env, org_id)] == [
        "device.review_rejected",
        "device.rejection_completed",
    ]


async def test_only_a_pending_device_can_be_rejected_and_other_orgs_are_404(api_env) -> None:
    org_id, device_id = await _pending(api_env)
    headers = await _as(api_env, org_id, "technician")
    other = await seed_device(api_env.admin_engine, org_id, hostname="approved-pc")
    assert (
        await api_env.client.post(f"/api/v1/devices/{other}/review/reject", headers=headers)
    ).status_code == 409
    assert (
        await api_env.client.post(f"/api/v1/devices/{uuid4()}/review/reject", headers=headers)
    ).status_code == 404

    stranger_org = (await seed_org(api_env.admin_engine, "rv-b")).org_id
    stranger = await _as(api_env, stranger_org, "technician")
    assert (
        await api_env.client.post(f"/api/v1/devices/{device_id}/review/reject", headers=stranger)
    ).status_code == 404


async def _user_id(api_env, org_id, sub: str) -> UUID:
    from app.models import User

    async with AsyncSession(api_env.admin_engine) as session:
        return (
            await session.execute(
                select(User.id).where(User.org_id == org_id, User.external_subject == sub)
            )
        ).scalar_one()


async def test_approve_from_another_org_is_404(api_env) -> None:
    org_id, device_id = await _pending(api_env)
    stranger_org = (await seed_org(api_env.admin_engine, "rv-c")).org_id
    stranger = await _as(api_env, stranger_org, "technician")
    response = await api_env.client.post(
        f"/api/v1/devices/{device_id}/review/approve", headers=stranger
    )
    assert response.status_code == 404
    async with AsyncSession(api_env.admin_engine) as session:
        device = await session.get(Device, device_id)
    assert device is not None and device.review_state == "pending"


async def test_a_second_reject_is_409_and_dispatches_nothing_more(api_env) -> None:
    org_id, device_id = await _pending(api_env)
    headers = await _as(api_env, org_id, "technician")
    first = await api_env.client.post(f"/api/v1/devices/{device_id}/review/reject", headers=headers)
    assert first.status_code == 200
    second = await api_env.client.post(
        f"/api/v1/devices/{device_id}/review/reject", headers=headers
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "this computer is not waiting for review"
    async with AsyncSession(api_env.admin_engine) as session:
        jobs = (
            (await session.execute(select(Job).where(Job.target_id == device_id))).scalars().all()
        )
    assert len(jobs) == 1


async def test_the_signed_envelope_reaches_the_agent_and_the_audit_names_the_person(
    api_env, monkeypatch
) -> None:
    from app.api.v1 import devices as devices_api

    published: list[tuple[UUID, dict]] = []

    async def capture(_redis, device_id: UUID, envelope: dict) -> None:
        published.append((device_id, envelope))

    monkeypatch.setattr(devices_api, "publish_job", capture)
    org_id, device_id = await _pending(api_env)
    sub = await api_env.create_user(org_id, "technician")
    response = await api_env.client.post(
        f"/api/v1/devices/{device_id}/review/reject",
        headers=await api_env.headers(org_id, sub),
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    [(to_device, envelope)] = published
    assert to_device == device_id
    assert envelope["kind"] == "job" and envelope["sig"]
    assert envelope["job"]["id"] == job_id
    assert envelope["job"]["type"] == "agent.uninstall"
    assert envelope["job"]["device"] == str(device_id)

    [row] = await _actions(api_env, org_id)
    assert row.action == "device.review_rejected"
    assert row.actor_type == "user"
    assert row.actor_id == await _user_id(api_env, org_id, sub)


# --------------------------------------------------------------------------- #
# "I removed it": acknowledging a rejection that left the agent behind
# --------------------------------------------------------------------------- #

#: The outcomes after which the agent may still be on the computer.
LEFT_BEHIND = ("timeout", "agent_did_not_confirm", "agent_cannot_uninstall")


async def _left_behind(api_env, outcome: str | None, *, prefix: str = "rm") -> tuple[UUID, UUID]:
    org = await seed_org(api_env.admin_engine, prefix)
    device_id = await seed_device(
        api_env.admin_engine,
        org.org_id,
        hostname="DESKTOP-7Q2M4KD",
        review_state="rejected",
        rejected_at=datetime.now(UTC),
        status="decommissioned" if outcome is not None else "active",
        rejection_outcome=outcome,
    )
    await seed_agent(api_env.admin_engine, org.org_id, device_id, cert_revoked=True)
    return org.org_id, device_id


def _ack(device_id: UUID) -> str:
    return f"/api/v1/devices/{device_id}/review/acknowledge-removal"


async def _review_ids(api_env, org_id, headers) -> list[str]:
    body = (await api_env.client.get("/api/v1/devices?state=review", headers=headers)).json()
    assert body["state_counts"]["review"] == len(body["items"])
    return [i["id"] for i in body["items"]]


@pytest.mark.parametrize("outcome", LEFT_BEHIND)
async def test_acknowledging_a_manual_removal_hides_the_row_and_is_audited(
    api_env, outcome: str
) -> None:
    org_id, device_id = await _left_behind(api_env, outcome)
    sub = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, sub)
    assert await _review_ids(api_env, org_id, headers) == [str(device_id)]

    response = await api_env.client.post(_ack(device_id), headers=headers)
    assert response.status_code == 204, response.text

    assert await _review_ids(api_env, org_id, headers) == []
    async with AsyncSession(api_env.admin_engine) as session:
        device = await session.get(Device, device_id)
    assert device is not None and device.removal_acknowledged_at is not None
    # Still a tombstone: nothing about the rejection itself changed.
    assert (device.review_state, device.rejection_outcome) == ("rejected", outcome)
    [row] = await _actions(api_env, org_id)
    assert row.action == "device.removal_acknowledged"
    assert row.target_type == "device" and row.target_id == str(device_id)
    assert row.actor_type == "user" and row.actor_id == await _user_id(api_env, org_id, sub)
    assert row.meta["rejection_outcome"] == outcome
    assert row.meta["hostname"] == "DESKTOP-7Q2M4KD"

    # Hidden everywhere again, and a second acknowledgement has nothing to do.
    assert (
        await api_env.client.get(f"/api/v1/devices/{device_id}", headers=headers)
    ).status_code == 404
    again = await api_env.client.post(_ack(device_id), headers=headers)
    assert again.status_code == 409
    assert again.json()["detail"] == "someone has already said they removed this agent"
    assert len(await _actions(api_env, org_id)) == 1


@pytest.mark.parametrize(
    ("review_state", "outcome", "detail"),
    [
        ("pending", None, "this computer was not rejected"),
        ("approved", None, "this computer was not rejected"),
        (
            "rejected",
            None,
            "this computer's agent has not finished removing itself yet",
        ),
        (
            "rejected",
            "agent_confirmed",
            "this computer's agent confirmed it removed itself; there is nothing to remove",
        ),
    ],
)
async def test_only_a_rejection_that_left_the_agent_behind_can_be_acknowledged(
    api_env, review_state: str, outcome: str | None, detail: str
) -> None:
    org = await seed_org(api_env.admin_engine, "rm-409")
    device_id = await seed_device(
        api_env.admin_engine,
        org.org_id,
        review_state=review_state,
        rejected_at=datetime.now(UTC) if review_state == "rejected" else None,
        rejection_outcome=outcome,
    )
    response = await api_env.client.post(
        _ack(device_id), headers=await _as(api_env, org.org_id, "technician")
    )
    assert response.status_code == 409
    assert response.json() == {"detail": detail}
    async with AsyncSession(api_env.admin_engine) as session:
        device = await session.get(Device, device_id)
    assert device is not None and device.removal_acknowledged_at is None
    assert await _actions(api_env, org.org_id) == []


async def test_acknowledging_a_removal_needs_devices_review(api_env) -> None:
    org_id, device_id = await _left_behind(api_env, "timeout")
    response = await api_env.client.post(
        _ack(device_id), headers=await _as(api_env, org_id, "read-only")
    )
    assert response.status_code == 403
    async with AsyncSession(api_env.admin_engine) as session:
        device = await session.get(Device, device_id)
    assert device is not None and device.removal_acknowledged_at is None


async def test_acknowledging_another_orgs_removal_is_404(api_env) -> None:
    org_id, device_id = await _left_behind(api_env, "timeout")
    stranger_org = (await seed_org(api_env.admin_engine, "rm-b")).org_id
    stranger = await _as(api_env, stranger_org, "technician")
    assert (await api_env.client.post(_ack(device_id), headers=stranger)).status_code == 404
    assert (await api_env.client.post(_ack(uuid4()), headers=stranger)).status_code == 404
    async with AsyncSession(api_env.admin_engine) as session:
        device = await session.get(Device, device_id)
    assert device is not None and device.removal_acknowledged_at is None
    assert await _actions(api_env, stranger_org) == []


async def test_rejecting_an_agent_too_old_to_uninstall_tells_the_notification_rules(
    api_env,
) -> None:
    org_id, device_id = await _pending(api_env, capabilities={"inventory.collect": True})
    response = await api_env.client.post(
        f"/api/v1/devices/{device_id}/review/reject",
        headers=await _as(api_env, org_id, "technician"),
    )
    assert response.status_code == 200, response.text
    async with AsyncSession(api_env.admin_engine) as session:
        [event] = (
            (
                await session.execute(
                    select(DomainEvent).where(
                        DomainEvent.org_id == org_id,
                        DomainEvent.event_type == "device.removal_incomplete",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert event.subject_type == "device" and event.subject_id == device_id
    assert event.payload["device_id"] == str(device_id)
    assert event.payload["hostname"] == "DESKTOP-7Q2M4KD"
    assert event.payload["rejection_outcome"] == "agent_cannot_uninstall"
    assert event.payload["link_joined_from"] == "10.50.0.9"


async def test_a_dispatched_uninstall_tells_nobody_yet(api_env) -> None:
    org_id, device_id = await _pending(api_env)
    response = await api_env.client.post(
        f"/api/v1/devices/{device_id}/review/reject",
        headers=await _as(api_env, org_id, "technician"),
    )
    assert response.status_code == 200, response.text
    async with AsyncSession(api_env.admin_engine) as session:
        events = (
            (
                await session.execute(
                    select(DomainEvent).where(
                        DomainEvent.org_id == org_id,
                        DomainEvent.event_type == "device.removal_incomplete",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert events == []


async def test_reject_withdraws_what_was_queued_while_pending(api_env) -> None:
    """Codex review on #89. A job the review gate allowed while the machine
    was pending (inventory, health, a certificate job) can still be QUEUED when
    someone rejects it, and the gateway drains every queued row on the next
    connect -- so the rejected machine would receive it although
    `ALLOWED_WHILE_REJECTED` admits only `agent.uninstall`. Reject withdraws
    those in the same transaction; only the uninstall stays queued."""
    from app.jobs.engine import JobStatus

    org_id, device_id = await _pending(api_env)
    admin = await _as(api_env, org_id, "org-admin")
    queued = await api_env.client.post(
        "/api/v1/jobs",
        json={"type": "inventory.collect", "device_id": str(device_id), "params": {}},
        headers=admin,
    )
    assert queued.status_code in (200, 201), queued.text
    inventory_id = UUID(queued.json()["id"])

    response = await api_env.client.post(
        f"/api/v1/devices/{device_id}/review/reject",
        headers=await _as(api_env, org_id, "technician"),
    )
    assert response.status_code == 200, response.text
    uninstall_id = UUID(response.json()["job_id"])

    async with AsyncSession(api_env.admin_engine) as session:
        inventory = await session.get(Job, inventory_id)
        still_queued = list(
            (
                await session.execute(
                    select(Job.id).where(
                        Job.target_id == device_id, Job.status == JobStatus.queued
                    )
                )
            ).scalars()
        )
    assert inventory is not None and inventory.status == JobStatus.cancelled
    assert still_queued == [uninstall_id]
