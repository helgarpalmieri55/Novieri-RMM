"""Change gating and change evidence for patch approvals and deployments
(docs/modules/change-mgmt/spec.md §11).

A `change_gates` row says patch approvals and/or deployments reaching a device
group (or every device) need a linked change in state `approved` or
`implementing`. The check runs when the action is requested and again when its
jobs are dispatched, because a change can be cancelled in between. A refusal
is audited (`change.gate_refused`) and carries a sentence an operator can act
on.

With no gate, an approval or deployment made without a change gets an `auto`
change record linked to it, so the evidence exists. System-initiated actions
(the PD-02 security auto-approval, the sweeps) have no person to record as the
requester and are not given one.

PER CLIENT (MSP clients plan 2026-10-26 Task 7, decision D4). Gates are
evaluated for one client at a time: the global ones (no client) and that
client's own. A client that does not use change management meets NO gate --
it could never create the change one would demand -- and gets no automatic
change. A deployment is gated as its own client (`deployments.client_id`, its
target's, written by trigger: Task 9); a ring's approval as every client its
machines belong to, each evaluated once. The change that authorizes a
deployment is the deployment's client's (`authorizing_change`).
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr, use_locale
from app.core.wire_words import say
from app.models import ChangeGate, ChangeLink, ChangeRequest, Device, DeviceGroup
from app.modules.change_mgmt.state import ChangeNotFoundError, ChangeValidationError
from app.modules.change_mgmt.words import CHANGE_STATE_WORDS
from app.services.client_modules import module_enabled, require_module

GateKind = Literal["patches", "deployments"]

# A change in one of these states authorizes the actions linked to it.
AUTHORIZING_STATES = ("approved", "implementing")


class ChangeGateRefused(Exception):
    """The action needs an approved change and has none."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Gates:
    """The gates in force for one kind of action."""

    everywhere: bool
    group_ids: frozenset[UUID]

    def applies_to_group(self, group_id: UUID | None) -> bool:
        return self.everywhere or (group_id is not None and group_id in self.group_ids)

    @property
    def any(self) -> bool:
        return self.everywhere or bool(self.group_ids)


NO_GATES = Gates(everywhere=False, group_ids=frozenset())


async def gates_for(
    session: AsyncSession, org_id: UUID, kind: GateKind, *, client_id: UUID
) -> Gates:
    """The gates `client_id` meets: the global ones and its own. A group's
    gate carries its group's client (migration 0231), so another client's
    group gates are not among them; `everywhere` is a group-less gate OF
    THESE -- a client's own group-less gate gates that client alone. None at
    all for a client without change management (D4)."""
    if not await module_enabled(session, org_id=org_id, client_id=client_id, module="change"):
        return NO_GATES
    column = (
        ChangeGate.applies_to_patches if kind == "patches" else ChangeGate.applies_to_deployments
    )
    rows = (
        await session.execute(
            select(ChangeGate.device_group_id).where(
                ChangeGate.org_id == org_id,
                column,
                or_(ChangeGate.client_id.is_(None), ChangeGate.client_id == client_id),
            )
        )
    ).scalars().all()
    return Gates(
        everywhere=any(r is None for r in rows),
        group_ids=frozenset(r for r in rows if r is not None),
    )


async def _group_names(session: AsyncSession, org_id: UUID, ids: Iterable[UUID]) -> list[str]:
    ids = list(ids)
    if not ids:
        return []
    return sorted(
        (
            await session.execute(
                select(DeviceGroup.name).where(
                    DeviceGroup.org_id == org_id, DeviceGroup.id.in_(ids)
                )
            )
        ).scalars()
    )


