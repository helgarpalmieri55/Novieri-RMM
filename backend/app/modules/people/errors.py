"""Typed errors for the people module; the API maps them by type."""


class PeopleError(Exception):
    """Bad request (HTTP 400)."""


class PeopleNotFoundError(PeopleError):
    """No such row in this org (HTTP 404). A cross-org id is indistinguishable."""


class PeopleConflictError(PeopleError):
    """The change collides with an existing row (HTTP 409)."""


class PeopleClientError(PeopleError):
    """The change names a row of another client than the one it must be in
    (HTTP 422): a device's holder from another client, an account whose
    person belongs elsewhere, or no client a person may be filed under."""
