"""Host-key pinning and confirmation -- the crux of the whole feature
(spec §4.4, §7.3; `CLAUDE.md` invariant 2a "does not claim").

**ON FIRST CONTACT THE PLATFORM CANNOT AUTHENTICATE THE TARGET.** There is no
host key, no certificate and no prior relationship -- that is what "unmanaged"
means. Presenting an administrative credential to whatever answers an address is
trust-on-first-use, and TOFU with an administrative credential is a real
exposure that no clause of invariant 2a removes (§1.1, §7.3). What this file
implements is the mitigation the spec specifies, and NOTHING WIDER:

1. A credential-free `agent.push_probe` fetches the host-key fingerprint and
   records it here as OBSERVED. It presents no secret, so learning the key costs
   nothing (falsifier 22).
2. A human CONFIRMS the observed fingerprint in the console -- `confirm` copies
   `observed_fingerprint` into `confirmed_fingerprint`. The operator's
   confirmation is the trust anchor; the observation alone is not one.
3. A push is dispatched only against a host whose confirmed key still equals the
   latest observation, and the confirmed key is signed into the install params
   so the agent's SSH client verifies against IT rather than accepting blindly.

**TWO REFUSALS ARE REQUIREMENTS, NOT WARNINGS (spec §4.4, §7.3, falsifier 21):**

* A push to a host with NO confirmed key is refused, not prompted.
* A push to a host whose latest observation DIFFERS from the confirmed key is
  refused, not warned about. `confirmed_key_for` returns the key only when the
  two still agree; a changed key makes it return `None` with a reason, and the
  operator must re-probe and re-confirm deliberately.

**DELIBERATELY NOT TRUST-ON-FIRST-USE, AND DELIBERATELY NOT BLIND ACCEPT.** The
spec is explicit that the install must verify against a confirmed key. Where the
spec is silent, the strictest reading is taken: no key means refuse, a changed
key means refuse. This module never invents a fingerprint and never accepts one
the operator has not seen.

**NOTHING HERE CONNECTS, SIGNS OR DECRYPTS.** It records what a probe reported
and what an operator confirmed. The observation is produced by the agent inside
a verified `agent.push_probe` job and arrives as an ordinary job result.

**AN ADDRESS NAMES A HOST ONLY WITHIN ONE CLIENT'S NETWORK** (migration 0231,
Task 8b). Two clients' 10.0.0.5 are two machines with two keys, so a row is
per `(org, client, address)` -- its client is its device's -- and every lookup
says whose host it means. A row with no device is GLOBAL (an O row: every
client's), and serves a client that has no row of its own at the address; a
client's own row, confirmed or not, always takes precedence over it. Before,
one row per address: a probe of Y's machine overwrote X's observation and moved
the row -- X's confirmation with it -- into Y.

**A PROBE WRITES ITS OWN NAMESPACE AND NO OTHER** (Task 8b review 1). An
observation of a device creates or updates that device's client's row; one with
no device, the global row. A device's probe never claims, rewrites or moves a
global row: that took the row from every other client it served, told them some
other client's machine at the address had been probed, and handed the device's
client a confirmation nobody made for it. The client's new row is unconfirmed,
and takes precedence over the global one, until a person confirms it. A global
row is changed only under scope all.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import Device
from app.modules.agent_push.errors import (
    PushConflictError,
    PushNotFoundError,
    PushValidationError,
)
from app.modules.agent_push.models import PushHostKey
from app.modules.agent_push.net import parse_target_address

__all__ = [
    "WINRM_OBSERVATION",
    "record_observation",
    "record_readiness",
    "confirm_host_key",
    "confirmed_key_for",
    "list_host_keys",
    "get_host_key",
]


#: What a `winrm` row holds in `observed_fingerprint` (design §2.4, W3). NOT A
#: KEY: WinRM has no host key, and Kerberos authenticates the target. The
#: marker exists so a row a winrm probe wrote can never be mistaken for an SSH
#: observation -- it disagrees with any SSH key a human confirmed there, and
#: `confirm_host_key` refuses to confirm it.
WINRM_OBSERVATION = "winrm"


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _client_of(
    session: AsyncSession, org_id: UUID, device_id: UUID | None
) -> UUID | None:
    """The client a host key of `device_id` belongs to; None (global) for none."""
    if device_id is None:
        return None
    client: UUID | None = (
        await session.execute(
            select(Device.client_id).where(Device.org_id == org_id, Device.id == device_id)
        )
    ).scalar_one_or_none()
    return client


async def _row_for(
    session: AsyncSession, org_id: UUID, address: str, client_id: UUID | None
) -> PushHostKey | None:
    """The row that serves `client_id` at `address`: the client's own, else
    the global one (for None, the global one only)."""
    whose = (
        PushHostKey.client_id.is_(None)
        if client_id is None
        else or_(PushHostKey.client_id == client_id, PushHostKey.client_id.is_(None))
    )
    return (
        await session.execute(
            select(PushHostKey)
            .where(PushHostKey.org_id == org_id, PushHostKey.address == address, whose)
            # The client's own first (`false` sorts before `true`).
            .order_by(PushHostKey.client_id.is_(None))
            .limit(1)
        )
    ).scalar_one_or_none()


async def _own_row(
    session: AsyncSession, org_id: UUID, address: str, device_id: UUID | None
) -> PushHostKey | None:
    """The row an observation of `device_id` at `address` writes: its client's
    own, or -- for no device -- the global one. Never the other namespace: a
    device whose client this session cannot read matches nothing here (the
    INSERT then meets the database's own refusal), never the global row."""
    whose: ColumnElement[bool]
    if device_id is None:
        whose = PushHostKey.client_id.is_(None)
    else:
        device_client = (
            select(Device.client_id)
            .where(Device.org_id == org_id, Device.id == device_id)
            .scalar_subquery()
        )
        whose = PushHostKey.client_id == device_client
    return (
        await session.execute(
            select(PushHostKey).where(
                PushHostKey.org_id == org_id, PushHostKey.address == address, whose
            )
        )
    ).scalar_one_or_none()


async def _audit_row(session: AsyncSession, row: PushHostKey) -> dict[str, str | None]:
    """The row an event is about: two clients' rows share the address that is
    the event's target. `client_id` is the trigger's, read back after the
    write (an UPDATE expires it). The event is filed under it too (plan
    2026-10-26 Task 12): NULL for a global row, which is every client's."""
    await session.refresh(row, ["client_id"])
    return {
        "host_key_id": str(row.id),
        "client_id": None if row.client_id is None else str(row.client_id),
    }


async def _row_in_scope(
    session: AsyncSession, org_id: UUID, address: str
) -> PushHostKey | None:
    """The row at `address` the caller means, from what its scope reads: the
    one client's row it reaches there, else the global row.

    The address alone cannot say whose host it is when the scope reaches TWO
    clients' rows at it -- refused, so the person says which (`X-Client-Id`),
    rather than confirming one machine's key for another.
    """
    rows = list(
        (
            await session.execute(
                select(PushHostKey).where(
                    PushHostKey.org_id == org_id, PushHostKey.address == address
                )
            )
        )
        .scalars()
        .all()
    )
    own = [row for row in rows if row.client_id is not None]
    if len(own) > 1:
        raise PushConflictError(
            tr(
                "{address} has a host key in more than one client; choose the client "
                "whose machine it is and try again",
                address=address,
            )
        )
    if own:
        return own[0]
    return rows[0] if rows else None


async def record_observation(
    session: AsyncSession,
    org_id: UUID,
    *,
    address: str,
    transport: str,
    fingerprint: str,
    key_type: str,
    device_id: UUID | None,
    job_id: UUID | None,
) -> PushHostKey:
    """Record (or update) what a probe saw for `address`.

    **A NEW OBSERVATION THAT DIFFERS FROM THE CONFIRMED KEY DOES NOT CLEAR THE
    CONFIRMATION -- IT INVALIDATES IT BY DISAGREEING.** The confirmed value stays
    on the row as the last thing a human vouched for; `confirmed_key_for` refuses
    to return it while the observation disagrees. Overwriting the confirmation
    here would erase the evidence of what was confirmed and when, which an
    auditor needs precisely when a key has changed.

    The row is the device's client's own, or -- for no device -- the global
    one (module docstring): a device's probe never touches a global row.
    """
    row = await _own_row(session, org_id, address, device_id)
    now = _utcnow()
    if row is None:
        row = PushHostKey(
            org_id=org_id,
            device_id=device_id,
            address=address,
            transport=transport,
            observed_fingerprint=fingerprint,
            observed_key_type=key_type,
            observed_at=now,
            observed_job_id=job_id,
        )
        session.add(row)
    else:
        row.observed_fingerprint = fingerprint
        row.observed_key_type = key_type
        row.observed_at = now
        row.observed_job_id = job_id
        row.transport = transport
        # An SSH observation supersedes any WinRM readiness recorded for this
        # address: the panel must not show "WinRM answers" for a host that now
        # answers SSH.
        row.readiness = None
        if device_id is not None:
            row.device_id = device_id
        session.add(row)
    await session.flush()
    about = await _audit_row(session, row)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="agent",
        actor_id=None,
        action="push.host_key_observed",
        target_type="push_target",
        target_id=address,
        client_id=row.client_id,
        # THE FINGERPRINT IS PUBLIC KEY MATERIAL, NOT A SECRET. It is exactly
        # what an operator must compare against the target out-of-band, so it
        # belongs in the record. No credential is anywhere near this path.
        metadata={
            "fingerprint": fingerprint,
            "key_type": key_type,
            "transport": transport,
            "job_id": None if job_id is None else str(job_id),
            **about,
        },
    )
    return row


