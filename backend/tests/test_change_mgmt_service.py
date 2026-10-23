"""Tests for the change-management service (docs/modules/change_management/
spec.md): create/edit a draft, drive lifecycle transitions through the pure
state machine, audit every action, and hit the pinned ticket-push call sites
through the injected `ChangeTicketPort`.

A `_SpyTicket` implementation of `ChangeTicketPort` records which of
push/update/close fired and with what, so the pinned call sites (submit ->
push_change, terminal -> close_change, else -> update_change) are asserted
directly rather than inferred.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, ChangeApproval, ChangeRequest, Organization, User
from app.modules.change_mgmt.service import (
    create_change,
    get_change,
    list_changes,
    transition_change,
    update_change,
)
from app.modules.change_mgmt.state import ChangeNotFoundError, ChangeValidationError


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


async def _org_with_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"chsvc-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        s.add(user)
        await s.commit()
        return org.id, user.id


async def _org_with_two_users(admin_engine: AsyncEngine) -> tuple[UUID, UUID, UUID]:
    org_id, requester_id = await _org_with_user(admin_engine)
    slug = f"chsvc2-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        approver = User(org_id=org_id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        s.add(approver)
        await s.commit()
        return org_id, requester_id, approver.id


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


# ---------------------------------------------------------------------------
# not-found routing: a missing change must raise the typed ChangeNotFoundError
# (a ChangeValidationError subclass) so the API layer can route it to a 404
# by type, not by matching exception-message prose.
# ---------------------------------------------------------------------------


async def test_transition_change_nonexistent_raises_change_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    spy = _SpyTicket()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(ChangeNotFoundError):
            await transition_change(
                s, spy, org_id=org_id, actor_id=actor_id, change_id=uuid4(), action="submit"
            )


async def test_update_change_nonexistent_raises_change_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(ChangeNotFoundError):
            await update_change(
                s,
                org_id=org_id,
                actor_id=actor_id,
                change_id=uuid4(),
                type_=None,
                title="new title",
                description=None,
                risk=None,
                affected=None,
                window_start=None,
                window_end=None,
                rollback_plan=None,
            )


# ---------------------------------------------------------------------------
# create_change
# ---------------------------------------------------------------------------


async def test_create_change_creates_draft_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        change = await create_change(
            s,
            org_id=org_id,
            actor_id=actor_id,
            type_="normal",
            title="Upgrade router firmware",
            description="Apply vendor patch",
            risk="medium",
            affected=["site-1"],
            window_start=None,
            window_end=None,
            rollback_plan="Revert to previous firmware image",
        )
        await s.commit()
        change_id = change.id

    assert change.state == "draft"
    assert change.created_by == actor_id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = (
            await s.execute(select(ChangeRequest).where(ChangeRequest.id == change_id))
        ).scalar_one()
        assert row.state == "draft"
        assert row.title == "Upgrade router firmware"

    actions = await _audit_actions(app_engine, org_id, change_id)
    assert actions == ["change.created"]

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        event = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "change.created"
                )
            )
        ).scalar_one()
        assert event.actor_type == "user"
        assert event.actor_id == actor_id
        assert event.meta["type"] == "normal"
        assert event.meta["risk"] == "medium"
        assert event.meta["title"] == "Upgrade router firmware"


# ---------------------------------------------------------------------------
# update_change
# ---------------------------------------------------------------------------


async def test_update_change_on_draft_applies_fields_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        change = await create_change(
            s,
            org_id=org_id,
            actor_id=actor_id,
            type_="normal",
            title="Original title",
            description="Original desc",
            risk="low",
            affected=[],
            window_start=None,
            window_end=None,
            rollback_plan=None,
        )
        await s.commit()
        change_id = change.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        updated = await update_change(
            s,
            org_id=org_id,
            actor_id=actor_id,
            change_id=change_id,
            title="Updated title",
            risk="high",
        )
        await s.commit()
        assert updated.title == "Updated title"
        assert updated.risk == "high"
        assert updated.description == "Original desc"  # untouched field preserved

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = (
            await s.execute(select(ChangeRequest).where(ChangeRequest.id == change_id))
        ).scalar_one()
        assert row.title == "Updated title"
        assert row.risk == "high"

    actions = await _audit_actions(app_engine, org_id, change_id)
    assert actions == ["change.created", "change.updated"]


async def test_update_change_on_non_draft_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    spy = _SpyTicket()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        change = await create_change(
            s,
            org_id=org_id,
            actor_id=actor_id,
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
        await transition_change(
            s, spy, org_id=org_id, actor_id=actor_id, change_id=change_id, action="submit"
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(ChangeValidationError, match="draft"):
            await update_change(
                s, org_id=org_id, actor_id=actor_id, change_id=change_id, title="nope"
            )


# ---------------------------------------------------------------------------
# transition_change: submit
# ---------------------------------------------------------------------------


async def test_transition_submit_moves_to_submitted_and_pushes_ticket(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    spy = _SpyTicket()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        change = await create_change(
            s,
            org_id=org_id,
            actor_id=actor_id,
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
        result = await transition_change(
            s, spy, org_id=org_id, actor_id=actor_id, change_id=change_id, action="submit"
        )
        await s.commit()
        assert result.state == "submitted"

    assert len(spy.push_calls) == 1
    assert spy.push_calls[0]["change_id"] == change_id
    assert spy.update_calls == []
    assert spy.close_calls == []

    actions = await _audit_actions(app_engine, org_id, change_id)
    assert actions == ["change.created", "change.submitted"]


# ---------------------------------------------------------------------------
# Full standard-change lifecycle
# ---------------------------------------------------------------------------


async def test_standard_change_full_lifecycle_submit_approve_begin_complete(
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
        await transition_change(
            s, spy, org_id=org_id, actor_id=requester_id, change_id=change_id, action="submit"
        )
        await s.commit()

    # Approval itself is Task 7; seed it directly here so `begin` is legal.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = (
            await s.execute(select(ChangeRequest).where(ChangeRequest.id == change_id))
        ).scalar_one()
        row.state = "approved"
        s.add(
            ChangeApproval(
                org_id=org_id,
                change_id=change_id,
                approver_id=approver_id,
                decision="approved",
                is_retrospective=False,
            )
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await transition_change(
            s, spy, org_id=org_id, actor_id=approver_id, change_id=change_id, action="begin"
        )
        await s.commit()
        assert result.state == "implementing"

    assert len(spy.update_calls) == 1
    assert spy.update_calls[0]["change_id"] == change_id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await transition_change(
            s, spy, org_id=org_id, actor_id=approver_id, change_id=change_id, action="complete"
        )
        await s.commit()
        assert result.state == "completed"

    assert len(spy.close_calls) == 1

    actions = await _audit_actions(app_engine, org_id, change_id)
    assert actions == [
        "change.created",
        "change.submitted",
        "change.transitioned",
        "change.transitioned",
    ]

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        event = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.target_id == str(change_id),
                    AuditEvent.action == "change.transitioned",
                )
                .order_by(AuditEvent.id.desc())
            )
        ).scalars().first()
        assert event.meta == {"from": "implementing", "to": "completed", "action": "complete"}


# ---------------------------------------------------------------------------
# Illegal transition
# ---------------------------------------------------------------------------


async def test_illegal_transition_raises_and_does_not_commit_state(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    spy = _SpyTicket()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        change = await create_change(
            s,
            org_id=org_id,
            actor_id=actor_id,
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
            await transition_change(
                s, spy, org_id=org_id, actor_id=actor_id, change_id=change_id, action="complete"
            )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = (
            await s.execute(select(ChangeRequest).where(ChangeRequest.id == change_id))
        ).scalar_one()
        assert row.state == "draft"

    actions = await _audit_actions(app_engine, org_id, change_id)
    assert actions == ["change.created"]  # no transitioned/errored audit was written
    assert spy.push_calls == spy.update_calls == spy.close_calls == []


# ---------------------------------------------------------------------------
# Emergency path
# ---------------------------------------------------------------------------


async def test_emergency_begin_from_submitted_and_complete_requires_retrospective_approval(
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

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = (
            await s.execute(select(ChangeRequest).where(ChangeRequest.id == change_id))
        ).scalar_one()
        row.state = "submitted"
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await transition_change(
            s, spy, org_id=org_id, actor_id=requester_id, change_id=change_id, action="begin"
        )
        await s.commit()
        assert result.state == "implementing"

    # No retrospective approval yet: complete must raise.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(ChangeValidationError):
            await transition_change(
                s, spy, org_id=org_id, actor_id=approver_id, change_id=change_id, action="complete"
            )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = (
            await s.execute(select(ChangeRequest).where(ChangeRequest.id == change_id))
        ).scalar_one()
        assert row.state == "implementing"

    # Insert the retrospective approval; now complete succeeds.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        s.add(
            ChangeApproval(
                org_id=org_id,
                change_id=change_id,
                approver_id=approver_id,
                decision="approved",
                is_retrospective=True,
            )
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await transition_change(
            s, spy, org_id=org_id, actor_id=approver_id, change_id=change_id, action="complete"
        )
        await s.commit()
        assert result.state == "completed"

    assert len(spy.close_calls) == 1


# ---------------------------------------------------------------------------
# get_change / list_changes
# ---------------------------------------------------------------------------


async def test_get_change_and_list_changes(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        change = await create_change(
            s,
            org_id=org_id,
            actor_id=actor_id,
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
        fetched = await get_change(s, org_id, change_id)
        assert fetched is not None
        assert fetched.id == change_id

        all_changes = await list_changes(s, org_id)
        assert [c.id for c in all_changes] == [change_id]


async def test_get_change_missing_returns_none(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _actor_id = await _org_with_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await get_change(s, org_id, uuid4())
        assert result is None


# ---------------------------------------------------------------------------
# Tenancy: another org's change is unreachable
# ---------------------------------------------------------------------------


async def test_another_orgs_change_is_hidden_by_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    spy = _SpyTicket()

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        change = await create_change(
            s,
            org_id=org_a,
            actor_id=actor_a,
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

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        fetched = await get_change(s, org_b, change_id)
        assert fetched is None

        with pytest.raises(ChangeValidationError):
            await update_change(
                s, org_id=org_b, actor_id=actor_b, change_id=change_id, title="hijack"
            )

        with pytest.raises(ChangeValidationError):
            await transition_change(
                s, spy, org_id=org_b, actor_id=actor_b, change_id=change_id, action="submit"
            )


async def test_the_change_org_predicates_hold_where_rls_is_not_doing_the_rejecting(
    admin_engine: AsyncEngine, bypassrls_session: AsyncSession
) -> None:
    """THE TEST IN THIS FILE THAT CAN ACTUALLY FAIL.

    The test above drives `get_change`, `update_change` and `transition_change`
    through `org_session` on the NOBYPASSRLS `app_user` role. `change_request`
    carries a forced `tenant_isolation` policy, so the neighbour's row is
    already gone when `ChangeRequest.org_id == org_id` runs -- it stays green
    with the predicate deleted, and what it pins is the ERROR TYPE
    (`ChangeValidationError`, not a 500, not a silent success). `its_admin`
    carries BYPASSRLS (`tests/conftest.py`), so here the predicate is the only
    scoping.

    THE WRITE HALF IS WHY THIS MATTERS MORE THAN A READ. `transition_change` is
    the approval workflow: a miss does not disclose a neighbour's change, it
    ADVANCES one -- an unapproved change moving to `approved` in a tenant whose
    administrator never saw it.

    FALSIFIED: deleting `ChangeRequest.org_id == org_id` from `get_change`
    answers with org B's change; deleting it from `_load_change` lets org A
    rename and transition a change it does not own; deleting it from
    `list_changes` returns both orgs' rows.
    """
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    spy = _SpyTicket()

    changes: dict[str, UUID] = {}
    for key, org_id, actor_id in (("a", org_a, actor_a), ("b", org_b, actor_b)):
        change = await create_change(
            bypassrls_session,
            org_id=org_id,
            actor_id=actor_id,
            type_="normal",
            title=f"title-{key}",
            description="D",
            risk="low",
            affected=[],
            window_start=None,
            window_end=None,
            rollback_plan=None,
        )
        changes[key] = change.id
    await bypassrls_session.commit()

    assert await get_change(bypassrls_session, org_a, changes["b"]) is None
    assert await get_change(bypassrls_session, org_a, changes["a"]) is not None
    assert [c.id for c in await list_changes(bypassrls_session, org_a)] == [changes["a"]]
    assert [c.id for c in await list_changes(bypassrls_session, org_b)] == [changes["b"]]

    with pytest.raises(ChangeValidationError):
        await update_change(
            bypassrls_session,
            org_id=org_a,
            actor_id=actor_a,
            change_id=changes["b"],
            title="hijack",
        )
    await bypassrls_session.rollback()

    with pytest.raises(ChangeValidationError):
        await transition_change(
            bypassrls_session,
            spy,
            org_id=org_a,
            actor_id=actor_a,
            change_id=changes["b"],
            action="submit",
        )
    await bypassrls_session.rollback()

    theirs = await get_change(bypassrls_session, org_b, changes["b"])
    assert theirs is not None
    assert (theirs.title, theirs.state) == ("title-b", "draft"), (
        "org A's write reached across the tenant boundary"
    )
