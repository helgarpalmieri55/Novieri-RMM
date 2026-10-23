"""The ticket state machine (docs/modules/service-desk/spec.md §3).

Asserts the INVALID transitions as hard as the valid ones. A state machine
tested only on its happy path is unproven: the whole point is what it refuses.
"""

import pytest

from app.modules.service_desk.errors import TicketValidationError
from app.modules.service_desk.lifecycle import (
    TERMINAL_STATUSES,
    assert_transition,
    can_transition,
    initial_status,
)


def test_incident_starts_new_and_walks_to_closed() -> None:
    assert initial_status("incident", requires_approval=False) == "new"
    for a, b in [
        ("new", "open"),
        ("open", "pending"),
        ("pending", "open"),
        ("open", "resolved"),
        ("resolved", "closed"),
    ]:
        assert can_transition("incident", a, b), f"{a} -> {b} should be allowed"


def test_request_skips_approval_when_category_requires_none() -> None:
    assert initial_status("request", requires_approval=False) == "in_progress"
    assert initial_status("request", requires_approval=True) == "pending_approval"


def test_request_walks_approval_to_closed() -> None:
    for a, b in [
        ("pending_approval", "in_progress"),
        ("pending_approval", "rejected"),
        ("in_progress", "fulfilled"),
        ("fulfilled", "closed"),
    ]:
        assert can_transition("request", a, b), f"{a} -> {b} should be allowed"


def test_reopen_is_allowed_from_resolved_and_closed() -> None:
    assert can_transition("incident", "resolved", "open")
    assert can_transition("incident", "closed", "open")
    assert can_transition("request", "fulfilled", "in_progress")
    assert can_transition("request", "closed", "in_progress")


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES - {"closed"}))
def test_no_transition_leaves_cancelled_or_rejected(terminal: str) -> None:
    """`closed` is deliberately reopenable; `cancelled` and `rejected` are not.
    A cancelled ticket that can be revived is a ticket whose history lies."""
    ticket_type = "request" if terminal == "rejected" else "incident"
    for target in ("new", "open", "in_progress", "resolved", "closed"):
        assert not can_transition(ticket_type, terminal, target)


def test_incident_cannot_use_request_statuses() -> None:
    assert not can_transition("incident", "new", "pending_approval")
    assert not can_transition("incident", "new", "in_progress")
    assert not can_transition("request", "new", "resolved")
    assert not can_transition("request", "in_progress", "pending")


def test_cannot_skip_states() -> None:
    assert not can_transition("incident", "new", "closed")
    assert not can_transition("incident", "new", "resolved")
    assert not can_transition("request", "pending_approval", "fulfilled")


def test_assert_transition_names_both_statuses() -> None:
    with pytest.raises(TicketValidationError) as exc:
        assert_transition("incident", "new", "closed")
    assert "new" in str(exc.value) and "closed" in str(exc.value)


def test_cancel_is_reachable_from_every_non_terminal_state() -> None:
    for status in ("new", "open", "pending", "resolved"):
        assert can_transition("incident", status, "cancelled")
    for status in ("new", "pending_approval", "in_progress", "fulfilled"):
        assert can_transition("request", status, "cancelled")


def test_pending_resolves_directly_without_bouncing_through_open() -> None:
    """The third party comes back and the fix lands from `pending`. Requiring a
    detour through `open` first would make the history claim a step that never
    happened.

    The spec's §3.1 diagram originally drew row 1 as a linear chain, which read
    literally as "every incident must pass through `pending` to reach
    `resolved`" -- the inverse of the intent. Both the diagram and this edge
    were corrected together; this test pins the behavior so the next reader of
    that picture cannot re-derive the wrong machine.
    """
    assert can_transition("incident", "pending", "resolved")
    assert can_transition("incident", "open", "resolved")


def test_unknown_ticket_type_is_rejected_not_silently_allowed() -> None:
    with pytest.raises(TicketValidationError):
        initial_status("problem", requires_approval=False)
    with pytest.raises(TicketValidationError):
        assert_transition("problem", "new", "open")
    assert not can_transition("problem", "new", "open")


def test_final_statuses_excludes_closed_but_terminal_includes_it() -> None:
    """`closed` gates edits yet stays reopenable, so the two sets differ. A
    caller meaning "never moves again" must reach for FINAL_STATUSES; using
    TERMINAL_STATUSES on the transition path would break every reopen."""
    from app.modules.service_desk.lifecycle import FINAL_STATUSES

    assert "closed" in TERMINAL_STATUSES
    assert "closed" not in FINAL_STATUSES
    assert FINAL_STATUSES == {"cancelled", "rejected"}
    for status in FINAL_STATUSES:
        ticket_type = "request" if status == "rejected" else "incident"
        for target in ("new", "open", "in_progress", "resolved", "closed"):
            assert not can_transition(ticket_type, status, target)