class GateRefusal(str):
    """Why a gate stops an action: the English sentence (what the audit chain stores,
    and what `str()` of this is) that can also say itself in the request's language.

    The gate is evaluated inside the request and its reason is written to the audit
    chain, which is never translated. `said()` builds the sentence again under the
    current locale for the person who is told no."""

    _build: Callable[[], str]

    def __new__(cls, build: Callable[[], str]) -> "GateRefusal":
        with use_locale("en"):
            refusal = super().__new__(cls, build())
        refusal._build = build
        return refusal

    def said(self) -> str:
        return self._build()

    # `str` hands its own text back to `__new__` on copy and pickle, which here
    # expects a builder; a copy of a refusal is its English sentence.
    def __copy__(self) -> "GateRefusal":
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> "GateRefusal":
        return self

    def __reduce__(self) -> tuple[type[str], tuple[str]]:
        return (str, (str(self),))


def gate_said(reason: str) -> str:
    """The refusal for the person: Spanish when they asked for it, else as stored."""
    return reason.said() if isinstance(reason, GateRefusal) else reason


def _where(everywhere: bool, names: Sequence[str]) -> str:
    if everywhere:
        return tr("every device")
    if len(names) == 1:
        return tr("the {name} group", name=names[0])
    return tr("the {names} and {last} groups", names=", ".join(names[:-1]), last=names[-1])


async def patch_approval_gate(
    session: AsyncSession, org_id: UUID, device_ids: Sequence[UUID], ring_name: str
) -> GateRefusal | None:
    """Why approving into `ring_name` needs a change, or None when it does not.
    `device_ids` is the ring's population, which may span clients: the
    approval is gated when ANY client it reaches meets a gate there. Gates are
    read once per client, never per machine."""
    groups_by_client: dict[UUID, set[UUID | None]] = {}
    for client_id, group_id in (
        await session.execute(
            select(Device.client_id, Device.device_group_id).where(
                Device.org_id == org_id, Device.id.in_(list(device_ids))
            )
        )
    ).all():
        groups_by_client.setdefault(client_id, set()).add(group_id)
    everywhere = False
    hit_groups: set[UUID] = set()
    for client_id, groups in groups_by_client.items():
        gates = await gates_for(session, org_id, "patches", client_id=client_id)
        everywhere = everywhere or gates.everywhere
        hit_groups |= {g for g in groups if g is not None and g in gates.group_ids}
    if not (everywhere or hit_groups):
        return None
    names = await _group_names(session, org_id, hit_groups)
    return GateRefusal(
        lambda: tr(
            "Approving updates for the {ring_name} ring reaches {where}, which needs an approved "
            "change. Choose an approved change to link, or have one approved first.",
            ring_name=ring_name,
            where=_where(everywhere, names),
        )
    )


async def deployment_gate(
    session: AsyncSession, org_id: UUID, target_type: str, target_id: UUID, *, client_id: UUID
) -> GateRefusal | None:
    """Why deploying to this target needs a change, or None when it does not.
    The gates are `client_id`'s: the deployment's own client, which its
    trigger writes from the target (migration 0239) -- at creation, the
    target's client the request has just read. A device's group is read for
    the group gates; a device that is gone is in no group and meets only the
    client's every-device gates."""
    if target_type == "group":
        group_id: UUID | None = target_id
    else:
        group_id = (
            await session.execute(
                select(Device.device_group_id).where(
                    Device.org_id == org_id, Device.id == target_id
                )
            )
        ).scalar_one_or_none()
    gates = await gates_for(session, org_id, "deployments", client_id=client_id)
    if not gates.applies_to_group(group_id):
        return None
    names = [] if gates.everywhere or group_id is None else await _group_names(
        session, org_id, [group_id]
    )
    everywhere = gates.everywhere
    return GateRefusal(
        lambda: tr(
            "Deployments to {where} need an approved change. Choose an approved change to link, "
            "or have one approved first.",
            where=_where(everywhere, names),
        )
    )


