"""Typed errors for the Notification module (docs/modules/notification/spec.md).

`NotificationNotFoundError` subclasses `NotificationValidationError` so a
single `except NotificationValidationError` handler in the API layer covers
both, and routes distinguish them by `isinstance` with the subclass checked
first to map 404 vs 400 -- matching `app/modules/service_desk/errors.py`'s
convention.
"""


class NotificationValidationError(Exception):
    """A request the service layer rejects -- becomes HTTP 400."""


class NotificationNotFoundError(NotificationValidationError):
    """A referenced object does not exist in the caller's org -- becomes HTTP
    404. A cross-org id and a nonexistent id are deliberately
    indistinguishable (invariant #5): both raise this."""
