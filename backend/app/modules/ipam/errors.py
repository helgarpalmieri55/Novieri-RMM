"""Typed exceptions for the IPAM module (`docs/modules/ipam/spec.md`).

Mirrors `app/modules/health/errors.py` and `app/modules/sla/errors.py`: one
validation base the API routes map to HTTP 400, and subclasses they map more
specifically -- so an existing `except IpamValidationError` handler still
catches every one of them.

`IpamAuthorizationError` is deliberately NOT a subclass of the validation base.
It is the deny-by-default sweep refusal (§0.12, §6.3), it is decided BEFORE
`JobEngine.create` so no job row is created and no signature is ever produced
for an unauthorized target, and it must never be swallowed by a handler written
for bad input. `create_sweep` RETURNS it rather than raising it -- see
`SweepDispatch` -- because raising it inside the caller's `org_session` block
rolled back the `refused` run row the refusal exists to record.
"""


class IpamError(Exception):
    """Base for everything this module raises."""


class IpamValidationError(IpamError):
    """Bad request -- invalid input, or a cross-org/nonexistent reference
    (maps to HTTP 400)."""


class IpamNotFoundError(IpamValidationError):
    """An IPAM row was not found IN THIS ORG (maps to HTTP 404).

    404 and never 403 for another org's row: a 403 confirms the id exists,
    which is a cross-tenant existence oracle.
    """


class IpamConflictError(IpamValidationError):
    """The write is legal but the current state refuses it (maps to HTTP 409).

    Sibling overlap (§0.3), a duplicate prefix, a VRF/site/VLAN still
    referenced by a subnet, a static assignment inside a `dhcp_pool` (§3.2).
    Each names what is blocking rather than surfacing a bare `IntegrityError`.
    """


class IpamClientError(IpamValidationError):
    """A row's client cannot be what the request makes it (maps to HTTP 422).

    A body `client_id` that contradicts the client derived from the row's
    VRF or site, a create with no VRF or site that names no client, a site or
    VLAN of another client, or "Sin asignar" (MSP clients plan 2026-10-26
    Task 10, C10). A client the caller cannot see is `IpamNotFoundError`
    instead: a 422 would confirm it exists.
    """


class IpamTooLargeError(IpamValidationError):
    """Enumeration was asked for above §0.4's ceiling, or for IPv6 (§0.5)
    (maps to HTTP 422, `subnet too large to enumerate`).

    ONE RULE, TWO APPLICATIONS. A /8 and a /64 are refused by the same check
    for the same reason: the answer would be arithmetically hopeless rather
    than merely slow.
    """


class IpamAuthorizationError(IpamError):
    """No active `ipam_scan_authorization` covers the requested target (§0.12).

    Decided on the NEAR side of the signing boundary: `create_sweep` produces
    this before calling `JobEngine.create`, so for an unauthorized target no job
    row exists, no canonical payload is built, and no signature is ever produced.

    **IT IS RETURNED, NOT RAISED, BY `create_sweep`** (`SweepDispatch.refusal`),
    and the caller raises it once its transaction has committed. Raising it
    inside an `org_session` block rolls back the `state='refused'` run row that
    §2.10 exists to make visible, which is what it did on every request
    `POST /subnets/{id}/sweep` served until 2026-08-26.
    """


class IpamSweepRejected(IpamError):
    """A `net.sweep` result failed the §6.4 scope check and is rejected WHOLE.

    Not "the bad rows are dropped": an agent returning addresses it was not
    authorized to probe is either buggy or controlled by someone else, and
    filtering keeps its good rows flowing while hiding the tell. Raising this
    flips the job to `failed` and, because the handler runs in the job's
    transaction, writes zero observations.
    """
