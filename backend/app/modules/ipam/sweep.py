"""`net.sweep` dispatch: choosing the sweeper and signing the authorized scope
(`docs/modules/ipam/spec.md` §5.2, §5.3, §6.2, §6.3, §11.3).

**SECURITY-CRITICAL, AND THE ONLY FILE IN THIS MODULE THAT MAY REACH
`JobEngine`.** `tests/test_ipam_sweep_dispatch.py` asserts that structurally --
the narrowed successor to Task 8's "no file here can dispatch yet" -- and also
asserts, from the AST of `create_sweep` itself, that `authorized_targets` is
called before anything that can produce a signature.

**THE REFUSAL IS ON THE NEAR SIDE OF THE SIGNING BOUNDARY** (§6.3). For an
unauthorized target `create_sweep` records the run `state='refused'`,
`error='not authorized'`, and RETURNS -- it does not raise -- BEFORE
`JobEngine.create`, so no job row is created, no canonical payload is built,
and **no signature is ever produced**. Revocation cannot recall an
already-signed in-flight job, which is why `SWEEP_TTL_S` is 600s rather than the
kernel's 3600s default: the TTL is the exposure window.

**IT RETURNS THE REFUSAL RATHER THAN RAISING IT, AND THAT IS THE WHOLE REASON
THE ROW SURVIVES.** This function used to `raise IpamAuthorizationError` right
after adding the `refused` row. `org_session` wraps its caller's block in
`session.begin()`, which rolls back on any exception leaving it -- so through
`POST /subnets/{id}/sweep` the row was written and immediately discarded, and
the only refusals that ever reached the database were the worker's, because
`ipam_sweep_dispatch_org` catches and commits. **The record of a refusal is the
entire point of refusing on the near side of the boundary**: a refusal nobody
can audit is indistinguishable from a request nobody made.

So `SweepDispatch.refusal` carries the exception and the CALLER raises it after
its transaction has committed. That is the shape `agent_push`'s
`request_push_batch` already uses for the same reason, pointed the same way, and
the mirror image of publishing an envelope only after commit.

**TWO REFUSALS ARE NOT "NOT AUTHORIZED", AND THEY ARE CHECKED FIRST.**
`authorized_targets` returns `[]` for an IPv6 subnet and for a `container`
subnet. That is the correct SAFE DEFAULT underneath -- deny, not "the first 4096
addresses of a /64" -- and the wrong ERROR: an operator who has authorized a
prefix and is told "not authorized" spends the afternoon re-granting a scope
that was never the problem. §0.5 has one error for both applications of the
enumeration rule, and this file raises it before it asks about authorization at
all.

**THE TARGET LIST IS WHAT IS SIGNED, AND `net.scope_digest` HAS EXACTLY ONE
IMPLEMENTATION.** This file computes the digest over the list it signs; the
result handler (`handler.py`) recomputes it over the job's own signed
`params.targets` with the SAME function. Two digests that drift is how an
authorized-scope check silently stops matching what was authorized -- both sides
agree, and neither is checking anything.

**`agents.capabilities` IS ADVISORY, REFUSAL IS THE FALLBACK** (§5.3). Nothing
in this repository populates that column -- the agent writes it at enrollment --
so an empty map is TOLERATED and dispatch is attempted. A non-empty map that
does not name `net.sweep` is taken at its word, and a device whose `net.sweep`
job came back `refused` is skipped for seven days. Without the cooldown a
refusing device is re-selected every hour, forever.

**NOTHING HERE PUBLISHES TO REDIS.** `create_sweep` returns the signed envelope
and the caller publishes it AFTER the transaction commits, so a job can never
reach an agent describing a run that rolled back. That is the shape
`app/worker.py`'s other `*_sweep_all_orgs` functions already use.
"""

from datetime import UTC, datetime, timedelta
from ipaddress import ip_network
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.jobs.engine import JobEngine
from app.modules.ipam.authorization import DEFAULT_MAX_TARGETS, authorized_targets
from app.modules.ipam.errors import (
    IpamAuthorizationError,
    IpamTooLargeError,
    IpamValidationError,
)
from app.modules.ipam.models import IpamSubnet, IpamSweepRun
from app.modules.ipam.net import scope_digest
from app.modules.ipam.observations import observation_ttl_days
from app.modules.ipam.schema import NetSweepParams
from app.modules.ipam.subnets import get_subnet

