"""Scan authorization: deny by default, and the authorized target set
(`docs/modules/ipam/spec.md` §0.12, §6.1, §6.2, §11.3).

**SECURITY-CRITICAL. THIS IS THE MODULE'S CONTROL SURFACE.** `net.sweep` is the
first capability in this platform that puts packets on SOMEBODY ELSE'S WIRE, and
`authorized_targets` is the function that decides where. `sweep.dispatch_sweep`
is the only caller of `JobEngine.create(type="net.sweep")` in this repository,
and it calls this module first.

**THAT SENTENCE IS A PROPERTY OF THREE PLACES, NOT OF THIS FILE, AND IT WAS
FALSE TWICE.** A docstring here cannot stop another package creating the job
type directly, so what makes the claim true is named rather than asserted:

  1. `sweep.dispatch_sweep` -- the only in-repo dispatcher, which resolves
     `authorized_targets`, records the `ipam_sweep_run` and refuses an empty
     set before any signature is produced.
  2. `app.core.approved_actions.NON_APPROVABLE_REASONS` -- `net.sweep` is
     refused as an approved action, so health's remediation sweep cannot reach
     it with an operator's own `targets` and no authorization row. That hole was
     live: packets went out with zero `ipam_scan_authorization` rows in the org.
  3. `app.jobs.direct_dispatch.MODULE_GOVERNED_REASONS` -- `net.sweep` is
     refused at `POST /api/v1/jobs`, which any holder of `jobs.dispatch` (the
     seeded `technician` role) could otherwise use to do exactly the same thing
     by hand.

Each is pinned by a test that asserts its allowlist PARTITIONS the live job-type
registry, so a future `net.sweep`-shaped job type cannot be registered into
either gate by silence.

**DENY BY DEFAULT** (§0.12). No `ipam_scan_authorization` row covering a target
means that target is never swept. There is no org-wide "allow everything"
switch and no authorization implied by having documented a subnet: documenting
is a filing action, authorizing packets against a range is a different decision
by a different person, and conflating them is how a customer's IPAM ends up
scanning a segment their contract does not cover. The convention followed is the
notification module's webhook allowlist (`docs/modules/notification/spec.md`
§2.6) -- explicit entries, deny by default, an admin-tier permission
(`ipam.scope.manage`), every add and remove audited.

**AN AUTHORIZATION CAN NEVER WIDEN A SWEEP.** §6.2's set is

    targets = ( S.prefix  ∩  ⋃{ a.prefix : a active in S.vrf } )
              −  ⋃{ range_to_cidrs(r) : r ∈ S's `excluded` ranges }

and **the intersection with `S.prefix` is taken FIRST**. That ordering is the
whole safeguard: an authorization for `10.0.0.0/8` covering a `/24` subnet
yields the `/24`, never the `/8`. Written the other way round, one over-broad
authorization would turn every subnet inside it into a licence to probe 16
million addresses.

**`authorized_by` IS NOT NULL: there is no system-authorized scope.** A human
with `ipam.scope.manage` put their name on every packet this module emits, and
the service refuses `actor_id=None` before the database has to.

**ROWS ARE NEVER HARD-DELETED.** Revocation stamps `revoked_at`/`revoked_by`, so
"who authorized probing that segment in March" stays answerable. The unique
index is PARTIAL on `revoked_at IS NULL`, which is what lets a revoked prefix be
authorized again -- and which means every upsert against it must INFER
(`index_elements=` + `index_where=`); `ON CONFLICT ON CONSTRAINT` fails at
runtime against a partial index.

**AN EMPTY TARGET SET IS A FIRST-CLASS OUTCOME**, returned as `[]`. Refusing to
dispatch on it belongs to the dispatcher (§6.2), which records the run
`state='refused'` with `error='not authorized'` and raises
`IpamAuthorizationError` BEFORE `JobEngine.create` -- so for an unauthorized
target no job row is created, no canonical payload is built, and no signature is
ever produced. Raising here would make "not authorized" and "something went
wrong" the same signal at the call site.

**THE CANONICAL LIST IS THE DIGEST'S INPUT.** `net.scope_digest` has exactly one
implementation and the dispatcher and the result handler both call it; this
module's job is to make sure the list it is handed is already sorted, minimal
and deduplicated, so the same authorized scope never hashes two ways.
"""

