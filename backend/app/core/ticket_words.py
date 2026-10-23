"""Ticket status and type words said inside a sentence (shared by service_desk,
portal and notification, which may not import each other).

The English messages print a wire value (`repr(status)` -> `'closed'`, or the bare
`closed`). The Spanish is the console's own word for it (`enums.TicketStatus`,
`enums.TicketType` in `frontend/src/i18n/es/enums.ts`), so a refusal on a Spanish
screen names the status the way the status pill does.

Every table is `N_`-marked and applied with `tr_marked`: in English a lookup returns
exactly what was printed before (the repr, or the raw word); a value no table knows
is printed as it arrived in both languages. The quoted forms (`'closed'`) are keyed
with their quotes so the English stays byte-identical; the Spanish puts the
console's word in «», as the console does ("Pasar este ticket a «Cancelado»").
"""

from app.core.i18n import N_, tr_marked

# The bare words are lowercase running text; the quoted forms carry the console's
# own capitalisation.
_STATUS_WORDS = {
    "new": N_("new"),
    "open": N_("open"),
    "pending": N_("pending"),
    "resolved": N_("resolved"),
    "closed": N_("closed"),
    "cancelled": N_("cancelled"),
    "pending_approval": N_("pending_approval"),
    "in_progress": N_("in_progress"),
    "fulfilled": N_("fulfilled"),
    "rejected": N_("rejected"),
}
_STATUS_QUOTED = {
    "'new'": N_("'new'"),
    "'open'": N_("'open'"),
    "'pending'": N_("'pending'"),
    "'resolved'": N_("'resolved'"),
    "'closed'": N_("'closed'"),
    "'cancelled'": N_("'cancelled'"),
    "'pending_approval'": N_("'pending_approval'"),
    "'in_progress'": N_("'in_progress'"),
    "'fulfilled'": N_("'fulfilled'"),
    "'rejected'": N_("'rejected'"),
}
_TYPE_WORDS = {
    "incident": N_("incident"),
    "request": N_("request"),
}
_TYPE_QUOTED = {
    "'incident'": N_("'incident'"),
    "'request'": N_("'request'"),
}

TICKET_STATUS_WORDS = _STATUS_WORDS


def word(table: dict[str, str], value: object) -> str:
    """`value` as the table says it in the current language, else as it is."""
    text = str(value)
    return tr_marked(table[text]) if text in table else text


def status_word(status: object) -> str:
    """A bare status (`closed`)."""
    return word(_STATUS_WORDS, status)


def status_quoted(status: object) -> str:
    """A status as `repr` printed it (`'closed'`)."""
    text = repr(status)
    return tr_marked(_STATUS_QUOTED[text]) if text in _STATUS_QUOTED else text


def type_word(ticket_type: object) -> str:
    """A bare ticket type (`incident`)."""
    return word(_TYPE_WORDS, ticket_type)


def type_quoted(ticket_type: object) -> str:
    """A ticket type as `repr` printed it (`'incident'`)."""
    text = repr(ticket_type)
    return tr_marked(_TYPE_QUOTED[text]) if text in _TYPE_QUOTED else text
