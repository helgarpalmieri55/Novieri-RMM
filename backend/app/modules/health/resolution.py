"""Effective-profile resolution
(`docs/modules/endpoint-health-alerting/spec.md` §3, §0.2, §0.6, §0.7).

EXACTLY ONE PROFILE IS EVER EFFECTIVE FOR A DEVICE. First match wins, most
specific first: device -> nearest assigned group ancestor -> the default of the
DEVICE'S CLIENT -> the global (org-wide) default. §0.2
rejects the "union of every matching profile" alternative because union has no
way to express REMOVAL -- a check an operator silenced for one troublesome
device is resurrected by the org-wide profile, and the only fix is an
anti-check, i.e. a second config concept.

IF NOTHING MATCHES, THE ANSWER IS `None`. Not an empty profile, not a
fabricated default. The console shows "no health profile", never "healthy" --
"healthy" is the single most dangerous default a monitoring module could pick,
and a fabricated one is worse than a visible absence.

THE GROUP WALK IS DEPTH-BOUNDED AT 16 AND THAT BOUND IS A SAFETY PROPERTY.
`device_groups.parent_group_id` has NO cycle constraint in the kernel schema, so
`A.parent = B, B.parent = A` is representable today, and an unbounded walk over
it is a hung worker inside a gateway message handler. Exceeding the bound
degrades to "no group match" -- falling through to the org default -- and logs
at ERROR with the device id, the same degrade-don't-fail posture
`docs/modules/sla/spec.md` §3.1 takes for a broken calendar. The bound is NOT a
cycle detector: a `visited` set would catch the cycle but not a legitimately
1000-deep hierarchy, and the cost of the walk is what is actually being bounded.

TWO DEFAULTS SINCE MIGRATION 0231: one global (`client_id IS NULL`) and one
per client (`uq_health_assignment_client_org`, NULLS NOT DISTINCT). A client's
default serves that client's machines only, and a machine of a client with
none -- or whose default points at an inactive profile -- falls to the global
one (MSP clients plan 2026-10-26 Task 14). The client predicate is EXPLICIT,
because the health sweep resolves under scope "all", where RLS would hand a
machine of Y the default of X. Both defaults are read in ONE statement, the
device's client's first.

AN INACTIVE PROFILE IS SKIPPED, NOT TREATED AS A MATCH. A device-level
assignment pointing at a deactivated profile means "that rule is out of
service", so resolution continues to the group and then the org default.
Reading it as a match would make deactivating a profile silently disarm every
device pinned to it, which is the opposite of what deactivation is for.

TWO OS FILTERS, AND BOTH ARE REQUIRED. §3 states the predicate as
`check.os_family IS NULL OR check.os_family = device.os_family`, and gives its
purpose as "an agent is never shipped a check it cannot answer -- that is one
less `unavailable` state to explain and one less shell-out to attempt". The
column predicate alone does not achieve that purpose: `posture.antivirus_healthy`
with `os_family = NULL` means "any OS" and passes it on Linux, where §4.1 says
the check cannot be answered. The registry's per-OS availability is therefore
applied as well. Recorded rather than assumed -- see the build report.
"""

import logging
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceGroup
from app.modules.health.models import HealthCheck, HealthProfile, HealthProfileAssignment
from app.modules.health.registry import CHECK_TYPES

__all__ = ["MAX_GROUP_DEPTH", "ResolvedProfile", "resolve_profile_for_device"]

logger = logging.getLogger("its.health.resolution")

# §0.2. Sixteen levels of device grouping is already far past any real
# hierarchy; the number exists to bound a CYCLE, and its falsifier is that
# deleting it makes the cycle test hang rather than fail.
MAX_GROUP_DEPTH = 16

ResolutionLevel = Literal["device", "group", "org"]


@dataclass(frozen=True)
class ResolvedProfile:
    """One device's effective policy, and the assignment that produced it.

    `assignment` is carried deliberately: §0.2 requires the device view to show
    WHICH assignment produced the profile, because "a resolution nobody can see
    is a resolution nobody trusts". A caller that only needed the profile could
    have been given the profile.

    TWO CHECK SETS, NOT ONE.
      - `checks` is everything that applies to this device, INCLUDING
        `agent.offline`. It is what the console renders and what §12's offline
        sweep reads.
      - `agent_checks` is the PUSH set: `checks` minus every server-evaluated
        type. An agent cannot report that it is offline (§0.6), so shipping it
        the check would be asking a question only silence can answer.
    A single list would force every consumer to re-apply the exclusion, and
    three copies of an exclusion is three chances to ship `agent.offline` to an
    endpoint.
    """

    profile: HealthProfile
    assignment: HealthProfileAssignment
    checks: tuple[HealthCheck, ...]
    agent_checks: tuple[HealthCheck, ...]

    @property
    def level(self) -> ResolutionLevel:
        if self.assignment.device_id is not None:
            return "device"
        if self.assignment.device_group_id is not None:
            return "group"
        return "org"


def _check_applies(check: HealthCheck, device: Device) -> bool:
    """§3's predicate, plus the registry's per-OS availability.

    `retired_at IS NULL` is one of the three places §2.4 names as having to
    carry the soft-delete predicate; `enabled` is the operator's own switch; the
    two OS tests are the module docstring's "TWO OS FILTERS".
    """
    if check.retired_at is not None or not check.enabled:
        return False
    if check.os_family is not None and check.os_family != device.os_family:
        return False
    entry = CHECK_TYPES.get(check.check_type)
    if entry is None:
        # A check type the registry no longer knows. Refusing to push it is the
        # safe direction: the agent has no handler for it either, and the row is
        # visible in the console for whoever has to clean it up.
        logger.error(
            "health: check %s on device %s names unregistered type %s; excluded from "
            "the effective set",
            check.id,
            device.id,
            check.check_type,
        )
        return False
    return device.os_family in entry.os_families


