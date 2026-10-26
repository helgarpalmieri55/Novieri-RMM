"""Typed errors for the Service Desk module.

`TicketNotFoundError` subclasses `TicketValidationError` so a single
`except TicketValidationError` in a service catches both, while `api.py`
distinguishes them by `isinstance` with the SUBCLASS CHECKED FIRST to map
404 vs 400. Never route on exception-message text. `TicketClientError` (422)
and `TicketConflictError` (409) are subclasses for the same reason: the
email and alert paths that catch `TicketValidationError` catch them too.
"""


class TicketValidationError(Exception):
    """A request the service layer rejects -- becomes HTTP 400."""


class TicketNotFoundError(TicketValidationError):
    """A resource that does not exist under this org -- becomes HTTP 404.

    A cross-org id and a nonexistent id are deliberately indistinguishable
    (invariant #5): both raise this.
    """


class TicketClientError(TicketValidationError):
    """The client a ticket would be filed under is not one it may have --
    becomes HTTP 422 (MSP clients spec §3.2; plan 2026-10-26 Task 2).

    None named and no device to take it from; one that contradicts the
    device's; "Sin asignar" from anywhere but email (C10); a contact
    requester of another client; an assignee who cannot see the client.
    The request is well formed and the client exists -- it is the
    combination the service refuses, which is what 422 says elsewhere for a
    client (`services/clients.py`, `user_admin`).
    """


class TicketConflictError(TicketValidationError):
    """The client is archived and takes no new ticket -- becomes HTTP 409,
    the status every other row filed under a client gets for it
    (`services/client_sites.py::NotAcceptingNewRows`)."""
