"""`app.core.attachments.sanitize_attachment_filename` -- the shared
attachment-filename sanitizer (extracted from `app.modules.budget.api`, now
also consumed by `app.modules.service_desk.threads`, per that module's
docstring).

The first block below is Budget's original sanitizer unit-test suite,
**moved here unchanged** -- same imports (`app.modules.budget.api.
_sanitize_invoice_filename`, `app.modules.budget.errors.
BudgetValidationError`), same assertions, same docstrings. Not one assertion
was edited to make the move go green: if the extraction had changed
behavior, one of these would have needed editing, and per the brief for
that work, that would have meant the extraction was wrong, not that the
test needed updating. `tests/test_budget_api.py` no longer defines these;
`pytest tests/test_budget_api.py tests/test_core_attachments.py -q` is the
gate.

The second block tests the shared function directly, the way a caller with
no module-specific wrapper would use it: it raises a plain `ValueError`
(never `BudgetValidationError`/`TicketValidationError` itself, since it has
no business picking a side between two otherwise-unrelated exception
hierarchies -- see the module docstring in `app.core.attachments`).

Driven DIRECTLY rather than through any API client, deliberately, in both
blocks: the in-process ASGI harness does not run h11, so a filename that
produces an illegal header value still returns 200 there while failing hard
under uvicorn in production -- an HTTP-level test literally cannot observe
the bug class this function exists to prevent. Asserting on the function
itself is the only way to hold it.
"""

import pytest

from app.core.attachments import sanitize_attachment_filename
from app.modules.budget.api import _sanitize_invoice_filename
from app.modules.budget.errors import BudgetValidationError

# ---------------------------------------------------------------------------
# Moved verbatim from tests/test_budget_api.py -- unchanged.
# ---------------------------------------------------------------------------


def test_sanitize_rejects_a_trailing_newline() -> None:
    """Python's `$` matches immediately BEFORE a trailing newline, so a
    `^...$` allow-list accepts "a.pdf\\n". That value has no path separator
    and survives basename, so it reaches Content-Disposition and every
    subsequent download dies in h11 with `Illegal header value` -- the same
    CRLF poisoning the allow-list exists to stop, one character different.
    `fullmatch` is what closes it."""
    for bad in ("a.pdf\n", "a.pdf\r", "a.pdf\r\n", "a.pdf\n\n"):
        with pytest.raises(BudgetValidationError):
            _sanitize_invoice_filename(bad)


def test_sanitize_rejects_non_ascii() -> None:
    """Bare `\\w` is Unicode-wide. Starlette encodes header values as latin-1,
    so an ordinary non-Latin invoice name uploads fine and then makes EVERY
    later download of it a 500 for the whole org. That is normal usage in a
    non-Latin locale, not an attack -- rejecting at upload gives the user an
    immediate, fixable 400 instead."""
    for bad in ("発票.pdf", "счёт.pdf", "İnvoice.pdf"):
        with pytest.raises(BudgetValidationError):
            _sanitize_invoice_filename(bad)


def test_sanitize_rejects_paths_traversal_and_empty() -> None:
    for bad in ("", "..", "../x.pdf", "a/b.pdf", "C:\\evil\\x.pdf", "x" * 256):
        with pytest.raises(BudgetValidationError):
            _sanitize_invoice_filename(bad)


def test_sanitize_accepts_ordinary_names_unchanged() -> None:
    for good in ("invoice.pdf", "INV-2026 03.pdf", "invoice", "a_b-c.1.pdf"):
        assert _sanitize_invoice_filename(good) == good


def test_sanitized_value_is_header_safe() -> None:
    """The property that actually matters: whatever survives sanitization must
    be encodable into a latin-1 header value without raising."""
    for good in ("invoice.pdf", "INV-2026 03.pdf", "a_b-c.1.pdf"):
        header = f'attachment; filename="{_sanitize_invoice_filename(good)}"'
        header.encode("latin-1")


# ---------------------------------------------------------------------------
# The shared `app.core.attachments.sanitize_attachment_filename` itself --
# same fixtures, but asserting the bare `ValueError` contract every caller's
# thin wrapper (`_sanitize_invoice_filename`, `threads.
# sanitize_attachment_filename`) relies on and translates.
# ---------------------------------------------------------------------------


def test_core_sanitizer_rejects_a_trailing_newline() -> None:
    """B5 (whole-branch review, round B): `match="."` matches ANY non-empty
    message -- it would still pass if `sanitize_attachment_filename` raised
    for a completely unrelated reason. None of these three inputs contain a
    path separator or `..`, so all three fall through to the allow-list
    check and raise the SAME real message, `"attachment filename is
    invalid"` (`app.core.attachments.sanitize_attachment_filename`)."""
    for bad in ("a.pdf\n", "a.pdf\r", "a.pdf\r\n"):
        with pytest.raises(ValueError, match="attachment filename is invalid"):
            sanitize_attachment_filename(bad)


def test_core_sanitizer_rejects_non_ascii() -> None:
    for bad in ("発票.pdf", "счёт.pdf"):
        with pytest.raises(ValueError, match="attachment filename is invalid"):
            sanitize_attachment_filename(bad)


def test_core_sanitizer_rejects_paths_traversal_and_empty() -> None:
    """The traversal/empty inputs all trip the FIRST check (`not filename or
    "/" in filename or "\\\\" in filename or ".." in filename`) and raise
    `"attachment filename must not contain a path segment"`; the over-length
    name has none of those and instead trips the allow-list/length check
    below it, raising the OTHER real message, `"attachment filename is
    invalid"` -- split out so each assertion states which message it expects,
    rather than a single `match="."` that would pass for either (or neither)."""
    for bad in ("", "..", "../x.pdf", "a/b.pdf", "C:\\evil\\x.pdf"):
        with pytest.raises(ValueError, match="attachment filename must not contain a path segment"):
            sanitize_attachment_filename(bad)

    with pytest.raises(ValueError, match="attachment filename is invalid"):
        sanitize_attachment_filename("x" * 256)


def test_core_sanitizer_accepts_ordinary_names_unchanged() -> None:
    for good in ("invoice.pdf", "INV-2026 03.pdf", "invoice", "a_b-c.1.pdf"):
        assert sanitize_attachment_filename(good) == good


def test_core_sanitizer_value_is_header_safe() -> None:
    for good in ("invoice.pdf", "INV-2026 03.pdf", "a_b-c.1.pdf"):
        header = f'attachment; filename="{sanitize_attachment_filename(good)}"'
        header.encode("latin-1")
