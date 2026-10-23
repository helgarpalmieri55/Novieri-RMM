"""Typed exceptions for the Budget module (docs/modules/budget/spec.md).

Mirrors `app/modules/subscriptions/errors.py`'s exception style: a single
validation error base that the API routes map to HTTP 400, and a not-found
subclass that routes map to HTTP 404 by isinstance check — existing
`except BudgetValidationError` handlers still catch it.
"""


class BudgetValidationError(Exception):
    """Bad request — invalid input or a cross-org/nonexistent reference
    (maps to HTTP 400)."""


class BudgetNotFoundError(BudgetValidationError):
    """A budget-module row was not found in this org (maps to HTTP 404).
    Subclass so existing `except BudgetValidationError` handlers still catch
    it; the API routes 404 by isinstance."""