from datetime import UTC, datetime
from ipaddress import ip_network
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text

from app.core.audit import audit_service
from app.core.i18n import tr
from app.modules.ipam.errors import (
    IpamConflictError,
    IpamNotFoundError,
    IpamValidationError,
)
from app.modules.ipam.models import IpamRange, IpamScanAuthorization, IpamSubnet
from app.modules.ipam.net import (
    IPNetwork,
    canonical_targets,
    intersect,
    parse_address,
    range_to_cidrs,
    subtract,
)
from app.modules.ipam.registry import get_vrf

__all__ = [
    "DEFAULT_MAX_TARGETS",
    "authorized_targets",
    "create_authorization",
    "list_authorizations",
    "revoke_authorization",
]

#: §5.2's `max_targets` default. It bounds ADDRESSES, not list entries: the
#: result handler checks `len(hosts) <= params.max_targets` against the same
#: number, so a cap counting CIDR entries would let one `/16` entry pass a cap
#: of 128.
DEFAULT_MAX_TARGETS = 4096

#: §5.2's bound on `max_targets` itself.
MAX_TARGETS_CEILING = 65_536

_MAX_NOTE = 500


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_prefix(value: object) -> IPNetwork:
    """Normalise to a canonical network, or raise a readable 400.

    `strict=False`, matching `subnets._parse_prefix`: an operator typing
    `10.70.0.5/24` means the /24, and Postgres's `cidr` would refuse the literal
    outright ("bits set to right of mask") with a message about masks rather
    than about their network.
    """
    if value is None:
        raise IpamValidationError(tr("prefix must be a non-empty string"))
    literal = str(value).strip()
    if not literal:
        raise IpamValidationError(tr("prefix must be a non-empty string"))
    try:
        return ip_network(literal, strict=False)
    except ValueError as exc:
        raise IpamValidationError(
            tr("{literal} is not a valid prefix: {exc}", literal=repr(literal), exc=exc)
        ) from None


