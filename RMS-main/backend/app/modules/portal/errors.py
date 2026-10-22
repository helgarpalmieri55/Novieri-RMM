"""Typed errors for the end-user portal (docs/modules/portal/spec.md §5).

`PortalNotFoundError` subclasses `PortalValidationError` so a single `except
PortalValidationError` in a future API layer catches both, distinguishing
them by `isinstance` with the SUBCLASS CHECKED FIRST to map 404 vs 400 --
mirrors `app.modules.service_desk.errors` and
`app.modules.notification.errors` exactly, the established convention in this
codebase.
"""


class PortalValidationError(Exception):
    """A request the portal service layer rejects -- becomes HTTP 400."""


class PortalNotFoundError(PortalValidationError):
    """A ticket that does not exist, or does not belong to the requester --
    becomes HTTP 404.

    Spec §5: "A ticket that is not the requester's returns 404, never 403."
    An absent ticket and someone else's ticket are deliberately
    indistinguishable -- both raise this, with the identical message shape --
    because a 403 would confirm the id exists, which is a cross-requester
    existence oracle. This is the row-scoped analogue of
    `service_desk.errors.TicketNotFoundError`'s org-scoped rule (invariant
    #5), one level narrower: requester, not just org.
    """
