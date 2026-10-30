"""Typed exceptions for the Budget module (docs/modules/budget/spec.md).

Mirrors `app/modules/subscriptions/errors.py`'s exception style: a single
validation error base that the API routes map to HTTP 400, and subclasses the
routes map by isinstance check -- not found 404, conflict 409, another
client's row 422 -- so existing `except BudgetValidationError` handlers still
catch them.
"""


class BudgetValidationError(Exception):
    """Bad request — invalid input or a cross-org/nonexistent reference
    (maps to HTTP 400)."""


class BudgetNotFoundError(BudgetValidationError):
    """A budget-module row was not found in this org (maps to HTTP 404).
    Subclass so existing `except BudgetValidationError` handlers still catch
    it; the API routes 404 by isinstance."""


class BudgetConflictError(BudgetValidationError):
    """The request conflicts with what exists (maps to HTTP 409): a row filed
    under a client that is archived (`client_for_new_rows`)."""


class BudgetClientError(BudgetValidationError):
    """A row of one client naming another client's category, cost center or
    linked record -- or "Sin asignar" as a row's client (maps to HTTP 422;
    plan 2026-10-26 Task 8)."""