async def authorizing_change(
    session: AsyncSession, org_id: UUID, change_id: UUID, *, client_id: UUID | None
) -> ChangeRequest:
    """The change, if it exists in this org and authorizes work now.

    `client_id` is the client the work is for -- a deployment's -- and has
    NO default: every caller decides. A change of another client is not
    found, word for word: a narrowed session cannot see it, and a wider one
    is told nothing more. And that client must use change management, since
    linking the work is a write on its change (`require_module`, a 404).
    That check reads the client FOR SHARE and is the write's FIRST lock,
    before anything takes the audit chain's (`app.services.client_modules`):
    call this before writing anything.

    None asks neither: the patch approval path, until plan 2026-10-26 Task
    14 names the client its approvals are for (C8: a global approval's is the
    internal client's) -- passing it is all that path needs.
    """
    change = (
        await session.execute(
            select(ChangeRequest).where(
                ChangeRequest.org_id == org_id, ChangeRequest.id == change_id
            )
        )
    ).scalar_one_or_none()
    if change is None or (client_id is not None and change.client_id != client_id):
        raise ChangeNotFoundError(tr("no such change"))
    if client_id is not None:
        await require_module(session, org_id=org_id, client_id=client_id, module="change")
    if change.state not in AUTHORIZING_STATES:
        raise ChangeValidationError(
            tr(
                "that change is {change_state}; only an approved change, or one being "
                "implemented, can authorize this",
                change_state=say(CHANGE_STATE_WORDS, change.state, marked=True),
            )
        )
    return change


async def has_authorizing_change(
    session: AsyncSession, org_id: UUID, action_type: str, action_id: UUID
) -> bool:
    return (
        await session.execute(
            select(ChangeLink.id)
            .join(ChangeRequest, ChangeRequest.id == ChangeLink.change_id)
            .where(
                ChangeLink.org_id == org_id,
                ChangeRequest.org_id == org_id,
                ChangeLink.action_type == action_type,
                ChangeLink.action_id == action_id,
                ChangeRequest.state.in_(AUTHORIZING_STATES),
            )
            .limit(1)
        )
    ).first() is not None


async def link_actions(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    change: ChangeRequest,
    action_type: str,
    action_ids: Sequence[UUID],
) -> None:
    """Link each action to the change once, audited `change.linked`. The
    caller vouches that the actions are the change's client's and that it
    uses change management: `authorizing_change` with their client, or
    `record_auto_change`, which made the change for it."""
    existing = set(
        (
            await session.execute(
                select(ChangeLink.action_id).where(
                    ChangeLink.org_id == org_id,
                    ChangeLink.change_id == change.id,
                    ChangeLink.action_type == action_type,
                )
            )
        ).scalars()
    )
    for action_id in dict.fromkeys(action_ids):
        if action_id in existing:
            continue
        session.add(
            ChangeLink(
                org_id=org_id,
                change_id=change.id,
                action_type=action_type,
                action_id=action_id,
                linked_by=actor_id,
            )
        )
        await session.flush()
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=actor_id,
            action="change.linked",
            target_type="change",
            target_id=str(change.id),
            client_id=change.client_id,
            metadata={"action_type": action_type, "action_id": str(action_id)},
        )


async def record_auto_change(
    session: AsyncSession,
    *,
    org_id: UUID,
    client_id: UUID,
    actor_id: UUID,
    title: str,
    description: str,
    affected: list[dict[str, Any]],
    action_type: str,
    action_ids: Sequence[UUID],
) -> ChangeRequest | None:
    """The evidence record for an action taken with no change and no gate:
    a completed standard change of `client_id`, marked `auto`, linked to what
    was done. It carries no approval because none was given. A client that
    does not use change management gets none (D4): nothing is written and
    the answer is None."""
    if not await module_enabled(session, org_id=org_id, client_id=client_id, module="change"):
        return None
    change = ChangeRequest(
        org_id=org_id,
        client_id=client_id,
        type="standard",
        title=title[:255],
        description=description,
        risk="low",
        affected=affected,
        state="completed",
        created_by=actor_id,
        origin="auto",
    )
    session.add(change)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="change.auto_recorded",
        target_type="change",
        target_id=str(change.id),
        client_id=change.client_id,
        metadata={"action_type": action_type, "action_count": len(action_ids)},
    )
    await link_actions(
        session,
        org_id=org_id,
        actor_id=actor_id,
        change=change,
        action_type=action_type,
        action_ids=action_ids,
    )
    return change


