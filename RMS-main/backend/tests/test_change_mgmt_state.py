"""Tests for the pure change-management state machine (docs/modules/
change_management/spec.md state transitions). No DB, no session — these
tests exercise `next_state` directly with plain UUIDs.
"""

from uuid import uuid4

import pytest

from app.modules.change_mgmt.state import ChangeValidationError, next_state

REQUESTER = uuid4()
APPROVER = uuid4()


# ---------------------------------------------------------------------------
# Legal transitions (standard/normal path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "action", "type_", "expected"),
    [
        ("draft", "submit", "standard", "submitted"),
        ("draft", "submit", "normal", "submitted"),
        ("submitted", "approve", "standard", "approved"),
        ("submitted", "approve", "normal", "approved"),
        ("submitted", "reject", "standard", "rejected"),
        ("submitted", "reject", "normal", "rejected"),
        ("approved", "begin", "standard", "implementing"),
        ("approved", "begin", "normal", "implementing"),
        ("implementing", "complete", "standard", "completed"),
        ("implementing", "complete", "normal", "completed"),
        ("draft", "cancel", "standard", "cancelled"),
        ("submitted", "cancel", "standard", "cancelled"),
        ("approved", "cancel", "standard", "cancelled"),
        ("rejected", "revise", "standard", "draft"),
    ],
)
def test_legal_transitions(current: str, action: str, type_: str, expected: str) -> None:
    result = next_state(
        current=current,
        action=action,  # type: ignore[arg-type]
        type_=type_,
        created_by=REQUESTER,
        actor_id=APPROVER,
        has_retrospective_approval=False,
    )
    assert result == expected


# ---------------------------------------------------------------------------
# Emergency path
# ---------------------------------------------------------------------------


def test_emergency_begin_fast_path_from_submitted() -> None:
    result = next_state(
        current="submitted",
        action="begin",
        type_="emergency",
        created_by=REQUESTER,
        actor_id=APPROVER,
        has_retrospective_approval=False,
    )
    assert result == "implementing"


def test_emergency_complete_with_retrospective_approval() -> None:
    result = next_state(
        current="implementing",
        action="complete",
        type_="emergency",
        created_by=REQUESTER,
        actor_id=APPROVER,
        has_retrospective_approval=True,
    )
    assert result == "completed"


def test_emergency_complete_without_retrospective_approval_raises() -> None:
    with pytest.raises(ChangeValidationError):
        next_state(
            current="implementing",
            action="complete",
            type_="emergency",
            created_by=REQUESTER,
            actor_id=APPROVER,
            has_retrospective_approval=False,
        )


def test_emergency_retrospective_approve_returns_current_unchanged() -> None:
    result = next_state(
        current="implementing",
        action="approve",
        type_="emergency",
        created_by=REQUESTER,
        actor_id=APPROVER,
        has_retrospective_approval=False,
    )
    assert result == "implementing"


def test_emergency_retrospective_reject_returns_current_unchanged() -> None:
    result = next_state(
        current="implementing",
        action="reject",
        type_="emergency",
        created_by=REQUESTER,
        actor_id=APPROVER,
        has_retrospective_approval=False,
    )
    assert result == "implementing"


def test_emergency_approve_from_submitted_raises_nothing_to_retrospect() -> None:
    with pytest.raises(ChangeValidationError):
        next_state(
            current="submitted",
            action="approve",
            type_="emergency",
            created_by=REQUESTER,
            actor_id=APPROVER,
            has_retrospective_approval=False,
        )


def test_emergency_reject_from_submitted_raises_nothing_to_retrospect() -> None:
    with pytest.raises(ChangeValidationError):
        next_state(
            current="submitted",
            action="reject",
            type_="emergency",
            created_by=REQUESTER,
            actor_id=APPROVER,
            has_retrospective_approval=False,
        )


# ---------------------------------------------------------------------------
# Segregation of duties (SoD)
# ---------------------------------------------------------------------------


def test_sod_standard_approve_by_requester_raises() -> None:
    with pytest.raises(ChangeValidationError, match="approver cannot be the change requester"):
        next_state(
            current="submitted",
            action="approve",
            type_="standard",
            created_by=REQUESTER,
            actor_id=REQUESTER,
            has_retrospective_approval=False,
        )


def test_sod_standard_reject_by_requester_raises() -> None:
    with pytest.raises(ChangeValidationError, match="approver cannot be the change requester"):
        next_state(
            current="submitted",
            action="reject",
            type_="standard",
            created_by=REQUESTER,
            actor_id=REQUESTER,
            has_retrospective_approval=False,
        )


def test_sod_normal_approve_by_requester_raises() -> None:
    with pytest.raises(ChangeValidationError, match="approver cannot be the change requester"):
        next_state(
            current="submitted",
            action="approve",
            type_="normal",
            created_by=REQUESTER,
            actor_id=REQUESTER,
            has_retrospective_approval=False,
        )


def test_sod_emergency_retrospective_approve_by_requester_raises() -> None:
    with pytest.raises(ChangeValidationError, match="approver cannot be the change requester"):
        next_state(
            current="implementing",
            action="approve",
            type_="emergency",
            created_by=REQUESTER,
            actor_id=REQUESTER,
            has_retrospective_approval=False,
        )


def test_sod_emergency_retrospective_reject_by_requester_raises() -> None:
    with pytest.raises(ChangeValidationError, match="approver cannot be the change requester"):
        next_state(
            current="implementing",
            action="reject",
            type_="emergency",
            created_by=REQUESTER,
            actor_id=REQUESTER,
            has_retrospective_approval=False,
        )


def test_sod_takes_priority_over_illegal_current_state() -> None:
    # SoD guard fires first, even when current state would also be illegal
    # for a non-SoD-violating approve.
    with pytest.raises(ChangeValidationError, match="approver cannot be the change requester"):
        next_state(
            current="draft",
            action="approve",
            type_="standard",
            created_by=REQUESTER,
            actor_id=REQUESTER,
            has_retrospective_approval=False,
        )


# ---------------------------------------------------------------------------
# Illegal transitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "action", "type_"),
    [
        ("draft", "approve", "standard"),
        ("draft", "reject", "standard"),
        ("submitted", "submit", "standard"),
        ("completed", "submit", "standard"),
        ("draft", "complete", "standard"),
        ("draft", "begin", "standard"),
        ("submitted", "begin", "standard"),  # standard must be approved first
        ("draft", "revise", "standard"),
        ("submitted", "revise", "standard"),
        ("approved", "revise", "standard"),
        ("completed", "cancel", "standard"),
        ("implementing", "cancel", "standard"),
        ("rejected", "cancel", "standard"),
        ("cancelled", "submit", "standard"),
        ("completed", "begin", "standard"),
        ("approved", "submit", "standard"),
        ("implementing", "begin", "standard"),
        ("draft", "begin", "emergency"),
        ("approved", "begin", "emergency"),  # emergency skips approval, so approved->begin unmapped
        ("draft", "complete", "emergency"),
        ("submitted", "complete", "emergency"),
    ],
)
def test_illegal_transitions_raise(current: str, action: str, type_: str) -> None:
    with pytest.raises(ChangeValidationError):
        next_state(
            current=current,
            action=action,  # type: ignore[arg-type]
            type_=type_,
            created_by=REQUESTER,
            actor_id=APPROVER,
            has_retrospective_approval=True,
        )
