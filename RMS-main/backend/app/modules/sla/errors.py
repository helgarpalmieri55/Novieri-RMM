"""Typed exceptions for the SLA module (docs/modules/sla/spec.md).

Mirrors `app/modules/budget/errors.py`'s exception style: a single validation
error base that the API routes map to HTTP 400, and subclasses that routes map
more specifically -- existing `except SlaValidationError` handlers still catch
them.
"""

from datetime import datetime
from typing import Any


class SlaValidationError(Exception):
    """Bad request -- invalid input or a cross-org/nonexistent reference
    (maps to HTTP 400)."""


class SlaNotFoundError(SlaValidationError):
    """An SLA-module row was not found in this org (maps to HTTP 404).
    Subclass so existing `except SlaValidationError` handlers still catch it;
    the API routes 404 by isinstance."""


class UnsatisfiableCalendar(SlaValidationError):
    """A working-time target cannot be reached on this calendar within
    `calendar.MAX_LOOKAHEAD_DAYS` (spec §4).

    Raised for a calendar with no windows at all, or one whose every day inside
    the lookahead horizon is a holiday. This names the calendar's problem
    rather than a loop limit: the calendar cannot deliver the target, and the
    sweep must surface that instead of hanging on it.

    Fix wave 6, I-5: carries its operands as ATTRIBUTES, not only inside the
    formatted message. `sla_misconfiguration_is_not_fatal`
    (`app/modules/sla/tracking.py`) is this exception's only catch site, and
    it logs by type per §2 -- `exc_info=exc` never puts this message's text on
    a log line. Before this, the four values that name WHY a calendar failed
    (its timezone, the working minutes requested, the instant they were
    requested from, how much budget was still outstanding) existed only
    inside that discarded str(), so an operator was left with a static
    "sla.start_tracking failed" line and no way to tell an empty calendar
    from a calendar that is merely too short for a huge target -- the exact
    failure mode this whole task exists to fix, one exception type over from
    where it was originally solved. None of the four can carry a credential
    (a stored calendar's timezone and the SLA policy's target minutes are
    admin configuration, not device or user input), so exposing them as named
    `extra=` fields is not the leak §2 exists to prevent -- it is the fix.
    """

    # Fix wave 8, finding 4.2, corrected by fix wave 9, I-1: DEFAULTED, not
    # because a raise site should ever rely on the defaults -- every raise
    # site in this codebase still names all four, and there is no way to
    # make that a hard requirement AND satisfy the constraint below, so this
    # is a deliberate loosening, not an oversight.
    # `celery.utils.serialization.get_pickleable_exception` is only reached
    # on Celery's PICKLE-family result serializers. `create_celery()`
    # (`app/worker.py`) uses the default `result_serializer='json'`, and on
    # that path `Backend.exception_to_python` reconstructs with
    # `cls(*exc_message)` where `exc_message` is `exc.args` -- just
    # `(message,)`, since `Exception.__init__` never saw the four
    # keyword-only attributes below. Measured before wave 8's fix:
    # `UnsatisfiableCalendar("bad calendar")` raised `TypeError` for the four
    # missing required arguments, `exception_to_python` caught it (it does
    # not propagate) and returned a bare `Exception(f'{cls}({exc_msg})')` --
    # the type lost on the path production actually uses, `__reduce__` below
    # notwithstanding (that fixes pickle/deepcopy/Celery's PICKLE-family
    # path, none of which this platform's worker is configured to use).
    #
    # I-1 (fix wave 9): the defaults wave 8 chose -- `""`, `0`, an epoch
    # `_UNKNOWN_START` sentinel, `0` -- are indistinguishable from REAL
    # measurements once on an instance. Measured:
    # `UnsatisfiableCalendar("forgot everything")` constructed cleanly with
    # `requested_minutes=0` and `timezone=""`, and both render into
    # `tracking.py`'s log line as ordinary named fields -- an operator
    # reading `requested_minutes: 0` cannot tell "the calendar genuinely
    # asked for zero working minutes" from "this value was never provided",
    # because both produce the identical field. `None` cannot be confused
    # with either: it is not a value the working-time arithmetic in
    # `calendar.py` (the only real raise site) would ever compute, so a
    # reconstructed exception's fields read as visibly ABSENT rather than
    # plausible. The `datetime | None` widening drops `_UNKNOWN_START`
    # entirely -- an invented 1970 date sorted into real time-series data was
    # itself a symptom of the same problem, not a solution to it.
    # `tracking.py`'s `sla_misconfiguration_is_not_fatal` is what checks for
    # `None` before writing a field at all -- see that module for the
    # log-line half of this fix.
    def __init__(
        self,
        message: str,
        *,
        timezone: str | None = None,
        requested_minutes: int | None = None,
        start: datetime | None = None,
        outstanding_minutes: int | None = None,
    ) -> None:
        super().__init__(message)
        self.timezone = timezone
        self.requested_minutes = requested_minutes
        self.start = start
        self.outstanding_minutes = outstanding_minutes

    def __reduce__(
        self,
    ) -> tuple[
        Any,
        tuple[
            type["UnsatisfiableCalendar"],
            str,
            str | None,
            int | None,
            datetime | None,
            int | None,
        ],
        dict[str, Any],
    ]:
        """I-2 (fix wave 7), widened by fix wave 8 findings 4.1 and 4.3:
        `BaseException.__reduce__` -- the default this overrides -- returns
        `(type(self), self.args, self.__dict__)`, and `self.args` is only
        `(message,)` (`Exception.__init__` never saw the four keyword-only
        attributes below). Pickle, `copy.deepcopy`, and
        `celery.utils.serialization.get_pickleable_exception` all reconstruct
        an exception through this protocol, so all three called
        `UnsatisfiableCalendar(message)` and got a `TypeError` for the four
        missing required keyword-only arguments.

        `get_pickleable_exception` is the one that matters: it does NOT
        propagate that `TypeError` -- it catches it and falls back to the
        nearest PICKLABLE base class, `SlaValidationError`, silently
        replacing the exception's own type. §2 only lets a log line carry an
        exception's TYPE (never its text); losing the type here loses the
        one thing that was safe to keep, replaced with a generic base that
        cannot be told apart from any other `SlaValidationError`.

        FINDING 4.1: this used to hardcode `_rebuild_unsatisfiable_calendar`'s
        RETURN TYPE to `UnsatisfiableCalendar` regardless of `type(self)` --
        so a subclass (`class Narrower(UnsatisfiableCalendar): ...`) silently
        reconstructed as the plain base across every one of pickle/deepcopy/
        Celery's exception handling. That is the exact defect this override
        exists to fix, one level down: before the original fix, losing the
        type raised loudly (`TypeError`); this version of the bug SUCCEEDED
        quietly, replacing a subclass with its base with no signal anything
        was lost. `type(self)` is now passed as the rebuilder's first
        argument -- a plain positional value, not `type(self)` used AS the
        reconstruction callable, because pickle calls a `__reduce__`
        callable directly and `UnsatisfiableCalendar.__init__` (and every
        subclass that does not override it) still requires all four keywords,
        which stays deliberate: this override gives pickle a positional-only
        path back in WITHOUT loosening the raise-site contract that keeps a
        caller from forgetting one.

        FINDING 4.3: this used to return a 2-tuple (callable, args), dropping
        the state slot `BaseException.__reduce__`'s default 3-tuple form
        carries. Measured: an attribute assigned to the instance AFTER
        construction (not one of the four keywords above) was silently
        absent from the rebuilt object -- pickle applies the third element to
        the reconstructed object's `__dict__` once the callable returns, and
        nothing did that here. `self.__dict__` (already containing
        `timezone`/`requested_minutes`/`start`/`outstanding_minutes` plus
        anything else ever assigned) restores that.
        """
        return (
            _rebuild_unsatisfiable_calendar,
            (
                type(self),
                self.args[0],
                self.timezone,
                self.requested_minutes,
                self.start,
                self.outstanding_minutes,
            ),
            self.__dict__,
        )


