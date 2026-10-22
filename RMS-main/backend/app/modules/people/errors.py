"""Typed errors for the people module; the API maps them by type."""


class PeopleError(Exception):
    """Bad request (HTTP 400)."""


class PeopleNotFoundError(PeopleError):
    """No such row in this org (HTTP 404). A cross-org id is indistinguishable."""


class PeopleConflictError(PeopleError):
    """The change collides with an existing row (HTTP 409)."""
