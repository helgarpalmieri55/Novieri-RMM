from collections import Counter
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    Device,
    DeviceGroup,
    DevicePatchStatus,
    Organization,
    Patch,
    PatchApproval,
    PatchPolicy,
    User,
)
from app.modules.patch.approve import (
    UnknownPatchError,
    approve_patch,
    approve_patches,
    revoke_approval,
)


async def _setup(
    admin_engine: AsyncEngine,
) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    """Returns (org_id, user_id, patch_id, device_id_test_ring, device_id_broad_ring)."""
    slug = f"pa-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()

        user = User(org_id=org.id, external_subject=f"s-{slug}", email=f"{slug}@x.test")
        session.add(user)

        test_group = DeviceGroup(org_id=org.id, name="test-ring-group", client_id=org.id)
        broad_group = DeviceGroup(org_id=org.id, name="broad-ring-group", client_id=org.id)
        session.add_all([test_group, broad_group])
        await session.flush()

        session.add(
            PatchPolicy(org_id=org.id, device_group_id=test_group.id, ring_name="test")
        )
        session.add(
            PatchPolicy(org_id=org.id, device_group_id=broad_group.id, ring_name="broad")
        )

        device_test = Device(
            org_id=org.id, hostname="h-test", os_family="windows",
            device_group_id=test_group.id,
            client_id=org.id,
            site_id=org.id,
        )
        device_broad = Device(
            org_id=org.id, hostname="h-broad", os_family="windows",
            device_group_id=broad_group.id,
            client_id=org.id,
            site_id=org.id,
        )
        session.add_all([device_test, device_broad])
        await session.flush()

        patch = Patch(
            org_id=org.id,
            os_family="windows",
            source="windows_update",
            identifier="KB1",
            title="Cum Update",
            severity="critical",
        )
        session.add(patch)
        await session.flush()

        session.add(
            DevicePatchStatus(
                org_id=org.id, device_id=device_test.id, patch_id=patch.id, state="missing"
            )
        )
        session.add(
            DevicePatchStatus(
                org_id=org.id, device_id=device_broad.id, patch_id=patch.id, state="missing"
            )
        )
        await session.commit()
        return org.id, user.id, patch.id, device_test.id, device_broad.id


async def test_approve_flips_in_ring_missing_to_approved_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, patch_id, device_test_id, device_broad_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        approval = await approve_patch(
            session, org_id=org_id, actor_id=user_id, patch_id=patch_id, ring_name="test"
        )
        assert approval.ring_name == "test"
        assert approval.approved_by == user_id
        assert approval.revoked is False

        status_test = (
            await session.execute(
                select(DevicePatchStatus).where(DevicePatchStatus.device_id == device_test_id)
            )
        ).scalar_one()
        assert status_test.state == "approved"

        # A device in a different ring is NOT flipped.
        status_broad = (
            await session.execute(
                select(DevicePatchStatus).where(DevicePatchStatus.device_id == device_broad_id)
            )
        ).scalar_one()
        assert status_broad.state == "missing"

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "patch.approved")
            )
        ).scalar_one()
        assert event.actor_type == "user"
        assert event.actor_id == user_id
        assert event.target_type == "patch"
        assert event.target_id == str(patch_id)
        assert event.meta["ring_name"] == "test"
        assert event.meta["devices_flipped"] == 1


