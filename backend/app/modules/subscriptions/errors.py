"""Typed exceptions for the Subscriptions module (docs/modules/subscriptions/spec.md).

Mirrors `app/modules/licenses/errors.py`'s exception style: a single
validation error base that the API routes map to HTTP 400, and subclasses the
routes map by isinstance check -- not found 404, conflict 409, another
client's row 422 -- so existing `except SubscriptionValidationError`
handlers still catch them.
"""


class SubscriptionValidationError(Exception):
    """Bad request — invalid input or a cross-org/nonexistent reference
    (maps to HTTP 400)."""


class SubscriptionNotFoundError(SubscriptionValidationError):
    """A subscriptions-module row was not found in this org (maps to HTTP
    404). Subclass so existing `except SubscriptionValidationError` handlers
    still catch it; the API routes 404 by isinstance."""


class SubscriptionConflictError(SubscriptionValidationError):
    """The request conflicts with what exists (maps to HTTP 409): a
    subscription filed under a client that is archived."""


class SubscriptionClientError(SubscriptionValidationError):
    """A row of one client naming another client's -- a seat's person, the
    license a subscription refers to -- or "Sin asignar" as a subscription's
    client (maps to HTTP 422; plan 2026-10-26 Task 6)."""
