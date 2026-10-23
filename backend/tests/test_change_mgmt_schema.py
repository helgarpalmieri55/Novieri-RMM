"""API-layer schema tests for the change management module
(docs/modules/change_management/spec.md).

Mirrors `tests/test_provisioning_schema.py`'s style for the plain API-layer
models: construct each model, assert out-of-domain Literal values are
rejected, and assert required fields are enforced. These are NOT wire
contracts (no `extra="forbid"` base) since this module has no agent jobs.
"""

import pytest
from pydantic import ValidationError

from app.modules.change_mgmt.schema import (
    ApprovalDecisionRequest,
    ApprovalOut,
    CalendarConflictOut,
    CalendarEntryOut,
    CalendarOut,
    ChangeCreateRequest,
    ChangeOut,
    ChangeUpdateRequest,
    EvidenceOut,
    LinkCreateRequest,
    LinkOut,
    TransitionRequest,
)


class TestChangeCreateRequest:
    def test_construct_with_defaults(self) -> None:
        req = ChangeCreateRequest(type="normal", title="t", description="d", risk="low")
        assert req.affected == []
        assert req.window_start is None
        assert req.window_end is None
        assert req.rollback_plan == ""

    def test_rejects_invalid_type(self) -> None:
        with pytest.raises(ValidationError):
            ChangeCreateRequest(type="huge", title="t", description="d", risk="low")

    def test_rejects_invalid_risk(self) -> None:
        with pytest.raises(ValidationError):
            ChangeCreateRequest(type="normal", title="t", description="d", risk="extreme")

    def test_requires_type(self) -> None:
        with pytest.raises(ValidationError):
            ChangeCreateRequest(title="t", description="d", risk="low")  # type: ignore[call-arg]

    def test_requires_title(self) -> None:
        with pytest.raises(ValidationError):
            ChangeCreateRequest(type="normal", description="d", risk="low")  # type: ignore[call-arg]

    def test_requires_description(self) -> None:
        with pytest.raises(ValidationError):
            ChangeCreateRequest(type="normal", title="t", risk="low")  # type: ignore[call-arg]

    def test_requires_risk(self) -> None:
        with pytest.raises(ValidationError):
            ChangeCreateRequest(type="normal", title="t", description="d")  # type: ignore[call-arg]


class TestChangeUpdateRequest:
    def test_construct_all_optional(self) -> None:
        req = ChangeUpdateRequest()
        assert req.type is None
        assert req.title is None
        assert req.description is None
        assert req.risk is None
        assert req.affected is None
        assert req.window_start is None
        assert req.window_end is None
        assert req.rollback_plan is None

    def test_rejects_invalid_type(self) -> None:
        with pytest.raises(ValidationError):
            ChangeUpdateRequest(type="huge")

    def test_rejects_invalid_risk(self) -> None:
        with pytest.raises(ValidationError):
            ChangeUpdateRequest(risk="extreme")


class TestChangeOut:
    def test_construct(self) -> None:
        out = ChangeOut(
            id="0198c1f4-0000-7000-8000-000000000000",
            type="normal",
            title="t",
            description="d",
            risk="low",
            affected=[],
            window_start=None,
            window_end=None,
            rollback_plan=None,
            state="draft",
            created_by="0198c1f4-0000-7000-8000-000000000001",
            created_at="2026-07-23T00:00:00Z",
        )
        assert out.state == "draft"


class TestApprovalDecisionRequest:
    def test_construct_defaults(self) -> None:
        req = ApprovalDecisionRequest()
        assert req.comment is None


class TestApprovalOut:
    def test_construct(self) -> None:
        out = ApprovalOut(
            id="0198c1f4-0000-7000-8000-000000000000",
            approver_id="0198c1f4-0000-7000-8000-000000000001",
            decision="approve",
            comment=None,
            is_retrospective=False,
            decided_at="2026-07-23T00:00:00Z",
        )
        assert out.decision == "approve"


class TestTransitionRequest:
    def test_construct_each_valid_action(self) -> None:
        for action in ("begin", "complete", "cancel", "revise"):
            assert TransitionRequest(action=action).action == action

    def test_rejects_invalid_action(self) -> None:
        with pytest.raises(ValidationError):
            TransitionRequest(action="approve")


class TestLinkCreateRequest:
    def test_construct(self) -> None:
        req = LinkCreateRequest(
            action_type="patch_run",
            action_id="0198c1f4-0000-7000-8000-000000000000",
        )
        assert req.action_type == "patch_run"

    def test_rejects_invalid_action_type(self) -> None:
        with pytest.raises(ValidationError):
            LinkCreateRequest(action_type="job", action_id="0198c1f4-0000-7000-8000-000000000000")


class TestLinkOut:
    def test_construct(self) -> None:
        out = LinkOut(
            id="0198c1f4-0000-7000-8000-000000000000",
            action_type="deployment",
            action_id="0198c1f4-0000-7000-8000-000000000001",
            linked_at="2026-07-23T00:00:00Z",
            linked_by="0198c1f4-0000-7000-8000-000000000002",
        )
        assert out.action_type == "deployment"


class TestCalendar:
    def test_calendar_entry_out(self) -> None:
        entry = CalendarEntryOut(
            change_id="0198c1f4-0000-7000-8000-000000000000",
            title="t",
            type="normal",
            risk="low",
            state="approved",
            window_start=None,
            window_end=None,
        )
        assert entry.state == "approved"

    def test_calendar_conflict_out(self) -> None:
        conflict = CalendarConflictOut(
            change_a="0198c1f4-0000-7000-8000-000000000000",
            change_b="0198c1f4-0000-7000-8000-000000000001",
        )
        assert conflict.change_a != conflict.change_b

    def test_calendar_out(self) -> None:
        cal = CalendarOut(entries=[], conflicts=[])
        assert cal.entries == []
        assert cal.conflicts == []


class TestEvidenceOut:
    def test_construct(self) -> None:
        change = ChangeOut(
            id="0198c1f4-0000-7000-8000-000000000000",
            type="normal",
            title="t",
            description="d",
            risk="low",
            affected=[],
            window_start=None,
            window_end=None,
            rollback_plan=None,
            state="completed",
            created_by="0198c1f4-0000-7000-8000-000000000001",
            created_at="2026-07-23T00:00:00Z",
        )
        evidence = EvidenceOut(change=change, approvals=[], links=[], audit_refs=[])
        assert evidence.change.state == "completed"
        assert evidence.audit_refs == []