__all__ = [
    "DEFAULT_SWEEP_INTERVAL_HOURS",
    "MIN_SWEEP_INTERVAL_HOURS",
    "REFUSAL_COOLDOWN_DAYS",
    "SWEEP_DISPATCH_INTERVAL_S",
    "SWEEP_TTL_S",
    "SweepDispatch",
    "create_sweep",
    "due_subnet_ids",
    "list_sweep_runs",
    "select_sweeper",
]


class SweepDispatch(NamedTuple):
    """What `create_sweep` did, in the three parts a caller has to act on.

    A THREE-TUPLE RATHER THAN THE OLD TWO, deliberately: every existing
    `run, envelope = await create_sweep(...)` breaks loudly at the unpack and
    under mypy, which is the correct way for a contract that moved a refusal
    from an exception to a return value to reach its callers.

    * `run` -- the `ipam_sweep_run` row, in every outcome including refusal. It
      has been flushed; it is the caller's transaction that makes it durable.
    * `envelope` -- the SIGNED job, or `None` when nothing was dispatched
      (`refused`, `no_agent`). **Published only after the caller's transaction
      commits**; this module never touches Redis.
    * `refusal` -- `IpamAuthorizationError` when the target was not authorized,
      `None` otherwise. **RAISE IT AFTER COMMITTING, NEVER BEFORE**: raising
      inside an `org_session` block rolls the `refused` row back, which is the
      defect this field exists to make impossible to reintroduce silently.
    """

    run: "IpamSweepRun"
    envelope: dict[str, Any] | None
    refusal: IpamAuthorizationError | None = None


#: §5.2. NOT the kernel's 3600s default: it bounds how long a REVOKED
#: authorization can still be executed by an in-flight signed job (§6.3).
SWEEP_TTL_S = 600

#: §5.3's beat cadence. A project constant rather than a literal in
#: `app/worker.py`, so the beat entry and the test cannot drift apart.
SWEEP_DISPATCH_INTERVAL_S = 3600.0

#: §0.10 point 6. Weekly by default, and never shorter than an hour: a
#: one-minute sweep interval is the exact mechanism by which an IPAM becomes a
#: ping monitor, and the clamp is the thing that prevents it. Restated in SQL by
#: `ck_ipam_subnet_sweep_interval` because the application check is one refactor
#: away from being the only one.
DEFAULT_SWEEP_INTERVAL_HOURS = 168
MIN_SWEEP_INTERVAL_HOURS = 1

#: §5.3. How long a `refused` `net.sweep` makes a device ineligible.
REFUSAL_COOLDOWN_DAYS = 7

#: How many ordered candidates the auto-selector examines before giving up. The
#: capability filter is a jsonb shape this codebase does not control, so it is
#: applied in Python; the bound keeps "every device on a /16" from being loaded
#: to answer a question the first row almost always answers.
_CANDIDATE_LIMIT = 200

SWEEP_TRIGGERS = ("scheduled", "manual")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _advertises_net_sweep(capabilities: Any) -> bool:
    """Does this agent say it can sweep? (§5.3)

    THREE ANSWERS COLLAPSED INTO TWO, deliberately. `agents.capabilities` is a
    shipped jsonb column that nothing in this repository writes, so an EMPTY map
    means "this agent has not told us", and refusing on it would make the whole
    capability path dead code that silently disables sweeps everywhere. A
    NON-EMPTY map that does not name `net.sweep` is an agent that has said what
    it can do, and is taken at its word.

    The shape is not fixed by any contract in this repository, so both the
    `{"jobs": [...]}` and the flat `{"net.sweep": true}` forms are recognised
    rather than one being guessed at.
    """
    if not isinstance(capabilities, dict) or not capabilities:
        return True
    if capabilities.get("net.sweep"):
        return True
    for value in capabilities.values():
        if isinstance(value, list) and "net.sweep" in value:
            return True
        if isinstance(value, dict) and value.get("net.sweep"):
            return True
    return False


async def _refusal_cutoff(now: datetime) -> datetime:
    return now - timedelta(days=REFUSAL_COOLDOWN_DAYS)


