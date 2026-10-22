"""Approve or reject a computer that joined through the install link.

docs/superpowers/specs/2026-09-28-install-link-and-windows-push-design.md §1.5
and §4; plan decisions D1 (a tombstone, not a DELETE) and D2 (revoke at
completion, not at the click).

APPROVE is one state change and one audit row. From then on the device is an
ordinary device and the review gate never looks at it again.

REJECT is three acts spread over time, because the one that has to happen first
-- telling the agent to remove itself -- travels over the connection that
revocation would cut:

  1. NOW: `review_state = 'rejected'`, audited, and for an agent that advertises
     `agent.uninstall` (1.6.0+) a signed `agent.uninstall` job valid for 24
     hours, so an agent that is switched off tonight still receives it tomorrow.
  2. WHEN THE AGENT CONFIRMS (the job `succeeded`) OR 24 HOURS LATER, whichever
     is first: the certificate is revoked and the row becomes a
     `decommissioned` tombstone. `complete_due_rejections`, run by a beat.
  3. AN AGENT TOO OLD TO UNINSTALL ITSELF skips the job: its certificate is
     revoked and the row tombstoned at once, and the technician is told to
     remove the agent by hand (spec §4).

A REJECTION THAT ENDS WITH THE AGENT POSSIBLY STILL ON THE COMPUTER --
`timeout`, `agent_did_not_confirm` or `agent_cannot_uninstall` (`LEFT_BEHIND`)
-- needs a person to go and remove it, and the tombstone is hidden everywhere.
So two things make sure somebody is told: the row is listed in *Waiting for
review* (`device_list`) until a technician says they removed it
(`acknowledge_removal` sets `removal_acknowledged_at`), and the notification
event `device.removal_incomplete` is published the moment the outcome is
stored, so a rule can email someone.

THE CRL WRITE AND THE CONNECTION DROP HAPPEN IN THE CALLER, after the tenant
transaction commits, exactly as `POST /agents/{device_id}/revoke` does them:
`agents.cert_revoked` is the gateway's admission gate and is committed first,
so a CRL write that has to be retried fails safe.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditEntry, audit_service
from app.core.events import publish as publish_event
from app.core.i18n import tr
from app.jobs.engine import JobEngine
from app.jobs.review_gate import ALLOWED_WHILE_REJECTED
from app.models import Agent, Device, Job, JobStatus

UNINSTALL_JOB_TYPE = "agent.uninstall"
#: How long a rejected machine has to confirm its uninstall before its
#: certificate is revoked anyway (spec §1.5: "or after 24 hours").
UNINSTALL_GRACE = timedelta(hours=24)
#: The completion beat's cadence (Task 16).
COMPLETION_INTERVAL_S = 60.0
#: An `agent.uninstall` that ended in one of these did NOT remove the agent.
_DID_NOT_CONFIRM = frozenset({JobStatus.failed, JobStatus.refused, JobStatus.expired})
#: How a rejection can end with the agent possibly still installed, so a
#: person has to remove it by hand. The only other outcome is `agent_confirmed`.
LEFT_BEHIND = frozenset({"timeout", "agent_did_not_confirm", "agent_cannot_uninstall"})
#: The notification event published when a rejection ends in `LEFT_BEHIND`.
REMOVAL_INCOMPLETE_EVENT = "device.removal_incomplete"
#: The most machines one Approve all may name (GPO MSI spec 2026-09-30 §2.5).
APPROVE_ALL_MAX = 1000



class ReviewNotFound(LookupError):
    """No device with this id in this organisation. The route answers 404."""


class NotWaitingForReview(ValueError):
    """The device is not `pending`. The route answers 409."""


class ApproveAllRefused(ValueError):
    """An Approve all that names too many machines, or whose digest is not the
    digest of the list it names. The route answers 422 with the message."""


class NothingToAcknowledge(ValueError):
    """The device is not a rejection that left its agent behind, or someone
    already said they removed it. The route answers 409 with the message."""


def can_uninstall(capabilities: object) -> bool:
    """Did the agent report `agent.uninstall`? Typed `object` for the reason
    `remote.capability.can_share_screen` gives: the column is JSON."""
    return isinstance(capabilities, dict) and bool(capabilities.get(UNINSTALL_JOB_TYPE))


@dataclass(frozen=True)
class RejectOutcome:
    device_id: UUID
    #: `dispatched` (the agent will remove itself) or `manual` (it cannot).
    uninstall: str
    job: Job | None
    #: Publish AFTER the transaction commits.
    envelope: dict[str, Any] | None
    #: A certificate serial the caller must write to the CRL, when revoked now.
    revoke_serial: str | None
    message: str


@dataclass(frozen=True)
class CompletedRejection:
    device_id: UUID
    #: `agent_confirmed` or `timeout`.
    reason: str
    revoke_serial: str | None


async def _pending(session: AsyncSession, org_id: UUID, device_id: UUID) -> Device:
    device = (
        await session.execute(
            select(Device).where(Device.org_id == org_id, Device.id == device_id).with_for_update()
        )
    ).scalar_one_or_none()
    if device is None:
        raise ReviewNotFound(tr("no device {device_id} in this organisation", device_id=device_id))
    if device.review_state != "pending":
        raise NotWaitingForReview(tr("this computer is not waiting for review"))
    return device


async def _agent(session: AsyncSession, org_id: UUID, device_id: UUID) -> Agent | None:
    agent: Agent | None = (
        await session.execute(
            select(Agent).where(Agent.org_id == org_id, Agent.device_id == device_id)
        )
    ).scalar_one_or_none()
    return agent


def _approval(
    device: Device, *, actor_id: UUID, now: datetime, bulk_id: UUID | None = None
) -> AuditEntry:
    """One approval: the state change, and the audit record it owes, as a
    value. `approve` records it alone; Approve all records all of its
    machines' entries in one `record_many` -- one record per machine, each
    carrying the same `bulk_id`, under a single take of the audit lock."""
    device.review_state = "approved"
    metadata: dict[str, Any] = {
        "hostname": device.hostname,
        "link_joined_from": str(device.link_joined_from) if device.link_joined_from else None,
        "approved_at": now.isoformat(),
    }
    if bulk_id is not None:
        metadata["bulk_id"] = str(bulk_id)
    return AuditEntry(
        actor_type="user",
        actor_id=actor_id,
        action="device.review_approved",
        target_type="device",
        target_id=str(device.id),
        metadata=metadata,
    )


async def approve(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, device_id: UUID, now: datetime
) -> Device:
    device = await _pending(session, org_id, device_id)
    entry = _approval(device, actor_id=actor_id, now=now)
    session.add(device)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type=entry.actor_type,
        actor_id=entry.actor_id,
        action=entry.action,
        target_type=entry.target_type,
        target_id=entry.target_id,
        metadata=entry.metadata,
    )
    await session.flush()
    return device


def review_list_digest(device_ids: Iterable[UUID]) -> str:
    """SHA-256, hex, of the sorted, de-duplicated ids, one per line. The
    console computes the same over the list it showed (`reviewListDigest`), so
    an Approve all confirms THAT list."""
    lines = "\n".join(sorted({str(device_id) for device_id in device_ids}))
    return hashlib.sha256(lines.encode()).hexdigest()


@dataclass(frozen=True)
class ApproveAllOutcome:
    bulk_id: UUID
    #: Listed and still pending: approved now.
    approved: int
    #: Listed, but no longer pending (someone approved or rejected it since),
    #: or not a device of this organisation: left as it is.
    not_pending: int
    #: Pending and NOT listed -- joined after the list was shown: still waiting.
    still_waiting: int


async def approve_all(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    device_ids: list[UUID],
    list_digest: str,
    now: datetime,
) -> ApproveAllOutcome:
    """Approve every LISTED machine that is still pending (GPO MSI spec
    2026-09-30 §2.5). Never "every pending machine": one that joined after the
    dialog opened is not in the list, so it stays pending and is counted."""
    listed = set(device_ids)
    if not listed:
        raise ApproveAllRefused(tr("the list names no computers"))
    if len(listed) > APPROVE_ALL_MAX:
        raise ApproveAllRefused(tr(
            "at most {approve_all_max} computers can be approved at once",
            approve_all_max=APPROVE_ALL_MAX,
        ))
    if review_list_digest(listed) != list_digest:
        raise ApproveAllRefused(
            tr("the list sent is not the list that was shown; open Approve all again")
        )
    rows = (
        (
            await session.execute(
                select(Device)
                .where(
                    Device.org_id == org_id,
                    Device.id.in_(listed),
                    Device.review_state == "pending",
                )
                # One order for every caller, so two Approve alls over
                # overlapping lists cannot deadlock on each other's rows.
                .order_by(Device.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    bulk_id = uuid4()
    entries = [_approval(device, actor_id=actor_id, now=now, bulk_id=bulk_id) for device in rows]
    session.add_all(rows)
    # ONE take of the audit lock for the whole list, on the org's one chain.
    await audit_service.record_many(session, org_id=org_id, entries=entries)
    await session.flush()
    waiting = (
        await session.execute(
            select(func.count())
            .select_from(Device)
            .where(Device.org_id == org_id, Device.review_state == "pending")
        )
    ).scalar_one()
    return ApproveAllOutcome(
        bulk_id=bulk_id,
        approved=len(rows),
        not_pending=len(listed) - len(rows),
        still_waiting=int(waiting),
    )


async def _tombstone(
    session: AsyncSession,
    *,
    org_id: UUID,
    device: Device,
    agent: Agent | None,
    reason: str,
    actor_id: UUID | None,
    job_status: str | None = None,
) -> str | None:
    """Revoke the certificate (the DB flag), decommission the row and record
    on it how the rejection ended (`reason`)."""
    serial: str | None = None
    if agent is not None:
        agent.cert_revoked = True
        session.add(agent)
        serial = agent.cert_serial
    device.status = "decommissioned"
    device.rejection_outcome = reason
    session.add(device)
    if reason in LEFT_BEHIND:
        # Machine-origin even when a technician's Reject stored the outcome:
        # what it reports -- the agent is still there -- is nobody's choice.
        await publish_event(
            session,
            org_id=org_id,
            event_type=REMOVAL_INCOMPLETE_EVENT,
            subject_type="device",
            subject_id=device.id,
            payload={
                "device_id": str(device.id),
                "hostname": device.hostname,
                "rejection_outcome": reason,
                "link_joined_from": (
                    str(device.link_joined_from) if device.link_joined_from else None
                ),
                "rejected_at": device.rejected_at.isoformat() if device.rejected_at else None,
            },
            actor_user_id=None,
        )
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user" if actor_id is not None else "system",
        actor_id=actor_id,
        action="device.rejection_completed",
        target_type="device",
        target_id=str(device.id),
        metadata={
            "reason": reason,
            # The uninstall job's terminal status when it ended without
            # success (`agent_did_not_confirm`); absent otherwise.
            **({"job_status": job_status} if job_status is not None else {}),
            "cert_serial": agent.cert_serial if agent is not None else None,
            "cert_fingerprint": agent.cert_fingerprint if agent is not None else None,
        },
    )
    await session.flush()
    return serial


async def reject(
    session: AsyncSession,
    job_engine: JobEngine,
    *,
    org_id: UUID,
    actor_id: UUID,
    device_id: UUID,
    now: datetime,
) -> RejectOutcome:
    device = await _pending(session, org_id, device_id)
    agent = await _agent(session, org_id, device_id)
    device.review_state = "rejected"
    device.rejected_at = now
    session.add(device)
    # FLUSHED BEFORE THE JOB IS CREATED: the review gate reads this row, and
    # `rejected` is the state that admits `agent.uninstall` and nothing else.
    await session.flush()
    # WHAT WAS QUEUED WHILE PENDING IS WITHDRAWN IN THE SAME TRANSACTION. The
    # gate is checked when a job is born; an inventory or certificate job it
    # allowed a minute ago can still be QUEUED, and the gateway drains every
    # queued row on the next connect -- so without this the rejected machine
    # would still receive it (Codex review on #89). `withdraw` is the queued-only
    # server-side cancel: nothing on the endpoint has seen these, so nothing on
    # it can contradict "cancelled". Dispatched or running jobs are not ours to
    # recall here; they are all allowed-while-pending types and end on their own.
    for queued in (
        await session.execute(
            select(Job).where(
                Job.org_id == org_id,
                Job.target_id == device_id,
                Job.status == JobStatus.queued,
                Job.type.not_in(ALLOWED_WHILE_REJECTED),
            )
        )
    ).scalars():
        await job_engine.withdraw(session, queued, requested_by=actor_id)

    capable = agent is not None and not agent.cert_revoked and can_uninstall(agent.capabilities)
    job: Job | None = None
    envelope: dict[str, Any] | None = None
    if capable:
        job = await job_engine.create(
            session,
            org_id=org_id,
            type=UNINSTALL_JOB_TYPE,
            target_device_id=device_id,
            params={},
            requested_by=actor_id,
            ttl_s=int(UNINSTALL_GRACE.total_seconds()),
        )
        envelope = await job_engine.signed_envelope(job)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="device.review_rejected",
        target_type="device",
        target_id=str(device_id),
        metadata={
            "hostname": device.hostname,
            "link_joined_from": str(device.link_joined_from) if device.link_joined_from else None,
            "uninstall": "dispatched" if capable else "manual",
            "job_id": str(job.id) if job is not None else None,
        },
    )
    await session.flush()
    if capable:
        return RejectOutcome(
            device_id,
            "dispatched",
            job,
            envelope,
            None,
            tr(
                "The agent has been told to uninstall itself. Its certificate is revoked as soon "
                "as it confirms, or in 24 hours if it does not."
            ),
        )
    serial = await _tombstone(
        session,
        org_id=org_id,
        device=device,
        agent=agent,
        reason="agent_cannot_uninstall",
        actor_id=actor_id,
    )
    return RejectOutcome(
        device_id,
        "manual",
        None,
        None,
        serial,
        tr(
            "This computer's agent is too old to remove itself. Its certificate has been "
            "revoked, so it can no longer connect; remove the agent from the computer by hand."
        ),
    )


async def acknowledge_removal(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, device_id: UUID, now: datetime
) -> Device:
    """Record that a technician removed a rejected machine's agent by hand.
    From then on the row is hidden everywhere again, like every other
    tombstone. Only a rejection in `LEFT_BEHIND` can be acknowledged: any
    other row has nothing to remove, or has not finished removing itself."""
    device = (
        await session.execute(
            select(Device).where(Device.org_id == org_id, Device.id == device_id).with_for_update()
        )
    ).scalar_one_or_none()
    if device is None:
        raise ReviewNotFound(tr("no device {device_id} in this organisation", device_id=device_id))
    if device.review_state != "rejected":
        raise NothingToAcknowledge(tr("this computer was not rejected"))
    if device.rejection_outcome is None:
        raise NothingToAcknowledge(tr("this computer's agent has not finished removing itself yet"))
    if device.rejection_outcome not in LEFT_BEHIND:
        raise NothingToAcknowledge(
            tr("this computer's agent confirmed it removed itself; there is nothing to remove")
        )
    if device.removal_acknowledged_at is not None:
        raise NothingToAcknowledge(tr("someone has already said they removed this agent"))
    device.removal_acknowledged_at = now
    session.add(device)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="device.removal_acknowledged",
        target_type="device",
        target_id=str(device_id),
        metadata={
            "hostname": device.hostname,
            "rejection_outcome": device.rejection_outcome,
            "acknowledged_at": now.isoformat(),
        },
    )
    await session.flush()
    return device


async def complete_due_rejections(
    session: AsyncSession, org_id: UUID, *, now: datetime
) -> list[CompletedRejection]:
    """Tombstone every rejected device whose agent confirmed, whose uninstall
    ended WITHOUT success, or whose 24 hours are up. `SKIP LOCKED` so two beat
    workers never complete one row twice.

    AN UNINSTALL THAT FAILED, WAS REFUSED OR EXPIRED completes at once as
    `agent_did_not_confirm`: the agent answered (or the window closed) and it
    did not remove itself, so waiting out the rest of the 24 hours changes
    nothing -- the certificate is revoked now, and the row and the audit say
    the agent may still be on the computer."""
    rows = (
        (
            await session.execute(
                select(Device)
                .where(
                    Device.org_id == org_id,
                    Device.review_state == "rejected",
                    Device.status != "decommissioned",
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    done: list[CompletedRejection] = []
    for device in rows:
        statuses = set(
            (
                await session.execute(
                    select(Job.status).where(
                        Job.org_id == org_id,
                        Job.target_id == device.id,
                        Job.type == UNINSTALL_JOB_TYPE,
                    )
                )
            ).scalars()
        )
        unconfirmed = sorted(s.value for s in statuses & _DID_NOT_CONFIRM)
        timed_out = device.rejected_at is None or device.rejected_at <= now - UNINSTALL_GRACE
        job_status: str | None = None
        if JobStatus.succeeded in statuses:
            reason = "agent_confirmed"
        elif unconfirmed:
            reason = "agent_did_not_confirm"
            job_status = unconfirmed[0]
        elif timed_out:
            reason = "timeout"
        else:
            continue
        serial = await _tombstone(
            session,
            org_id=org_id,
            device=device,
            agent=await _agent(session, org_id, device.id),
            reason=reason,
            actor_id=None,
            job_status=job_status,
        )
        done.append(CompletedRejection(device.id, reason, serial))
    return done
