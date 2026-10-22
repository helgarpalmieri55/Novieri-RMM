"""The console's words for the wire values the budget messages say.

`say(table, value)` (app/core/wire_words.py) returns the wire value itself in
English, so every English message is what it always was.
"""

from app.core.i18n import N_

GRANULARITY_WORDS = {
    "month": N_("budget granularity: month"),
    "quarter": N_("budget granularity: quarter"),
}
EXPENSE_STATUS_WORDS = {
    "expected": N_("budget expense status: expected"),
    "confirmed": N_("budget expense status: confirmed"),
    "void": N_("budget expense status: void"),
}
EXPENSE_SOURCE_WORDS = {
    "manual": N_("budget expense source: manual"),
    "subscription": N_("budget expense source: subscription"),
    "license": N_("budget expense source: license"),
}
# With the article, so "no se encontró la suscripción X" agrees.
LINK_TYPE_WORDS = {
    "subscription": N_("budget link type: subscription"),
    "license": N_("budget link type: license"),
    "device": N_("budget link type: device"),
    "change": N_("budget link type: change"),
}
