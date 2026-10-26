"""The `net.sweep` job type and its REJECTING result handler
(`docs/modules/ipam/spec.md` §5.1, §5.4, §6.4, §0.6, §10, §11.3).

**THE AGENT'S REPORT IS UNTRUSTED INPUT.** This handler runs inside the job's
tenant transaction (`JobEngine.complete`'s module extension point) and checks the
whole document before a single row is written. Any failure raises
`IpamSweepRejected`, which flips the job to `failed` and -- because the checks
run before any write -- leaves **zero** observations.

**THE WHOLE RESULT, NOT THE BAD ROWS.** This follows the shipped precedent in
`app/modules/security_logs/ingest.py`, which rejects an entire shipped batch on a
single event outside the taxonomy. Filtering optimises for keeping data;
rejecting optimises for **detecting the compromise**. An agent returning
addresses it was not authorized to probe is either buggy or controlled by
someone else, and quietly dropping the bad rows keeps its good rows flowing while
hiding the tell. The cost is real and small: one malformed address wastes one
sweep, and the next scheduled run recovers it.

**`max_targets` IS AN ADDRESS BUDGET AND THE DOCUMENT IS BOUNDED AS ONE.**
`authorization._cap` computes it by counting ADDRESSES, so checking it against
the number of host ENTRIES bounds the wrong thing: 4096 records for one address,
each with an agent-chosen MAC, are all inside the signed scope and all pass
§6.4's containment check, and `mac_key`'s `coalesce(mac::text,'')` gives every
one of them its own row. That turns §2.7 from the current-state table §0.10 and
§7 depend on into the probe log this module exists not to keep -- without a
schema change, from one signed sweep. Two checks restore the invariant: the
record count is bounded by `min(max_targets, addresses in the signed targets)`,
and **each address may appear at most once**.

**THE PERMITTED SET IS RECOMPUTED FROM THE JOB'S OWN SIGNED `params.targets`,
NEVER FROM THE CURRENT DATABASE** (§6.4 step 1). Recomputing from live
authorizations would let a scope granted *after* dispatch retroactively bless
results that were out of scope when they were collected.

**`net.scope_digest` HAS EXACTLY ONE IMPLEMENTATION AND THIS IS ITS SECOND CALL
SITE.** `sweep.py` computes it over the list it signs; this recomputes it over
`params.targets` and compares against both the params and the document. Two
digests that drift is how a scope check passes on both sides while checking
nothing.

**THREE PROPERTIES THAT GET LOST IN A REFACTOR**, so each has its own test:

1. `org_id` on every written observation comes from the **job**. The result
   document has no org field this code reads.
2. `device_id` on a sweep-derived observation is **always NULL**. A sweep sees
   addresses and MACs, not identity, and letting it assert one would be a
   compromised agent's cheapest way to re-point an assignment.
3. Ingestion writes `ipam_observation` rows **only**. It cannot write
   `ipam_address.state`, `device_id` or `assigned_by` -- §0.6's split is the
   control, and there is no code path here that could.

**AND ONE PROPERTY THAT CHANGED, DELIBERATELY, ON 2026-08-26.** A successful
sweep now also writes `devices` rows, one per host it found, with
`provenance='sweep'` (`asset-inventory/spec.md` §3). §0.6's split is untouched:
this still cannot write `ipam_address`, and the asset row it creates asserts no
address ASSIGNMENT -- it records that the estate contains a machine, which is
the inventory's question, not IPAM's. The write happens after every check in
§6.4 has passed, so a rejected result creates nothing.

**AUDIT BEFORE THE RAISE, DELIBERATELY.** `JobEngine.complete` CATCHES the
exception rather than rolling the transaction back, so the audit rows and the
domain event written here survive the rejection and the observations never
existed to be rolled back. Nothing in this file raises a *database* error before
those writes -- every address and MAC is parsed in Python first -- because a
poisoned transaction would take the audit trail down with the result.
"""

from datetime import UTC, datetime
from ipaddress import ip_network
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.events import publish
from app.gateway.dispatch import job_client
from app.jobs.registry import JobType, register
from app.models import Job
from app.modules.ipam.candidates import SweepSighting, auto_create_from_sweep
from app.modules.ipam.errors import IpamSweepRejected
from app.modules.ipam.models import IpamSubnet, IpamSweepRun
from app.modules.ipam.net import IPNetwork, parse_address, parse_mac, scope_digest
from app.modules.ipam.observations import upsert_observation
from app.modules.ipam.schema import NetSweepDocument, NetSweepParams

__all__ = ["handle_sweep_result", "register_job_types"]