def _rebuild_unsatisfiable_calendar(
    cls: type["UnsatisfiableCalendar"],
    message: str,
    timezone: str | None,
    requested_minutes: int | None,
    start: datetime | None,
    outstanding_minutes: int | None,
) -> "UnsatisfiableCalendar":
    """Reconstruction target for `UnsatisfiableCalendar.__reduce__` (I-2,
    widened by fix wave 8 finding 4.1).

    A module-level function, not `UnsatisfiableCalendar` itself: pickle calls
    the callable a `__reduce__` returns with ONLY positional arguments, and
    `UnsatisfiableCalendar.__init__`'s four attributes are keyword-only BY
    DESIGN, so a raise site cannot construct one without naming every value.
    This function is the one place that translates the positional tuple
    pickle/deepcopy/Celery hand back into the keyword call the class actually
    requires -- it does not exist to be called directly.

    `cls`, not a hardcoded `UnsatisfiableCalendar`: finding 4.1 measured that
    hardcoding it silently downgraded every subclass across a round trip.
    `__reduce__` passes `type(self)` positionally so this function builds an
    instance of the ACTUAL runtime type, not just the base.
    """
    return cls(
        message,
        timezone=timezone,
        requested_minutes=requested_minutes,
        start=start,
        outstanding_minutes=outstanding_minutes,
    )
