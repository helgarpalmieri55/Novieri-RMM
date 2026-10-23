"""Effective-profile resolution
(docs/modules/endpoint-health-alerting/spec.md §3, §0.2, §0.6, §0.7).

READ THIS BEFORE ADDING A TEST HERE.

1. PRECEDENCE NEEDS ALL THREE LEVELS PRESENT AT ONCE. A test with a single
   candidate assignment cannot detect a reversed resolution order -- it returns
   the only profile there is either way. `test_precedence_is_device_then_group_
   then_org` therefore seeds all three and removes them one at a time.

2. THE CYCLE TEST IS GUARDED BY A REAL TIMEOUT, NOT BY AN ASSERTION. If
   `MAX_GROUP_DEPTH` is deleted, an unbounded walk over `A.parent = B,
   B.parent = A` does not fail -- it HANGS, and a hung test is a test that never
   reports. `@pytest.mark.timeout` is what turns that into a red run, and the
   ERROR log assertion is what proves the degrade path was taken deliberately
   rather than by accident.

3. OS FILTERING IS ASSERTED THROUGH THE REGISTRY, NOT ONLY THROUGH THE COLUMN.
   A `posture.antivirus_healthy` check with `os_family = NULL` passes §3's
   literal predicate on every OS; only the registry knows Linux cannot answer
   it. Both filters are tested, because the column one alone leaves §3's stated
   purpose ("an agent is never shipped a check it cannot answer") unmet for the
   most common way to write the check.
"""

import asyncio
import logging
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Device, DeviceGroup
from app.modules.health.resolution import (
    MAX_GROUP_DEPTH,
    ResolvedProfile,
    resolve_profile_for_device,
)
from tests.health_fixtures import (
    seed_assignment,
    seed_check,
    seed_device,
    seed_group,
    seed_org,
    seed_profile,
)

CPU = {
    "check_type": "cpu.busy_pct",
    "comparator": "gt",
    "threshold_num": 95,
    "severity": "warning",
}


async def _device(api_env, device_id) -> Device:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = await session.get(Device, device_id)
        assert device is not None
        return device


async def _resolve(api_env, org_id, device: Device) -> ResolvedProfile | None:
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        return await resolve_profile_for_device(session, org_id, device)


async def _set_parent(api_env, group_id, parent_group_id) -> None:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        group = await session.get(DeviceGroup, group_id)
        assert group is not None
        group.parent_group_id = parent_group_id
        await session.commit()


# ---------------------------------------------------------------------------
# The three levels
# ---------------------------------------------------------------------------


async def test_a_device_level_assignment_resolves(api_env) -> None:
    org = await seed_org(api_env, "res-d")
    profile_id = await seed_profile(api_env, org.org_id, name="Device profile")
    assignment_id = await seed_assignment(
        api_env, org.org_id, profile_id, device_id=org.device_id
    )
    resolved = await _resolve(api_env, org.org_id, await _device(api_env, org.device_id))
    assert resolved is not None
    assert resolved.profile.id == profile_id
    assert resolved.assignment.id == assignment_id
    assert resolved.level == "device"


async def test_a_group_level_assignment_resolves(api_env) -> None:
    org = await seed_org(api_env, "res-g")
    profile_id = await seed_profile(api_env, org.org_id)
    await seed_assignment(api_env, org.org_id, profile_id, device_group_id=org.group_id)
    resolved = await _resolve(api_env, org.org_id, await _device(api_env, org.device_id))
    assert resolved is not None
    assert (resolved.profile.id, resolved.level) == (profile_id, "group")


async def test_the_org_default_resolves(api_env) -> None:
    org = await seed_org(api_env, "res-o")
    profile_id = await seed_profile(api_env, org.org_id)
    await seed_assignment(api_env, org.org_id, profile_id)
    resolved = await _resolve(api_env, org.org_id, await _device(api_env, org.device_id))
    assert resolved is not None
    assert (resolved.profile.id, resolved.level) == (profile_id, "org")


