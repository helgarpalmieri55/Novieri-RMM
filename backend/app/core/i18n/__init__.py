"""Server-side translation (docs/superpowers/specs/2026-10-09-spanish-language-design.md §3).

The message ID is the English template itself. `tr` formats with str.format
and nothing else: the catalogue is data, never code.

Log messages are NOT routed through `tr`: the logging kernel's static-template
rule keeps them literal English, and a person never reads them.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.core.i18n.es import CATALOG as CATALOG_ES

logger = logging.getLogger(__name__)

SUPPORTED = ("en", "es")
# A header is a few short tags; anything longer is not worth parsing.
_MAX_ACCEPT_LANGUAGE = 512
locale_var: ContextVar[str] = ContextVar("locale", default="en")

__all__ = [
    "CATALOG_ES",
    "N_",
    "SUPPORTED",
    "locale_var",
    "parse_accept_language",
    "tr",
    "tr_marked",
    "use_locale",
]


def tr(template: str, /, **params: object) -> str:
    """Translate and format. English formatting is the contract and raises on a
    template/parameter mismatch (tests/test_i18n_catalog.py keeps that from
    shipping). A catalogue value that cannot be formatted is a catalogue bug,
    not a reason to fail a request, so Spanish falls back to the English text."""
    if locale_var.get() == "es":
        translated = CATALOG_ES.get(template)
        if translated is not None:
            try:
                return translated.format(**params)
            except (KeyError, IndexError, AttributeError, ValueError, TypeError):
                # Static message, no parameter: the exception names the offending
                # field, and tests/test_i18n_catalog.py keeps this from shipping.
                logger.warning("spanish catalogue entry cannot be formatted", exc_info=True)
    return template.format(**params)


def N_(message: str, /) -> str:
    """Mark a message that is stored as data (a module-level table) so the
    catalogue guard can see it. Identity at runtime: the English stays the
    constant's value, and `tr_marked` translates it where it reaches a response."""
    return message


def tr_marked(message: str) -> str:
    """The Spanish for a message that was marked with `N_`, else the message.

    Never calls `.format`: a marked message is looked up verbatim, and one of
    them (an approved-action reason) contains a literal `{device_id}` that must
    reach the reader as written."""
    if locale_var.get() == "es":
        return CATALOG_ES.get(message, message)
    return message


@contextmanager
def use_locale(lang: str) -> Iterator[None]:
    token = locale_var.set(lang if lang in SUPPORTED else "en")
    try:
        yield
    finally:
        locale_var.reset(token)


def parse_accept_language(header: str | None) -> str:
    """The highest-weighted supported language, or English.

    `q=0` means "not acceptable" (RFC 9110 §12.4.2) and is never chosen; a
    malformed weight skips that entry; ties go to the client's own order.
    """
    best, best_q = "en", 0.0
    for part in (header or "")[:_MAX_ACCEPT_LANGUAGE].split(","):
        tag, *params = (piece.strip() for piece in part.split(";"))
        lang = tag.split("-")[0].lower()
        if lang not in SUPPORTED:
            continue
        weight = 1.0
        for param in params:
            name, _, value = param.partition("=")
            if name.strip().lower() == "q":
                try:
                    weight = float(value)
                except ValueError:
                    weight = 0.0
        if weight > best_q:
            best, best_q = lang, weight
    return best
