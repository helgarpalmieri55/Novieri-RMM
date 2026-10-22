"""Wire contracts for `net.sweep` (`docs/modules/ipam/spec.md` §5.2, §5.4).

Exported to `docs/kernel/wire-protocol/` by `scripts/export_wire_schema.py` as
`NetSweepParams.schema.json` and `NetSweepDocument.schema.json` -- **that export
is the contract `agent/internal/capabilities/netsweep/` is written against**, and
that package's own tests read the exported JSON to assert its structs match.

The agent lives in THIS repository now (`CLAUDE.md`, "the agent moved in-tree"),
which changes who is obliged without changing that the export is a contract: a
deployed agent is older than the control plane, so a field added here is
additive or it breaks the fleet. §5.5's idle-budget requirements -- dormant by
default, no local schedule, sockets opened at job start and closed at job end --
are now MEASURED by that package rather than restated as somebody else's
obligation.

**WHAT IS ABSENT FROM `NetSweepDocument` IS THE POINT** (§0.10 points 2 and 5,
§7). There is no RTT field, no per-probe timing, no retry count and no
`reachable: false` entry: a host that did not answer is simply not in `hosts`.
`SweepHost` is `extra="forbid"` so that is a SCHEMA PROPERTY rather than a
matter of restraint -- if latency were present somebody would chart it, and the
chart would be the monitoring product this module exists not to become.

**THE TWO `extra` SETTINGS DIFFER ON PURPOSE, and the difference is the module's
threat model.**

* `SweepHost` forbids extras. The per-host record is where a monitoring field
  would creep in one release at a time, and closing it is how §7's boundary
  becomes checkable.
* `NetSweepDocument` IGNORES unknown top-level keys, matching
  `InventoryDocument`. The keys an untrusted agent would want to smuggle at that
  level are `org_id` and `device_id`, and the control that matters for those is
  not "the agent may not say it" but **"the server never reads it"**: §6.4 takes
  `org_id` from the job and pins `device_id` to NULL, and `handle_sweep_result`
  reads every value through this model rather than out of the raw dict. Forbid
  there would express the property in a schema that a later field addition
  relaxes, instead of in a handler that cannot read it at all.

**`subnet_id` AND `sweep_run_id` ARE `str`, NOT `UUID`, AND THAT IS FORCED BY
THE KERNEL.** `JobEngine.create` stores `params_model(**params).model_dump()`
into `jobs.params` (JSONB) and signs `canonical_bytes` of it, and
`app/jobs/canonical.py::_reject_unknown` raises `TypeError: not canonically
serializable: UUID` for anything json cannot encode. `ProvisionEnrollParams`
hit the same wall and resolved it the same way (`provision_run_id: str`). The
validator below normalises and REJECTS a non-UUID, so the contract stays honest:
the field is a UUID, carried as its canonical text.
"""

from datetime import datetime
from ipaddress import ip_network
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

__all__ = [
    "DEFAULT_BUDGET_S",
    "DEFAULT_MAX_TARGETS",
    "DEFAULT_PROBE_TIMEOUT_MS",
    "DEFAULT_RATE_PPS",
    "NetSweepDocument",
    "NetSweepParams",
    "SweepHost",
]

#: §5.2's defaults, named so `sweep.py`, the API layer and the exported schema
#: cannot disagree about them.
DEFAULT_RATE_PPS = 50
DEFAULT_PROBE_TIMEOUT_MS = 500
DEFAULT_MAX_TARGETS = 4096
DEFAULT_BUDGET_S = 300

#: A CIDR string is at most `xxxx:....:xxxx/128` -- 43 characters. The bound is
#: on the *entry*; the list bound is on the count; and `sweep.py` caps the
#: ADDRESS total. All three are needed: a short list of short strings can still
#: name sixteen million addresses.
_Cidr = Annotated[str, StringConstraints(min_length=7, max_length=43)]
_Error = Annotated[str, StringConstraints(max_length=200)]


def _uuid_text(value: str) -> str:
    """Canonical UUID text, or a validation error. See the module docstring for
    why these fields are text at all."""
    return str(UUID(str(value)))


class NetSweepParams(BaseModel):
    """server -> agent (§5.2). SIGNED: every bound here is covered by the job
    signature (invariant #1).

    `targets` is an **explicit enumerated list**, never "the subnet". The agent
    is never asked to decide what it may touch and has no local configuration to
    consult -- §0.9's whole argument is that a locally scheduled or locally
    scoped sweep is a standing authorization signed once and executed for weeks.

    `rate_pps`, `max_targets` and `budget_s` are inside these params rather than
    in agent configuration so the ceiling on what a sweep can COST is set by the
    server and cannot be raised by anything on the endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    subnet_id: str
    sweep_run_id: str
    targets: list[_Cidr] = Field(min_length=1, max_length=4096)
    scope_digest: str = Field(min_length=64, max_length=64)
    method: Literal["icmp", "icmp+arp"] = "icmp+arp"
    rate_pps: int = Field(default=DEFAULT_RATE_PPS, ge=1, le=200)
    probe_timeout_ms: int = Field(default=DEFAULT_PROBE_TIMEOUT_MS, ge=100, le=5000)
    max_targets: int = Field(default=DEFAULT_MAX_TARGETS, ge=1, le=65536)
    budget_s: int = Field(default=DEFAULT_BUDGET_S, ge=10, le=900)

    _normalise_ids = field_validator("subnet_id", "sweep_run_id")(_uuid_text)

    @field_validator("targets")
    @classmethod
    def _parseable_cidrs(cls, value: list[str]) -> list[str]:
        """Every entry is a real network in canonical form.

        Validated HERE rather than only in `sweep.py` because `JobEngine.create`
        runs this model over the params it is about to sign: a target list that
        the agent could not parse must never acquire a signature.
        """
        out: list[str] = []
        for entry in value:
            network = ip_network(entry, strict=True)
            out.append(str(network))
        return out


class SweepHost(BaseModel):
    """One responding address (§5.4). `extra="forbid"` -- see the module
    docstring; this is where §0.10 point 5 is enforced.

    `hostname` is display-only agent-side reverse DNS. It is never a
    reconciliation predicate and never matches an address to a device: identity
    comes from MACs and from inventory, both of which have a chain of custody.
    """

    model_config = ConfigDict(extra="forbid")

    address: Annotated[str, StringConstraints(max_length=64)]
    mac: Annotated[str, StringConstraints(max_length=32)] | None = None
    responded_via: Literal["icmp", "arp", "both"]
    hostname: Annotated[str, StringConstraints(max_length=255)] | None = None


class NetSweepDocument(BaseModel):
    """agent -> server (§5.4). Checked in full by §6.4 before a single row is
    written, and rejected WHOLE on any failure.

    `hosts` is bounded here at the schema's ceiling for `max_targets`; §6.4
    additionally checks it against the value this job was actually SIGNED with,
    which is the bound that matters.
    """

    model_config = ConfigDict(extra="ignore")

    subnet_id: str
    sweep_run_id: str
    scope_digest: str = Field(min_length=64, max_length=64)
    method: Annotated[str, StringConstraints(max_length=16)]
    started_at: datetime
    finished_at: datetime
    probed_count: int = Field(ge=0, le=16_777_216)
    truncated: bool = False
    hosts: list[SweepHost] = Field(default_factory=list, max_length=65536)
    errors: list[_Error] = Field(default_factory=list, max_length=20)

    _normalise_ids = field_validator("subnet_id", "sweep_run_id")(_uuid_text)
