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
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr, use_locale
from app.core.wire_words import say
from app.models import ChangeGate, ChangeLink, ChangeRequest, Device, DeviceGroup
from app.modules.change_mgmt.state import ChangeNotFoundError, ChangeValidationError
from app.modules.change_mgmt.words import CHANGE_STATE_WORDS

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


async def gates_for(session: AsyncSession, org_id: UUID, kind: GateKind) -> Gates:
    column = (
        ChangeGate.applies_to_patches if kind == "patches" else ChangeGate.applies_to_deployments
    )
    rows = (
        await session.execute(
            select(ChangeGate.device_group_id).where(ChangeGate.org_id == org_id, column)
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
    `device_ids` is the ring's population."""
    gates = await gates_for(session, org_id, "patches")
    if not gates.any:
        return None
    hit_groups: set[UUID] = set()
    if gates.everywhere:
        hit = True
    else:
        groups = set(
            (
                await session.execute(
                    select(Device.device_group_id).where(
                        Device.org_id == org_id, Device.id.in_(list(device_ids))
                    )
                )
            ).scalars()
        )
        hit_groups = {g for g in groups if g is not None and g in gates.group_ids}
        hit = bool(hit_groups)
    if not hit:
        return None
    everywhere = gates.everywhere
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
    session: AsyncSession, org_id: UUID, target_type: str, target_id: UUID
) -> GateRefusal | None:
    """Why deploying to this target needs a change, or None when it does not."""
    gates = await gates_for(session, org_id, "deployments")
    if not gates.any:
        return None
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


async def authorizing_change(session: AsyncSession, org_id: UUID, change_id: UUID) -> ChangeRequest:
    """The change, if it exists in this org and authorizes work now."""
    change = (
        await session.execute(
            select(ChangeRequest).where(
                ChangeRequest.org_id == org_id, ChangeRequest.id == change_id
            )
        )
    ).scalar_one_or_none()
    if change is None:
        raise ChangeNotFoundError(tr("no such change"))
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
    """Link each action to the change once, audited `change.linked`."""
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
            metadata={"action_type": action_type, "action_id": str(action_id)},
        )


async def record_auto_change(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    title: str,
    description: str,
    affected: list[dict[str, Any]],
    action_type: str,
    action_ids: Sequence[UUID],
) -> ChangeRequest:
    """The evidence record for an action taken with no change and no gate:
    a completed standard change, marked `auto`, linked to what was done. It
    carries no approval because none was given."""
    change = ChangeRequest(
        org_id=org_id,
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
) -> None:
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user" if actor_id is not None else "system",
        actor_id=actor_id,
        action="change.gate_refused",
        target_type=target_type,
        target_id=target_id,
        metadata={"reason": str(reason), **(metadata or {})},
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
    """Create or replace the gate for one group (or every device)."""
    if device_group_id is not None:
        group = (
            await session.execute(
                select(DeviceGroup.id).where(
                    DeviceGroup.org_id == org_id, DeviceGroup.id == device_group_id
                )
            )
        ).scalar_one_or_none()
        if group is None:
            raise ChangeValidationError(tr("no such device group"))
    if not (applies_to_patches or applies_to_deployments):
        raise ChangeValidationError(tr("a gate must cover patch approvals, deployments, or both"))
    query = select(ChangeGate).where(ChangeGate.org_id == org_id)
    query = query.where(
        ChangeGate.device_group_id.is_(None)
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
