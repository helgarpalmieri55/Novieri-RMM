"""The console's words for the wire values the change-management messages say
(`enums.ChangeState` and the transition buttons in `frontend/src/i18n/es/change.ts`).

`say(table, value)` (app/core/wire_words.py) returns the wire value itself in
English, so every English message is what it always was.
"""

from app.core.i18n import N_

CHANGE_STATE_WORDS = {
    "draft": N_("change state: draft"),
    "submitted": N_("change state: submitted"),
    "approved": N_("change state: approved"),
    "implementing": N_("change state: implementing"),
    "completed": N_("change state: completed"),
    "rejected": N_("change state: rejected"),
    "cancelled": N_("change state: cancelled"),
}
CHANGE_ACTION_WORDS = {
    "submit": N_("change action: submit"),
    "approve": N_("change action: approve"),
    "reject": N_("change action: reject"),
    "begin": N_("change action: begin"),
    "complete": N_("change action: complete"),
    "cancel": N_("change action: cancel"),
    "revise": N_("change action: revise"),
}
