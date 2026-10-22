"""Wire contracts for `site.wake` (spec §5.2, §5.3).

Exported to `docs/kernel/wire-protocol/` by `scripts/export_wire_schema.py` as
`SiteWakeParams.schema.json` and `SiteWakeResult.schema.json`.

**THAT EXPORT IS A COMPATIBILITY CONTRACT, NOT AN INTERNAL DETAIL**, even though
the agent now lives in this same tree under `agent/`. A deployed agent is older
than the control plane it talks to and updates on its own schedule, so a change
to these two models is additive, or versioned, or it breaks agents in the field
— and **no test in this repository will tell you which**. `CLAUDE.md`'s
"Repository layout" section is explicit that living in one repo makes it
*possible* to change both sides in one commit and *tempting* to treat the
envelope as internal.

**NO Go CAPABILITY EXISTS FOR THIS JOB TYPE.** `agent/internal/capabilities/`
has no `site` package and `docs/modules/agent/plan.md` does not plan one. The
deliverable of this module is the schema, not the handler.

**`broadcast` IS AN ADDRESS AND NEVER A CIDR, AND THAT IS THE SECURITY
PROPERTY** (§0.2, §5.2). The control plane picks the declared segment containing
the target and computes the broadcast address itself; the agent broadcasts to
exactly that address and to nothing else. Handing down a CIDR for the agent to
resolve would make an authorization revoked in the control plane depend on the
endpoint re-deriving something — the same mistake IPAM's enumerated `targets`
exists to avoid. The model REFUSES a value carrying a prefix length, so this is
a schema property rather than a matter of discipline at the call site.

**THE TARGET MAC IS A PARAMETER, NOT A JOB TARGET.** The signed `device` field
names the SITE SERVER. The machine being woken usually has no `devices` row at
all — waking bare metal for provisioning is the primary use — so it cannot be a
job target and must not be modelled as one.

**`SiteWakeResult` HAS NO `woke` FIELD** (§0.9). A magic packet is an
unauthenticated, unacknowledged, fire-and-forget L2 broadcast; there is no reply
to wait for. A result field that cannot carry true information is worse than a
missing one, and a "did it come up?" poll is how this module becomes the
monitoring product. Confirmation that a machine woke is its agent connecting or
its provisioning run advancing, both already observed elsewhere.
"""

from datetime import datetime
from ipaddress import AddressValueError, IPv4Address
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.i18n import tr

__all__ = [
    "SiteServerOut",
    "SiteWakeParams",
    "SiteWakeRequest",
    "SiteWakeResult",
    "SiteServerCreate",
    "SiteServerUpdate",
]


class SiteWakeParams(BaseModel):
    """server -> agent. Everything the endpoint is permitted to know and do."""

    model_config = ConfigDict(extra="forbid")

    #: Canonical lowercase colon form. Validated here as well as at the service
    #: boundary so the SIGNED bytes are canonical: two spellings of one NIC
    #: produce two different signatures over the same intent.
    target_mac: str
    #: An explicit broadcast ADDRESS. Never a CIDR (see the module docstring).
    broadcast: str
    #: The two ports Wake-on-LAN has ever used. Not a free integer: an arbitrary
    #: destination port turns a wake capability into a general "send a UDP
    #: datagram of my choosing to a broadcast address" primitive.
    port: Literal[7, 9] = 9
    #: Magic packets are lossy UDP with no acknowledgement, so a small fixed
    #: repeat is the only available reliability. Bounded at 3: this is a wake,
    #: not a traffic generator.
    repeat: int = Field(default=3, ge=1, le=3)

    @field_validator("target_mac")
    @classmethod
    def _canonical_mac(cls, value: str) -> str:
        from app.modules.site_server.errors import SiteServerValidationError
        from app.modules.site_server.net import normalize_mac

        try:
            return normalize_mac(value)
        except SiteServerValidationError as exc:
            raise ValueError(str(exc)) from None

    @field_validator("broadcast")
    @classmethod
    def _plain_address(cls, value: str) -> str:
        """A bare IPv4 address, refusing anything carrying a prefix length.

        `IPv4Address("10.20.7.255/24")` already raises, so the explicit check is
        for the message: "broadcast must be an address, not a CIDR" names the
        actual mistake, and the mistake is the one §0.2 is about.
        """
        text = value.strip()
        if "/" in text:
            raise ValueError(
                tr(
                    "broadcast must be an explicit address, not a CIDR (got {value}): the control "
                    "plane computes it from the declared segment so the endpoint never decides "
                    "what it may touch",
                    value=repr(value),
                )
            )
        try:
            return str(IPv4Address(text))
        except (AddressValueError, ValueError) as exc:
            raise ValueError(
                tr("invalid broadcast address {value}: {exc}", value=repr(value), exc=exc)
            ) from None


class SiteWakeResult(BaseModel):
    """agent -> server. `sent`, never `woke` (§0.9)."""

    model_config = ConfigDict(extra="forbid")

    sent: bool
    #: Must equal the signed `params.broadcast`. The result handler rejects a
    #: mismatch: a wake that went somewhere else is not a successful wake, and
    #: the check costs one comparison.
    broadcast: str
    sent_at: datetime


# --------------------------------------------------------------------------- #
# HTTP request/response models. These are NOT the agent wire contract and are
# deliberately in the same file only because the module has one schema module;
# nothing here is exported to `docs/kernel/wire-protocol/`.
# --------------------------------------------------------------------------- #


class SiteServerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: UUID
    segments: list[str]
    roles: list[str] = ["wake"]
    name: str | None = None
    ipam_site_id: UUID | None = None
    notes: str = ""


class SiteServerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    segments: list[str] | None = None
    roles: list[str] | None = None
    notes: str | None = None
    enabled: bool | None = None
    ipam_site_id: UUID | None = None


class SiteWakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_mac: str
    #: Which declared segment to wake on. Optional only when the site server
    #: declares exactly one; otherwise the caller must say, because guessing
    #: puts a datagram on a wire nobody named.
    segment: str | None = None


class SiteServerOut(BaseModel):
    id: UUID
    device_id: UUID
    name: str
    segments: list[str]
    roles: list[str]
    enabled: bool
    notes: str
    ipam_site_id: UUID | None
    agent_status: str | None
    agent_last_seen_at: datetime | None
