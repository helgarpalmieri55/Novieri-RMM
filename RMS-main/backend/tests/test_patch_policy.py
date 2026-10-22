from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import Device, DeviceGroup, Organization, PatchPolicy
from app.modules.patch.policy import resolve_patch_policy, resolve_ring_populations


async def _setup(admin_engine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="pp", slug=f"pp-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        grp = DeviceGroup(org_id=org.id, name="servers")
        s.add(grp)
        await s.flush()
        dev = Device(org_id=org.id, hostname="h", os_family="linux", device_group_id=grp.id)
        s.add(dev)
        await s.commit()
        return org.id, grp.id, dev.id


async def test_defaults_when_no_policy(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    org_id, _, device_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        pol = await resolve_patch_policy(s, org_id, device_id)
    assert pol.ring_name == "broad" and pol.auto_approve_security is False
    assert pol.reboot == "if_required" and pol.scan_interval_hours == 24


async def test_group_policy_wins_and_dup_org_default_safe(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, group_id, device_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        s.add(PatchPolicy(org_id=org_id, device_group_id=None, ring_name="broad"))
        s.add(PatchPolicy(org_id=org_id, device_group_id=group_id, ring_name="test",
                          ring_order=0, auto_approve_security=True))
        await s.flush()
        pol = await resolve_patch_policy(s, org_id, device_id)
    assert pol.ring_name == "test" and pol.ring_order == 0 and pol.auto_approve_security is True


async def test_org_default_resolves_when_duplicate_rows_exist(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Postgres UNIQUE(org_id, device_group_id) does not prevent multiple NULL
    device_group_id rows for the same org, so more than one org-default policy
    row can exist. resolve_patch_policy must not raise MultipleResultsFound and
    must deterministically pick the lowest-id row (device has no group-specific
    policy, so the org-default branch is genuinely exercised)."""
    org_id, _, device_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        first = PatchPolicy(org_id=org_id, device_group_id=None, ring_name="pilot",
                             scan_interval_hours=6)
        s.add(first)
        await s.flush()
        second = PatchPolicy(org_id=org_id, device_group_id=None, ring_name="broad",
                              scan_interval_hours=48)
        s.add(second)
        await s.flush()
        pol = await resolve_patch_policy(s, org_id, device_id)
    assert pol.ring_name == "pilot"
    assert pol.scan_interval_hours == 6


# --------------------------------------------------------------------------- #
# resolve_ring_populations -- the same question `resolve_patch_policy` answers
# one device at a time, asked once for the whole fleet.
#
# IT EXISTS BECAUSE THE PER-DEVICE FORM IS AN N+1 THAT APPROVAL PAYS TWICE OVER.
# `approve.py::_devices_in_ring` resolved every device in the org individually
# and did it AGAIN for every patch in a batch: approving 400 patches across a
# 200-device fleet was 80,000 resolutions of an answer that had not changed.
# The rule these tests enforce is that the fast form must give the SAME answer
# as the slow one -- an optimisation that quietly re-rings a device would
# approve patches onto machines the operator did not clear them for.
# --------------------------------------------------------------------------- #


async def _mixed_fleet(admin_engine) -> tuple[UUID, dict[str, UUID]]:
    """An org whose devices reach their ring by each of the three routes
    `resolve_patch_policy` documents: a group policy, the org default, and the
    hardcoded fallback when neither exists."""
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="rp", slug=f"rp-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        pilot_group = DeviceGroup(org_id=org.id, name="pilot-group")
        # A group with a policy but NO devices: its ring must still be listed,
        # at zero, because a ring an operator can approve into and cannot see
        # is a ring they will approve into by accident.
        empty_group = DeviceGroup(org_id=org.id, name="empty-group")
        plain_group = DeviceGroup(org_id=org.id, name="plain-group")
        s.add_all([pilot_group, empty_group, plain_group])
        await s.flush()
        s.add(PatchPolicy(org_id=org.id, device_group_id=None, ring_name="broad", ring_order=100))
        s.add(
            PatchPolicy(
                org_id=org.id, device_group_id=pilot_group.id, ring_name="pilot", ring_order=50
            )
        )
        s.add(
            PatchPolicy(
                org_id=org.id, device_group_id=empty_group.id, ring_name="canary", ring_order=10
            )
        )
        devices = {
            "in_pilot": Device(
                org_id=org.id, hostname="a", os_family="windows",
                device_group_id=pilot_group.id,
            ),
            # Grouped, but that group names no policy -> org default.
            "grouped_no_policy": Device(
                org_id=org.id, hostname="b", os_family="windows",
                device_group_id=plain_group.id,
            ),
            # No group at all -> org default.
            "ungrouped": Device(org_id=org.id, hostname="c", os_family="linux"),
        }
        s.add_all(list(devices.values()))
        await s.commit()
        return org.id, {name: dev.id for name, dev in devices.items()}


async def test_ring_populations_agree_with_the_per_device_resolver(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """THE FALSIFIER FOR THE WHOLE OPTIMISATION. Every device is resolved both
    ways and the two answers must be identical, device by device."""
    org_id, devices = await _mixed_fleet(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        populations = await resolve_ring_populations(s, org_id)
        fast = {
            device_id: population.ring_name
            for population in populations
            for device_id in population.device_ids
        }
        slow = {
            device_id: (await resolve_patch_policy(s, org_id, device_id)).ring_name
            for device_id in devices.values()
        }
    assert fast == slow
    assert slow == {
        devices["in_pilot"]: "pilot",
        devices["grouped_no_policy"]: "broad",
        devices["ungrouped"]: "broad",
    }


async def test_every_device_lands_in_exactly_one_ring(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The number the console prints as a blast radius is only readable if the
    rings PARTITION the fleet: a device counted twice inflates the reach of an
    approval, and a device counted nowhere hides one."""
    org_id, devices = await _mixed_fleet(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        populations = await resolve_ring_populations(s, org_id)
    placed = [d for population in populations for d in population.device_ids]
    assert sorted(placed) == sorted(devices.values())
    assert len(placed) == len(set(placed)), "a device appears in two rings"


async def test_a_ring_a_policy_names_is_listed_even_with_nobody_in_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _ = await _mixed_fleet(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        populations = await resolve_ring_populations(s, org_id)
    by_name = {p.ring_name: p for p in populations}
    assert by_name["canary"].device_ids == ()
    # Ordered by the rollout order the policies declare, not alphabetically:
    # canary(10) -> pilot(50) -> broad(100).
    assert [p.ring_name for p in populations] == ["canary", "pilot", "broad"]


async def test_a_ring_with_no_policy_at_all_falls_back_to_broad(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`ResolvedPatchPolicy()`'s hardcoded default is a real ring an approval
    can be made into, so it must be listed on an org that has no policy rows."""
    org_id, _, device_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        populations = await resolve_ring_populations(s, org_id)
    assert [(p.ring_name, p.device_ids) for p in populations] == [("broad", (device_id,))]