async def confirm_host_key(
    session: AsyncSession,
    org_id: UUID,
    *,
    actor_id: UUID,
    address: str,
    fingerprint: str,
) -> PushHostKey:
    """A human confirms the observed fingerprint. HIGH-CONSEQUENCE.

    The confirmation is the trust anchor for every later push to this host, so
    the operator states the fingerprint they are confirming and it MUST equal
    what the last probe observed. A mismatch means the operator is confirming
    something other than what is currently there -- a stale screenshot, a
    man-in-the-middle -- and is refused.
    """
    parsed = str(parse_target_address(address))
    row = await _row_in_scope(session, org_id, parsed)
    if row is None:
        raise PushNotFoundError(
            tr(
                "no host key has been observed for {parsed}; run a probe first "
                "(agent.push_probe) so there is a fingerprint to confirm",
                parsed=parsed,
            )
        )
    if row.transport == "winrm":
        raise PushValidationError(
            tr(
                "{parsed} was last probed over WinRM, which has no host key to confirm: a "
                "Windows push authenticates the target with Kerberos (design §2.3). "
                "Nothing needs confirming before a WinRM push",
                parsed=parsed,
            )
        )
    if fingerprint != row.observed_fingerprint:
        raise PushValidationError(
            tr(
                "the fingerprint you are confirming ({fingerprint}) is not the one most "
                "recently observed for {parsed} ({row_observed_fingerprint}). Confirm the "
                "key that is actually there, or re-probe if it has changed",
                fingerprint=repr(fingerprint),
                parsed=parsed,
                row_observed_fingerprint=repr(row.observed_fingerprint),
            )
        )
    if row.confirmed_fingerprint == fingerprint:
        raise PushConflictError(
            tr(
                "{parsed}'s host key {fingerprint} is already confirmed",
                parsed=parsed,
                fingerprint=repr(fingerprint),
            )
        )
    row.confirmed_fingerprint = fingerprint
    row.confirmed_at = _utcnow()
    row.confirmed_by = actor_id
    session.add(row)
    await session.flush()
    about = await _audit_row(session, row)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="push.host_key_confirmed",
        target_type="push_target",
        target_id=parsed,
        client_id=row.client_id,
        metadata={
            "fingerprint": fingerprint,
            "key_type": row.observed_key_type,
            **about,
        },
    )
    return row


