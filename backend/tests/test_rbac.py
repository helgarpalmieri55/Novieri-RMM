from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.deps import require_permission
from app.core.rbac import (
    PERMISSIONS,
    PLATFORM_OPERATOR_CODES,
    has_permission,
    route_permission,
    user_permission_codes,
)
from app.core.tenancy import org_session
from app.models import Client, Organization, Permission, Role, RolePermission, User, UserRole


async def _seed_user_with_role(
    admin_engine: AsyncEngine,
    role_name: str,
    slug_prefix: str,
    client: bool = False,
) -> tuple[UUID, UUID, UUID | None]:
    slug = f"{slug_prefix}-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@x.test")
        session.add(user)
        role = (
            await session.execute(select(Role).where(Role.name == role_name, Role.org_id.is_(None)))
        ).scalar_one()
        client_id: UUID | None = None
        if client:
            customer = Client(org_id=org.id, name="helpdesk-customer")
            session.add(customer)
            await session.flush()
            client_id = customer.id
        await session.flush()
        session.add(UserRole(org_id=org.id, user_id=user.id, role_id=role.id, client_id=client_id))
        await session.commit()
        return org.id, user.id, client_id


async def test_permission_registry_complete() -> None:
    assert set(PERMISSIONS) == {
        "devices.read",
        "devices.manage",
        # Approve or reject a machine that joined through the install link
        # (spec 2026-09-28 §3). Technician tier.
        "devices.review",
        # An asset is a devices row with no agent (asset-inventory spec §2).
        # There is deliberately no `assets.read`: `devices.read` already gates
        # the fleet list, and a second read gate would blind every existing
        # read-only role to part of it.
        "assets.manage",
        "jobs.read",
        "jobs.dispatch",
        "enrollment.manage",
        # The install link (spec 2026-09-28 §3): switch it on or off, edit its
        # networks, regenerate it. Admin tier.
        "enrollment.link.manage",
        # Adjacent to enrollment.manage on purpose: generating an install
        # command reads the release catalogue AND mints an enrollment token.
        "agent.release.read",
        "agent.release.manage",
        # Push install (`CLAUDE.md` invariant 2a, cycle B1). Beside the
        # release codes because they answer the two halves of the same
        # question -- what artefact, and by what means it reaches a machine --
        # and deliberately NOT collapsed into them: `agent.release.read` mints
        # a token for a HUMAN to carry, and `agent.push.run` causes an
        # unenrolled machine to execute the artefact. Neither is seeded to
        # `technician`; `agent.push.run` additionally requires
        # `credentials.checkout`, enforced at the service boundary.
        "agent.push.run",
        "agent.push.scope.manage",
        "agent.update.read",
        "agent.update.rollout",
        "agent.update.rollback",
        "audit.read",
        "credentials.read",
        "credentials.checkout",
        "credentials.manage",
        "policies.manage",
        "orgs.manage",
        "roles.read",
        "roles.manage",
        "users.read",
        "users.manage",
        "docs.read",
        "docs.manage",
        # spec-attachments.md §9: ONE new code for the module's only
        # irreversible act. Three dot-segments is established here
        # (`remote.recordings.read`, `licenses.keys.read`,
        # `health.action.admin`, `ipam.sweep.manage`), not novel.
        "docs.document.purge",
        "remote.control",
        "remote.recordings.read",
        "remote.policy.manage",
        "remote.unattended",
        "remote.privacy_blank",
        "patch.read",
        "patch.scan",
        "patch.approve",
        "patch.apply",
        "patch.policy.manage",
        "deploy.read",
        "deploy.package.manage",
        "deploy.create",
        "deploy.execute",
        "deploy.script.manage",
        "deploy.script.run",
        "deploy.script.schedule",
        "backup.read",
        "backup.policy.manage",
        "backup.run",
        "backup.restore",
        # Off-site copy and its recovery key (spec-server-backup §9): org
        # admin holds both, technician neither.
        "backup.offsite.manage",
        "backup.recovery.manage",
        "provisioning.read",
        "provisioning.manage",
        "provisioning.run",
        "change.read",
        "change.request",
        "change.approve",
        "change.manage",
        "change.gate.manage",
        "bcp.read",
        "bcp.manage",
        "bcp.approve",
        "licenses.read",
        "licenses.manage",
        "licenses.keys.read",
        "subscriptions.read",
        "subscriptions.manage",
        "people.read",
        "people.manage",
        "people.offboard",
        "budget.read",
        "budget.manage",
        "expense.record",
        "expense.confirm",
        "ticket.read",
        "ticket.write",
        "ticket.assign",
        "ticket.approve",
        "ticket.admin",
        "notification.read",
        "notification.admin",
        "portal.ticket.read",
        "portal.ticket.write",
        "email_intake.read",
        "email_intake.admin",
        "sla.read",
        "sla.admin",
        "health.read",
        "health.alert.manage",
        "health.admin",
        "health.remediate",
        "health.action.admin",
        "ipam.read",
        "ipam.assign",
        "ipam.manage",
        "ipam.sweep.run",
        "ipam.sweep.manage",
        "ipam.scope.manage",
        "ai.use",
        "ai.admin",
        "ai.act.propose",
        "ai.act.confirm",
        "reporting.read",
        "site.read",
        "site.manage",
        "site.wake",
        "platform.status.read",
        "platform.errors.read",
        "platform.errors.write",
        "mcp.use",
    }


