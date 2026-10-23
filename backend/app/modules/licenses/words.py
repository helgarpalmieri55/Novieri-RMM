"""The console's words for the wire values the licence messages say
(`licenses.device` / `licenses.person` in `frontend/src/i18n/es/licenses.ts`).

`say(table, value)` (app/core/wire_words.py) returns the wire value itself in
English, so every English message is what it always was.
"""

from app.core.i18n import N_

ALLOCATION_TARGET_WORDS = {
    "device": N_("license allocation target: device"),
    "user": N_("license allocation target: user"),
    "person": N_("license allocation target: person"),
}
