"""Tests for advisory action-linking (docs/modules/change_management/spec.md):
associate a change with the patch/deploy/provision action that implements it,
via org-scoped polymorphic resolution.

Linking is advisory only -- it never mutates the linked action. The
cross-org resolution guard is the load-bearing security requirement: an
`action_id` belonging to another org must resolve to "not found", exactly
like a nonexistent id, because the existence check is always
`WHERE model.org_id == org_id AND model.id == action_id` (invariant #5).
"""

import datetime as _dt
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    ChangeLink,
    ChangeRequest,
    Deployment,
    Device,
    Job,
    Organization,
    OsImage,
    Package,
    Patch,
    PatchApproval,
    PatchRun,
    ProvisionProfile,
    ProvisionRun,
    User,
)
from app.modules.change_mgmt.links import add_link, list_links, remove_link
from app.modules.change_mgmt.state import ChangeNotFoundError, ChangeValidationError


async def _org_with_user_and_change(admin_engine: AsyncEngine) -> tuple[UUID, UUID, UUID]:
    """Returns (org_id, user_id, change_id)."""
    slug = f"chlink-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()

        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        s.add(user)
        await s.flush()

        change = ChangeRequest(
            org_id=org.id,
            type="normal",
            title="T",
            description="D",
            risk="low",
            affected=[],
            state="draft",
            created_by=user.id,
        )
        s.add(change)
        await s.commit()
        return org.id, user.id, change.id


async def _seed_deployment(admin_engine: AsyncEngine, org_id: UUID) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        device = Device(
            org_id=org_id, hostname="h1", os_family="windows", client_id=org_id, site_id=org_id
        )
        s.add(device)
        await s.flush()

        package = Package(
            org_id=org_id, name="Acme", version="1.0", os_family="windows", installer_type="msi"
        )
        s.add(package)
        await s.flush()

        deployment = Deployment(
            org_id=org_id,
            package_id=package.id,
            action="install",
            target_type="device",
            target_id=device.id,
            state="pending",
        )
        s.add(deployment)
        await s.commit()
        return deployment.id


async def _seed_patch_run(admin_engine: AsyncEngine, org_id: UUID) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        device = Device(
            org_id=org_id, hostname="h1", os_family="windows", client_id=org_id, site_id=org_id
        )
        s.add(device)
        await s.flush()

        patch = Patch(
            org_id=org_id,
            os_family="windows",
            source="windows_update",
            identifier="KB1",
            title="Cum Update",
            severity="critical",
        )
        s.add(patch)
        await s.flush()

        job = Job(
            org_id=org_id,
            type="patch.apply",
            target_type="device",
            target_id=device.id,
            params={},
            nonce="n",
            issued_at=_dt.datetime.now(_dt.UTC),
            expires_at=_dt.datetime.now(_dt.UTC),
            signature="fake-signature",
        )
        s.add(job)
        await s.flush()

        run = PatchRun(org_id=org_id, job_id=job.id, device_id=device.id)
        s.add(run)
        await s.commit()
        return run.id


async def _seed_provision_run(admin_engine: AsyncEngine, org_id: UUID) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        device = Device(
            org_id=org_id, hostname="h1", os_family="windows", client_id=org_id, site_id=org_id
        )
        s.add(device)
        await s.flush()

        image = OsImage(
            org_id=org_id, os_family="windows", name="win-image", kind="image", params={}
        )
        s.add(image)
        await s.flush()

        profile = ProvisionProfile(
            org_id=org_id, name="profile-1", image_id=image.id, post_config={}, target_scope={}
        )
        s.add(profile)
        await s.flush()

        job = Job(
            org_id=org_id,
            type="provision.enroll",
            target_type="device",
            target_id=device.id,
            params={},
            nonce="n",
            issued_at=_dt.datetime.now(_dt.UTC),
            expires_at=_dt.datetime.now(_dt.UTC),
            signature="fake-signature",
        )
        s.add(job)
        await s.flush()

        run = ProvisionRun(
            org_id=org_id,
            profile_id=profile.id,
            device_id=device.id,
            job_id=job.id,
            state="enrolled",
            actor_type="system",
            started_at=_dt.datetime.now(_dt.UTC),
        )
        s.add(run)
        await s.commit()
        return run.id


