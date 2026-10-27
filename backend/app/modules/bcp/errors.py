"""Typed exceptions for the BCP/DR module (docs/modules/bcp/spec.md).

Mirrors `app/modules/change_mgmt/state.py`'s exception style: a single
validation error base that the API routes map to HTTP 400, and subclasses
the routes map by isinstance check -- not found 404, conflict 409, another
client's row 422 -- so existing `except BcpValidationError` handlers still
catch them.
"""


class BcpValidationError(Exception):
    """Bad request — invalid input or a cross-org/nonexistent reference
    (maps to HTTP 400)."""


class BcpNotFoundError(BcpValidationError):
    """A BCP row was not found in this org (maps to HTTP 404). Subclass so
    existing `except BcpValidationError` handlers still catch it; the API
    routes 404 by isinstance."""


class BcpConflictError(BcpValidationError):
    """The request conflicts with what exists (maps to HTTP 409): a plan filed
    under a client that is archived (`client_for_new_rows`)."""


class BcpClientError(BcpValidationError):
    """A plan of one client naming another client's device, group, article or
    change -- or "Sin asignar" as a plan's client (maps to HTTP 422; plan
    2026-10-26 Task 7)."""
