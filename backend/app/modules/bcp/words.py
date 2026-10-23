"""The console's words for the wire values the BCP messages say
(`scopeKind` in `frontend/src/i18n/es/bcp.ts`).

`say(table, value)` (app/core/wire_words.py) returns the wire value itself in
English, so every English message is what it always was.
"""

from app.core.i18n import N_

SCOPE_KIND_WORDS = {
    "device": N_("bcp scope kind: device"),
    "group": N_("bcp scope kind: group"),
}
