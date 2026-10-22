"""Shared attachment-filename sanitizer.

Extracted from `app.modules.budget.api` (its expense-invoice attachment
upload, P1(b) of the post-review controller fix,
`.git/sdd/task-12-report.md`) so Service Desk's ticket attachments
(`docs/modules/service-desk/spec.md` §2, §6 -- "storage follows the hardened
path Budget landed ... and does not re-derive it") can consume the identical,
already-hardened logic rather than a second copy of it. Two verbatim copies
of security-critical logic is the shape that produced this codebase's worst
defect (two implementations of one guard, 160 lines apart, silently
disagreeing) -- `app.core.csv_export` and `app.core.http` are the established
precedent for pulling a security-relevant helper like this one out to a
shared module instead.

This module makes no assumption about which caller is using it and raises a
plain `ValueError` on any invalid input -- each caller (`app.modules.budget.
api._sanitize_invoice_filename`, `app.modules.service_desk.threads.
sanitize_attachment_filename`) catches that and re-raises its own module's
typed validation error (`BudgetValidationError`, `TicketValidationError`), so
this shared function never has to pick a side between the two modules'
otherwise-unrelated exception hierarchies.

The extraction is MECHANICAL: both load-bearing regex details below are
carried over unchanged, together with the reasoning that pins them, because
each was a live production bug before it was caught here.
"""

import os
import re

from app.core.i18n import tr

# A conservative allow-list for the sanitized DISPLAY filename. `\r`/`\n`
# (CRLF) and `:` are deliberately excluded -- both are how the raw filename
# could otherwise poison a `Content-Disposition` header.
#
# Two subtleties, each of which was a live bug before it was pinned here:
#
# 1. `re.ASCII`, because bare `\w` is Unicode-wide and matches CJK/Cyrillic/
#    etc. Starlette encodes header values as latin-1, so a perfectly ordinary
#    non-Latin invoice name ("発票.pdf") uploaded fine and then made EVERY
#    later download of it a hard 500 (UnicodeEncodeError) for the whole org.
#    That is ordinary usage in a non-Latin locale, not an attack.
# 2. `fullmatch`, not `match`: Python's `$` also matches immediately BEFORE a
#    trailing newline, so `^[\w.\- ]+$` accepts "a.pdf\n". That value has no
#    path separator and survives basename, so it reached the header and
#    killed every download with h11's `LocalProtocolError: Illegal header
#    value` -- the exact CRLF poisoning this allow-list exists to stop, one
#    character different. The in-process ASGI test harness does not run
#    h11, so a test driven through an API client cannot see it: the
#    sanitizer is unit-tested directly for precisely that reason.
_FILENAME_RE = re.compile(r"[\w.\- ]+", re.ASCII)


def sanitize_attachment_filename(filename: str) -> str:
    """Sanitize a client-supplied display filename BEFORE it ever reaches an
    object-storage key or a response header. Reject outright -- never
    store-and-hope -- rather than silently truncate to a trailing path
    segment: a raw value containing `..` or a path separator is a traversal
    attempt on its face, even when the object key itself has no filename
    component left to escape (both callers derive their key entirely
    server-side). What survives that check still goes through
    `os.path.basename` (defence in depth) and then the conservative
    allow-list above; empty (post-basename) and over-255-char names are
    rejected too.

    Raises a plain `ValueError` on any rejection -- callers translate it into
    their own module's typed validation error, never propagate `ValueError`
    itself past the module boundary.
    """
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError(tr("attachment filename must not contain a path segment"))
    name = os.path.basename(filename)
    if not name or len(name) > 255 or not _FILENAME_RE.fullmatch(name):
        raise ValueError(tr("attachment filename is invalid"))
    return name