async def _seed_patch_approval(admin_engine: AsyncEngine, org_id: UUID) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        patch = Patch(
            org_id=org_id,
            os_family="windows",
            source="windows_update",
            identifier="KB2",
            title="Cum Update 2",
            severity="critical",
        )
        s.add(patch)
        await s.flush()

        approval = PatchApproval(org_id=org_id, patch_id=patch.id, ring_name="broad")
        s.add(approval)
        await s.commit()
        return approval.id


async def _audit_actions(app_engine: AsyncEngine, org_id: UUID, change_id: UUID) -> list[str]:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        events = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.target_type == "change",
                    AuditEvent.target_id == str(change_id),
                )
                .order_by(AuditEvent.id)
            )
        ).scalars().all()
        return [e.action for e in events]


# ---------------------------------------------------------------------------
# add_link: success for each of the 4 action_types, advisory (no mutation)
# ---------------------------------------------------------------------------


async def test_add_link_deployment_succeeds_and_is_advisory(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, change_id = await _org_with_user_and_change(admin_engine)
    deployment_id = await _seed_deployment(admin_engine, org_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        before = (
            await s.execute(select(Deployment).where(Deployment.id == deployment_id))
        ).scalar_one()
        before_state = before.state
        before_action = before.action

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        link = await add_link(
            s,
            org_id=org_id,
            actor_id=user_id,
            change_id=change_id,
            action_type="deployment",
            action_id=deployment_id,
        )
        await s.commit()
        assert link.action_type == "deployment"
        assert link.action_id == deployment_id
        assert link.change_id == change_id

    # Advisory: the linked Deployment row is completely unchanged.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        after = (
            await s.execute(select(Deployment).where(Deployment.id == deployment_id))
        ).scalar_one()
        assert after.state == before_state == "pending"
        assert after.action == before_action

    actions = await _audit_actions(app_engine, org_id, change_id)
    assert actions == ["change.linked"]

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        event = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "change.linked"
                )
            )
        ).scalar_one()
        assert event.actor_type == "user"
        assert event.actor_id == user_id
        assert event.target_type == "change"
        assert event.target_id == str(change_id)
        assert event.meta["action_type"] == "deployment"
        assert event.meta["action_id"] == str(deployment_id)


