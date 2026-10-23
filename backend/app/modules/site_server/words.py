"""The console's words for the wire values the site-server messages say
(`roles` in `frontend/src/i18n/es/siteServers.ts`).

`say_list(table, values)` (app/core/wire_words.py) returns the list's own text in
English, so every English message is what it always was.

"""

from app.core.i18n import N_

__all__ = ["ROLE_WORDS"]

ROLE_WORDS = {
    "wake": N_("site server role: wake"),
    "cache": N_("site server role: cache"),
    "pxe": N_("site server role: pxe"),
}