async def _recently_refused(
    session: AsyncSession, org_id: UUID, device_id: UUID, now: datetime
) -> bool:
    """Has this device refused a `net.sweep` inside the cooldown?

    Scoped to `type='net.sweep'`: a device that refused a `patch.apply` last
    night has said nothing about whether it can sweep, and banning it would
    quietly shrink the eligible fleet for an unrelated reason.
    """
    found = (
        await session.execute(
            text(
                "SELECT 1 FROM jobs"
                " WHERE org_id = :org AND target_id = :device AND type = 'net.sweep'"
                "   AND status = 'refused' AND finished_at >= :cutoff"
                " LIMIT 1"
            ),
            {
                "org": str(org_id),
                "device": str(device_id),
                "cutoff": await _refusal_cutoff(now),
            },
        )
    ).first()
    return found is not None


async def _designated(
    session: AsyncSession, org_id: UUID, subnet: IpamSubnet, now: datetime
) -> UUID | None:
    """§5.3 step 1: the operator's designated sweeper, if it can actually run.

    The conditions are CONJUNCTIVE -- designation is not liveness. A designated
    device whose agent is offline falls through to auto-selection rather than
    producing a job addressed to a machine that cannot receive it and a subnet
    page that shows a dispatch which never happened.

    **AND A MACHINE OF THE SUBNET'S CLIENT** (MSP clients, invariant 5a). A
    designation naming another client's machine -- written before
    `set_sweep_settings` refused one, or straight into the table -- is ignored
    exactly as one whose agent cannot sweep is: it falls through to
    auto-selection, which stays inside the client too.
    """
    if subnet.sweep_device_id is None:
        return None
    row = (
        await session.execute(
            text(
                "SELECT a.status AS status, a.capabilities AS capabilities"
                " FROM agents a JOIN devices d ON d.id = a.device_id AND d.org_id = a.org_id"
                " WHERE a.org_id = :org AND a.device_id = :device"
                "   AND d.client_id = :client"
                "   AND d.review_state = 'approved'"
            ),
            {
                "org": str(org_id),
                "device": str(subnet.sweep_device_id),
                "client": str(subnet.client_id),
            },
        )
    ).first()
    if row is None or row.status != "online":
        return None
    if not _advertises_net_sweep(row.capabilities):
        return None
    if await _recently_refused(session, org_id, subnet.sweep_device_id, now):
        return None
    return subnet.sweep_device_id