async def _assignment_for_device(
    session: AsyncSession, org_id: UUID, device_id: UUID
) -> HealthProfileAssignment | None:
    return (
        await session.execute(
            select(HealthProfileAssignment).where(
                HealthProfileAssignment.org_id == org_id,
                HealthProfileAssignment.device_id == device_id,
            )
        )
    ).scalar_one_or_none()


async def _assignment_for_group(
    session: AsyncSession, org_id: UUID, group_id: UUID
) -> HealthProfileAssignment | None:
    return (
        await session.execute(
            select(HealthProfileAssignment).where(
                HealthProfileAssignment.org_id == org_id,
                HealthProfileAssignment.device_group_id == group_id,
            )
        )
    ).scalar_one_or_none()


async def _default_assignments(
    session: AsyncSession, org_id: UUID, client_id: UUID
) -> list[HealthProfileAssignment]:
    """The defaults that may serve a machine of `client_id`, most specific
    first: its client's own, then the global one -- at most one of each
    (`uq_health_assignment_client_org`), in one statement."""
    return list(
        (
            await session.execute(
                select(HealthProfileAssignment)
                .where(
                    HealthProfileAssignment.org_id == org_id,
                    HealthProfileAssignment.device_id.is_(None),
                    HealthProfileAssignment.device_group_id.is_(None),
                    or_(
                        HealthProfileAssignment.client_id == client_id,
                        HealthProfileAssignment.client_id.is_(None),
                    ),
                )
                # The client's own first (`false` sorts before `true`).
                .order_by(HealthProfileAssignment.client_id.is_(None))
            )
        )
        .scalars()
        .all()
    )


async def _parent_group_id(
    session: AsyncSession, org_id: UUID, group_id: UUID
) -> UUID | None:
    """The group's parent, org-scoped. A group id that does not resolve in this
    org ends the walk rather than raising: a device pointing at a deleted group
    is a race with a group delete, and a gateway message handler is not the
    place to turn that into a 500."""
    row = (
        await session.execute(
            select(DeviceGroup.parent_group_id).where(
                DeviceGroup.org_id == org_id, DeviceGroup.id == group_id
            )
        )
    ).one_or_none()
    return None if row is None else row[0]


async def _resolve_assignment(
    session: AsyncSession,
    org_id: UUID,
    device: Device,
    assignment: HealthProfileAssignment | None,
) -> ResolvedProfile | None:
    """Turn a candidate assignment into a `ResolvedProfile`, or `None` when it
    does not qualify -- in which case the caller CONTINUES to the next level."""
    if assignment is None:
        return None
    profile = (
        await session.execute(
            select(HealthProfile).where(
                HealthProfile.org_id == org_id, HealthProfile.id == assignment.profile_id
            )
        )
    ).scalar_one_or_none()
    if profile is None or not profile.active:
        return None

    all_checks = list(
        (
            await session.execute(
                select(HealthCheck)
                .where(
                    HealthCheck.org_id == org_id,
                    HealthCheck.profile_id == profile.id,
                    HealthCheck.retired_at.is_(None),
                )
                .order_by(HealthCheck.check_type, HealthCheck.selector)
            )
        )
        .scalars()
        .all()
    )
    applicable = tuple(c for c in all_checks if _check_applies(c, device))
    pushable = tuple(
        c for c in applicable if CHECK_TYPES[c.check_type].evaluated_by == "agent"
    )
    return ResolvedProfile(
        profile=profile, assignment=assignment, checks=applicable, agent_checks=pushable
    )


async def resolve_profile_for_device(
    session: AsyncSession, org_id: UUID, device: Device
) -> ResolvedProfile | None:
    """This device's effective profile, or `None` (§3).

    Called when a device enrols, when a profile/check/assignment is written,
    when a device's `device_group_id` changes, and on the reconciliation sweep.
    It is a pure read: it queues nothing and writes nothing, because a resolver
    that also dispatched could not be used by the console.
    """
    if device.org_id != org_id:
        # The caller passed a device row from another tenant. Returning nothing
        # rather than resolving it: `org_id` is the authority here, and a
        # mismatched pair is a bug that must not become a cross-tenant policy.
        logger.error(
            "health: device %s belongs to org %s, not %s; refusing to resolve",
            device.id,
            device.org_id,
            org_id,
        )
        return None

    resolved = await _resolve_assignment(
        session, org_id, device, await _assignment_for_device(session, org_id, device.id)
    )
    if resolved is not None:
        return resolved

    group_id = device.device_group_id
    depth = 0
    while group_id is not None and depth < MAX_GROUP_DEPTH:
        resolved = await _resolve_assignment(
            session, org_id, device, await _assignment_for_group(session, org_id, group_id)
        )
        if resolved is not None:
            return resolved
        group_id = await _parent_group_id(session, org_id, group_id)
        depth += 1

    if group_id is not None:
        # The walk was TRUNCATED, not finished. Loud, because the cause is
        # either a cycle in `device_groups.parent_group_id` (which the kernel
        # schema permits) or a hierarchy nobody meant to build, and both leave
        # this device on a less specific profile than its operator intended.
        logger.error(
            "health: group walk for device %s exceeded MAX_GROUP_DEPTH=%d at group %s; "
            "falling through to the org default. This usually means a cycle in "
            "device_groups.parent_group_id.",
            device.id,
            MAX_GROUP_DEPTH,
            group_id,
        )

    for default in await _default_assignments(session, org_id, device.client_id):
        resolved = await _resolve_assignment(session, org_id, device, default)
        if resolved is not None:
            return resolved
    return None