async def test_approve_is_idempotent(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    org_id, user_id, patch_id, _, _ = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        first = await approve_patch(
            session, org_id=org_id, actor_id=user_id, patch_id=patch_id, ring_name="test"
        )
        second = await approve_patch(
            session, org_id=org_id, actor_id=user_id, patch_id=patch_id, ring_name="test"
        )
        assert first.id == second.id

        approvals = (
            await session.execute(
                select(PatchApproval).where(
                    PatchApproval.patch_id == patch_id, PatchApproval.ring_name == "test"
                )
            )
        ).scalars().all()
        assert len(approvals) == 1


async def test_revoke_flips_approved_back_to_missing_but_leaves_installed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, patch_id, device_test_id, _ = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        approval = await approve_patch(
            session, org_id=org_id, actor_id=user_id, patch_id=patch_id, ring_name="test"
        )

        # A second device in the test ring, already installed — must not be
        # flipped back to missing by revoke.
        installed_device = Device(
            org_id=org_id, hostname="h-installed", os_family="windows",
            device_group_id=(await session.get(Device, device_test_id)).device_group_id,
            client_id=org_id,
            site_id=org_id,
        )
        session.add(installed_device)
        await session.flush()
        session.add(
            DevicePatchStatus(
                org_id=org_id,
                device_id=installed_device.id,
                patch_id=patch_id,
                state="installed",
            )
        )
        await session.flush()

        ok = await revoke_approval(
            session, org_id=org_id, actor_id=user_id, approval_id=approval.id
        )
        assert ok is True

        status_test = (
            await session.execute(
                select(DevicePatchStatus).where(DevicePatchStatus.device_id == device_test_id)
            )
        ).scalar_one()
        assert status_test.state == "missing"

        status_installed = (
            await session.execute(
                select(DevicePatchStatus).where(
                    DevicePatchStatus.device_id == installed_device.id
                )
            )
        ).scalar_one()
        assert status_installed.state == "installed"

        refreshed = await session.get(PatchApproval, approval.id)
        assert refreshed is not None
        assert refreshed.revoked is True
        assert refreshed.revoked_by == user_id
        assert refreshed.revoked_at is not None

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "patch.revoked")
            )
        ).scalar_one()
        assert event.actor_type == "user"
        assert event.actor_id == user_id
        assert event.target_type == "patch"
        assert event.target_id == str(patch_id)
        assert event.meta["ring_name"] == "test"


async def test_revoke_of_unknown_id_returns_false(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, _, _, _ = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        ok = await revoke_approval(
            session, org_id=org_id, actor_id=user_id, approval_id=uuid4()
        )
        assert ok is False


async def test_approve_patch_concurrent_first_approvals_do_not_race(
    admin_engine: AsyncEngine,
) -> None:
    """Two colleagues both approve the same patch for the same ring at
    once, for the very first time. The pre-check in `approve_patch` is a
    fast path, not the sole guard: two concurrent calls for the identical
    `(org_id, patch_id, ring_name)` can both miss it (both see "not found")
    before either has written. The fix mirrors the `create_rate`/
    `create_category` shape (hardening-3): the actual insert goes through
    `ON CONFLICT DO NOTHING` against `uq_patch_approvals_client_patch_ring`.

    Unlike those, `approve_patch`'s side effects (flipping
    `DevicePatchStatus` rows, writing the audit event) must happen exactly
    ONCE per (patch, ring) -- so the race loser must not merely avoid an
    `IntegrityError`, it must take the identical no-op path as the
    "already approved and not revoked" branch: return the winner's row
    without flipping devices again or emitting a second audit event for a
    row it did not write.

    Deterministic interleave: session A's pre-check SELECT against
    `patch_approvals` is intercepted; right after it misses, session B runs
    a FULL concurrent `approve_patch` call (insert + device flip + audit)
    and commits, before A gets to insert its own row.
    """
    org_id, user_id, patch_id, device_test_id, device_broad_id = await _setup(admin_engine)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session_a:
        original_execute = session_a.execute
        precheck_seen = False

        async def interleaving_execute(statement, *args, **kwargs):
            nonlocal precheck_seen
            result = await original_execute(statement, *args, **kwargs)
            if (
                not precheck_seen
                and getattr(statement, "is_select", False)
                and "patch_approvals" in str(statement).lower()
            ):
                precheck_seen = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as session_b:
                    await approve_patch(
                        session_b,
                        org_id=org_id,
                        actor_id=user_id,
                        patch_id=patch_id,
                        ring_name="test",
                    )
                    await session_b.commit()
            return result

        session_a.execute = interleaving_execute  # type: ignore[method-assign]
        loser_result = await approve_patch(
            session_a, org_id=org_id, actor_id=user_id, patch_id=patch_id, ring_name="test"
        )
        await session_a.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        approvals = (
            await session.execute(
                select(PatchApproval).where(
                    PatchApproval.org_id == org_id,
                    PatchApproval.patch_id == patch_id,
                    PatchApproval.ring_name == "test",
                )
            )
        ).scalars().all()
        # Exactly one row was ever created, and the "loser" returned that
        # SAME row rather than a discarded one of its own.
        assert len(approvals) == 1
        assert loser_result.id == approvals[0].id

        # Devices were flipped exactly once, not once per racing caller.
        status_test = (
            await session.execute(
                select(DevicePatchStatus).where(DevicePatchStatus.device_id == device_test_id)
            )
        ).scalar_one()
        assert status_test.state == "approved"
        status_broad = (
            await session.execute(
                select(DevicePatchStatus).where(DevicePatchStatus.device_id == device_broad_id)
            )
        ).scalar_one()
        assert status_broad.state == "missing"

        # Exactly one audit event -- the loser did not audit a row it did
        # not write.
        events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "patch.approved"
                )
            )
        ).scalars().all()
        assert len(events) == 1
        assert events[0].meta["devices_flipped"] == 1


