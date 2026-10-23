"""Typed errors for email intake.

`MailboxNotFoundError` subclasses `MailboxValidationError` so a caller that
only cares "the request was bad" catches both, mirroring
`service_desk/errors.py`'s `TicketNotFoundError(TicketValidationError)`.
"""


class MailboxValidationError(Exception):
    """The request is not valid for this org's mailbox configuration."""


class MailboxNotFoundError(MailboxValidationError):
    """No mailbox with that id exists IN THIS ORG. Never distinguish
    'wrong org' from 'does not exist' -- that difference is an org-membership
    oracle."""
