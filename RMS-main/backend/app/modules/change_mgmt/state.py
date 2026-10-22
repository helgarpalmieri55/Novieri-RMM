"""Pure change-request state machine (docs/modules/change_management/spec.md
state transitions).

`next_state` is a pure function: no DB, no session, no I/O. It takes the
current `change_request.state`, the requested action, the change `type_`
("standard"/"normal"/"emergency"), the requester and acting-user ids, and
whether a retrospective approval has already been recorded, and returns the
new state or raises `ChangeValidationError` for an illegal transition or a
segregation-of-duties (SoD) violation.

Emergency changes follow a different shape than standard/normal ones:
- `begin` is a fast-path from `submitted` straight to `implementing` — no
  prior approval is required.
- `approve`/`reject` on an emergency change *after* it has started
  (`current == "implementing"`) are retrospective decisions: they do not
  drive the run state forward, so the row's `state` column is left
  unchanged (the decision itself is recorded elsewhere, e.g. in
  `change_approval`).
- `complete` is refused unless a retrospective approval has already been
  recorded (`has_retrospective_approval`), regardless of a retrospective
  reject having been attempted separately.

The SoD guard (the acting user may never be the change's own requester) is
checked before any state-specific logic for `approve`/`reject`, in both the
standard/normal and emergency paths.
"""

from typing import Literal
from uuid import UUID

from app.core.i18n import tr
from app.core.wire_words import say
from app.modules.change_mgmt.words import CHANGE_ACTION_WORDS, CHANGE_STATE_WORDS


class ChangeValidationError(Exception):
    pass


class ChangeNotFoundError(ChangeValidationError):
    """A change/link/target could not be found in this org (maps to HTTP 404)."""


def _illegal(action: str, current: str) -> ChangeValidationError:
    """`action` is not allowed from `current` (words as the console says them)."""
    return ChangeValidationError(
        tr(
            "illegal transition: {action} from {current}",
            action=say(CHANGE_ACTION_WORDS, action),
            current=say(CHANGE_STATE_WORDS, current, marked=True),
        )
    )


ChangeAction = Literal["submit", "approve", "reject", "begin", "complete", "cancel", "revise"]

_EMERGENCY = "emergency"
_STANDARD_TYPES = {"standard", "normal"}

# Transitions that do not depend on `type_` or the SoD guard.
_SIMPLE_TRANSITIONS: dict[tuple[str, ChangeAction], str] = {
    ("draft", "submit"): "submitted",
    ("draft", "cancel"): "cancelled",
    ("submitted", "cancel"): "cancelled",
    ("approved", "cancel"): "cancelled",
    ("rejected", "revise"): "draft",
}

# Approve/reject target state for standard/normal changes, keyed by action.
_STANDARD_DECISION_TARGET: dict[str, str] = {
    "approve": "approved",
    "reject": "rejected",
}


def next_state(
    *,
    current: str,
    action: ChangeAction,
    type_: str,
    created_by: UUID,
    actor_id: UUID,
    has_retrospective_approval: bool,
) -> str:
    """Compute the next `change_request.state`, or raise `ChangeValidationError`."""
    if action in ("approve", "reject"):
        if actor_id == created_by:
            raise ChangeValidationError(tr("approver cannot be the change requester"))

        if type_ == _EMERGENCY:
            # Retrospective decision: does not move the run state.
            if current != "implementing":
                raise _illegal(action, current)
            return current

        if type_ in _STANDARD_TYPES and current == "submitted":
            return _STANDARD_DECISION_TARGET[action]

        raise _illegal(action, current)

    if action == "begin":
        if type_ == _EMERGENCY:
            if current == "submitted":
                return "implementing"
            raise _illegal(action, current)

        if current == "approved":
            return "implementing"
        raise _illegal(action, current)

    if action == "complete":
        if current != "implementing":
            raise _illegal(action, current)
        if type_ == _EMERGENCY and not has_retrospective_approval:
            raise ChangeValidationError(
                tr("emergency change requires retrospective approval before completion")
            )
        return "completed"

    key = (current, action)
    if key in _SIMPLE_TRANSITIONS:
        return _SIMPLE_TRANSITIONS[key]

    raise _illegal(action, current)