async def test_reapprove_after_revoke_unrevokes_in_place(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, patch_id, device_test_id, _ = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        first = await approve_patch(
            session, org_id=org_id, actor_id=user_id, patch_id=patch_id, ring_name="test"
        )

        ok = await revoke_approval(
            session, org_id=org_id, actor_id=user_id, approval_id=first.id
        )
        assert ok is True

        status_test = (
            await session.execute(
                select(DevicePatchStatus).where(DevicePatchStatus.device_id == device_test_id)
            )
        ).scalar_one()
        assert status_test.state == "missing"

        # Re-approve the same (patch, ring) after revocation. Must not raise
        # IntegrityError from the (org_id, patch_id, ring_name) unique
        # constraint, and must not create a second row.
        second = await approve_patch(
            session, org_id=org_id, actor_id=user_id, patch_id=patch_id, ring_name="test"
        )
        assert second.id == first.id

        approvals = (
            await session.execute(
                select(PatchApproval).where(
                    PatchApproval.patch_id == patch_id, PatchApproval.ring_name == "test"
                )
            )
        ).scalars().all()
        assert len(approvals) == 1

        refreshed = approvals[0]
        assert refreshed.revoked is False
        assert refreshed.approved_by == user_id
        assert refreshed.revoked_by is None
        assert refreshed.revoked_at is None

        status_test = (
            await session.execute(
                select(DevicePatchStatus).where(DevicePatchStatus.device_id == device_test_id)
            )
        ).scalar_one()
        assert status_test.state == "approved"


# --------------------------------------------------------------------------- #
# approve_patches -- one ring decision over many patches.
#
# THE OPERATOR'S REPORT, 2026-08-24: "there are a lot of packages and lines and
# a lot of data just for 1 device, it is super hard to approve all updates one
# by one". Approval was ALREADY fleet-wide -- `(org_id, patch_id, ring_name)`,
# no device column -- so the one-by-one was the console asking for one POST and
# one confirmation per row. This is the service that lets the decision be made
# once for a selection, and the tests below pin the two things a batch must not
# quietly lose: an audit event PER APPROVAL (invariant #4 -- a single event for
# a batch stops the log answering "who cleared THIS patch"), and the org
# predicate on every patch id it is handed.
# --------------------------------------------------------------------------- #


async def _extra_patch(admin_engine: AsyncEngine, org_id: UUID, identifier: str) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        patch = Patch(
            org_id=org_id,
            os_family="windows",
            source="windows_update",
            identifier=identifier,
            title=f"Update {identifier}",
            severity="important",
        )
        session.add(patch)
        await session.commit()
        return patch.id


async def test_bulk_approval_writes_one_audit_event_per_patch_not_one_for_the_batch(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """INVARIANT #4, and the reason a batch endpoint is not just a loop with a
    summary. One event naming three patches answers "somebody approved a batch";
    the log has to answer "who cleared KB2, and when", which is the question
    asked after the update that broke a fleet."""
    org_id, user_id, patch_one, _, _ = await _setup(admin_engine)
    patch_two = await _extra_patch(admin_engine, org_id, "KB2")
    patch_three = await _extra_patch(admin_engine, org_id, "KB3")

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        outcomes = await approve_patches(
            session,
            org_id=org_id,
            actor_id=user_id,
            patch_ids=[patch_one, patch_two, patch_three],
            ring_name="test",
        )
        assert [o.created for o in outcomes] == [True, True, True]
        # Request order is answer order, so a console can pair the outcome with
        # the row the operator ticked.
        assert [o.approval.patch_id for o in outcomes] == [patch_one, patch_two, patch_three]

        events = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "patch.approved")
            )
        ).scalars().all()
        assert len(events) == 3
        assert {e.target_id for e in events} == {str(patch_one), str(patch_two), str(patch_three)}
        assert {e.actor_id for e in events} == {user_id}
        assert {e.meta["ring_name"] for e in events} == {"test"}


