"""Typed exceptions for the BCP/DR module (docs/modules/bcp/spec.md).

Mirrors `app/modules/change_mgmt/state.py`'s exception style: a single
validation error base that the API routes map to HTTP 400, and a
not-found subclass that routes map to HTTP 404 by isinstance check —
existing `except BcpValidationError` handlers still catch it.
"""


class BcpValidationError(Exception):
    """Bad request — invalid input or a cross-org/nonexistent reference
    (maps to HTTP 400)."""


class BcpNotFoundError(BcpValidationError):
    """A BCP row was not found in this org (maps to HTTP 404). Subclass so
    existing `except BcpValidationError` handlers still catch it; the API
    routes 404 by isinstance."""
