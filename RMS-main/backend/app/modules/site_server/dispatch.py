"""`site.wake` dispatch: resolving the broadcast address and signing the job
(spec §0.2, §0.9, §0.10, §5.2, §11).

**THIS IS THE FILE THAT PUTS A PACKET ON SOMEBODY ELSE'S LAN, AND IT IS THE ONLY
FILE IN THIS MODULE THAT MAY IMPORT `JobEngine`.**
`tests/test_site_server_kernel_boundary.py` asserts that structurally, as an
exact map rather than a subset, so a second egress path added anywhere else in
the package is named by the failure.

**THE RULE THAT MAKES THIS SAFE IS §0.2's: THE ENDPOINT NEVER DECIDES WHAT IT
MAY TOUCH.** The control plane picks the declared segment CONTAINING the target
address, computes the broadcast ADDRESS from it, and puts that in the signed
params. The agent broadcasts to exactly that address and to nothing else. A
target with no containing segment is a REFUSAL BEFORE ANY JOB IS CREATED --
never a guess, never "the first segment", never a CIDR handed down for the agent
to resolve. Same shape as IPAM's enumerated `targets`, and for the same reason:
an authorization revoked in the control plane must not depend on the endpoint
re-deriving anything.

**INVARIANT #1 IS UNTOUCHED AND THERE IS NO RELAY.** A `site.wake` job is an
ordinary kernel-signed job targeting the SITE SERVER'S OWN DEVICE. The machine
being woken is a PARAMETER: it usually has no `devices` row at all -- waking
bare metal for provisioning is the primary use -- so it cannot be a job target
and must not be modelled as one. Nothing here forwards a job to a third machine,
and the magic packet the agent emits is not a command: it is a link-layer power
signal that carries no instruction, executes nothing, and is verified by
nothing, which is why it can exist at all without an unsigned command path.

**INVARIANT #2 IS UNTOUCHED.** Nothing here gives the control plane an inbound
route to an endpoint. The job travels down the mutual-TLS WebSocket the site
server itself dialled, and the agent's side of it opens one outbound UDP socket,
sends, and closes.

**NOTHING HERE PUBLISHES TO REDIS.** This returns the signed envelope and the
CALLER publishes it after the tenant transaction commits, so a job can never
reach an agent describing state that rolled back. That is the shape
`ipam/sweep.py::create_sweep` already uses, for the reason its module docstring
gives.

**`agents.capabilities` IS NEVER CONSULTED HERE, AND THAT IS DELIBERATE.**
Nothing in this repository writes that column -- `DeviceFacts` has five fields
and none is a capability, and no agent->server message kind carries them (spec
§13.1). Capability advertisement is not a degraded path, it is a dead one.
Refusal (a real `JobStatus`) is the working mechanism, and `site.wake` inherits
it unchanged.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.wire_words import say_list
from app.jobs.engine import JobEngine
from app.models import Job
from app.modules.site_server.errors import SiteServerValidationError
from app.modules.site_server.handler import WAKE_TTL_S
from app.modules.site_server.net import (
    broadcast_for,
    containing_segment,
    normalize_mac,
    parse_segment,
    segment_strings,
)
from app.modules.site_server.registry import get_site_server

__all__ = ["wake_via_site_server"]


def _resolve_broadcast(
    declared: list[str], *, segment: str | None, target_mac: str
) -> tuple[str, str]:
    """`(chosen segment, broadcast address)`, or a refusal.

    Three cases, and none of them guesses:

    * `segment` names one of the declared segments -> that one. Matched by
      CONTAINMENT so a caller may name `10.20.7.0/24` on a server declared for
      `10.20.0.0/16`; the broadcast is then computed from the NAMED prefix,
      because that is the wire the caller says the machine is on.
    * `segment` names an address -> the MOST SPECIFIC declared segment
      containing it. A branch server declaring both `10.20.0.0/16` and
      `10.20.7.0/24` must broadcast to `10.20.7.255`, not `10.20.255.255`.
    * `segment` is absent and exactly ONE segment is declared -> that one.
      Absent with several declared is a refusal: the caller has not said which
      wire, and the module has no basis for choosing.

    A MAC address carries no topology, so the machine being woken cannot be
    located from `target_mac` alone. That is why `segment` exists at all, and
    why its absence is only defaulted when there is nothing to choose between.
    """
    if segment is None:
        if len(declared) != 1:
            raise SiteServerValidationError(
                tr(
                    "this site server declares {count} segments ({declared}); name the one "
                    "{target_mac} is on. A MAC address carries no topology, so it cannot be "
                    "located from the address alone, and picking a segment for you would put a "
                    "datagram on a wire nobody named",
                    count=len(declared),
                    declared=say_list({}, declared),
                    target_mac=target_mac,
                )
            )
        chosen = parse_segment(declared[0])
        return str(chosen), broadcast_for(chosen)

    text = segment.strip()
    if "/" in text:
        named = parse_segment(text)
        covering = [
            candidate
            for candidate in (parse_segment(value) for value in declared)
            if named.subnet_of(candidate)
        ]
        if not covering:
            raise SiteServerValidationError(
                tr(
                    "segment {text} is not served by this site server (declared: {declared}). A "
                    "wake must originate on the target's own segment, and this server is not on "
                    "that one",
                    text=text,
                    declared=say_list({}, declared),
                )
            )
        return str(named), broadcast_for(named)

    chosen_network = containing_segment(declared, text)
    if chosen_network is None:
        raise SiteServerValidationError(
            tr(
                "address {text} is not inside any segment this site server declares "
                "({declared}). Nothing is dispatched: guessing a segment would put a datagram "
                "on a wire nobody authorized",
                text=text,
                declared=say_list({}, declared),
            )
        )
    return str(chosen_network), broadcast_for(chosen_network)


async def wake_via_site_server(
    session: AsyncSession,
    job_engine: JobEngine,
    *,
    org_id: UUID,
    site_server_id: UUID,
    target_mac: str,
    segment: str | None = None,
    actor_id: UUID | None = None,
    port: int = 9,
    repeat: int = 3,
) -> tuple[Job, dict[str, Any]]:
    """Create and sign one `site.wake` job. Returns `(job, envelope)`.

    **THE ENVELOPE IS NOT PUBLISHED HERE.** The caller publishes it after the
    tenant transaction commits.

    Raises `SiteServerNotFoundError` (404) for a site server outside this org,
    and `SiteServerValidationError` (400) for a disabled server, a malformed
    MAC, or a target on no declared segment — every one of them BEFORE
    `JobEngine.create`, so a refused wake creates no job row, builds no
    canonical payload, and produces no signature.
    """
    site_server = await get_site_server(session, org_id, site_server_id)
    if not site_server.enabled:
        raise SiteServerValidationError(
            tr(
                "site server {site_server_id} is disabled; enable it before waking through it",
                site_server_id=site_server_id,
            )
        )

    mac = normalize_mac(target_mac)
    declared = segment_strings(site_server.segments)
    chosen_segment, broadcast = _resolve_broadcast(
        declared, segment=segment, target_mac=mac
    )

    from app.modules.site_server.schema import SiteWakeParams

    params = SiteWakeParams(
        target_mac=mac, broadcast=broadcast, port=port, repeat=repeat
    )
    job = await job_engine.create(
        session,
        org_id=org_id,
        # The SITE SERVER'S device. Not the machine being woken, which usually
        # has no `devices` row and could not be a target if it did.
        target_device_id=site_server.device_id,
        type="site.wake",
        params=params.model_dump(),
        requested_by=actor_id,
        ttl_s=WAKE_TTL_S,
    )

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user" if actor_id is not None else "system",
        actor_id=actor_id,
        action="site.wake_dispatched",
        target_type="site_server",
        target_id=str(site_server.id),
        metadata={
            "job_id": str(job.id),
            "device_id": str(site_server.device_id),
            "target_mac": mac,
            "broadcast": broadcast,
            "segment": chosen_segment,
        },
    )
    envelope = await job_engine.signed_envelope(job)
    await session.flush()
    return job, envelope