async def record_readiness(
    session: AsyncSession,
    org_id: UUID,
    *,
    address: str,
    device_id: UUID | None,
    job_id: UUID | None,
    readiness: dict[str, bool],
) -> PushHostKey:
    """Record a WinRM probe's readiness on the (org, address) row (design §2.4).

    No host key and no confirmation: Kerberos authenticates the target, so
    there is nothing for a human to confirm. This records whether 5986/5985
    answered and whether a KDC was reachable, for the readiness panel.

    **`observed_fingerprint` BECOMES THE `winrm` MARKER, ON PURPOSE.** If this
    address once had an SSH key confirmed, that confirmation no longer agrees
    with the latest observation, so `confirmed_key_for` answers
    `host_key_changed` and an SSH push there is refused until somebody
    re-probes and re-confirms -- the same fail-closed answer a changed key
    gets. The confirmed value stays on the row as evidence, exactly as
    `record_observation` keeps it.

    The row is the device's client's own, or -- for no device -- the global
    one, as in `record_observation`.
    """
    parsed = str(parse_target_address(address))
    row = await _own_row(session, org_id, parsed, device_id)
    now = _utcnow()
    if row is None:
        row = PushHostKey(
            org_id=org_id,
            device_id=device_id,
            address=parsed,
            transport="winrm",
            observed_fingerprint=WINRM_OBSERVATION,
            observed_key_type="",
            observed_at=now,
            observed_job_id=job_id,
            readiness=readiness,
        )
        session.add(row)
    else:
        row.transport = "winrm"
        row.observed_fingerprint = WINRM_OBSERVATION
        row.observed_key_type = ""
        row.observed_at = now
        row.observed_job_id = job_id
        row.readiness = readiness
        if device_id is not None:
            row.device_id = device_id
        session.add(row)
    await session.flush()
    about = await _audit_row(session, row)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="agent",
        actor_id=None,
        action="push.winrm_readiness_observed",
        target_type="push_target",
        target_id=parsed,
        client_id=row.client_id,
        # Booleans about what answered. No credential was presented to learn
        # them -- the probe carries none (design §2.4, §C1).
        metadata={
            "transport": "winrm",
            **readiness,
            "job_id": None if job_id is None else str(job_id),
            **about,
        },
    )
    return row