async def test_bulk_approval_says_which_patches_were_already_in_force(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Re-approving an approval already in force changes nothing and writes no
    event -- so a console that reported "3 approved" after a batch of which two
    were already cleared would be describing writes that never happened."""
    org_id, user_id, patch_one, _, _ = await _setup(admin_engine)
    patch_two = await _extra_patch(admin_engine, org_id, "KB2")

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await approve_patch(
            session, org_id=org_id, actor_id=user_id, patch_id=patch_one, ring_name="test"
        )
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        outcomes = await approve_patches(
            session,
            org_id=org_id,
            actor_id=user_id,
            patch_ids=[patch_one, patch_two],
            ring_name="test",
        )
        assert [o.created for o in outcomes] == [False, True]
        events = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "patch.approved")
            )
        ).scalars().all()
    by_patch = Counter(e.target_id for e in events)
    # ONE event for `patch_one` -- the original approval, not a second one for
    # the batch that re-named it. A re-approval of a clearance already in force
    # changes nothing, so an event would put a second "who approved this" answer
    # in the trail for a decision somebody else made at another time.
    assert by_patch == {str(patch_one): 1, str(patch_two): 1}


async def test_bulk_approval_deduplicates_repeated_ids(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, patch_id, _, _ = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        outcomes = await approve_patches(
            session,
            org_id=org_id,
            actor_id=user_id,
            patch_ids=[patch_id, patch_id],
            ring_name="test",
        )
    assert [o.created for o in outcomes] == [True]


async def test_bulk_approval_refuses_a_patch_from_another_org_and_writes_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A patch id is an opaque uuid the CALLER supplies, and `patch_approvals`
    has an FK to `patches` that referential integrity checks with row security
    BYPASSED -- so without this predicate an org could mint an approval in its
    own tenant against a patch row it cannot see. Nothing leaks outward, but the
    approval gate ends up holding a clearance for an update this org has no
    record of, and the batch is where an operator would never notice one id in
    four hundred."""
    org_id, user_id, patch_id, _, _ = await _setup(admin_engine)
    other_org_id, _, foreign_patch_id, _, _ = await _setup(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(UnknownPatchError) as raised:
            await approve_patches(
                session,
                org_id=org_id,
                actor_id=user_id,
                patch_ids=[patch_id, foreign_patch_id],
                ring_name="test",
            )
        assert raised.value.patch_ids == [foreign_patch_id]

    # NOTHING from the batch was written -- not even the id that was legitimate.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        approvals = (
            (await session.execute(select(PatchApproval))).scalars().all()
        )
        assert approvals == []
    assert other_org_id != org_id


async def test_single_approval_refuses_a_patch_from_another_org_too(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The same predicate on the route that has existed since the module
    shipped. It is one function; the batch did not invent the check."""
    org_id, user_id, _, _, _ = await _setup(admin_engine)
    _, _, foreign_patch_id, _, _ = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(UnknownPatchError):
            await approve_patch(
                session,
                org_id=org_id,
                actor_id=user_id,
                patch_id=foreign_patch_id,
                ring_name="test",
            )


async def test_bulk_approval_flips_only_the_ring_it_names(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, patch_id, device_test_id, device_broad_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        outcomes = await approve_patches(
            session, org_id=org_id, actor_id=user_id, patch_ids=[patch_id], ring_name="test"
        )
        assert outcomes[0].devices_flipped == 1
        states = {
            row.device_id: row.state
            for row in (await session.execute(select(DevicePatchStatus))).scalars().all()
        }
    assert states[device_test_id] == "approved"
    assert states[device_broad_id] == "missing"