async def select_sweeper(
    session: AsyncSession, org_id: UUID, subnet: IpamSubnet, *, now: datetime | None = None
) -> UUID | None:
    """Which device probes this segment (§5.3), or `None` for `no_agent`.

    Deterministic by construction: among devices whose **current** inventory
    observations include an address inside the subnet, ordered by
    (`Agent.status='online'` desc, `Agent.last_seen_at` desc, `Device.id`). An
    unordered `LIMIT 1` passes a single-candidate test and picks a different
    machine on every run in production.

    NEVER "any device in the org". A machine that is not on that wire cannot
    sweep it, and dispatching anyway emits somebody's probe from the wrong
    place. `no_agent` is a first-class outcome (§2.10) that the subnet page
    shows and §4.3's Gate E reads -- not an error swallowed in a log.

    **AND NEVER ANOTHER CLIENT'S MACHINE** (MSP clients, invariant 5a). Two
    clients can both run 10.0.0.0/24 on two unrelated wires, so an address
    inside the prefix says nothing about which network a machine is on: the
    candidate devices are the SUBNET'S CLIENT'S (`d.client_id = :client`) and
    so is the site-server row that earns the preference (`s.client_id =
    :client`). The signed `net.sweep` params carry that client's target list,
    the probe leaves from its wire and the results are filed under it. Y's
    machine on Y's 10.0.0.0/24 is `no_agent` for X's, not X's sweeper -- not
    even for a technician who may act on every client, and not even when Y's
    site server declares the more specific segment. The same rule as push
    install's `_site_server_for` (`agent_push/preflight.py`), for the same
    reason. Applied in the query, not left to RLS: the hourly worker runs
    under the every-client scope, and so does a technician of every client.
    A site server is H from its device, so its client is already the
    candidate's; the predicate says so where the preference is decided, and
    a test plants the row the trigger forbids to show it holds on its own.

    **SITE SERVERS ARE PREFERRED, AND THE PREFERENCE SITS BELOW LIVENESS.** A
    device declared in `site_server` for a segment CONTAINING this subnet, and
    enabled, sorts ahead of an equally-live ordinary endpoint
    (`docs/modules/site-server/spec.md` §6.1). The term is one clause; every
    other predicate here is untouched, and there is deliberately no second
    selection function -- two would be two answers to "who probed this segment",
    diverging on the first bug fixed in one of them.

    **THE SITE-SERVER SPEC CONTRADICTS ITSELF ON THE ORDER AND THIS FOLLOWS ITS
    BEHAVIOUR TABLE, NOT ITS SQL.** Spec §6.1 writes the preference term FIRST;
    spec §6.2's fallback table says an offline site server "loses the preference;
    an ordinary endpoint sweeps", explaining that "`(a.status='online') DESC` is
    already the second term". Those cannot both be true. With preference first,
    an OFFLINE site server outranks an ONLINE ordinary endpoint, and the sweep is
    dispatched to a machine that cannot receive it: the job sits `queued` until
    the 600s TTL expires it and the segment goes unswept for the cycle.

    §6.2's behaviour is implemented, for the reason THIS FILE already
    establishes: `_designated`'s docstring says designation "is not liveness",
    and a designated device whose agent is offline falls through to
    auto-selection rather than producing a job addressed to a machine that cannot
    receive it. An inferred preference is a WEAKER claim than an operator's
    explicit designation, so it cannot outrank liveness where designation does
    not.

    **PREFERENCE REORDERS THE CANDIDATE SET; IT NEVER ADDS TO IT** (§0.8). A
    site server must still clear the inventory-observation `EXISTS` above. A
    registry row is an operator's CLAIM and an observation is EVIDENCE: a claim
    may earn preference, but only evidence may earn candidacy, or a typo'd CIDR
    sends a probe onto a segment nobody authorized.

    The `org_id` predicate is explicit as well as enforced by RLS (invariant #5)
    and is pinned on the admin engine by the tests: through `org_session` RLS
    rejects first, so a cross-org test alone passes with the predicate deleted.
    That applies to the site-server subquery's own `s.org_id = :org` too.
    """
    stamp = now or _utcnow()
    designated = await _designated(session, org_id, subnet, stamp)
    if designated is not None:
        return designated

    cutoff = stamp - timedelta(days=await observation_ttl_days(session, org_id))
    rows = (
        await session.execute(
            text(
                "SELECT d.id AS device_id, a.capabilities AS capabilities"
                " FROM devices d"
                " JOIN agents a ON a.device_id = d.id AND a.org_id = d.org_id"
                " WHERE d.org_id = :org"
                "   AND d.client_id = :client"
                "   AND d.review_state = 'approved'"
                "   AND EXISTS ("
                "     SELECT 1 FROM ipam_observation o"
                "     WHERE o.org_id = :org AND o.device_id = d.id"
                "       AND o.source = 'inventory' AND o.last_seen_at >= :cutoff"
                "       AND o.address <<= CAST(:prefix AS inet))"
                "   AND NOT EXISTS ("
                "     SELECT 1 FROM jobs j"
                "     WHERE j.org_id = :org AND j.target_id = d.id"
                "       AND j.type = 'net.sweep' AND j.status = 'refused'"
                "       AND j.finished_at >= :refused_since)"
                # SITE-SERVER PREFERENCE, and it sits BELOW liveness on purpose
                # (`docs/modules/site-server/spec.md` §6.1/§6.2 -- see this
                # function's docstring for why §6.1's literal ordering is wrong).
                # `<<=` is containment, not equality: a branch server declared
                # for 10.20.0.0/16 is preferred for 10.20.7.0/24, because a
                # branch server serves a branch and not one VLAN.
                " ORDER BY (a.status = 'online') DESC,"
                "          (EXISTS ("
                "            SELECT 1 FROM site_server s"
                "            WHERE s.org_id = :org AND s.device_id = d.id"
                "              AND s.client_id = :client"
                "              AND s.enabled"
                "              AND EXISTS ("
                "                SELECT 1 FROM unnest(s.segments) seg"
                "                WHERE CAST(:prefix AS cidr) <<= seg))) DESC,"
                "          a.last_seen_at DESC NULLS LAST,"
                "          d.id"
                " LIMIT :limit"
            ),
            {
                "org": str(org_id),
                "client": str(subnet.client_id),
                "cutoff": cutoff,
                "prefix": str(subnet.prefix),
                "refused_since": await _refusal_cutoff(stamp),
                "limit": _CANDIDATE_LIMIT,
            },
        )
    ).all()
    for row in rows:
        if _advertises_net_sweep(row.capabilities):
            device_id: UUID = row.device_id
            return device_id
    return None


