"""Matching an enrolling agent to a record the inventory already has.

`docs/modules/asset-inventory/spec.md` §4, as amended 2026-08-18.

THE ASYMMETRY IS THE DESIGN, and it is worth reading before changing anything
here. A physical MAC that matches exactly one agentless record LINKS. Anything
weaker -- a shared hostname, a shared address, two records matching at once --
creates the device and files a suggestion for a human.

That is not caution for its own sake. Auto-linking on a hostname silently
merges two real machines that happen to share a name across sites, and the
merge is destructive: afterwards nothing says which inventory data, which job
history or which documentation belonged to which machine. A suggestion queue is
mildly annoying; a bad merge is data loss discovered months later.

WHAT "LINK" MEANS HERE: reuse the existing row. The `agents` row attaches to
the record already present and its machine-reported facts are written onto it.
No second `devices` row is created, so there is never a pair to merge later --
which is what keeps the warning above from ever applying to the LINKING path.

WHAT THIS DELIBERATELY DOES NOT DO:

  * Match on serial. §4 wanted to, and nothing collects one at enrolment: the
    agent sends `Serial: nil` because a serial needs a per-OS collector (WMI,
    ioreg, DMI) that belongs to `inventory.collect`. The rule is inert and is
    described that way rather than replaced by a weaker one.
  * Trust the agent's own MAC filtering. The field arrives over the wire, and
    the consequence of a bad entry is a MERGE -- so the locally-administered
    and multicast checks are re-applied here.
  * Merge two rows that already exist. Nothing in this module does that; the
    suggestion queue's `linked` resolution is the only place it is even
    contemplated.
"""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, Device
from app.modules.ipam.net import parse_mac
from app.modules.ipam.oui import is_locally_administered, is_multicast


@dataclass(frozen=True)
class Suggestion:
    """One record that MIGHT be the same machine, and why it is only a might."""

    candidate_device_id: UUID
    #: `hostname | ip | multiple`
    basis: str


@dataclass(frozen=True)
class MatchOutcome:
    """`device_id` set = link to that row. Otherwise create, and file these."""

    device_id: UUID | None = None
    suggestions: list[Suggestion] = field(default_factory=list)
    #: What linked, for the audit event. `None` when nothing did.
    matched_on: str | None = None


def physical_macs(macs: list[str]) -> set[str]:
    """The subset of `macs` that can identify a machine, normalised.

    RE-APPLIED SERVER-SIDE even though the agent filters the same way. A
    locally-administered address is assigned by software -- Docker, KVM, most
    hypervisor defaults, phone randomisation -- so two machines can legitimately
    carry the same one, and this function is the last thing between that and an
    auto-link. Trusting the sender here would put a merge one buggy or
    compromised agent away.
    """
    out: set[str] = set()
    for raw in macs:
        parsed = parse_mac(raw)
        if parsed is None:
            continue
        if is_multicast(parsed) or is_locally_administered(parsed):
            continue
        out.add(parsed.lower())
    return out


def _device_macs(device: Device) -> set[str]:
    """Every MAC on a device row, from the `nics` shape inventory writes.

    `{"items": [{"identifier": ..., "mac": ...}]}` -- the same structure
    `promote()` writes for a discovered row, so one reader serves both sources.
    Tolerant of anything else: this is a JSONB column three code paths write,
    and a matcher that raised on an odd row would fail an ENROLMENT.
    """
    nics = device.nics if isinstance(device.nics, dict) else {}
    items = nics.get("items")
    if not isinstance(items, list):
        return set()
    out: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("mac", "identifier"):
            parsed = parse_mac(str(item.get(key) or ""))
            if parsed is not None:
                out.add(parsed.lower())
    return out


async def resolve_enrolment_target(
    session: AsyncSession, org_id: UUID, facts: object
) -> MatchOutcome:
    """Decide whether this enrolment joins an existing record or creates one.

    `facts` is a `DeviceFacts`; it is typed loosely to keep this module free of
    an import cycle with `services.enrollment`.
    """
    hostname = getattr(facts, "hostname", None)
    wire_macs = physical_macs(list(getattr(facts, "macs", []) or []))

    # ONLY AGENTLESS ROWS ARE CANDIDATES. `agents.device_id` is unique, so
    # linking to a managed device would surface as an IntegrityError in the
    # middle of an enrolment -- a 500 an operator has to interpret rather than a
    # refusal with a reason. The realistic shape is a rebuilt machine
    # re-enrolling while its old record still carries the old agent.
    agentless = (
        await session.execute(
            select(Device)
            .outerjoin(Agent, Agent.device_id == Device.id)
            .where(Device.org_id == org_id, Agent.id.is_(None))
        )
    ).scalars().all()

    if wire_macs:
        by_mac = [d for d in agentless if _device_macs(d) & wire_macs]
        if len(by_mac) == 1:
            return MatchOutcome(device_id=by_mac[0].id, matched_on="mac")
        if len(by_mac) > 1:
            # Rule 4. An `ORDER BY ... LIMIT 1` here would silently pick one,
            # which is the shape of every merge that gets noticed a year later.
            return MatchOutcome(
                suggestions=[Suggestion(d.id, "multiple") for d in by_mac]
            )

    # Rule 3. Hostname is weak -- renames are routine, and so are duplicates
    # across sites -- so a match is a question, never an answer.
    if hostname:
        by_hostname = [d for d in agentless if d.hostname == hostname]
        if by_hostname:
            basis = "hostname" if len(by_hostname) == 1 else "multiple"
            return MatchOutcome(
                suggestions=[Suggestion(d.id, basis) for d in by_hostname]
            )

    return MatchOutcome()
