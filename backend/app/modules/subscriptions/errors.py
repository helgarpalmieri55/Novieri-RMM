"""Typed exceptions for the Subscriptions module (docs/modules/subscriptions/spec.md).

Mirrors `app/modules/licenses/errors.py`'s exception style: a single
validation error base that the API routes map to HTTP 400, and a not-found
subclass that routes map to HTTP 404 by isinstance check — existing
`except SubscriptionValidationError` handlers still catch it.
"""


class SubscriptionValidationError(Exception):
    """Bad request — invalid input or a cross-org/nonexistent reference
    (maps to HTTP 400)."""


class SubscriptionNotFoundError(SubscriptionValidationError):
    """A subscriptions-module row was not found in this org (maps to HTTP
    404). Subclass so existing `except SubscriptionValidationError` handlers
    still catch it; the API routes 404 by isinstance."""
