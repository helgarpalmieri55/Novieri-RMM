"""The console's words for the wire values the remote-session messages say
(`enums.RemoteSessionState` in `frontend/src/i18n/es/enums.ts`).

`say(table, value)` (app/core/wire_words.py) returns the wire value itself in
English, so every English message is what it always was.
"""

from app.core.i18n import N_

SESSION_STATE_WORDS = {
    "awaiting_consent": N_("remote session state: awaiting_consent"),
    "connecting": N_("remote session state: connecting"),
    "active": N_("remote session state: active"),
    "ended": N_("remote session state: ended"),
}