async def record_gate_refusal(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID | None,
    target_type: str,
    target_id: str,
    reason: str,
    metadata: dict[str, Any] | None = None,
    client_id: UUID | None = None,
) -> None:
    """`client_id` is the refused action's client where it has one -- a
    deployment's (plan 2026-10-26 Task 9); None for a ring's approval, which
    can reach many."""
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user" if actor_id is not None else "system",
        actor_id=actor_id,
        action="change.gate_refused",
        target_type=target_type,
        target_id=target_id,
        metadata={"reason": str(reason), **(metadata or {})},
        client_id=client_id,
    )


# --------------------------------------------------------------------------- #
# Managing gates
# --------------------------------------------------------------------------- #


async def list_gates(session: AsyncSession, org_id: UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(ChangeGate, DeviceGroup.name)
            .outerjoin(DeviceGroup, DeviceGroup.id == ChangeGate.device_group_id)
            .where(ChangeGate.org_id == org_id)
            .order_by(ChangeGate.created_at)
        )
    ).all()
    return [
        {
            "id": gate.id,
            "device_group_id": gate.device_group_id,
            "device_group_name": name,
            "applies_to_patches": gate.applies_to_patches,
            "applies_to_deployments": gate.applies_to_deployments,
            "created_at": gate.created_at,
        }
        for gate, name in rows
    ]


async def set_gate(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    device_group_id: UUID | None,
    applies_to_patches: bool,
    applies_to_deployments: bool,
) -> ChangeGate:
    """Create or replace the gate for one group (or every device). A group's
    gate is its group's client's, so that client must use change management
    (`ModuleNotEnabled`, 404) -- a gate it would meet but never satisfy is
    the D4 trap. A group-less gate here is the GLOBAL one: every client that
    uses the module meets it."""
    if device_group_id is not None:
        group_client = (
            await session.execute(
                select(DeviceGroup.client_id).where(
                    DeviceGroup.org_id == org_id, DeviceGroup.id == device_group_id
                )
            )
        ).scalar_one_or_none()
        if group_client is None:
            raise ChangeValidationError(tr("no such device group"))
        await require_module(session, org_id=org_id, client_id=group_client, module="change")
    if not (applies_to_patches or applies_to_deployments):
        raise ChangeValidationError(tr("a gate must cover patch approvals, deployments, or both"))
    query = select(ChangeGate).where(ChangeGate.org_id == org_id)
    # A group's gate is the group's client's; a group-less one is the GLOBAL
    # "every device" gate (phase 1 writes no client), and a client's
    # group-less gate is another row (`uq_change_gates_client_group`,
    # migration 0231, Task 8b).
    query = query.where(
        and_(ChangeGate.device_group_id.is_(None), ChangeGate.client_id.is_(None))
        if device_group_id is None
        else ChangeGate.device_group_id == device_group_id
    )
    gate = (await session.execute(query)).scalar_one_or_none()
    if gate is None:
        gate = ChangeGate(org_id=org_id, device_group_id=device_group_id, created_by=actor_id)
        session.add(gate)
    gate.applies_to_patches = applies_to_patches
    gate.applies_to_deployments = applies_to_deployments
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="change.gate_set",
        target_type="change_gate",
        target_id=str(gate.id),
        metadata={
            "device_group_id": str(device_group_id) if device_group_id else None,
            "applies_to_patches": applies_to_patches,
            "applies_to_deployments": applies_to_deployments,
        },
    )
    return gate


async def remove_gate(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, gate_id: UUID
) -> None:
    gate = (
        await session.execute(
            select(ChangeGate).where(ChangeGate.org_id == org_id, ChangeGate.id == gate_id)
        )
    ).scalar_one_or_none()
    if gate is None:
        raise ChangeNotFoundError(tr("no such gate"))
    await session.delete(gate)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="change.gate_removed",
        target_type="change_gate",
        target_id=str(gate_id),
        metadata={"device_group_id": str(gate.device_group_id) if gate.device_group_id else None},
    )