async def test_precedence_is_device_then_group_then_org(api_env) -> None:
    """§3's order, with all three candidates present AT ONCE. A test with one
    candidate returns the only profile there is whichever way the order runs;
    this one detects a reversal, because the answer changes three times as the
    more specific rows are removed."""
    org = await seed_org(api_env, "res-prec")
    device_profile = await seed_profile(api_env, org.org_id, name="device")
    group_profile = await seed_profile(api_env, org.org_id, name="group")
    org_profile = await seed_profile(api_env, org.org_id, name="org")

    device_assignment = await seed_assignment(
        api_env, org.org_id, device_profile, device_id=org.device_id
    )
    group_assignment = await seed_assignment(
        api_env, org.org_id, group_profile, device_group_id=org.group_id
    )
    await seed_assignment(api_env, org.org_id, org_profile)

    device = await _device(api_env, org.device_id)
    first = await _resolve(api_env, org.org_id, device)
    assert first is not None and first.profile.name == "device"

    await _delete_assignment_row(api_env, device_assignment)
    second = await _resolve(api_env, org.org_id, device)
    assert second is not None and second.profile.name == "group"

    await _delete_assignment_row(api_env, group_assignment)
    third = await _resolve(api_env, org.org_id, device)
    assert third is not None and third.profile.name == "org"


async def _delete_assignment_row(api_env, assignment_id) -> None:
    from sqlalchemy import text as sqltext

    async with api_env.admin_engine.begin() as conn:
        await conn.execute(
            sqltext("DELETE FROM health_profile_assignment WHERE id = :a"), {"a": assignment_id}
        )


# ---------------------------------------------------------------------------
# The group walk
# ---------------------------------------------------------------------------


async def test_the_nearest_assigned_ancestor_wins(api_env) -> None:
    """Device in C; C's parent B has an assignment; B's parent A also has one.
    B's wins. A walk that collected every ancestor and took the last would
    return A's, and a walk that took the first ANCESTOR rather than the nearest
    would too."""
    org = await seed_org(api_env, "res-anc")
    group_a = await seed_group(api_env, org.org_id, name="A")
    group_b = await seed_group(api_env, org.org_id, name="B")
    group_c = await seed_group(api_env, org.org_id, name="C")
    await _set_parent(api_env, group_c, group_b)
    await _set_parent(api_env, group_b, group_a)
    device_id = await seed_device(api_env, org.org_id, device_group_id=group_c)

    profile_a = await seed_profile(api_env, org.org_id, name="from A")
    profile_b = await seed_profile(api_env, org.org_id, name="from B")
    await seed_assignment(api_env, org.org_id, profile_a, device_group_id=group_a)
    await seed_assignment(api_env, org.org_id, profile_b, device_group_id=group_b)

    resolved = await _resolve(api_env, org.org_id, await _device(api_env, device_id))
    assert resolved is not None and resolved.profile.name == "from B"


async def test_the_devices_own_group_is_considered_first(api_env) -> None:
    """"self first": the device's OWN group is level 2, not its parent."""
    org = await seed_org(api_env, "res-self")
    parent = await seed_group(api_env, org.org_id, name="parent")
    own = await seed_group(api_env, org.org_id, name="own")
    await _set_parent(api_env, own, parent)
    device_id = await seed_device(api_env, org.org_id, device_group_id=own)

    own_profile = await seed_profile(api_env, org.org_id, name="own")
    parent_profile = await seed_profile(api_env, org.org_id, name="parent")
    await seed_assignment(api_env, org.org_id, own_profile, device_group_id=own)
    await seed_assignment(api_env, org.org_id, parent_profile, device_group_id=parent)

    resolved = await _resolve(api_env, org.org_id, await _device(api_env, device_id))
    assert resolved is not None and resolved.profile.name == "own"


async def test_a_deep_but_legal_hierarchy_still_resolves(api_env) -> None:
    """The positive control for the depth bound: a chain exactly
    `MAX_GROUP_DEPTH` long must still find its assignment, or the bound is a
    silent functional limit rather than a cycle guard."""
    org = await seed_org(api_env, "res-deep")
    groups = [await seed_group(api_env, org.org_id, name=f"g{i}") for i in range(MAX_GROUP_DEPTH)]
    for child, parent in zip(groups, groups[1:], strict=False):
        await _set_parent(api_env, child, parent)
    device_id = await seed_device(api_env, org.org_id, device_group_id=groups[0])

    profile_id = await seed_profile(api_env, org.org_id, name="top")
    await seed_assignment(api_env, org.org_id, profile_id, device_group_id=groups[-1])

    resolved = await _resolve(api_env, org.org_id, await _device(api_env, device_id))
    assert resolved is not None and resolved.profile.name == "top"