async def test_notification_permissions_exist() -> None:
    assert "notification.read" in PERMISSIONS
    assert "notification.admin" in PERMISSIONS


async def test_notification_admin_is_not_technician_grant(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # Spec §8: notification.admin configures egress destinations (the webhook
    # allowlist and channel config). A technician holding it could point the
    # control plane at an internal host.
    #
    # This codebase has no static `ROLE_PERMISSIONS` dict -- role -> permission
    # grants are DB rows (roles/permissions/role_permissions, seeded per
    # migration; see migrations/versions/0025_notification_core.py), read
    # through `has_permission`/`user_permission_codes` inside a tenant
    # session, exactly like every other grant check in this file
    # (test_docs_permission_grants, test_remote_permission_grants above).
    org_id, tech_id, _ = await _seed_user_with_role(
        admin_engine, "technician", "rbac-notif-t"
    )
    async with org_session(org_id, engine=app_engine) as session:
        assert not await has_permission(session, tech_id, "notification.admin")

    org_id, admin_id, _ = await _seed_user_with_role(
        admin_engine, "org-admin", "rbac-notif-a"
    )
    async with org_session(org_id, engine=app_engine) as session:
        assert await has_permission(session, admin_id, "notification.read")
        assert await has_permission(session, admin_id, "notification.admin")


async def test_sla_permissions_are_not_technician_grants(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # SLA spec §8: "sla.admin ... Admin tier only, never technician." It sets
    # what the organisation has promised its customers and can silence breach
    # escalation entirely; `sla.read` exposes every deadline the org is
    # working to. Same mechanism as the notification check above -- grants are
    # DB rows seeded by migrations/versions/0033_sla.py, so a later migration
    # adding `technician` to ROLE_GRANTS for convenience is exactly what this
    # test exists to fail on.
    org_id, tech_id, _ = await _seed_user_with_role(admin_engine, "technician", "rbac-sla-t")
    async with org_session(org_id, engine=app_engine) as session:
        assert not await has_permission(session, tech_id, "sla.read")
        assert not await has_permission(session, tech_id, "sla.admin")

    org_id, admin_id, _ = await _seed_user_with_role(admin_engine, "org-admin", "rbac-sla-a")
    async with org_session(org_id, engine=app_engine) as session:
        assert await has_permission(session, admin_id, "sla.read")
        assert await has_permission(session, admin_id, "sla.admin")


async def test_technician_can_dispatch_but_not_manage_enrollment(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, _ = await _seed_user_with_role(admin_engine, "technician", "rbac-tech")
    async with org_session(org_id, engine=app_engine) as session:
        assert await has_permission(session, user_id, "jobs.dispatch")
        assert not await has_permission(session, user_id, "enrollment.manage")


async def test_auditor_reads_audit_but_cannot_dispatch(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, _ = await _seed_user_with_role(admin_engine, "auditor", "rbac-aud")
    async with org_session(org_id, engine=app_engine) as session:
        assert await has_permission(session, user_id, "audit.read")
        assert not await has_permission(session, user_id, "jobs.dispatch")


async def test_org_admin_has_every_permission_except_the_platform_operator_codes(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """THIS TEST USED TO READ `codes == set(PERMISSIONS)`, AND THAT EQUALITY
    WAS MANUFACTURING GRANTS.

    Three migrations say so in as many words -- 0054, 0057 and 0060 each open
    their "who gets it" paragraph with "`org-admin` is not a choice", because a
    code in `PERMISSIONS` and not seeded to `org-admin` turned this assertion
    red. So the RBAC seed for a platform-scoped code was decided by a test
    about org-admin rather than by anyone asking who should hold it, and
    `platform.errors.*` reached every tenant's own administrator that way
    (audit 2026-08-21-tenancy-test-efficacy F1: it made the org, user, job and
    device ids of every other tenant's errors readable by any org-admin).

    The equality is kept, because "org-admin holds everything, so a new module
    code needs no separate grant" is a real and useful property. What changed
    is that it is now equality with the codes an ORGANISATION can hold: a new
    `platform.*` code lands in `PLATFORM_OPERATOR_CODES`, and the next author
    has to decide which side of that line it sits on rather than being told by
    a red test to grant it to every tenant.
    """
    org_id, user_id, _ = await _seed_user_with_role(admin_engine, "org-admin", "rbac-adm")
    async with org_session(org_id, engine=app_engine) as session:
        codes = await user_permission_codes(session, user_id)
    assert codes == set(PERMISSIONS) - PLATFORM_OPERATOR_CODES
    assert codes & PLATFORM_OPERATOR_CODES == set()


async def test_the_platform_operator_codes_are_held_by_super_admin_and_nobody_else(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The other half, asserted from both sides so the revocation cannot pass
    by refusing everyone. `super-admin` is "Cross-org platform administration
    (SaaS operator)" (migration 0003) and is unreachable from every org-scoped
    console surface (`app/core/rbac_admin.py`), which is exactly the property a
    cross-tenant read needs behind it."""
    org_id, admin_id, _ = await _seed_user_with_role(admin_engine, "org-admin", "rbac-plat-a")
    async with org_session(org_id, engine=app_engine) as session:
        for code in sorted(PLATFORM_OPERATOR_CODES):
            assert not await has_permission(session, admin_id, code), code

    org_id, su_id, _ = await _seed_user_with_role(admin_engine, "super-admin", "rbac-plat-s")
    async with org_session(org_id, engine=app_engine) as session:
        for code in sorted(PLATFORM_OPERATOR_CODES):
            assert await has_permission(session, su_id, code), code


def test_the_migration_and_the_application_name_the_same_operator_codes() -> None:
    """The migration that installs the rail hard-codes the codes rather than
    importing `PLATFORM_OPERATOR_CODES`, because a migration that imports
    application code runs against whatever that code has become years later.
    The cost of that correctness is two copies, and this is what keeps them
    equal -- including inside the `set_role_permissions` body the migration
    installs, which refuses by literal SQL array.

    IT NAMES 0070, NOT 0068, and the reason is the whole point of this test.
    0068 introduced the set with two members; 0070 added `agent.release.manage`
    to it and reinstalled the function with all three. Whichever migration
    installed the CURRENT function body is the one whose copy has to match, so
    this assertion moves forward with the set. It went red on 0070 with the
    pointer still on 0068, which is exactly the signal it exists to give."""
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "versions"
        / "0070_agent_release_manage_operator_only.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0070", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert set(module._OPERATOR_CODES) == PLATFORM_OPERATOR_CODES
    # THE SQL ARRAY, NOT THE WHOLE FUNCTION BODY. The body carries a comment
    # naming the codes, so `code in body` would be satisfied by prose while the
    # rail itself refused something else entirely. `_SQL_ARRAY` is the literal
    # the `= ANY(...)` compares against and nothing else.
    for code in PLATFORM_OPERATOR_CODES:
        assert f"'{code}'" in module._SQL_ARRAY
    assert module._SQL_ARRAY in module._SET_ROLE_PERMISSIONS_WITH_OPERATOR_RAIL


async def test_an_org_admin_reads_the_release_catalogue_but_cannot_publish_to_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """MIGRATION 0070, AT THE GRANT LEVEL, AND IT IS A SPLIT RATHER THAN A
    REVOCATION.

    `agent_releases` is one catalogue every tenant shares -- no `org_id`, no
    RLS beneath it (`tests/test_tenant_column_sweep.py`'s `GLOBAL_BY_NATURE`).
    So `agent.release.manage` writes the bytes EVERY tenant's endpoints
    execute, and 0060 gave it to `org-admin`, the role every tenant's own
    administrator holds.

    `agent.release.read` stays exactly where 0060 put it, and asserting that
    here is what stops a later tidy-up from "closing" this finding by revoking
    both. 0060's own docstring says read "is needed by anyone who installs an
    agent"; an org that cannot list the catalogue cannot onboard a machine."""
    org_id, admin_id, _ = await _seed_user_with_role(admin_engine, "org-admin", "rbac-rel-a")
    async with org_session(org_id, engine=app_engine) as session:
        assert await has_permission(session, admin_id, "agent.release.read")
        assert not await has_permission(session, admin_id, "agent.release.manage")

    org_id, su_id, _ = await _seed_user_with_role(admin_engine, "super-admin", "rbac-rel-s")
    async with org_session(org_id, engine=app_engine) as session:
        # BOTH on the operator, not just the one that moved. Publishing without
        # being able to list what is published is a console page the publisher
        # cannot open (`frontend/src/shell/nav.ts` gates Agent Packages on
        # `agent.release.read`) and a delete route whose id has to come from
        # somewhere.
        assert await has_permission(session, su_id, "agent.release.read")
        assert await has_permission(session, su_id, "agent.release.manage")


async def test_no_role_other_than_super_admin_carries_a_platform_operator_code(
    admin_engine: AsyncEngine,
) -> None:
    """Not "org-admin does not hold it" -- NO role does, global or org-scoped.

    Migration 0068 revokes the codes from every role rather than from
    `org-admin` by name, because a deployment that has already run 0057 may
    have had an administrator clone `org-admin` into an org-local role, or
    tick the box on a bespoke one. Revoking the built-in grant and leaving a
    copy behind would fix the finding on a fresh database and nowhere else.

    SCOPED TO THE GLOBAL ROLES, and that is a property of the harness rather
    than a weakening of the claim: `tests/conftest.py`'s `pg_cluster` is one
    database for the entire run, and `tests/test_error_api.py::_holder`
    deliberately seeds an ORG-SCOPED role carrying these codes through the
    admin engine to prove the ROUTES are gated on them. Asserting over every
    role in the database would therefore pass or fail on test ordering. The
    seeded grants -- the thing this test is about -- are all global."""
    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(
                select(Role.name, Permission.code)
                .join(RolePermission, RolePermission.role_id == Role.id)
                .join(Permission, Permission.id == RolePermission.permission_id)
                .where(Permission.code.in_(sorted(PLATFORM_OPERATOR_CODES)))
                .where(Role.org_id.is_(None))
            )
        ).all()
    holders = {(name, code) for name, code in rows}
    assert holders == {
        ("super-admin", code) for code in PLATFORM_OPERATOR_CODES
    }, sorted(holders)


async def test_user_without_roles_has_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    slug = f"rbac-none-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@x.test")
        session.add(user)
        await session.commit()
        org_id, user_id = org.id, user.id
    async with org_session(org_id, engine=app_engine) as session:
        assert await user_permission_codes(session, user_id) == set()
        assert not await has_permission(session, user_id, "devices.read")


async def test_client_scoped_role_limits_to_that_client(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id, client_id = await _seed_user_with_role(
        admin_engine, "technician", "rbac-scope", client=True
    )
    assert client_id is not None
    async with org_session(org_id, engine=app_engine) as session:
        # granted within the scoped client
        assert await has_permission(session, user_id, "jobs.dispatch", client_id=client_id)
        # not granted for every client, nor for a different client
        assert not await has_permission(session, user_id, "jobs.dispatch")
        assert not await has_permission(session, user_id, "jobs.dispatch", client_id=uuid4())


async def test_docs_permission_grants(admin_engine, app_engine) -> None:
    org_id, tech_id, _ = await _seed_user_with_role(admin_engine, "technician", "rbac-docs-t")
    async with org_session(org_id, engine=app_engine) as session:
        assert await has_permission(session, tech_id, "docs.read")
        assert await has_permission(session, tech_id, "docs.manage")

    org_id, ro_id, _ = await _seed_user_with_role(admin_engine, "read-only", "rbac-docs-r")
    async with org_session(org_id, engine=app_engine) as session:
        assert await has_permission(session, ro_id, "docs.read")
        assert not await has_permission(session, ro_id, "docs.manage")


async def test_remote_permission_grants(admin_engine, app_engine) -> None:
    org_id, tech_id, _ = await _seed_user_with_role(admin_engine, "technician", "rbac-remote-t")
    async with org_session(org_id, engine=app_engine) as session:
        assert await has_permission(session, tech_id, "remote.control")
        assert not await has_permission(session, tech_id, "remote.recordings.read")

    org_id, aud_id, _ = await _seed_user_with_role(admin_engine, "auditor", "rbac-remote-a")
    async with org_session(org_id, engine=app_engine) as session:
        assert await has_permission(session, aud_id, "remote.recordings.read")
        assert not await has_permission(session, aud_id, "remote.control")


# ---------------------------------------------------------------------------
# route_permission canary (whole-branch review, ALSO item on `require_permission`'s
# closure-walking). `route_permission` recovers a route's permission code by
# reflecting on `require_permission`'s closure (see the warning comment at its
# definition site, app/api/deps.py). That reflection is correct today, but a
# refactor of `require_permission`'s SHAPE (renaming the inner `dependency`
# function, renaming/removing `code` as a free variable) would make
# `route_permission` silently return `None` for EVERY route in the codebase --
# a completeness test in some OTHER module (e.g.
# `test_every_notification_route_requires_a_permission`) would still catch
# it, but the failure would surface there, pointing at that module's router,
# not at the actual break in `app/api/deps.py`. This test builds a throwaway
# route with `require_permission` directly, so a break here fails AT THIS
# FILE, naming the real cause.
# ---------------------------------------------------------------------------


def test_route_permission_recovers_the_code_from_a_throwaway_route() -> None:
    router = APIRouter()

    @router.get("/canary")
    async def _canary_route(ctx=Depends(require_permission("audit.read"))):  # type: ignore[no-untyped-def]  # noqa: B008
        return {}

    (route,) = router.routes
    assert route_permission(route) == "audit.read"