#: §6.4's bounded evidence sample. The payload comes from a device that has just
#: been caught reporting outside its authorization, so the sample is truncated
#: while the COUNT is reported in full -- the bound limits the evidence, never
#: the finding.
_SAMPLE_LIMIT = 10

#: `ipam_sweep_run.error` is `String(500)`.
_MAX_ERROR = 500

#: §2.7: confidence is a property of the method, fixed at write time. `arp` is
#: authoritative for address<->MAC on-segment and not for identity; `icmp` means
#: something answered, and that is all it means. §4.3's `mac_mismatch` reads this
#: column, which is why it is a column and not a comment.
_METHOD_BY_RESPONSE = {"icmp": "icmp", "arp": "arp", "both": "arp"}
_CONFIDENCE_BY_METHOD = {"icmp": "low", "arp": "medium"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _reject(
    session: AsyncSession,
    job: Job,
    *,
    params: NetSweepParams | None,
    run: IpamSweepRun | None,
    reason: str,
    offending: list[str] | None = None,
) -> IpamSweepRejected:
    """Record the rejection and RETURN the exception for the caller to raise.

    Returned rather than raised so every call site reads `raise await
    _reject(...)` -- the raise is visible at the point of the failing check, and
    a future edit cannot add a check that records a rejection and then forgets
    to stop.
    """
    now = _utcnow()
    if run is not None:
        run.state = "failed"
        run.error = reason[:_MAX_ERROR]
        run.finished_at = now
        session.add(run)
        await session.flush()

    subject_id = run.id if run is not None else job.id
    payload: dict[str, Any] = {
        "device_id": str(job.target_id),
        "subnet_id": params.subnet_id if params is not None else None,
        "sweep_run_id": params.sweep_run_id if params is not None else None,
        "reason": reason[:_MAX_ERROR],
        "offending_count": len(offending or []),
        "sample": (offending or [])[:_SAMPLE_LIMIT],
    }
    await audit_service.record(
        session,
        org_id=job.org_id,
        actor_type="agent",
        actor_id=None,
        action="ipam.sweep_rejected",
        target_type="device",
        target_id=str(job.target_id),
        metadata={
            "job_id": str(job.id),
            "subnet_id": payload["subnet_id"],
            "sweep_run_id": payload["sweep_run_id"],
            "reason": payload["reason"],
        },
    )
    if offending:
        # §6.4: the scope violation specifically gets its own action, because
        # "this agent sent us something we could not parse" and "this agent
        # probed an address nobody authorized" are different events for whoever
        # reads the audit log.
        await audit_service.record(
            session,
            org_id=job.org_id,
            actor_type="agent",
            actor_id=None,
            action="ipam.sweep_out_of_scope",
            target_type="device",
            target_id=str(job.target_id),
            metadata={
                "job_id": str(job.id),
                "subnet_id": payload["subnet_id"],
                "sweep_run_id": payload["sweep_run_id"],
                "offending_count": len(offending),
                "sample": offending[:_SAMPLE_LIMIT],
            },
        )
    await publish(
        session,
        org_id=job.org_id,
        event_type="ipam.sweep_rejected",
        subject_type="ipam_sweep_run",
        subject_id=subject_id,
        payload=payload,
        actor_user_id=None,
        client_id=await job_client(session, job.id),
    )
    await session.flush()
    return IpamSweepRejected(reason)


async def _load_run(
    session: AsyncSession, org_id: UUID, run_id: UUID
) -> IpamSweepRun | None:
    """The run, loaded with the JOB's `org_id`.

    A signed job for org A can never complete a run belonging to org B, even
    though `ipam_sweep_run.id` is a bare UUID that org A could name. RLS says the
    same thing inside a tenant session; the predicate says it everywhere else.
    """
    return (
        await session.execute(
            select(IpamSweepRun).where(
                IpamSweepRun.org_id == org_id, IpamSweepRun.id == run_id
            )
        )
    ).scalar_one_or_none()


async def _load_subnet(
    session: AsyncSession, org_id: UUID, subnet_id: UUID
) -> IpamSubnet | None:
    return (
        await session.execute(
            select(IpamSubnet).where(
                IpamSubnet.org_id == org_id, IpamSubnet.id == subnet_id
            )
        )
    ).scalar_one_or_none()


async def handle_sweep_result(
    session: AsyncSession, job: Job, result: dict[str, Any]
) -> None:
    """§6.4, in order. Every check runs before any write.

    The order is not arbitrary: identity first (does this document describe the
    job we signed?), then bounds, then parsing, then scope. Checking scope before
    parsing would mean deciding whether an unparseable string is inside a
    network.
    """
    try:
        params = NetSweepParams(**job.params)
    except ValidationError as exc:  # pragma: no cover - created through this model
        raise IpamSweepRejected(  # i18n-exempt: gateway result path, stored English
            f"unreadable net.sweep params: {exc}"
        ) from exc

    run = await _load_run(session, job.org_id, UUID(params.sweep_run_id))
    if run is None:
        raise await _reject(
            session,
            job,
            params=params,
            run=None,
            reason="the signed sweep run does not exist in this org",
        )

    # 1. The digest, recomputed over the JOB'S OWN SIGNED TARGETS with the one
    #    implementation the dispatcher used.
    recomputed = scope_digest(params.targets)
    if recomputed != params.scope_digest:
        raise await _reject(
            session,
            job,
            params=params,
            run=run,
            reason="the signed scope_digest does not match the signed targets",
        )

    try:
        doc = NetSweepDocument(**result)
    except ValidationError as exc:
        raise await _reject(
            session,
            job,
            params=params,
            run=run,
            reason=f"invalid net.sweep document: {exc}",
        ) from exc

    # 2. The document echoes the params.
    if doc.scope_digest != recomputed:
        raise await _reject(
            session,
            job,
            params=params,
            run=run,
            reason="the reported scope_digest does not match the authorized scope",
        )
    if doc.subnet_id != params.subnet_id or doc.sweep_run_id != params.sweep_run_id:
        raise await _reject(
            session,
            job,
            params=params,
            run=run,
            reason="the result names a different subnet or sweep run than the job",
        )
    if run.subnet_id != UUID(params.subnet_id):
        raise await _reject(
            session,
            job,
            params=params,
            run=run,
            reason="the signed sweep run belongs to a different subnet",
        )

    subnet = await _load_subnet(session, job.org_id, UUID(params.subnet_id))
    if subnet is None:
        raise await _reject(
            session,
            job,
            params=params,
            run=run,
            reason="the signed subnet no longer exists in this org",
        )

    prefix = ip_network(str(subnet.prefix), strict=False)
    permitted: list[IPNetwork] = [ip_network(target) for target in params.targets]

    # 3. The bound the job was SIGNED with, not the schema's ceiling -- AND IT
    #    IS AN ADDRESS BUDGET, NOT A LIST-ENTRY BUDGET. `authorization._cap`
    #    computes `max_targets` by counting ADDRESSES ("a cap that counted CIDR
    #    entries would sign a /16 as one target"), so applying the same number
    #    to host ENTRIES lets one signed address carry `max_targets` records --
    #    4096 of them at the default, all for `10.70.0.10`, every one of them
    #    inside the signed scope and so past §6.4's containment check.
    #
    #    The scope's own size is the tighter half of the bound and it is signed
    #    too: a sweep of a /29 cannot legitimately report more hosts than the
    #    /29 has addresses, whatever `max_targets` says.
    scope_addresses = sum(network.num_addresses for network in permitted)
    budget = min(params.max_targets, scope_addresses)
    if len(doc.hosts) > budget:
        raise await _reject(
            session,
            job,
            params=params,
            run=run,
            reason=(
                f"the result carries {len(doc.hosts)} host records, above the "
                f"budget of {budget} address(es) this job was signed for "
                f"(max_targets {params.max_targets}, scope {scope_addresses})"
            ),
        )

    parsed: list[tuple[str, str | None, str, str, str]] = []
    #: §2.7 IS A CURRENT-STATE TABLE, NOT A PROBE LOG (§0.10, §7). One sweep
    #: reports what answered at each address ONCE; a document naming the same
    #: address twice is describing a time series, and `mac_key`'s
    #: `coalesce(mac::text,'')` -- with the MAC chosen by the agent -- would
    #: give each repeat its own row in the upsert key. Repeats are a whole-
    #: document rejection like every other §6.4 failure: a compromised agent
    #: that can grow this table without a schema change is the monitoring-
    #: boundary breach the module exists not to have.
    seen: set[str] = set()
    repeated: list[str] = []
    offending: list[str] = []
    for host in doc.hosts:
        address = parse_address(host.address)
        if address is None:
            raise await _reject(
                session,
                job,
                params=params,
                run=run,
                reason=f"the result carries an unparseable address: {host.address!r}",
            )
        if address.version != prefix.version:
            raise await _reject(
                session,
                job,
                params=params,
                run=run,
                reason=(
                    f"the result carries an IPv{address.version} address in an "
                    f"IPv{prefix.version} subnet: {address}"
                ),
            )
        mac: str | None = None
        if host.mac is not None:
            mac = parse_mac(host.mac)
            if mac is None:
                raise await _reject(
                    session,
                    job,
                    params=params,
                    run=run,
                    reason=f"the result carries an unparseable MAC: {host.mac!r}",
                )
        # 4. EVERY address is contained by the SIGNED targets. The `max_targets`
        #    cap does not stand in for this: it bounds how many packets a
        #    widened scope emits, never where they go.
        if not any(address in network for network in permitted):
            offending.append(str(address))
            continue
        # 5. EACH ADDRESS AT MOST ONCE, on the CANONICAL text -- `10.70.0.010`
        #    and `10.70.0.10` are one address and `parse_address` has already
        #    made them one string. Counted after containment so an out-of-scope
        #    result is still reported as a scope violation, which is the finding
        #    that matters about it.
        if str(address) in seen:
            repeated.append(str(address))
            continue
        seen.add(str(address))
        method = _METHOD_BY_RESPONSE[host.responded_via]
        parsed.append(
            (
                str(address),
                mac,
                method,
                _CONFIDENCE_BY_METHOD[method],
                host.hostname or "",
            )
        )

    if offending:
        raise await _reject(
            session,
            job,
            params=params,
            run=run,
            reason=(
                f"{len(offending)} address(es) outside the authorized scope this "
                "job was signed for"
            ),
            offending=offending,
        )

    if repeated:
        sample = ", ".join(sorted(set(repeated))[:_SAMPLE_LIMIT])
        raise await _reject(
            session,
            job,
            params=params,
            run=run,
            reason=(
                f"the result repeats {len(repeated)} host record(s) at "
                f"{len(set(repeated))} address(es) already reported in the same "
                f"document: {sample}"
            ),
        )

    now = _utcnow()
    for address_text, mac, method, confidence, hostname in parsed:
        await upsert_observation(
            session,
            # FROM THE JOB. The document has no org field this code reads.
            org_id=job.org_id,
            vrf_id=subnet.vrf_id,
            address=address_text,
            mac=mac,
            hostname=hostname,
            source="sweep",
            method=method,
            # ALWAYS NULL: a sweep sees addresses and MACs, not identity.
            device_id=None,
            confidence=confidence,
            sweep_run_id=run.id,
            now=now,
        )

    # 6. EVERY HOST FOUND BECOMES AN ASSET (asset-inventory spec §3, on the
    #    operator's ruling of 2026-08-26). This is the one place where a sweep
    #    writes outside IPAM, and it runs LAST for two reasons that are not
    #    stylistic: every §6.4 check has already passed, so a rejected result
    #    cannot create a row -- an agent reporting outside its authorization
    #    must not be able to write inventory for addresses nobody authorized it
    #    to touch -- and the observations are already written, so the identity
    #    lookups below read the same state a later `list_candidates` would.
    #
    #    `org_id` comes from the JOB here too. Property 1 at the top of this
    #    file now governs a second table.
    await auto_create_from_sweep(
        session,
        job.org_id,
        sightings=[
            SweepSighting(
                address=address_text, mac=mac, hostname=hostname, confidence=confidence
            )
            for address_text, mac, _method, confidence, hostname in parsed
        ],
        sweep_run_id=run.id,
        subnet_id=subnet.id,
    )

    run.state = "succeeded"
    run.hosts_found = len(parsed)
    run.truncated = doc.truncated
    run.finished_at = now
    session.add(run)
    subnet.last_swept_at = now
    session.add(subnet)
    await session.flush()


def register_job_types() -> None:
    """§5.1: one job type, one params model, one result handler -- exactly what
    `patch/handler.py` does for `patch.scan`. The module registers into the
    kernel and forks nothing.

    Called from `ipam.register(app)` (API), `app/gateway/main.py` (where agent
    results COMPLETE) and `app/worker.py::register_worker_job_types` (where the
    beat dispatches). All three are needed and none is redundant: registering
    only in `register(app)` leaves the result handler dead in production while
    every test passes.
    """
    # `cancellable` (2026-08-22-job-lifecycle-design.md §9.1): netsweep's own
    # probe loop checks `ctx.Err()`, so the flag claims something the agent code
    # actually does, and a sweep stopped part-way leaves nothing half-written --
    # it just probed fewer hosts. The result handler already checks the returned
    # scope against the `ipam_sweep_run` row, so a partial answer is a partial
    # answer and not a corruption.
    register(
        JobType(
            "net.sweep",
            NetSweepParams,
            result_handler=handle_sweep_result,
            cancellable=True,
        )
    )