@pytest.mark.timeout(60)
async def test_a_group_cycle_degrades_to_the_org_default_and_logs(api_env, caplog) -> None:
    """§0.2's degrade-don't-fail posture. `device_groups.parent_group_id` has NO
    cycle constraint in the kernel schema, so `A.parent = B, B.parent = A` is
    representable and an unbounded walk over it is a hung worker.

    THE FALSIFIER FOR `MAX_GROUP_DEPTH` IS THE TIMEOUT, NOT AN ASSERTION.
    Deleting the bound does not make this test fail -- it makes it never return.
    `@pytest.mark.timeout(60)` is what converts a hang into a red run, and the
    `asyncio.wait_for` below bounds it a second time from inside the event loop
    so the failure names this call rather than the whole session.

    Not a hang, not an exception, and NOT `None`: the device falls through to
    the org default, which is what "degrade" means here.
    """
    org = await seed_org(api_env, "res-cycle")
    group_a = await seed_group(api_env, org.org_id, name="cycle-A")
    group_b = await seed_group(api_env, org.org_id, name="cycle-B")
    await _set_parent(api_env, group_a, group_b)
    await _set_parent(api_env, group_b, group_a)
    device_id = await seed_device(api_env, org.org_id, device_group_id=group_a)

    default_profile = await seed_profile(api_env, org.org_id, name="org default")
    await seed_assignment(api_env, org.org_id, default_profile)

    device = await _device(api_env, device_id)
    with caplog.at_level(logging.ERROR, logger="its.health.resolution"):
        resolved = await asyncio.wait_for(_resolve(api_env, org.org_id, device), timeout=30)

    assert resolved is not None
    assert resolved.profile.name == "org default"
    assert resolved.level == "org"

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a truncated group walk must be visible, not silent"
    assert str(device_id) in errors[0].getMessage()


async def test_a_device_with_no_group_skips_the_walk(api_env) -> None:
    org = await seed_org(api_env, "res-nogrp")
    device_id = await seed_device(api_env, org.org_id, device_group_id=None)
    profile_id = await seed_profile(api_env, org.org_id)
    await seed_assignment(api_env, org.org_id, profile_id)
    resolved = await _resolve(api_env, org.org_id, await _device(api_env, device_id))
    assert resolved is not None and resolved.level == "org"


# ---------------------------------------------------------------------------
# No match, and inactive profiles
# ---------------------------------------------------------------------------


async def test_no_assignment_at_any_level_returns_none(api_env) -> None:
    """§3: "If nothing matches, the device has no health profile" -- no checks
    pushed, no job dispatched, and the console shows "no health profile", not
    "healthy". A fabricated default is worse than a visible absence, and
    "healthy" is the single most dangerous default a monitoring module could
    pick."""
    org = await seed_org(api_env, "res-none")
    await seed_profile(api_env, org.org_id)  # exists, but is assigned to nothing
    assert await _resolve(api_env, org.org_id, await _device(api_env, org.device_id)) is None


async def test_an_inactive_profile_is_skipped_and_the_walk_continues(api_env) -> None:
    """A device-level assignment pointing at a DEACTIVATED profile does not mean
    "no profile": it means that rule is out of service, so resolution falls
    through. Asserting `None` here would be the reading that makes deactivating
    a profile silently disarm every device pinned to it."""
    org = await seed_org(api_env, "res-inact")
    inactive = await seed_profile(api_env, org.org_id, name="retired", active=False)
    default_profile = await seed_profile(api_env, org.org_id, name="org default")
    await seed_assignment(api_env, org.org_id, inactive, device_id=org.device_id)
    await seed_assignment(api_env, org.org_id, default_profile)

    resolved = await _resolve(api_env, org.org_id, await _device(api_env, org.device_id))
    assert resolved is not None
    assert resolved.profile.name == "org default"


async def test_every_candidate_inactive_returns_none(api_env) -> None:
    org = await seed_org(api_env, "res-allinact")
    inactive = await seed_profile(api_env, org.org_id, name="retired", active=False)
    await seed_assignment(api_env, org.org_id, inactive, device_id=org.device_id)
    await seed_assignment(api_env, org.org_id, inactive)
    assert await _resolve(api_env, org.org_id, await _device(api_env, org.device_id)) is None


# ---------------------------------------------------------------------------
# Which checks apply (§3, §0.6, §0.7)
# ---------------------------------------------------------------------------


