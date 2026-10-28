"""Promote, edit, demote and read the site-server registry (spec §3.3, §10.3).

**PROMOTION IS A CONTROL-PLANE ACT AND THE ENDPOINT NEVER DECLARES ITS OWN
SCOPE** (§0.2). Enrollment is authenticated by a token that is deliberately
broadly distributable — `docs/05-agent-spec.md` calls it "zero-touch friendly,
so it can be pushed by GPO, MDM, kickstart, or an installer parameter", and one
token legitimately enrols a fleet: `services/enrollment.py::MAX_TOKEN_USES` caps
it at 1000 machines, and `agent_distribution` mints exactly such a token for a
GPO or Intune rollout. "This host serves an entire subnet" must not be claimable
by whoever holds such a token, so it is claimable only by a human holding
`site.manage`, and the audit row is the record of who granted LAN-wide privilege.

**THE PREMISE ABOVE USED TO READ "`EnrollmentToken.max_uses` is nullable,
meaning unlimited by default", AND THE NULLABLE DEFAULT IS GONE** —
`POST /enrollment-tokens` now requires a finite, capped `max_uses`. The
CONCLUSION is untouched, because it never rested on the word *unlimited*: a
credential distributed by GPO to a domain is held by everyone the domain can
read, whether it enrols one machine or a thousand. Nothing in this file reads
`max_uses`; it is cited here as a property of the enrolment credential, and the
property that matters is *who holds it*, not how many times it works.

**EVERY QUERY CARRIES AN EXPLICIT `org_id` PREDICATE**, in addition to RLS
(invariant #5). RLS MASKS AN APPLICATION PREDICATE: a cross-org test driven
through `org_session` passes even when the predicate has been deleted, because
RLS rejects first. `tests/test_site_server_registry.py` pins these on the ADMIN
engine, where RLS is not the thing doing the rejecting.

**`cache` AND `pxe` ARE REFUSED, NOT ACCEPTED-AND-IGNORED.** Spec §2.1 says "v1
only `'wake'` is honoured", and there are two readings of that. Stored and
ignored, a `roles=['cache']` row says a cache is running on a machine that binds
no socket, and no operator has any way to find that out — the registry lies and
the page renders the lie. Refused, the cost is one later `PATCH` on the day the
§4.4 invariant amendment is accepted. This module refuses, and names the gate in
the error so the relaxation is one predicate in one place.

**PROMOTION DOES NOT CHECK THAT THE DEVICE IS ON THE SEGMENTS IT CLAIMS**, and
does not fail if it is not (§3.3). That is §0.8's design and not an omission: a
registry row is an operator's CLAIM and an `ipam_observation` is EVIDENCE. A
claim earns sweep PREFERENCE; only evidence earns CANDIDACY, so an unverified
claim cannot put a probe on a wire. The mismatch surfaces on the segment check
(§11), where an operator can see it, rather than as a refusal here that would
make a freshly imaged branch server unpromotable until inventory had run.

**A SITE SERVER IS ITS DEVICE'S CLIENT'S** (`site_server` is H from `devices`,
migration 0231), and the site it is filed under -- `site_id`, grouping only,
which replaced the IPAM site in migration 0240 -- is one of that client's
sites (422, and `fk_site_server_site_client` underneath). The site plays no
part in what the server serves: push install and wake choose an originator by
segment and by client (`agent_push.preflight._site_server_for`), never by site.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.wire_words import say_list
from app.models import Agent, Device
from app.modules.site_server.errors import (
    SiteServerClientError,
    SiteServerConflictError,
    SiteServerNotFoundError,
    SiteServerValidationError,
)
from app.modules.site_server.models import SiteServer
from app.modules.site_server.net import parse_segment, segment_strings
from app.modules.site_server.words import ROLE_WORDS
from app.services.client_sites import (
    NotAcceptingNewRows,
    SiteNotFound,
    SiteOfAnotherClient,
    loaded_client,
    site_of_client,
)

__all__ = [
    "MAX_SEGMENTS",
    "SUPPORTED_ROLES",
    "UPDATABLE_FIELDS",
    "demote",
    "get_site_server",
    "list_site_servers",
    "promote",
    "update_site_server",
]

#: The roles v1 actually honours. The DB CHECK is wider on purpose (see the
#: model's docstring); THIS is the gate, and it is one tuple to widen when
#: `CLAUDE.md` invariant #2 gains §4.4's amendment.
SUPPORTED_ROLES = ("wake",)

#: Mirrors `ck_site_server_segment_count`. Stated here as well so the refusal
#: is a 400 naming the ceiling rather than an `IntegrityError` naming a
#: constraint.
MAX_SEGMENTS = 32

UPDATABLE_FIELDS = ("name", "segments", "roles", "notes", "enabled", "site_id")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _validated_segments(segments: Sequence[str]) -> list[str]:
    """Every segment as a canonical network prefix, or a refusal.

    Cardinality is checked FIRST so "you gave me none" and "you gave me 40" are
    distinct messages, rather than both arriving as whichever segment happened
    to fail parsing.
    """
    if not segments:
        raise SiteServerValidationError(
            tr(
                "a site server must declare at least one segment: one that serves no segment "
                "serves nothing, which is a configuration mistake rather than a valid row"
            )
        )
    if len(segments) > MAX_SEGMENTS:
        raise SiteServerValidationError(
            tr(
                "at most {max_segments} segments (got {count})",
                max_segments=MAX_SEGMENTS,
                count=len(segments),
            )
        )
    return [str(parse_segment(value)) for value in segments]


def _validated_roles(roles: Sequence[str]) -> list[str]:
    if not roles:
        raise SiteServerValidationError(tr("a site server must declare at least one role"))
    refused = [role for role in roles if role not in SUPPORTED_ROLES]
    if refused:
        raise SiteServerValidationError(
            tr(
                "roles {refused} are not available in v1 — only {supported_roles} is honoured. "
                "The artefact cache and PXE roles require a listening socket, which "
                "`CLAUDE.md` invariant #2 forbids; the amendment that would permit it is "
                "drafted in `docs/modules/site-server/spec.md` §4.4 and HAS NOT BEEN ACCEPTED. "
                "Storing the role now would put a row in the registry claiming a service is "
                "running on a machine that binds no socket",
                refused=say_list(ROLE_WORDS, sorted(set(refused))),
                supported_roles=say_list(ROLE_WORDS, SUPPORTED_ROLES),
            )
        )
    # Deduplicated and ordered so two rows that declare the same roles compare
    # equal, and so the audit metadata is stable.
    return sorted(set(roles))


async def _device_in_org(session: AsyncSession, org_id: UUID, device_id: UUID) -> Device:
    """The device, or a NOT-FOUND naming neither its existence nor its org.

    The `Device.org_id == org_id` predicate is EXPLICIT as well as enforced by
    RLS, and it is the thing under test on the admin engine: through
    `org_session` RLS rejects first and this function would pass its cross-org
    test with the predicate deleted.
    """
    device = (
        await session.execute(
            select(Device).where(Device.id == device_id, Device.org_id == org_id)
        )
    ).scalar_one_or_none()
    if device is None:
        raise SiteServerNotFoundError(tr("device {device_id} not found", device_id=device_id))
    return device


async def _site_for_site_server(
    session: AsyncSession, org_id: UUID, site_id: UUID, client_id: UUID
) -> None:
    """`site_id` may hold a site server of `client_id`: a site this session
    can see (404), not archived and of an active client (409, read FOR SHARE
    until commit, as every new row's site is), and that client's (422).
    `client_sites.site_of_client`, in this module's errors."""
    try:
        await site_of_client(session, org_id=org_id, site_id=site_id, client_id=client_id)
    except SiteNotFound:
        raise SiteServerNotFoundError(tr("site {site_id} not found", site_id=site_id)) from None
    except NotAcceptingNewRows as exc:
        raise SiteServerConflictError(exc.detail) from None
    except SiteOfAnotherClient as exc:
        raise SiteServerClientError(exc.detail) from None


async def promote(
    session: AsyncSession,
    org_id: UUID,
    *,
    device_id: UUID,
    segments: Sequence[str],
    roles: Sequence[str] = ("wake",),
    name: str | None = None,
    site_id: UUID | None = None,
    notes: str = "",
    actor_id: UUID | None = None,
) -> SiteServer:
    """Promote a device to site server (§3.3), or refuse and write nothing.

    Validated in this order, so the operator gets the first real problem rather
    than a downstream symptom of it:

    1. the device exists IN THIS ORG (404, never 403 — a 403 confirms the id);
    2. it has an `agents` row (a device with no agent can never receive a job,
       so promoting one would create a site server that cannot wake anything);
    3. it is not already promoted;
    4. every segment parses as a network prefix with host bits zero;
    5. `roles` names only what v1 honours;
    6. the site, if any, is one of the device's client's.
    """
    device = await _device_in_org(session, org_id, device_id)

    agent_id = (
        await session.execute(
            select(Agent.id).where(Agent.device_id == device_id, Agent.org_id == org_id)
        )
    ).scalar_one_or_none()
    if agent_id is None:
        raise SiteServerValidationError(
            tr(
                "device {device_id} has no enrolled agent: a site server receives its work as "
                "an ordinary signed job on its own mutual-TLS connection, so a device with no "
                "agent can never be sent one",
                device_id=device_id,
            )
        )
    if device.review_state != "approved":
        raise SiteServerConflictError(
            tr(
                "waiting for review: device {device_id} has not been approved, and a machine "
                "nobody has approved cannot serve a segment",
                device_id=device_id,
            )
        )

    existing = (
        await session.execute(
            select(SiteServer.id).where(
                SiteServer.device_id == device_id, SiteServer.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise SiteServerValidationError(
            tr(
                "device {device_id} is already a site server ({existing}); edit that row "
                "rather than promoting twice",
                device_id=device_id,
                existing=existing,
            )
        )

    checked_segments = _validated_segments(segments)
    checked_roles = _validated_roles(roles)
    if site_id is not None:
        await _site_for_site_server(session, org_id, site_id, device.client_id)

    row = SiteServer(
        org_id=org_id,
        device_id=device_id,
        name=(name or device.hostname or "")[:200] or str(device_id),
        segments=checked_segments,
        roles=checked_roles,
        site_id=site_id,
        notes=notes,
        created_by=actor_id,
        enabled=True,
    )
    session.add(row)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user" if actor_id is not None else "system",
        actor_id=actor_id,
        action="site.promoted",
        target_type="device",
        target_id=str(device_id),
        client_id=device.client_id,
        metadata={
            "site_server_id": str(row.id),
            "device_id": str(device_id),
            "segments": checked_segments,
            "roles": checked_roles,
        },
    )
    await session.flush()
    return row


async def get_site_server(
    session: AsyncSession, org_id: UUID, site_server_id: UUID
) -> SiteServer:
    """One row IN THIS ORG, or `SiteServerNotFoundError` (404, never 403)."""
    row = (
        await session.execute(
            select(SiteServer).where(
                SiteServer.id == site_server_id, SiteServer.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise SiteServerNotFoundError(
            tr("site server {site_server_id} not found", site_server_id=site_server_id)
        )
    return row


async def list_site_servers(
    session: AsyncSession,
    org_id: UUID,
    *,
    enabled: bool | None = None,
    site_id: UUID | None = None,
    segment: str | None = None,
    client_id: UUID | None = None,
) -> list[SiteServer]:
    """The org's site servers, oldest first — or MOST SPECIFIC FIRST when
    `segment` is given.

    `client_id` keeps only the site servers whose device belongs to that
    client (`site_server.client_id`, inherited from the device). A push picks
    its originator with it (`agent_push.preflight._site_server_for`): the
    machine that will hold a decrypted credential is one of the target's own
    client, whatever the session's scope.

    `segment` filters by CONTAINMENT (`<<=`), not equality: asking "who serves
    10.20.7.0/24" must find the branch server declared for `10.20.0.0/16`,
    because a branch server serves a branch and not one VLAN. The comparison is
    done by Postgres against the native `cidr[]`; in `text` it would be a prefix
    match, and `10.200.7.0/24` starts with `10.20`.

    **CONTAINMENT MAKES THE RESULT SET AMBIGUOUS, SO THE ORDER IS PART OF THE
    ANSWER AND NOT DECORATION.** Once `10.20.0.0/16` and `10.20.7.0/24` both
    match `10.20.7.5`, "oldest first" would hand a caller that picks
    `candidates[0]` the server that merely claims the supernet, over the one
    actually declared for that wire — silently, and dependent on which row an
    operator happened to promote first. Rows are therefore ordered by the
    MASKLEN OF THEIR MOST SPECIFIC MATCHING SEGMENT, descending; `created_at, id`
    remains only as the tie-break between two servers equally specific about the
    same wire. This is `net.containing_segment`'s "most specific, not first"
    rule, applied one level up — across site servers rather than within one — and
    it is the same rule for the same reason.

    The `masklen()` subquery cannot be NULL for a returned row: the `WHERE`
    above admits only rows with at least one matching segment, and it is the
    identical predicate.
    """
    statement = select(SiteServer).where(SiteServer.org_id == org_id)
    if enabled is not None:
        statement = statement.where(SiteServer.enabled == enabled)
    if site_id is not None:
        statement = statement.where(SiteServer.site_id == site_id)
    if client_id is not None:
        statement = statement.where(SiteServer.client_id == client_id)
    if segment is not None:
        prefix = str(parse_segment(segment))
        statement = statement.where(
            text(
                "EXISTS (SELECT 1 FROM unnest(site_server.segments) seg"
                "        WHERE CAST(:seg_filter AS cidr) <<= seg)"
            ).bindparams(seg_filter=prefix)
        )
        statement = statement.order_by(
            text(
                "(SELECT max(masklen(seg)) FROM unnest(site_server.segments) seg"
                "        WHERE CAST(:seg_rank AS cidr) <<= seg) DESC"
            ).bindparams(seg_rank=prefix)
        )
    statement = statement.order_by(SiteServer.created_at, SiteServer.id)
    return list((await session.execute(statement)).scalars().all())


async def update_site_server(
    session: AsyncSession,
    org_id: UUID,
    site_server_id: UUID,
    *,
    changes: dict[str, Any],
    actor_id: UUID | None = None,
) -> SiteServer:
    """Edit segments, roles, name, notes, enable state or site grouping.

    `enabled=False` is the demote-WITHOUT-delete: the row keeps its history and
    its audit trail, and stops attracting sweep preference on the next dispatch
    because §6.1's preference subquery requires `s.enabled`.
    """
    row = await get_site_server(session, org_id, site_server_id)
    unknown = sorted(set(changes) - set(UPDATABLE_FIELDS))
    if unknown:
        raise SiteServerValidationError(tr("cannot update {unknown}", unknown=unknown))
    # Read before the flush below: it expires the trigger-written client.
    client_id = row.client_id
    if changes.get("site_id") is not None:
        await _site_for_site_server(session, org_id, changes["site_id"], client_id)

    before = {
        "segments": segment_strings(row.segments),
        "roles": list(row.roles),
        "enabled": row.enabled,
    }
    if "segments" in changes:
        row.segments = _validated_segments(changes["segments"])
    if "roles" in changes:
        row.roles = _validated_roles(changes["roles"])
    if "name" in changes:
        row.name = str(changes["name"])[:200]
    if "notes" in changes:
        row.notes = str(changes["notes"])
    if "enabled" in changes:
        row.enabled = bool(changes["enabled"])
    if "site_id" in changes:
        row.site_id = changes["site_id"]
    row.updated_at = _utcnow()
    session.add(row)
    await session.flush()
    await loaded_client(session, row)  # the response names it

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user" if actor_id is not None else "system",
        actor_id=actor_id,
        action="site.updated",
        target_type="site_server",
        target_id=str(row.id),
        client_id=client_id,
        metadata={
            "device_id": str(row.device_id),
            "changed": sorted(changes),
            "before": before,
            "after": {
                "segments": segment_strings(row.segments),
                "roles": list(row.roles),
                "enabled": row.enabled,
            },
        },
    )
    await session.flush()
    return row


async def demote(
    session: AsyncSession,
    org_id: UUID,
    site_server_id: UUID,
    *,
    reason: str = "",
    actor_id: UUID | None = None,
) -> None:
    """Delete the registry row and audit it.

    **THERE IS NO LEASE TO REVOKE IN v1**, because v1 binds no socket and issues
    no `site.serve` lease (spec §0.3, §7.3). Said here rather than left implicit
    so the cache slice adds the revocation NEXT TO this delete, instead of
    discovering months later that demotion left a listener running on a machine
    the registry no longer describes.
    """
    row = await get_site_server(session, org_id, site_server_id)
    device_id, client_id = row.device_id, row.client_id
    await session.delete(row)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user" if actor_id is not None else "system",
        actor_id=actor_id,
        action="site.demoted",
        target_type="site_server",
        target_id=str(site_server_id),
        client_id=client_id,
        metadata={"device_id": str(device_id), "reason": reason},
    )
    await session.flush()