async def due_subnet_ids(
    session: AsyncSession, org_id: UUID, *, now: datetime | None = None
) -> list[UUID]:
    """Subnets the hourly beat should sweep now (§5.3).

    Due iff `sweep_enabled`, `sweep_interval_hours IS NOT NULL` (NULL means
    on-demand only, §2.4), and `last_swept_at` is null or older than that many
    hours -- **clamped to at least one** in the SQL as well as at write time
    (§0.10 point 6).

    `container` and IPv6 subnets are excluded HERE rather than left to fail in
    `create_sweep`: neither can be swept (§0.2, §0.5), and a beat that offered
    them every hour would write an error row an hour, forever, for a subnet
    whose configuration is simply not sweepable.
    """
    rows = (
        await session.execute(
            text(
                "SELECT id FROM ipam_subnet"
                " WHERE org_id = :org AND sweep_enabled"
                "   AND sweep_interval_hours IS NOT NULL"
                "   AND kind = 'assignable' AND family(prefix) = 4"
                "   AND (last_swept_at IS NULL"
                "        OR last_swept_at < CAST(:now AS timestamptz)"
                "           - make_interval(hours => GREATEST("
                "               sweep_interval_hours, :floor)))"
                " ORDER BY last_swept_at NULLS FIRST, id"
            ),
            {"org": str(org_id), "now": now or _utcnow(), "floor": MIN_SWEEP_INTERVAL_HOURS},
        )
    ).all()
    return [row.id for row in rows]


def _address_total(targets: list[str]) -> int:
    return sum(ip_network(target).num_addresses for target in targets)