async def confirmed_key_for(
    session: AsyncSession, org_id: UUID, address: str, *, device_id: UUID | None
) -> tuple[str | None, str | None]:
    """`(confirmed_fingerprint, refusal_reason)` for a push to `device_id` at
    `address`: its client's own row, else the global one (module docstring).
    `device_id` has no default: None reads the global row only, and a caller
    that forgot the device must not do so silently.

    Returns the fingerprint only when a human confirmed it AND the latest probe
    still sees the same key. Otherwise the fingerprint is `None` and the reason
    is one of the spec's two host-key refusals (§7.3):

    * `host_key_unconfirmed` -- no confirmed key (never probed, or probed and
      not confirmed). Refused, not prompted.
    * `host_key_changed` -- confirmed, but the latest observation disagrees.
      Refused, not warned about; the operator must re-confirm deliberately.
    """
    row = await _row_for(session, org_id, address, await _client_of(session, org_id, device_id))
    if row is None or row.confirmed_fingerprint is None:
        return None, "host_key_unconfirmed"
    if row.confirmed_fingerprint != row.observed_fingerprint:
        return None, "host_key_changed"
    return row.confirmed_fingerprint, None


async def get_host_key(
    session: AsyncSession, org_id: UUID, address: str
) -> PushHostKey | None:
    """The row at `address` the caller's scope means (`_row_in_scope`)."""
    return await _row_in_scope(session, org_id, str(parse_target_address(address)))


async def list_host_keys(
    session: AsyncSession, org_id: UUID
) -> list[PushHostKey]:
    return list(
        (
            await session.execute(
                select(PushHostKey)
                .where(PushHostKey.org_id == org_id)
                .order_by(PushHostKey.address)
            )
        )
        .scalars()
        .all()
    )