async def _profile_with_checks(api_env, org, *specs) -> tuple:
    profile_id = await seed_profile(api_env, org.org_id)
    for spec in specs:
        await seed_check(api_env, org.org_id, profile_id, **spec)
    await seed_assignment(api_env, org.org_id, profile_id, device_id=org.device_id)
    return profile_id


async def test_a_disabled_check_is_not_in_the_resolved_set(api_env) -> None:
    org = await seed_org(api_env, "res-dis")
    await _profile_with_checks(
        api_env,
        org,
        {"check_type": "cpu.busy_pct", "enabled": False},
        {"check_type": "memory.used_pct", "threshold_num": 90},
    )
    resolved = await _resolve(api_env, org.org_id, await _device(api_env, org.device_id))
    assert resolved is not None
    assert [c.check_type for c in resolved.checks] == ["memory.used_pct"]


async def test_a_retired_check_is_not_in_the_resolved_set(api_env) -> None:
    """§3, via §2.4's amendment: a retired check is history, and history is
    never pushed to an endpoint."""
    from datetime import UTC, datetime

    from sqlalchemy import text as sqltext

    org = await seed_org(api_env, "res-ret")
    profile_id = await _profile_with_checks(
        api_env,
        org,
        {"check_type": "cpu.busy_pct"},
        {"check_type": "memory.used_pct", "threshold_num": 90},
    )
    async with api_env.admin_engine.begin() as conn:
        await conn.execute(
            sqltext(
                "UPDATE health_check SET retired_at = :t WHERE profile_id = :p "
                "AND check_type = 'cpu.busy_pct'"
            ),
            {"t": datetime.now(UTC), "p": profile_id},
        )
    resolved = await _resolve(api_env, org.org_id, await _device(api_env, org.device_id))
    assert resolved is not None
    assert [c.check_type for c in resolved.checks] == ["memory.used_pct"]


async def test_a_check_pinned_to_another_os_family_is_filtered_out(api_env) -> None:
    """§3's literal predicate: `check.os_family IS NULL OR = device.os_family`."""
    org = await seed_org(api_env, "res-oscol")
    await _profile_with_checks(
        api_env,
        org,
        {"check_type": "cpu.busy_pct", "os_family": "linux"},
        {"check_type": "memory.used_pct", "threshold_num": 90, "os_family": "windows"},
    )
    # `seed_org` creates a WINDOWS device.
    resolved = await _resolve(api_env, org.org_id, await _device(api_env, org.device_id))
    assert resolved is not None
    assert [c.check_type for c in resolved.checks] == ["memory.used_pct"]


async def test_a_check_type_this_os_cannot_answer_is_filtered_out(api_env) -> None:
    """THE FILTER §3's LITERAL PREDICATE MISSES. `posture.antivirus_healthy`
    with `os_family = NULL` means "any OS" and passes the column test on Linux,
    yet §4.1 says Linux cannot answer it -- so §3's stated purpose ("an agent is
    never shipped a check it cannot answer ... one less `unavailable` state to
    explain and one less shell-out to attempt") needs the REGISTRY, not just the
    column. Both halves are asserted: absent for Linux, present for Windows."""
    org = await seed_org(api_env, "res-osreg")
    linux_device = await seed_device(api_env, org.org_id, os_family="linux")
    profile_id = await seed_profile(api_env, org.org_id)
    await seed_check(
        api_env,
        org.org_id,
        profile_id,
        check_type="posture.antivirus_healthy",
        comparator="is",
        threshold_num=None,
        threshold_bool=True,
        severity="critical",
        os_family=None,
    )
    await seed_check(api_env, org.org_id, profile_id, check_type="cpu.busy_pct")
    await seed_assignment(api_env, org.org_id, profile_id)

    on_linux = await _resolve(api_env, org.org_id, await _device(api_env, linux_device))
    on_windows = await _resolve(api_env, org.org_id, await _device(api_env, org.device_id))
    assert on_linux is not None and on_windows is not None
    assert [c.check_type for c in on_linux.checks] == ["cpu.busy_pct"]
    assert sorted(c.check_type for c in on_windows.checks) == [
        "cpu.busy_pct",
        "posture.antivirus_healthy",
    ]


