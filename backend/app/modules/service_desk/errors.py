"""Typed errors for the Service Desk module.

`TicketNotFoundError` subclasses `TicketValidationError` so a single
`except TicketValidationError` in a service catches both, while `api.py`
distinguishes them by `isinstance` with the SUBCLASS CHECKED FIRST to map
404 vs 400. Never route on exception-message text.
"""


class TicketValidationError(Exception):
    """A request the service layer rejects -- becomes HTTP 400."""


class TicketNotFoundError(TicketValidationError):
    """A resource that does not exist under this org -- becomes HTTP 404.

    A cross-org id and a nonexistent id are deliberately indistinguishable
    (invariant #5): both raise this.
    """
