"""Typed exceptions for the Licenses/SAM module (docs/modules/licenses/spec.md).

Mirrors `app/modules/bcp/errors.py`'s exception style: a single validation
error base that the API routes map to HTTP 400, and a not-found subclass
that routes map to HTTP 404 by isinstance check — existing
`except LicenseValidationError` handlers still catch it.
"""


class LicenseValidationError(Exception):
    """Bad request — invalid input or a cross-org/nonexistent reference
    (maps to HTTP 400)."""


class LicenseNotFoundError(LicenseValidationError):
    """A licenses-module row was not found in this org (maps to HTTP 404).
    Subclass so existing `except LicenseValidationError` handlers still
    catch it; the API routes 404 by isinstance."""