async def create_sweep(
    session: AsyncSession,
    job_engine: JobEngine,
    org_id: UUID,
    *,
    subnet_id: UUID,
    actor_id: UUID | None,
    trigger: str,
    now: datetime | None = None,
    method: str = "icmp+arp",
    max_targets: int = DEFAULT_MAX_TARGETS,
) -> SweepDispatch:
    """Dispatch one `net.sweep`, or record why not.

    Returns `SweepDispatch(run, envelope, refusal)`. The envelope is `None` when
    nothing was dispatched (`state='refused'`, `state='no_agent'`), and **the
    caller publishes it only after the transaction commits** -- this function
    never touches Redis.

    **THE UNAUTHORIZED TARGET IS RETURNED, NOT RAISED.** `refusal` holds the
    `IpamAuthorizationError` and the run is recorded `state='refused'` before
    anything that could be signed. The caller raises it AFTER committing; see
    `SweepDispatch` and this module's docstring for why raising it here destroyed
    the very row it was recording.

    Raises, before `JobEngine.create` and before any row is added, in every case:
    * `IpamNotFoundError` -- the subnet is not in this org (404, never 403).
    * `IpamValidationError` -- a `container` subnet holds no addresses (§0.2).
    * `IpamTooLargeError` -- IPv6 is never enumerated (§0.5).

    Those three raise rather than return because they add nothing to the
    database: there is no evidence to lose, so no transaction to protect.
    """
    if trigger not in SWEEP_TRIGGERS:
        raise IpamValidationError(
            tr("trigger must be one of {sweep_triggers}", sweep_triggers=list(SWEEP_TRIGGERS))
        )
    stamp = now or _utcnow()
    subnet = await get_subnet(session, org_id, subnet_id)
    # The subnet's client: who sweeps it, and whose dispatch the audit records.
    client_id = subnet.client_id

    # BOTH CHECKS PRECEDE `authorized_targets`, WHICH ANSWERS `[]` FOR BOTH.
    # That empty set is the right safe default and the wrong message: see the
    # module docstring.
    if subnet.kind != "assignable":
        raise IpamValidationError(
            tr(
                "subnet {subnet_prefix} is a container: it exists so the tree reads and "
                "holds no addresses (§0.2), so there is nothing in it to sweep",
                subnet_prefix=subnet.prefix,
            )
        )
    prefix = ip_network(str(subnet.prefix), strict=False)
    if prefix.version == 6:
        raise IpamTooLargeError(
            tr(
                "subnet too large to enumerate: {prefix} -- an ICMP sweep of an IPv6 "
                "prefix is not a hard query, it is a category error (§0.5). IPv6 is "
                "stored, documented and reconciled; it is never enumerated",
                prefix=prefix,
            )
        )

    targets = await authorized_targets(
        session, org_id, subnet, now=stamp, max_targets=max_targets
    )
    if not targets:
        run = IpamSweepRun(
            org_id=org_id,
            subnet_id=subnet.id,
            state="refused",
            trigger=trigger,
            target_count=0,
            scope_digest="",
            requested_by=actor_id,
            started_at=stamp,
            finished_at=stamp,
            error="not authorized",
        )
        session.add(run)
        await session.flush()
        # RETURNED, NOT RAISED, and the flushed row above is the reason. See
        # `SweepDispatch.refusal`.
        return SweepDispatch(
            run,
            None,
            IpamAuthorizationError(
                tr(
                    "no active scan authorization covers {subnet_prefix} in this VRF (§0.12: "
                    "deny by default -- documenting a subnet authorizes nothing). A holder of "
                    "ipam.scope.manage must authorize the prefix first",
                    subnet_prefix=subnet.prefix,
                )
            ),
        )

    device_id = await select_sweeper(session, org_id, subnet, now=stamp)
    if device_id is None:
        run = IpamSweepRun(
            org_id=org_id,
            subnet_id=subnet.id,
            state="no_agent",
            trigger=trigger,
            target_count=_address_total(targets),
            scope_digest=scope_digest(targets),
            requested_by=actor_id,
            started_at=stamp,
            finished_at=stamp,
            error="no eligible agent on this segment",
        )
        session.add(run)
        await session.flush()
        return SweepDispatch(run, None)

    digest = scope_digest(targets)
    run = IpamSweepRun(
        org_id=org_id,
        subnet_id=subnet.id,
        device_id=device_id,
        state="queued",
        trigger=trigger,
        target_count=_address_total(targets),
        scope_digest=digest,
        requested_by=actor_id,
        started_at=stamp,
    )
    session.add(run)
    await session.flush()

    params = NetSweepParams(
        subnet_id=str(subnet.id),
        sweep_run_id=str(run.id),
        targets=targets,
        scope_digest=digest,
        method=method,
        max_targets=max_targets,
    )
    job = await job_engine.create(
        session,
        org_id=org_id,
        type="net.sweep",
        target_device_id=device_id,
        params=params.model_dump(),
        requested_by=actor_id,
        ttl_s=SWEEP_TTL_S,
    )
    run.job_id = job.id
    run.state = "dispatched"
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user" if actor_id is not None else "system",
        actor_id=actor_id,
        action="ipam.sweep_dispatched",
        target_type="device",
        target_id=str(device_id),
        client_id=client_id,
        metadata={
            "sweep_run_id": str(run.id),
            "job_id": str(job.id),
            "subnet_id": str(subnet.id),
            "prefix": str(subnet.prefix),
            "target_count": run.target_count,
            "targets": targets[:10],
            "scope_digest": digest,
            "trigger": trigger,
        },
    )
    envelope = await job_engine.signed_envelope(job)
    await session.flush()
    return SweepDispatch(run, envelope)


async def list_sweep_runs(
    session: AsyncSession, org_id: UUID, subnet_id: UUID, *, limit: int = 50
) -> list[IpamSweepRun]:
    """§9's `/subnets/{id}/sweep-runs`, newest first.

    The subnet is resolved first, so a run list for another org's subnet is a
    404 about the subnet rather than an empty list -- an empty list is what a
    subnet that has never been swept looks like, and the two must not be the
    same answer.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise IpamValidationError(tr("limit must be between 1 and 200"))
    subnet = await get_subnet(session, org_id, subnet_id)
    rows = (
        await session.execute(
            select(IpamSweepRun)
            .where(
                IpamSweepRun.org_id == org_id, IpamSweepRun.subnet_id == subnet.id
            )
            .order_by(IpamSweepRun.started_at.desc(), IpamSweepRun.id.desc())
            .limit(limit)
        )
    )
    return list(rows.scalars().all())
