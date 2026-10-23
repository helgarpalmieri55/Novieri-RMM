"""Tests for `decide_change` (docs/modules/change_management/spec.md): the
insert-only approval/rejection decision trail.

`decide_change` never re-implements segregation-of-duties (SoD) or transition
legality itself -- it calls `app.modules.change_mgmt.state.next_state` and
lets a `ChangeValidationError` propagate. Every decision is inserted as a NEW
`ChangeApproval` row (never updated/revoked in place), so a change's full
decision history survives across resubmissions. An emergency change's
retrospective approval (recorded while the change is already `implementing`)
does not move the run state, but unblocks a subsequent `complete` transition.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, ChangeApproval, ChangeRequest, Organization, User
from app.modules.change_mgmt.service import create_change, decide_change, transition_change
from app.modules.change_mgmt.state import ChangeValidationError


class _SpyTicket:
    def __init__(self) -> None:
        self.push_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.close_calls: list[dict] = []

    async def push_change(self, session, *, org_id, change) -> str:
        self.push_calls.append({"org_id": org_id, "change_id": change.id})
        return ""

    async def update_change(self, session, *, org_id, external_id, change) -> None:
        self.update_calls.append(
            {"org_id": org_id, "external_id": external_id, "change_id": change.id}
        )
        return None

    async def close_change(self, session, *, org_id, external_id) -> None:
        self.close_calls.append({"org_id": org_id, "external_id": external_id})
        return None


async def _org_with_two_users(admin_engine: AsyncEngine) -> tuple[UUID, UUID, UUID]:
    slug = f"chappr-{uuid4().hex[:6]}"
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
        s.add(requester)
        s.add(approver)
        await s.commit()
        return org.id, requester.id, approver.id


async def _approval_rows(
    app_engine: AsyncEngine, org_id: UUID, change_id: UUID
) -> list[ChangeApproval]:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        return list(
            (
                await s.execute(
                    select(ChangeApproval).where(
                        ChangeApproval.org_id == org_id, ChangeApproval.change_id == change_id
                    )
                )
            )
            .scalars()
            .all()
        )


async def _audit_actions(app_engine: AsyncEngine, org_id: UUID, change_id: UUID) -> list[str]:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        events = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.target_type == "change",
                    AuditEvent.target_id == str(change_id),
                )
                .order_by(AuditEvent.id)
            )
        ).scalars().all()
        return [e.action for e in events]


async def _create_and_submit(
    app_engine: AsyncEngine, spy: _SpyTicket, *, org_id: UUID, requester_id: UUID, type_: str
) -> UUID:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        change = await create_change(
            s,
            org_id=org_id,
            actor_id=requester_id,
            type_=type_,
            title="T",
            description="D",
            risk="low",
            affected=[],
            window_start=None,
            window_end=None,
            rollback_plan=None,
        )
        await s.commit()
        change_id = change.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await transition_change(
            s, spy, org_id=org_id, actor_id=requester_id, change_id=change_id, action="submit"
        )
        await s.commit()

    return change_id


# ---------------------------------------------------------------------------
# Approve by a different user
# ---------------------------------------------------------------------------


async def test_decide_change_approve_by_different_user_moves_to_approved(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, requester_id, approver_id = await _org_with_two_users(admin_engine)
    spy = _SpyTicket()
    change_id = await _create_and_submit(
        app_engine, spy, org_id=org_id, requester_id=requester_id, type_="normal"
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        approval = await decide_change(
            s,
            spy,
            org_id=org_id,
            actor_id=approver_id,
            change_id=change_id,
            decision="approved",
            comment="looks good",
        )
        await s.commit()
        assert approval.decision == "approved"
        assert approval.approver_id == approver_id
        assert approval.is_retrospective is False
        assert approval.comment == "looks good"

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = (
            await s.execute(select(ChangeRequest).where(ChangeRequest.id == change_id))
        ).scalar_one()
        assert row.state == "approved"

    rows = await _approval_rows(app_engine, org_id, change_id)
    assert len(rows) == 1
    assert rows[0].decision == "approved"
    assert rows[0].is_retrospective is False

    actions = await _audit_actions(app_engine, org_id, change_id)
    assert actions == ["change.created", "change.submitted", "change.approved"]

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        event = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "change.approved"
                )
            )
        ).scalar_one()
        assert event.actor_type == "user"
        assert event.actor_id == approver_id
        assert event.meta == {"decision": "approved", "is_retrospective": False}

    assert len(spy.update_calls) == 1
    assert spy.update_calls[0]["change_id"] == change_id


# ---------------------------------------------------------------------------
# SoD: requester cannot approve their own change
# ---------------------------------------------------------------------------


async def test_decide_change_approve_by_requester_raises_sod(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, requester_id, _approver_id = await _org_with_two_users(admin_engine)
    spy = _SpyTicket()
    change_id = await _create_and_submit(
        app_engine, spy, org_id=org_id, requester_id=requester_id, type_="normal"
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(ChangeValidationError):
            await decide_change(
                s,
                spy,
                org_id=org_id,
                actor_id=requester_id,
                change_id=change_id,
                decision="approved",
                comment=None,
            )

    rows = await _approval_rows(app_engine, org_id, change_id)
    assert rows == []

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = (
            await s.execute(select(ChangeRequest).where(ChangeRequest.id == change_id))
        ).scalar_one()
        assert row.state == "submitted"


# ---------------------------------------------------------------------------
# Reject by a different user
# ---------------------------------------------------------------------------


async def test_decide_change_reject_by_different_user_moves_to_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, requester_id, approver_id = await _org_with_two_users(admin_engine)
    spy = _SpyTicket()
    change_id = await _create_and_submit(
        app_engine, spy, org_id=org_id, requester_id=requester_id, type_="normal"
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        approval = await decide_change(
            s,
            spy,
            org_id=org_id,
            actor_id=approver_id,
            change_id=change_id,
            decision="rejected",
            comment="needs more testing",
        )
        await s.commit()
        assert approval.decision == "rejected"

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = (
            await s.execute(select(ChangeRequest).where(ChangeRequest.id == change_id))
        ).scalar_one()
        assert row.state == "rejected"

    rows = await _approval_rows(app_engine, org_id, change_id)
    assert len(rows) == 1
    assert rows[0].decision == "rejected"

    actions = await _audit_actions(app_engine, org_id, change_id)
    assert actions == ["change.created", "change.submitted", "change.rejected"]


# ---------------------------------------------------------------------------
# Insert-only: multiple decisions over a change's life all persist
# ---------------------------------------------------------------------------


async def test_decide_change_insert_only_multiple_decisions_persist(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, requester_id, approver_id = await _org_with_two_users(admin_engine)
    spy = _SpyTicket()
    change_id = await _create_and_submit(
        app_engine, spy, org_id=org_id, requester_id=requester_id, type_="normal"
    )

    # First decision: reject.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await decide_change(
            s,
            spy,
            org_id=org_id,
            actor_id=approver_id,
            change_id=change_id,
            decision="rejected",
            comment="rework",
        )
        await s.commit()

    # Revise back to draft, then resubmit.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await transition_change(
            s, spy, org_id=org_id, actor_id=requester_id, change_id=change_id, action="revise"
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await transition_change(
            s, spy, org_id=org_id, actor_id=requester_id, change_id=change_id, action="submit"
        )
        await s.commit()

    # Second decision: approve.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await decide_change(
            s,
            spy,
            org_id=org_id,
            actor_id=approver_id,
            change_id=change_id,
            decision="approved",
            comment="fixed",
        )
        await s.commit()

    rows = await _approval_rows(app_engine, org_id, change_id)
    assert len(rows) == 2
    decisions = sorted(r.decision for r in rows)
    assert decisions == ["approved", "rejected"]

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = (
            await s.execute(select(ChangeRequest).where(ChangeRequest.id == change_id))
        ).scalar_one()
        assert row.state == "approved"


# ---------------------------------------------------------------------------
# Emergency retrospective approval
# ---------------------------------------------------------------------------


async def test_emergency_retrospective_approve_records_row_and_unblocks_complete(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, requester_id, approver_id = await _org_with_two_users(admin_engine)
    spy = _SpyTicket()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        change = await create_change(
            s,
            org_id=org_id,
            actor_id=requester_id,
            type_="emergency",
            title="Emergency patch",
            description="Sev1 fix",
            risk="high",
            affected=[],
            window_start=None,
            window_end=None,
            rollback_plan=None,
        )
        await s.commit()
        change_id = change.id

    # Fast-path: draft -> submitted -> implementing (no prior approval needed).
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = (
            await s.execute(select(ChangeRequest).where(ChangeRequest.id == change_id))
        ).scalar_one()
        row.state = "submitted"
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await transition_change(
            s, spy, org_id=org_id, actor_id=requester_id, change_id=change_id, action="begin"
        )
        await s.commit()

    # Retrospective approval by a different user.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        approval = await decide_change(
            s,
            spy,
            org_id=org_id,
            actor_id=approver_id,
            change_id=change_id,
            decision="approved",
            comment="approved after the fact",
        )
        await s.commit()
        assert approval.is_retrospective is True
        assert approval.decision == "approved"

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = (
            await s.execute(select(ChangeRequest).where(ChangeRequest.id == change_id))
        ).scalar_one()
        assert row.state == "implementing"  # unchanged by the retrospective decision

    rows = await _approval_rows(app_engine, org_id, change_id)
    assert len(rows) == 1
    assert rows[0].is_retrospective is True

    actions = await _audit_actions(app_engine, org_id, change_id)
    assert actions == ["change.created", "change.transitioned", "change.approved"]

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        event = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "change.approved"
                )
            )
        ).scalar_one()
        assert event.meta == {"decision": "approved", "is_retrospective": True}

    # Now completion succeeds because a retrospective approval was recorded.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await transition_change(
            s, spy, org_id=org_id, actor_id=approver_id, change_id=change_id, action="complete"
        )
        await s.commit()
        assert result.state == "completed"


async def test_emergency_retrospective_approve_by_requester_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, requester_id, _approver_id = await _org_with_two_users(admin_engine)
    spy = _SpyTicket()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        change = await create_change(
            s,
            org_id=org_id,
            actor_id=requester_id,
            type_="emergency",
            title="Emergency patch",
            description="Sev1 fix",
            risk="high",
            affected=[],
            window_start=None,
            window_end=None,
            rollback_plan=None,
        )
        await s.commit()
        change_id = change.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = (
            await s.execute(select(ChangeRequest).where(ChangeRequest.id == change_id))
        ).scalar_one()
        row.state = "submitted"
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await transition_change(
            s, spy, org_id=org_id, actor_id=requester_id, change_id=change_id, action="begin"
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(ChangeValidationError):
            await decide_change(
                s,
                spy,
                org_id=org_id,
                actor_id=requester_id,
                change_id=change_id,
                decision="approved",
                comment=None,
            )

    rows = await _approval_rows(app_engine, org_id, change_id)
    assert rows == []


# ---------------------------------------------------------------------------
# Illegal decision (deciding a draft)
# ---------------------------------------------------------------------------


async def test_decide_change_on_draft_raises_illegal(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, requester_id, approver_id = await _org_with_two_users(admin_engine)
    spy = _SpyTicket()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        change = await create_change(
            s,
            org_id=org_id,
            actor_id=requester_id,
            type_="normal",
            title="T",
            description="D",
            risk="low",
            affected=[],
            window_start=None,
            window_end=None,
            rollback_plan=None,
        )
        await s.commit()
        change_id = change.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(ChangeValidationError):
            await decide_change(
                s,
                spy,
                org_id=org_id,
                actor_id=approver_id,
                change_id=change_id,
                decision="approved",
                comment=None,
            )

    rows = await _approval_rows(app_engine, org_id, change_id)
    assert rows == []


# ---------------------------------------------------------------------------
# Bad decision literal
# ---------------------------------------------------------------------------


async def test_decide_change_invalid_decision_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, requester_id, approver_id = await _org_with_two_users(admin_engine)
    spy = _SpyTicket()
    change_id = await _create_and_submit(
        app_engine, spy, org_id=org_id, requester_id=requester_id, type_="normal"
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(ChangeValidationError):
            await decide_change(
                s,
                spy,
                org_id=org_id,
                actor_id=approver_id,
                change_id=change_id,
                decision="maybe",
                comment=None,
            )


# ---------------------------------------------------------------------------
# Tenancy: another org's change is unreachable
# ---------------------------------------------------------------------------


async def test_decide_change_refuses_a_change_rls_has_already_hidden(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, requester_a, _approver_a = await _org_with_two_users(admin_engine)
    org_b, _requester_b, approver_b = await _org_with_two_users(admin_engine)
    spy = _SpyTicket()

    change_id = await _create_and_submit(
        app_engine, spy, org_id=org_a, requester_id=requester_a, type_="normal"
    )

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(ChangeValidationError):
            await decide_change(
                s,
                spy,
                org_id=org_b,
                actor_id=approver_b,
                change_id=change_id,
                decision="approved",
                comment=None,
            )
