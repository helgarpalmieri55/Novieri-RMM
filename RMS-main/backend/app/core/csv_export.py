"""Shared CSV-export formula-injection guard (docs/03-security-model.md is
silent on it specifically, but it is the same family of bug as every other
"operator-controlled text reaches a context that interprets it" issue this
codebase treats as a security defect, not a cosmetic one).

Five modules -- budget (both expense exports), inventory, security_logs, and
backup/patch (their compliance-evidence exports) -- write operator- or
agent-supplied free text straight into ``csv.writer``: expense descriptions,
invoice references, vendor/category/cost-center names, hostnames, usernames,
error strings. A cell whose value BEGINS with ``=``, ``+``, ``-``, or ``@``
is executed as a formula the instant the file is opened in Excel or Google
Sheets ("CSV injection") -- e.g. a vendor named
``=HYPERLINK("http://evil","x")`` or ``@SUM(1,1)`` in an expense record
becomes a live formula in the next person's spreadsheet, and budget's Odoo
export is explicitly the one `deploy/README.md` tells operators to point an
ERP import job at, so it is the most exposed of the five.

**The fix neutralizes; it never rejects or strips.** A naive rule that drops
or refuses a leading ``-`` would silently mangle a legitimate negative
amount or a credit note -- budget's exports carry signed money, and quietly
altering a financial figure to dodge a spreadsheet bug is a strictly WORSE
defect than the one being fixed here. Instead, every dangerous leading
character is neutralized by PREFIXING -- never truncating -- the cell with a
single quote: the standard mitigation (OWASP "CSV Injection"). Excel/Sheets
treats a leading ``'`` as an explicit "force text" marker and does not
display it, so a reader opening the export in a spreadsheet sees the exact
original text (a vendor named ``-50 Corp``, a description starting with
``+VAT``), just rendered as text instead of executed as a formula. A
plain-text/``csv.reader`` view of the raw file (bypassing Excel's quote
convention) shows the literal leading quote; that is the visible,
inspectable cost of this mitigation, not a bug in it.

**This function is applied PER COLUMN by each caller, never blanket over an
entire row.** Only a column that can actually hold operator- or
agent-supplied free text (a vendor/category/cost-center name, a
description, an invoice reference, a hostname, a username, an error
string, ...) is a candidate for a formula payload in the first place. A
column the server itself formatted -- a ``Decimal`` amount stringified for
the wire, a ``date``/``datetime`` isoformat, a UUID, a fixed-set status/
enum value -- can never begin with ``=``/``+``/``-``/``@`` for any reason
OTHER than "the value is legitimately negative" (a signed amount) or
"today's date starts with a digit" (never a problem); there is no
injection surface there to guard, and running this function over it anyway
would actively corrupt it for a machine reader. Concretely: budget's Odoo
export contract (spec §7, pinned) is read by an ERP importer, not opened in
Excel -- it has no notion of a leading ``'`` meaning "force text" -- so
escaping its ``amount``/``amount_base`` columns would hand the importer the
literal string ``'-100.00`` for every credit note or negative figure, an
import failure at best and a silently wrong booked figure at worst. Each
call site therefore names its own free-text columns explicitly and leaves
every numeric/date/id/enum column untouched.
"""

from typing import Any

# Every leading character Excel/Sheets treats as "this cell is a formula".
_DANGEROUS_PREFIXES = ("=", "+", "-", "@")


def escape_formula(value: Any) -> Any:
    """Return ``value`` unchanged UNLESS it is a string beginning with one of
    ``=``, ``+``, ``-``, ``@`` -- in which case it is returned prefixed with
    a single quote, so it round-trips as inert text rather than executing as
    a formula.

    Callers must apply this ONLY to columns that can carry operator-/agent-
    supplied free text (module docstring) -- never to a column the server
    itself formatted from a ``Decimal``/date/UUID/enum, where a leading
    dangerous character (if any) reflects genuine data, e.g. a negative
    amount, not an injection attempt.

    Non-string values (``None``, ``Decimal``, ``int``, ``UUID``,
    ``datetime``, ...) pass through untouched: every call site here already
    converts a value to its final CSV-cell string (or leaves it ``None``/
    empty) before this runs, so this function is purely the injection guard,
    never a general-purpose formatter."""
    if isinstance(value, str) and value.startswith(_DANGEROUS_PREFIXES):
        return "'" + value
    return value
