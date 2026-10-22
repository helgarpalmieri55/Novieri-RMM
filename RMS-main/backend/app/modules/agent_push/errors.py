"""The module's error vocabulary, and why refusal has a type of its own.

**A REFUSAL IS NOT A VALIDATION FAILURE AND MUST NOT ARRIVE AS ONE.** "Your
request was malformed" and "you are not authorized to present administrative
credentials at 10.0.4.17" are different facts, an operator acts on them
differently, and collapsing them is how "not authorized" becomes an afternoon
spent re-typing an address that was never the problem. `ipam/errors.py` makes
the same split for the same reason.

`PushDispatchUnavailableError` IS THE SHAPE OF CYCLE B1. Every authorization
holds, the batch is legal, and the platform still does nothing -- because the
job type, the signing and the agent capability are cycle B2 (spec §14). It is
a distinct type so that a test can tell "the ladder passed and dispatch does
not exist" from "the ladder let something through", which are the same HTTP
status and opposite outcomes.
"""

__all__ = [
    "PushAuthorizationError",
    "PushConflictError",
    "PushDispatchUnavailableError",
    "PushError",
    "PushListChangedError",
    "PushNotFoundError",
    "PushPermissionError",
    "PushValidationError",
]


class PushError(Exception):
    """Base for everything this module raises."""


class PushValidationError(PushError):
    """The request could not be understood: a bad prefix, a bad address, a
    batch over the ceiling, a duplicate target."""


class PushNotFoundError(PushValidationError):
    """The named row is not in this org.

    A SUBCLASS of the validation error, and the API maps it FIRST, so a
    handler that forgets the ordering degrades to 400 rather than to 200.
    404 and never 403 for another org's id: a 403 confirms the id exists,
    which is a cross-tenant existence oracle.
    """


class PushConflictError(PushError):
    """The row already exists in the state being asked for -- an active
    authorization for this prefix, a scope already bound, a revocation of
    something already revoked."""


class PushListChangedError(PushConflictError):
    """The subnet no longer expands to the list the person was shown
    (design §1.3). Refused before any row is written or any secret touched;
    the console shows the list again. A 409 whose body names `list_changed`,
    so the console can tell it from "a run is already active"."""


class PushPermissionError(PushError):
    """The caller holds `agent.push.run` and not `credentials.checkout`.

    Its own type because it is a 403 and every other error here is not, and
    because the conjunction is the whole point of it: a push USES a stored
    secret, so the permission to decrypt one is not optional (spec §8).
    """


class PushAuthorizationError(PushError):
    """One or more targets failed at least one of invariant 2a's four bounds.

    Carries `targets`: `{address: [reason, ...]}`, every failing reason for
    every failing address. NOT the first failure -- an operator told only that
    "something was refused" cannot act, and an operator told only the first of
    three missing grants comes back twice.
    """

    def __init__(self, message: str, targets: dict[str, list[str]]) -> None:
        super().__init__(message)
        self.targets = targets


class PushDispatchUnavailableError(PushError):
    """Everything passed, and cycle B1 dispatches nothing (spec §14)."""