async def create_authorization(
    session: AsyncSession,
    org_id: UUID,
    *,
    actor_id: UUID | None,
    vrf_id: UUID,
    prefix: str,
    note: str = "",
    expires_at: datetime | None = None,
) -> IpamScanAuthorization:
    """Authorize the fleet to probe `prefix` in `vrf_id`. HIGH PRIVILEGE.

    Gated on `ipam.scope.manage` at the API (§11.2) -- deliberately separate
    from `ipam.sweep.run`, because running a sweep inside an already-authorized
    scope is routine technician work and deciding what may be scoped at all is
    not. Collapsing them would let anyone who can run a scan also decide what
    may be scanned, which is the whole control.
    """
    if actor_id is None:
        raise IpamValidationError(
            tr(
                "authorized_by is required: there is no system-authorized scope (§2.9). A "
                "human with ipam.scope.manage puts their name on every packet this module "
                "emits"
            )
        )
    network = _parse_prefix(prefix)
    if not isinstance(note, str) or len(note) > _MAX_NOTE:
        raise IpamValidationError(
            tr("note must be a string of at most {max_note} characters", max_note=_MAX_NOTE)
        )
    if expires_at is not None and expires_at.tzinfo is None:
        raise IpamValidationError(tr("expires_at must be timezone-aware"))
    await get_vrf(session, org_id, vrf_id)

    now = _utcnow()
    inserted_id = (
        await session.execute(
            pg_insert(IpamScanAuthorization)
            .values(
                org_id=org_id,
                vrf_id=vrf_id,
                prefix=str(network),
                note=note,
                authorized_by=actor_id,
                authorized_at=now,
                expires_at=expires_at,
            )
            # INFERENCE, not `ON CONFLICT ON CONSTRAINT`: `uq_ipam_scope_active`
            # is a PARTIAL unique index (`WHERE revoked_at IS NULL`) and a
            # partial index has no `pg_constraint` row to name. The predicate is
            # written exactly as migration 0043 wrote it -- Postgres infers the
            # matching index from the column list plus the predicate, and a
            # differently-shaped-but-equivalent predicate infers nothing.
            .on_conflict_do_nothing(
                index_elements=["org_id", "vrf_id", "prefix"],
                index_where=text("revoked_at IS NULL"),
            )
            .returning(IpamScanAuthorization.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        raise IpamConflictError(
            tr(
                "{network} is already authorized in this VRF; revoke the existing "
                "authorization before creating another",
                network=network,
            )
        )

    row = (
        await session.execute(
            select(IpamScanAuthorization).where(
                IpamScanAuthorization.org_id == org_id,
                IpamScanAuthorization.id == inserted_id,
            )
        )
    ).scalar_one()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ipam.scope_authorized",
        target_type="ipam_scan_authorization",
        target_id=str(row.id),
        metadata={
            "prefix": str(network),
            "requested_prefix": str(prefix),
            "vrf_id": str(vrf_id),
            "note": note,
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
    )
    await session.flush()
    return row


async def revoke_authorization(
    session: AsyncSession, org_id: UUID, *, actor_id: UUID | None, authorization_id: UUID
) -> None:
    """Stamp `revoked_at`/`revoked_by`. THE ROW STAYS.

    Revocation cannot recall an already-signed in-flight job -- the exposure
    window is the job's TTL, which §5.2 pins at 600s for exactly this reason.
    What it does do is stop the next dispatch, immediately and permanently, and
    leave the record of what was authorized when.
    """
    row = (
        await session.execute(
            select(IpamScanAuthorization).where(
                IpamScanAuthorization.org_id == org_id,
                IpamScanAuthorization.id == authorization_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise IpamNotFoundError(
            tr("scan authorization {authorization_id} not found", authorization_id=authorization_id)
        )
    if row.revoked_at is not None:
        raise IpamConflictError(
            tr(
                "scan authorization {authorization_id} is already revoked",
                authorization_id=authorization_id,
            )
        )
    row.revoked_at = _utcnow()
    row.revoked_by = actor_id
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ipam.scope_revoked",
        target_type="ipam_scan_authorization",
        target_id=str(row.id),
        metadata={
            "prefix": str(row.prefix),
            "vrf_id": str(row.vrf_id),
            "note": row.note,
            "authorized_by": str(row.authorized_by),
        },
    )
    await session.flush()


async def list_authorizations(
    session: AsyncSession, org_id: UUID, *, include_revoked: bool = False
) -> list[IpamScanAuthorization]:
    """Active authorizations, or every one ever written.

    "Active" here is `revoked_at IS NULL` only -- an EXPIRED row is still listed,
    because an operator looking at this list needs to see the authorization that
    lapsed last night as much as the one that never existed. `authorized_targets`
    applies the expiry; the list surfaces it.
    """
    query = select(IpamScanAuthorization).where(IpamScanAuthorization.org_id == org_id)
    if not include_revoked:
        query = query.where(IpamScanAuthorization.revoked_at.is_(None))
    return list(
        (await session.execute(query.order_by(IpamScanAuthorization.prefix))).scalars().all()
    )


async def _active_scopes(
    session: AsyncSession, org_id: UUID, vrf_id: UUID, now: datetime
) -> list[IPNetwork]:
    """Every active authorization prefix in this org and VRF.

    THREE PREDICATES, ALL LOAD-BEARING, and each has its own test:
    * `org_id` -- the foreign keys are org-blind, so a row carrying another
      org's `org_id` and this org's `vrf_id` is constructible. RLS rejects it
      through `org_session`, and this predicate is what rejects it everywhere
      else.
    * `vrf_id` -- overlapping address space in two routing domains is the
      ordinary case. `10.0.0.0/8` in a customer's VRF and in ours are different
      networks.
    * active -- `revoked_at IS NULL` AND (`expires_at IS NULL` OR
      `expires_at > now`).
    """
    rows = (
        (
            await session.execute(
                select(IpamScanAuthorization.prefix).where(
                    IpamScanAuthorization.org_id == org_id,
                    IpamScanAuthorization.vrf_id == vrf_id,
                    IpamScanAuthorization.revoked_at.is_(None),
                    (IpamScanAuthorization.expires_at.is_(None))
                    | (IpamScanAuthorization.expires_at > now),
                )
            )
        )
        .scalars()
        .all()
    )
    return [ip_network(str(prefix), strict=False) for prefix in rows]


async def _excluded_holes(
    session: AsyncSession, org_id: UUID, subnet_id: UUID
) -> list[IPNetwork]:
    """§6.2's holes: `excluded` ranges, and ONLY `excluded` ranges.

    A `dhcp_pool` is the part of a segment most worth sweeping -- it is where
    undocumented hosts live -- and a `reserved` range is documentation, not a
    no-probe instruction. Subtracting either would make the module stop finding
    exactly the thing it exists to find.
    """
    rows = (
        await session.execute(
            select(IpamRange.start_address, IpamRange.end_address).where(
                IpamRange.org_id == org_id,
                IpamRange.subnet_id == subnet_id,
                IpamRange.kind == "excluded",
            )
        )
    ).all()
    holes: list[IPNetwork] = []
    for row in rows:
        start = parse_address(str(row.start_address))
        end = parse_address(str(row.end_address))
        if start is None or end is None:  # pragma: no cover - CHECKed at write time
            continue
        holes.extend(range_to_cidrs(start, end))
    return holes


def _cap(nets: list[IPNetwork], limit: int) -> list[IPNetwork]:
    """Trim `nets` to at most `limit` ADDRESSES, in ascending order.

    The cap counts addresses because §5.2's `max_targets` does: the result
    handler verifies `len(hosts) <= params.max_targets`, so a cap that counted
    CIDR entries would sign a `/16` as "one target" and then reject the 65,536
    hosts it produced.

    A block too large to fit is SPLIT rather than dropped, so a cap of 128 on a
    `/16` yields the first `/25` instead of nothing. Deterministic: the same
    scope always produces the same list, which is what makes `scope_digest`
    reproducible across the dispatcher and any replay.
    """
    out: list[IPNetwork] = []
    remaining = limit
    queue = list(nets)
    while queue and remaining > 0:
        block = queue.pop(0)
        if block.num_addresses <= remaining:
            out.append(block)
            remaining -= block.num_addresses
        elif block.prefixlen < block.max_prefixlen:
            queue = [*block.subnets(), *queue]
        else:  # pragma: no cover - a single address always fits a limit >= 1
            break
    return out


async def authorized_targets(
    session: AsyncSession,
    org_id: UUID,
    subnet: IpamSubnet,
    *,
    now: datetime | None = None,
    max_targets: int = DEFAULT_MAX_TARGETS,
) -> list[str]:
    """§6.2's set, as a canonically sorted, minimal, capped list of CIDR strings.

    Returns `[]` -- never raises -- when nothing is authorized. There is no
    branch that returns a non-empty set without at least one ACTIVE row in this
    org and VRF whose prefix intersects `subnet.prefix`.

    IPv6 and `container` subnets return `[]` as well. Neither is a
    "not authorized" answer and the dispatcher must refuse both with its own
    message BEFORE calling here (§0.5: an ICMP sweep of a /64 is a thought
    experiment, not a hard query) -- but the safe default underneath that
    refusal is deny, not "the first 4096 addresses of a /64".
    """
    if subnet.org_id != org_id:
        # The caller hands us a loaded row. Trusting it would let a subnet
        # loaded under one identity be swept under another's authorizations.
        raise IpamNotFoundError(tr("subnet {subnet_id} not found", subnet_id=subnet.id))
    if not isinstance(max_targets, int) or isinstance(max_targets, bool):
        raise IpamValidationError(tr("max_targets must be an integer"))
    if not 1 <= max_targets <= MAX_TARGETS_CEILING:
        raise IpamValidationError(
            tr(
                "max_targets must be between 1 and {max_targets_ceiling}",
                max_targets_ceiling=MAX_TARGETS_CEILING,
            )
        )
    if subnet.kind != "assignable":
        return []
    prefix = ip_network(str(subnet.prefix), strict=False)
    if prefix.version == 6:
        return []

    scopes = await _active_scopes(session, org_id, subnet.vrf_id, now or _utcnow())
    if not scopes:
        return []
    # THE INTERSECTION WITH `subnet.prefix` IS TAKEN FIRST (§0.12). Everything
    # after this line operates on a set that is already inside the subnet, so no
    # later step can widen it.
    allowed = intersect([prefix], scopes)
    if not allowed:
        return []
    holes = await _excluded_holes(session, org_id, subnet.id)
    remaining = subtract(allowed, holes)
    return canonical_targets(_cap(remaining, max_targets))