async def test_agent_offline_is_evaluated_but_never_pushed(api_env) -> None:
    """§0.6. An agent that is offline cannot report that it is offline, so
    `agent.offline` is server-evaluated: it belongs to the set §12's sweep reads
    and NOT to the set `health.policy.apply` ships. Both halves matter -- absent
    from `checks` too would silently disable the offline check entirely."""
    org = await seed_org(api_env, "res-offline")
    await _profile_with_checks(
        api_env,
        org,
        {"check_type": "cpu.busy_pct"},
        {"check_type": "agent.offline", "comparator": "gt", "threshold_num": 900},
    )
    resolved = await _resolve(api_env, org.org_id, await _device(api_env, org.device_id))
    assert resolved is not None
    assert sorted(c.check_type for c in resolved.checks) == ["agent.offline", "cpu.busy_pct"]
    assert [c.check_type for c in resolved.agent_checks] == ["cpu.busy_pct"]


async def test_a_profile_with_no_applicable_checks_still_resolves(api_env) -> None:
    """An empty check set is a legal policy meaning "run nothing" (§5.2), and it
    is NOT the same as having no profile: Task 7 pushes `checks=[]` for this
    device, which is how a disarm becomes an explicit instruction."""
    org = await seed_org(api_env, "res-empty")
    profile_id = await seed_profile(api_env, org.org_id)
    await seed_check(api_env, org.org_id, profile_id, check_type="cpu.busy_pct", os_family="linux")
    await seed_assignment(api_env, org.org_id, profile_id, device_id=org.device_id)

    resolved = await _resolve(api_env, org.org_id, await _device(api_env, org.device_id))
    assert resolved is not None
    assert resolved.checks == ()
    assert resolved.agent_checks == ()


# ---------------------------------------------------------------------------
# Tenancy -- load-bearing, and only visible on the admin engine
# ---------------------------------------------------------------------------


async def test_resolution_carries_its_own_org_predicate(admin_engine) -> None:
    """THE ONE RLS CANNOT MASK. Two orgs, each with an org-default assignment.
    On the admin engine the `org_id` predicates in the assignment lookup are the
    only thing separating them: delete one and org A's device resolves to
    whichever org-default row the planner returns -- which for a health module
    means org B's thresholds, and therefore org B's remediations, on org A's
    fleet."""
    a = await seed_org(admin_engine, "res-pa")
    b = await seed_org(admin_engine, "res-pb")
    profile_a = await seed_profile(admin_engine, a.org_id, name="A's profile")
    profile_b = await seed_profile(admin_engine, b.org_id, name="B's profile")
    await seed_assignment(admin_engine, a.org_id, profile_a)
    await seed_assignment(admin_engine, b.org_id, profile_b)
    await seed_check(admin_engine, a.org_id, profile_a, check_type="cpu.busy_pct")
    await seed_check(admin_engine, b.org_id, profile_b, check_type="memory.used_pct")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device_a = await session.get(Device, a.device_id)
        device_b = await session.get(Device, b.device_id)
        assert device_a is not None and device_b is not None
        resolved_a = await resolve_profile_for_device(session, a.org_id, device_a)
        resolved_b = await resolve_profile_for_device(session, b.org_id, device_b)

    assert resolved_a is not None and resolved_a.profile.name == "A's profile"
    assert [c.check_type for c in resolved_a.checks] == ["cpu.busy_pct"]
    assert resolved_b is not None and resolved_b.profile.name == "B's profile"
    assert [c.check_type for c in resolved_b.checks] == ["memory.used_pct"]


async def test_a_device_from_another_org_resolves_to_nothing(api_env) -> None:
    """`resolve_profile_for_device` takes `org_id` separately from the device
    row, so a caller that hands it a mismatched pair must get nothing rather
    than the other tenant's policy."""
    a = await seed_org(api_env, "res-xa")
    b = await seed_org(api_env, "res-xb")
    profile_b = await seed_profile(api_env, b.org_id)
    await seed_assignment(api_env, b.org_id, profile_b, device_id=b.device_id)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device_b = await session.get(Device, b.device_id)
        assert device_b is not None
        assert await resolve_profile_for_device(session, a.org_id, device_b) is None


async def test_a_nonexistent_device_group_does_not_raise(api_env) -> None:
    """Defensive: a device pointing at a group that no longer exists (a race
    with a group delete) degrades to the org default rather than raising inside
    a gateway message handler."""
    org = await seed_org(api_env, "res-ghost")
    profile_id = await seed_profile(api_env, org.org_id)
    await seed_assignment(api_env, org.org_id, profile_id)
    device = await _device(api_env, org.device_id)
    device.device_group_id = uuid4()

    resolved = await _resolve(api_env, org.org_id, device)
    assert resolved is not None and resolved.level == "org"
