"""Module error types (spec §6.5).

Routing at the API layer is by TYPE, never by matching exception prose -- the
convention every module since Phase 11 follows.

**EVERY ERROR CARRIES ITS TYPED BODY'S DATA AS AN ATTRIBUTE, NOT ONLY IN THE
MESSAGE STRING.** §6.5 names the bodies: `403 report_permission_denied` carries
`missing`, `413 report_export_too_large` carries `cap` and `matched`, `504
report_timeout` carries the report key. A handler that had to parse those back
out of `str(exc)` would be one message rewording away from serving a malformed
error body, and the message is the one part of an exception nobody tests.

**TWO OF THESE ARE BUILD-TIME, NOT REQUEST-TIME.** `UndeclaredSource` and
`DenylistedSource` are raised by the source-declaration verifier
(`sources.py`), which runs over every definition at import. They never reach a
caller: they abort the application's import instead. They live here rather than
beside the verifier because they are part of the same hierarchy an operator's
error is -- a definition that reads an undeclared table is the same failure
`ReportPermissionDenied` exists to make impossible, caught earlier.
"""

from collections.abc import Iterable


class ReportingError(Exception):
    """Base for everything this module raises."""


class ReportNotFound(ReportingError):
    """No definition with this key. 404 at the API layer.

    In practice unreachable through the router -- §6.1 generates a LITERAL path
    per definition rather than a `/{key}` template, so an unknown key never
    reaches a handler -- and that is exactly why it is defined: the service is
    also called directly (by tests, and by anything later that resolves a key
    from a stored value), and "unknown key" must be a typed answer there rather
    than a `KeyError` escaping as a 500.
    """

    def __init__(self, report_key: str) -> None:
        self.report_key = report_key
        super().__init__(  # i18n-exempt: never raised on a route, a caller words its own answer
            f"no report definition {report_key!r}"
        )


class ReportPermissionDenied(ReportingError):
    """The caller does not hold every code in the definition's `requires` set
    (§0.4, §4.1). `403 report_permission_denied`, with `missing` in the body.

    The missing codes are NAMED because a report an operator cannot run is a
    permissions conversation; an opaque denial produces a support ticket
    instead. Naming them leaks nothing: `requires` is derived from the
    definition, which is code compiled into the image, and the catalogue route
    already tells the same caller the same thing (§6.2).
    """

    def __init__(self, report_key: str, missing: Iterable[str]) -> None:
        self.report_key = report_key
        self.missing: frozenset[str] = frozenset(missing)
        listed = ", ".join(sorted(self.missing))
        super().__init__(  # i18n-exempt: never raised, deps.py answers from `missing`
            f"report {report_key!r} additionally requires: {listed}"
        )


class ReportExportTooLarge(ReportingError):
    """The export matched more rows than `export_row_cap` (§0.8). `413
    report_export_too_large`, with `cap` and `matched`, and NO CSV BODY.

    Refusing rather than truncating is the whole point: a truncated CSV is
    indistinguishable from a complete one once it is in a spreadsheet, so a
    quietly short file is a wrong-number defect, not a paging inconvenience.
    `matched` is carried so the caller is told how much to narrow by, rather
    than being asked to guess.
    """

    def __init__(self, report_key: str, cap: int, matched: int) -> None:
        self.report_key = report_key
        self.cap = cap
        self.matched = matched
        super().__init__(  # i18n-exempt: api.py answers from cap and matched, str is never shown
            f"report {report_key!r} matched {matched} rows, over the export cap of {cap}; "
            "narrow the parameters and retry"
        )


class ReportTimeout(ReportingError):
    """The statement exceeded `SET LOCAL statement_timeout` and PostgreSQL
    cancelled it (§0.3 cost 1, §6.4). `504 report_timeout`.

    NEVER A PARTIAL PAGE, and never a page silently computed from a smaller
    window: the transaction is aborted, so there is no half-answer to serve and
    no way to accidentally serve one.
    """

    def __init__(self, report_key: str) -> None:
        self.report_key = report_key
        super().__init__(  # i18n-exempt: api.py answers from the key, str is never shown
            f"report {report_key!r} exceeded its statement timeout; narrow the window "
            "or the filters and retry"
        )


class UndeclaredSource(ReportingError):
    """BUILD-TIME. A table is outside the definition's declared, mapped source
    set (§0.4, §12.2) -- either because the compiled SQL reads a table `sources`
    does not declare, or because `sources` declares a table `TABLE_PERMISSIONS`
    does not map to a read code.

    This is the failure the whole authorization design rests on. `requires` is
    derived from `sources` through the map; a definition that declared
    `{"devices"}` and joined `ticket` would derive `{"devices.read"}` and serve
    ticket data to a caller the ticket API refuses, and a definition declaring a
    table the map does not cover would derive a requirement from nothing at all.
    Nothing else in the design catches either, so both abort the import rather
    than becoming runtime concerns.

    `tables` names the offending tables so the message says WHICH -- a bare
    "undeclared source" tells whoever hits it nothing about what to add. The
    default `reason` describes the first case because it is the one the verifier
    raises; the registry passes its own for the second.
    """

    def __init__(
        self,
        report_key: str,
        tables: Iterable[str],
        *,
        reason: str = "reads undeclared table(s)",
    ) -> None:
        self.report_key = report_key
        self.tables: frozenset[str] = frozenset(tables)
        self.reason = reason
        listed = ", ".join(sorted(self.tables))
        super().__init__(  # i18n-exempt: build-time, aborts the import and is never shown
            f"report {report_key!r} {reason}: {listed}. "
            "Add them to `sources` (which grows `requires` with them), or stop reading them."
        )


class DenylistedSource(ReportingError):
    """BUILD-TIME. The definition names, or its SQL reads, a table on
    `_NEVER_A_SOURCE` (§3, §8).

    Checked BEFORE the undeclared-source check, so a forbidden table declared
    honestly is still refused. Declaring it is not a mitigation -- the denylist
    is about what a report may contain at ANY gate, not about whether the
    declaration was truthful.
    """

    def __init__(self, report_key: str, name: str) -> None:
        self.report_key = report_key
        self.name = name
        super().__init__(  # i18n-exempt: build-time, aborts the import and is never shown
            f"report {report_key!r} names denylisted source {name!r}: no report may read "
            "it at any gate"
        )
