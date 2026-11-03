"""Typed exceptions for the health module
(`docs/modules/endpoint-health-alerting/spec.md`).

Mirrors `app/modules/sla/errors.py`: one validation base the API routes map to
HTTP 400, and subclasses routes map more specifically -- existing
`except HealthValidationError` handlers still catch them.
"""


class HealthValidationError(Exception):
    """Bad request -- invalid input, or a cross-org/nonexistent reference
    (maps to HTTP 400)."""


class HealthNotFoundError(HealthValidationError):
    """A health-module row was not found IN THIS ORG (maps to HTTP 404).

    404 and never 403 for another org's row: a 403 confirms the id exists,
    which is a cross-tenant existence oracle over a table keyed by device.
    """


class HealthForbiddenError(HealthValidationError):
    """The caller is authenticated and in-org, and is not allowed to do THIS
    (maps to HTTP 403).

    Distinct from `HealthNotFoundError` on purpose, and it adds no oracle: it is
    raised only after the alert and the approved action have BOTH resolved in the
    caller's own org, so a 403 here confirms nothing a `health.remediate` holder
    could not already read. It exists for §20's two-permission remediation gate,
    where the second permission depends on the resolved job type and therefore
    cannot be a route dependency -- see `actions.py::_require_dispatch_grant`.
    """


class HealthConflictError(HealthValidationError):
    """The write is legal but the current state refuses it (maps to HTTP 409).

    Two cases, both from spec §10 and §2.4: deleting a profile that assignments
    still reference, and hard-deleting a check that has opened alerts. Both name
    what is blocking rather than surfacing a bare `IntegrityError`.
    """


class HealthUnprocessableError(HealthValidationError):
    """The request names something that exists and is visible, but that cannot
    take this write (maps to HTTP 422).

    One case today: a default profile for the Unassigned client, the inbox
    for what has no owner (MSP clients plan 2026-10-26 C10), which is not a
    client to configure -- `client_sites.named_client_for_new_rows` answers
    it with 422 for every module, and this keeps health's answer the same.
    """
