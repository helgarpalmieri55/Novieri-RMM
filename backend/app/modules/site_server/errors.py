"""Typed exceptions for the site-server module.

Mirrors `app/modules/ipam/errors.py`: one validation base the API maps to HTTP
400, and a not-found subclass it maps to 404 — 404 and never 403 for another
org's row, because a 403 confirms the id exists and that is a cross-tenant
existence oracle.

`SiteWakeRejected` is deliberately NOT under the validation base. It is raised
by the result handler against an AGENT's report, inside the job's tenant
transaction, and it must never be swallowed by an `except
SiteServerValidationError` written for bad operator input.
"""


class SiteServerError(Exception):
    """Base for everything this module raises."""


class SiteServerValidationError(SiteServerError):
    """Bad request — invalid input, or a refused promotion (maps to HTTP 400)."""


class SiteServerNotFoundError(SiteServerValidationError):
    """A site server (or a device) was not found IN THIS ORG (maps to HTTP 404).

    404 and never 403 for another org's id (spec §14).
    """


class SiteServerClientError(SiteServerValidationError):
    """The site named is another client's than the device's (maps to HTTP
    422; MSP clients plan 2026-10-26 Task 10). A site server's site is one of
    its device's client's sites -- `fk_site_server_site_client` holds it in
    the database too."""


class SiteServerConflictError(SiteServerError):
    """A promotion refused because of the device's state, not the request's
    shape (maps to HTTP 409): a device waiting for review (install-link spec
    §1.5). Promotion is an operator action, not a job, so the review gate in
    `JobEngine.create` never sees it -- this is its own check."""


class SiteWakeRejected(SiteServerError):
    """A `site.wake` result did not match the signed params (spec §5.3).

    Raised by the result handler, which flips the job to `failed`. A wake that
    went somewhere other than the address the control plane signed is not a
    successful wake, whatever the agent says about it.
    """