async def test_add_link_patch_run_succeeds_and_is_advisory(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, change_id = await _org_with_user_and_change(admin_engine)
    run_id = await _seed_patch_run(admin_engine, org_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        before = (await s.execute(select(PatchRun).where(PatchRun.id == run_id))).scalar_one()
        before_finished = before.finished_at
        before_results = dict(before.results)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        link = await add_link(
            s,
            org_id=org_id,
            actor_id=user_id,
            change_id=change_id,
            action_type="patch_run",
            action_id=run_id,
        )
        await s.commit()
        assert link.action_type == "patch_run"
        assert link.action_id == run_id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        after = (await s.execute(select(PatchRun).where(PatchRun.id == run_id))).scalar_one()
        assert after.finished_at == before_finished
        assert dict(after.results) == before_results


async def test_add_link_provision_run_succeeds_and_is_advisory(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, change_id = await _org_with_user_and_change(admin_engine)
    run_id = await _seed_provision_run(admin_engine, org_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        before = (
            await s.execute(select(ProvisionRun).where(ProvisionRun.id == run_id))
        ).scalar_one()
        before_state = before.state

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        link = await add_link(
            s,
            org_id=org_id,
            actor_id=user_id,
            change_id=change_id,
            action_type="provision_run",
            action_id=run_id,
        )
        await s.commit()
        assert link.action_type == "provision_run"

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        after = (
            await s.execute(select(ProvisionRun).where(ProvisionRun.id == run_id))
        ).scalar_one()
        assert after.state == before_state == "enrolled"


async def test_add_link_patch_approval_succeeds_and_is_advisory(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, change_id = await _org_with_user_and_change(admin_engine)
    approval_id = await _seed_patch_approval(admin_engine, org_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        before = (
            await s.execute(select(PatchApproval).where(PatchApproval.id == approval_id))
        ).scalar_one()
        before_revoked = before.revoked

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        link = await add_link(
            s,
            org_id=org_id,
            actor_id=user_id,
            change_id=change_id,
            action_type="patch_approval",
            action_id=approval_id,
        )
        await s.commit()
        assert link.action_type == "patch_approval"

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        after = (
            await s.execute(select(PatchApproval).where(PatchApproval.id == approval_id))
        ).scalar_one()
        assert after.revoked == before_revoked is False


# ---------------------------------------------------------------------------
# add_link: failures
# ---------------------------------------------------------------------------


async def test_add_link_nonexistent_action_id_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, change_id = await _org_with_user_and_change(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(ChangeValidationError):
            await add_link(
                s,
                org_id=org_id,
                actor_id=user_id,
                change_id=change_id,
                action_type="deployment",
                action_id=uuid4(),
            )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await list_links(s, org_id, change_id)
        assert result == []


async def test_add_link_cross_org_action_raises_and_creates_no_link(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, user_a, change_a = await _org_with_user_and_change(admin_engine)
    org_b, _user_b, _change_b = await _org_with_user_and_change(admin_engine)

    deployment_b = await _seed_deployment(admin_engine, org_b)

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(ChangeValidationError):
            await add_link(
                s,
                org_id=org_a,
                actor_id=user_a,
                change_id=change_a,
                action_type="deployment",
                action_id=deployment_b,
            )

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        result = await list_links(s, org_a, change_a)
        assert result == []

    # Confirm the org-B deployment itself is untouched and still resolvable
    # in its own org (i.e. it really exists -- this wasn't "nonexistent").
    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        dep = (
            await s.execute(select(Deployment).where(Deployment.id == deployment_b))
        ).scalar_one()
        assert dep.org_id == org_b


async def test_add_link_unknown_action_type_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, change_id = await _org_with_user_and_change(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(ChangeValidationError, match="unknown action_type"):
            await add_link(
                s,
                org_id=org_id,
                actor_id=user_id,
                change_id=change_id,
                action_type="frobnicate",
                action_id=uuid4(),
            )


async def test_add_link_nonexistent_change_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, _change_id = await _org_with_user_and_change(admin_engine)
    deployment_id = await _seed_deployment(admin_engine, org_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(ChangeNotFoundError):
            await add_link(
                s,
                org_id=org_id,
                actor_id=user_id,
                change_id=uuid4(),
                action_type="deployment",
                action_id=deployment_id,
            )


async def test_add_link_duplicate_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, change_id = await _org_with_user_and_change(admin_engine)
    deployment_id = await _seed_deployment(admin_engine, org_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await add_link(
            s,
            org_id=org_id,
            actor_id=user_id,
            change_id=change_id,
            action_type="deployment",
            action_id=deployment_id,
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(ChangeValidationError, match="already linked"):
            await add_link(
                s,
                org_id=org_id,
                actor_id=user_id,
                change_id=change_id,
                action_type="deployment",
                action_id=deployment_id,
            )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await list_links(s, org_id, change_id)
        assert len(result) == 1


async def test_add_link_concurrent_duplicate_raises_typed_error_not_integrity_error(
    admin_engine: AsyncEngine,
) -> None:
    """Two colleagues both link the same change to the same deployment at
    once. The duplicate pre-check in `add_link` is a fast path, not the
    sole guard: two concurrent calls for the exact same `(org_id,
    change_id, action_type, action_id)` can both miss it before either has
    written. The fix mirrors `budget.rates.create_rate` (hardening-3): the
    actual insert goes through `ON CONFLICT DO NOTHING` against
    `uq_change_link` -- and since `add_link` is a single-caller CREATE (not
    a get-or-create), the race loser must raise the SAME typed
    `ChangeValidationError` the pre-check raises, never someone else's
    link, and never a raw `IntegrityError`.

    Deterministic interleave, the repo's established technique: session A's
    duplicate-check SELECT against `change_link` is intercepted; right
    after it misses, session B inserts and commits the EXACT link session A
    is about to try to create.
    """
    org_id, user_id, change_id = await _org_with_user_and_change(admin_engine)
    deployment_id = await _seed_deployment(admin_engine, org_id)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session_a:
        original_execute = session_a.execute
        precheck_seen = False

        async def interleaving_execute(statement, *args, **kwargs):
            nonlocal precheck_seen
            result = await original_execute(statement, *args, **kwargs)
            if (
                not precheck_seen
                and getattr(statement, "is_select", False)
                and "change_link" in str(statement).lower()
            ):
                precheck_seen = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as session_b:
                    await add_link(
                        session_b,
                        org_id=org_id,
                        actor_id=user_id,
                        change_id=change_id,
                        action_type="deployment",
                        action_id=deployment_id,
                    )
                    await session_b.commit()
            return result

        session_a.execute = interleaving_execute  # type: ignore[method-assign]
        with pytest.raises(ChangeValidationError, match="already linked"):
            await add_link(
                session_a,
                org_id=org_id,
                actor_id=user_id,
                change_id=change_id,
                action_type="deployment",
                action_id=deployment_id,
            )
        await session_a.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        links = (
            await session.execute(
                select(ChangeLink).where(
                    ChangeLink.org_id == org_id, ChangeLink.change_id == change_id
                )
            )
        ).scalars().all()
        assert len(links) == 1

        # The loser never emitted a second "change.linked" audit event for a
        # row it did not write.
        events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "change.linked",
                    AuditEvent.target_id == str(change_id),
                )
            )
        ).scalars().all()
        assert len(events) == 1


# ---------------------------------------------------------------------------
# remove_link
# ---------------------------------------------------------------------------


async def test_remove_link_deletes_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, change_id = await _org_with_user_and_change(admin_engine)
    deployment_id = await _seed_deployment(admin_engine, org_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        link = await add_link(
            s,
            org_id=org_id,
            actor_id=user_id,
            change_id=change_id,
            action_type="deployment",
            action_id=deployment_id,
        )
        await s.commit()
        link_id = link.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await remove_link(
            s, org_id=org_id, actor_id=user_id, change_id=change_id, link_id=link_id
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await list_links(s, org_id, change_id)
        assert result == []

    actions = await _audit_actions(app_engine, org_id, change_id)
    assert actions == ["change.linked", "change.unlinked"]

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        event = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "change.unlinked"
                )
            )
        ).scalar_one()
        assert event.target_type == "change"
        assert event.target_id == str(change_id)
        assert event.meta["link_id"] == str(link_id)

    # The linked action itself is still untouched.
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        dep = (
            await s.execute(select(Deployment).where(Deployment.id == deployment_id))
        ).scalar_one()
        assert dep.state == "pending"


async def test_remove_link_nonexistent_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, change_id = await _org_with_user_and_change(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(ChangeNotFoundError):
            await remove_link(
                s, org_id=org_id, actor_id=user_id, change_id=change_id, link_id=uuid4()
            )


async def test_remove_link_cross_org_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, user_a, change_a = await _org_with_user_and_change(admin_engine)
    org_b, user_b, change_b = await _org_with_user_and_change(admin_engine)
    deployment_b = await _seed_deployment(admin_engine, org_b)

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        link = await add_link(
            s,
            org_id=org_b,
            actor_id=user_b,
            change_id=change_b,
            action_type="deployment",
            action_id=deployment_b,
        )
        await s.commit()
        link_id = link.id

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(ChangeNotFoundError):
            await remove_link(
                s, org_id=org_a, actor_id=user_a, change_id=change_a, link_id=link_id
            )

    # The link in org_b is untouched.
    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        result = await list_links(s, org_b, change_b)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# list_links: tenancy
# ---------------------------------------------------------------------------


async def test_list_links_returns_only_the_calling_orgs_links_under_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, user_a, change_a = await _org_with_user_and_change(admin_engine)
    org_b, user_b, change_b = await _org_with_user_and_change(admin_engine)
    deployment_a = await _seed_deployment(admin_engine, org_a)
    deployment_b = await _seed_deployment(admin_engine, org_b)

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        await add_link(
            s,
            org_id=org_a,
            actor_id=user_a,
            change_id=change_a,
            action_type="deployment",
            action_id=deployment_a,
        )
        await s.commit()

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        await add_link(
            s,
            org_id=org_b,
            actor_id=user_b,
            change_id=change_b,
            action_type="deployment",
            action_id=deployment_b,
        )
        await s.commit()

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        result_a = await list_links(s, org_a, change_a)
        assert len(result_a) == 1
        assert result_a[0].action_id == deployment_a

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        result_b = await list_links(s, org_b, change_b)
        assert len(result_b) == 1
        assert result_b[0].action_id == deployment_b


async def test_the_link_org_predicates_hold_where_rls_is_not_doing_the_rejecting(
    admin_engine: AsyncEngine, bypassrls_session: AsyncSession
) -> None:
    """THE TEST IN THIS FILE THAT CAN ACTUALLY FAIL.

    Every other assertion in this file runs through `org_session` on the
    NOBYPASSRLS `app_user` role, where the neighbour's deployment and the
    neighbour's link are both gone before any application `WHERE org_id` is
    consulted -- so they hold with those predicates deleted. `its_admin`
    carries BYPASSRLS (`tests/conftest.py`), so here they are the only scoping.

    Two predicates, and the first is the interesting one. `change_link.action_id`
    is a BARE UUID with no foreign key -- the module's own docstring says so --
    which means nothing in the schema stops a link in org A from naming a
    deployment in org B. `_validate_action_ref`'s `model.org_id == org_id` is
    the entire defence, and a change record that claims to cover a neighbour's
    deployment is a false audit trail rather than a leak.

    FALSIFIED: deleting `model.org_id == org_id` from `_validate_action_ref`
    lets org A link org B's deployment; deleting `ChangeLink.org_id == org_id`
    from `list_links` shows both orgs' links for the same change id.
    """
    org_a, user_a, change_a = await _org_with_user_and_change(admin_engine)
    org_b, user_b, change_b = await _org_with_user_and_change(admin_engine)
    deployment_a = await _seed_deployment(admin_engine, org_a)
    deployment_b = await _seed_deployment(admin_engine, org_b)

    with pytest.raises(ChangeValidationError, match="linked action not found"):
        await add_link(
            bypassrls_session,
            org_id=org_a,
            actor_id=user_a,
            change_id=change_a,
            action_type="deployment",
            action_id=deployment_b,
        )
    await bypassrls_session.rollback()

    for org_id, actor_id, change_id, action_id in (
        (org_a, user_a, change_a, deployment_a),
        (org_b, user_b, change_b, deployment_b),
    ):
        await add_link(
            bypassrls_session,
            org_id=org_id,
            actor_id=actor_id,
            change_id=change_id,
            action_type="deployment",
            action_id=action_id,
        )
    await bypassrls_session.commit()

    links_a = await list_links(bypassrls_session, org_a, change_a)
    links_b = await list_links(bypassrls_session, org_b, change_b)
    assert [link.action_id for link in links_a] == [deployment_a]
    assert [link.action_id for link in links_b] == [deployment_b]
    assert await list_links(bypassrls_session, org_a, change_b) == [], (
        "org A read a neighbour's links by naming the neighbour's change id"
    )
