"""Every write to Novieri-global configuration needs a scope of every client.

MSP clients spec §2.2 ("un técnico limitado puede ver la configuración global,
pero no cambiarla") and §2.5 ("lo que queda global"). A row that is not one
client's data -- organization settings, notification channels and rules, the
agent release catalogue and its rollouts, AI settings, the shared libraries
(scripts, packages, health profiles), ticket queues and categories, SLA
calendars, technicians and roles, incoming mail -- serves every client, so a
technician whose grants are per client, or whose request X-Client-Id narrowed,
may read it but not change it. Those routes depend on
`app.api.deps.require_permission_on_every_client`.

THE LIST BELOW IS THE DECISION, AND IT IS DECLARED ONCE. `GLOBAL_WRITES` names
every such route; the router is checked against it in both directions, so a
route gated without being listed fails, and so does a listed route that lost
its gate. A NEW write route under a code that already gates a global write
fails too until it is classified: listed in `GLOBAL_WRITES`, or in
`NOT_GLOBAL` with the reason it is one client's data.

THE SAME RULE FOR A FEW READS (`GLOBAL_READS`): a G table that RLS cannot
scope by client, whose rows nonetheless DESCRIBE clients -- the audit log
(every client's events, addresses and fingerprints), the incoming-mail log
(senders and parsed bodies of every client's mail), the notification
delivery log (recipient addresses). Reading one of those with a per-client
grant would be reading every client's data, so these reads need every client
too until phase 2 gives those rows a client. Checked against the router the
same way, in both directions.

NOT HERE, deliberately: the O-class rows that may be global OR one client's
(patch/backup/remote policies, health profile assignments, SLA policies,
credentials, the documentation family, backup offsite targets and recovery
keys, change gates, the software ignore list). Task 6's O-table RLS governs
them -- a row with no client is writable only under an every-client scope.
"""

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rbac import _walk_dependant, route_permission
from app.core.router_inspect import flatten
from app.models import Client, Permission, Role, RolePermission, User, UserRole

EVERY_CLIENT = "only a technician with access to every client can do this"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
GATE = "require_permission_on_every_client.<locals>.on_every_client"

#: (method, path) -> the code that gates it. THE single declared list.
GLOBAL_WRITES: dict[tuple[str, str], str] = {
    # organization settings (`organizations`, the realm's mail settings)
    ("PATCH", "/api/v1/orgs/current"): "orgs.manage",
    # the list of clients itself (MSP clients spec §2.1): creating and
    # archiving one need `clients.manage` AND a scope of every client
    ("POST", "/api/v1/clients"): "clients.manage",
    ("POST", "/api/v1/clients/{client_id}/archive"): "clients.manage",
    ("PUT", "/api/v1/settings/email"): "orgs.manage",
    ("POST", "/api/v1/settings/email/test"): "orgs.manage",
    # the platform error monitor (`platform_error_group`)
    ("POST", "/api/v1/platform/errors/{group_id}/resolve"): "platform.errors.write",
    ("POST", "/api/v1/platform/errors/{group_id}/unresolve"): "platform.errors.write",
    # technicians and roles
    ("POST", "/api/v1/users"): "users.manage",
    ("DELETE", "/api/v1/users/{user_id}"): "users.manage",
    ("POST", "/api/v1/users/{user_id}/disable"): "users.manage",
    ("POST", "/api/v1/users/{user_id}/enable"): "users.manage",
    ("POST", "/api/v1/users/{user_id}/resend-enrolment"): "users.manage",
    ("POST", "/api/v1/users/{user_id}/reset-password"): "users.manage",
    ("POST", "/api/v1/users/{user_id}/roles"): "users.manage",
    ("DELETE", "/api/v1/users/{user_id}/roles/{assignment_id}"): "users.manage",
    ("POST", "/api/v1/roles"): "roles.manage",
    ("PATCH", "/api/v1/roles/{role_id}"): "roles.manage",
    ("DELETE", "/api/v1/roles/{role_id}"): "roles.manage",
    ("PUT", "/api/v1/roles/{role_id}/permissions"): "roles.manage",
    # the agent release catalogue (`agent_releases`, shared by every tenant)
    ("POST", "/api/v1/agent/releases"): "agent.release.manage",
    ("POST", "/api/v1/agent/releases/{release_id}/artifact"): "agent.release.manage",
    ("DELETE", "/api/v1/agent/releases/{release_id}"): "agent.release.manage",
    ("DELETE", "/api/v1/agent/releases/versions/{version}"): "agent.release.manage",
    ("POST", "/api/v1/agent/releases/versions/{version}/promote"): "agent.release.manage",
    ("PUT", "/api/v1/agent/releases/{release_id}/release-signature"): "agent.release.manage",
    # agent update rollouts (`agent_update_rollouts`: one per organization)
    ("POST", "/api/v1/agent/update-rollouts"): "agent.update.rollout",
    ("POST", "/api/v1/agent/update-rollouts/preview"): "agent.update.rollout",
    ("POST", "/api/v1/agent/update-rollouts/{rollout_id}/start"): "agent.update.rollout",
    ("POST", "/api/v1/agent/update-rollouts/{rollout_id}/advance"): "agent.update.rollout",
    ("POST", "/api/v1/agent/update-rollouts/{rollout_id}/pause"): "agent.update.rollout",
    ("POST", "/api/v1/agent/update-rollouts/{rollout_id}/resume"): "agent.update.rollout",
    ("POST", "/api/v1/agent/update-rollouts/{rollout_id}/cancel"): "agent.update.rollout",
    (
        "POST",
        "/api/v1/agent/update-rollouts/{rollout_id}/rings/{ring_id}/close",
    ): "agent.update.rollout",
    # AI settings and the autonomy ceiling
    ("PUT", "/api/v1/ai/settings"): "ai.admin",
    ("PUT", "/api/v1/ai/autonomy"): "ai.admin",
    # approved actions (`approved_action`, its script exemptions)
    ("POST", "/api/v1/approved-actions"): "health.action.admin",
    ("PATCH", "/api/v1/approved-actions/{action_id}"): "health.action.admin",
    ("DELETE", "/api/v1/approved-actions/{action_id}"): "health.action.admin",
    ("POST", "/api/v1/approved-actions/{action_id}/exempt-script"): "health.action.admin",
    ("DELETE", "/api/v1/approved-actions/{action_id}/exempt-script"): "health.action.admin",
    # health profiles and checks (a library) and the org's health settings
    ("POST", "/api/v1/health/profiles"): "health.admin",
    ("PATCH", "/api/v1/health/profiles/{profile_id}"): "health.admin",
    ("DELETE", "/api/v1/health/profiles/{profile_id}"): "health.admin",
    ("POST", "/api/v1/health/profiles/{profile_id}/clone"): "health.admin",
    ("PUT", "/api/v1/health/profiles/{profile_id}/checks"): "health.admin",
    ("PUT", "/api/v1/health/settings"): "health.admin",
    # notifications: channels, rules, queue membership, the webhook allowlist
    ("POST", "/api/v1/notifications/channels"): "notification.admin",
    ("PATCH", "/api/v1/notifications/channels/{channel_id}"): "notification.admin",
    ("DELETE", "/api/v1/notifications/channels/{channel_id}"): "notification.admin",
    ("POST", "/api/v1/notifications/channels/{channel_id}/test"): "notification.admin",
    ("POST", "/api/v1/notifications/rules"): "notification.admin",
    ("PATCH", "/api/v1/notifications/rules/{rule_id}"): "notification.admin",
    ("DELETE", "/api/v1/notifications/rules/{rule_id}"): "notification.admin",
    ("PUT", "/api/v1/notifications/queues/{queue_id}/members/{user_id}"): "notification.admin",
    (
        "DELETE",
        "/api/v1/notifications/queues/{queue_id}/members/{user_id}",
    ): "notification.admin",
    ("POST", "/api/v1/notifications/allowlist"): "notification.admin",
    ("DELETE", "/api/v1/notifications/allowlist/{entry_id}"): "notification.admin",
    # service desk: queues, categories, canned responses
    ("POST", "/api/v1/service-desk/queues"): "ticket.admin",
    ("PATCH", "/api/v1/service-desk/queues/{queue_id}"): "ticket.admin",
    ("POST", "/api/v1/service-desk/categories"): "ticket.admin",
    ("PATCH", "/api/v1/service-desk/categories/{category_id}"): "ticket.admin",
    ("POST", "/api/v1/service-desk/canned-responses"): "ticket.admin",
    ("PATCH", "/api/v1/service-desk/canned-responses/{response_id}"): "ticket.admin",
    ("DELETE", "/api/v1/service-desk/canned-responses/{response_id}"): "ticket.admin",
    # SLA calendars and the org's SLA settings
    ("POST", "/api/v1/sla/calendars"): "sla.admin",
    ("PATCH", "/api/v1/sla/calendars/{calendar_id}"): "sla.admin",
    ("DELETE", "/api/v1/sla/calendars/{calendar_id}"): "sla.admin",
    ("PUT", "/api/v1/sla/calendars/{calendar_id}/windows"): "sla.admin",
    ("POST", "/api/v1/sla/calendars/{calendar_id}/holidays"): "sla.admin",
    ("DELETE", "/api/v1/sla/calendars/{calendar_id}/holidays/{holiday_id}"): "sla.admin",
    ("PUT", "/api/v1/sla/settings"): "sla.admin",
    # the software library: packages and scripts
    ("POST", "/api/v1/deploy/packages"): "deploy.package.manage",
    ("POST", "/api/v1/deploy/packages/{package_id}/artifact"): "deploy.package.manage",
    ("DELETE", "/api/v1/deploy/packages/{package_id}"): "deploy.package.manage",
    ("POST", "/api/v1/deploy/scripts"): "deploy.script.manage",
    ("PUT", "/api/v1/deploy/scripts/{script_id}"): "deploy.script.manage",
    # the license catalogue: vendors, products and their match rules
    ("POST", "/api/v1/vendors"): "licenses.manage",
    ("PATCH", "/api/v1/vendors/{vendor_id}"): "licenses.manage",
    ("POST", "/api/v1/licenses/products"): "licenses.manage",
    ("PATCH", "/api/v1/licenses/products/{product_id}"): "licenses.manage",
    ("POST", "/api/v1/licenses/review-queue/map"): "licenses.manage",
    # exchange rates (`fx_rate`)
    ("POST", "/api/v1/budget/fx-rates"): "budget.manage",
    ("PATCH", "/api/v1/budget/fx-rates/{fx_rate_id}"): "budget.manage",
    # OS images and provisioning profiles (module hidden; still global)
    ("POST", "/api/v1/provisioning/images"): "provisioning.manage",
    ("POST", "/api/v1/provisioning/profiles"): "provisioning.manage",
    ("PUT", "/api/v1/provisioning/profiles/{profile_id}"): "provisioning.manage",
    # incoming mail: mailboxes, the OAuth app and their credentials
    ("POST", "/api/v1/email-intake/mailboxes"): "email_intake.admin",
    ("PATCH", "/api/v1/email-intake/mailboxes/{mailbox_id}"): "email_intake.admin",
    ("DELETE", "/api/v1/email-intake/mailboxes/{mailbox_id}"): "email_intake.admin",
    ("POST", "/api/v1/email-intake/mailboxes/{mailbox_id}/test"): "email_intake.admin",
    ("POST", "/api/v1/email-intake/mailboxes/{mailbox_id}/oauth/start"): "email_intake.admin",
    (
        "POST",
        "/api/v1/email-intake/mailboxes/{mailbox_id}/oauth/disconnect",
    ): "email_intake.admin",
    ("POST", "/api/v1/email-intake/oauth/complete"): "email_intake.admin",
    ("POST", "/api/v1/email-intake/oauth/abandon"): "email_intake.admin",
    ("PUT", "/api/v1/email-intake/oauth-app"): "email_intake.admin",
}

#: (method, path) -> code: READS of a table that holds every client's data
#: and that RLS does not scope by client (see the module docstring).
GLOBAL_READS: dict[tuple[str, str], str] = {
    # the audit log (`audit_events`, G): every client's events
    ("GET", "/api/v1/audit"): "audit.read",
    ("GET", "/api/v1/audit/verify"): "audit.read",
    # the incoming-mail log (senders, parsed bodies, the quarantine count)
    ("GET", "/api/v1/email-intake/messages"): "email_intake.read",
    # the notification delivery log (`notification_deliveries`, G): recipients
    ("GET", "/api/v1/notifications/deliveries"): "notification.read",
}

#: Write routes under a code that ALSO gates a global write, which are not
#: global themselves -- and why. A new route under such a code fails the
#: router check until it is listed here or in `GLOBAL_WRITES`.
NOT_GLOBAL: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/health/assignments"): "O: health_profile_assignment",
    ("DELETE", "/api/v1/health/assignments/{assignment_id}"): "O: health_profile_assignment",
    ("POST", "/api/v1/sla/policies"): "O: sla_policy",
    ("PATCH", "/api/v1/sla/policies/{policy_id}"): "O: sla_policy",
    ("DELETE", "/api/v1/sla/policies/{policy_id}"): "O: sla_policy",
    ("POST", "/api/v1/licenses"): "C: license",
    ("PATCH", "/api/v1/licenses/{license_id}"): "C: license",
    ("DELETE", "/api/v1/licenses/{license_id}"): "C: license",
    ("POST", "/api/v1/licenses/{license_id}/allocations"): "H: license_allocation",
    ("POST", "/api/v1/allocations/{allocation_id}/release"): "H: license_allocation",
    ("POST", "/api/v1/licenses/review-queue/ignore"): "O: software_ignore",
    ("POST", "/api/v1/budget/budgets"): "C: budget",
    ("PATCH", "/api/v1/budget/budgets/{budget_id}"): "C: budget",
    ("POST", "/api/v1/budget/budgets/{budget_id}/close"): "C: budget",
    ("PUT", "/api/v1/budget/budgets/{budget_id}/lines"): "H: budget_line",
    ("DELETE", "/api/v1/budget/budgets/{budget_id}/lines/{line_id}"): "H: budget_line",
    ("POST", "/api/v1/budget/categories"): "C: expense_category",
    ("PATCH", "/api/v1/budget/categories/{category_id}"): "C: expense_category",
    ("POST", "/api/v1/budget/cost-centers"): "C: cost_center",
    ("PATCH", "/api/v1/budget/cost-centers/{cost_center_id}"): "C: cost_center",
    ("PUT", "/api/v1/budget/settings"): "C: budget_settings",
    # Spec §2.1: editing a client and its contacts needs `clients.manage` ON
    # THAT CLIENT -- one client's own row, which RLS confines to the scope.
    ("PATCH", "/api/v1/clients/{client_id}"): "clients: that client's own row",
    ("POST", "/api/v1/clients/{client_id}/contacts"): "O: users, a contact of that client",
    ("PATCH", "/api/v1/contacts/{contact_id}"): "O: users, a contact of that client",
    # `mcp.use` gates a person's own revocation; revoking an app for EVERYONE
    # is global and is refused inside the handler (tested below).
    ("POST", "/api/v1/mcp/connections/{client_id}/revoke"): "personal, or global by body",
}


def _write_routes(app: Any) -> dict[tuple[str, str], Any]:
    return {
        (method, path): route
        for path, route in flatten(app.router)
        for method in (getattr(route, "methods", None) or set()) & WRITE_METHODS
    }


def _read_routes(app: Any) -> dict[tuple[str, str], Any]:
    return {
        ("GET", path): route
        for path, route in flatten(app.router)
        if "GET" in (getattr(route, "methods", None) or set())
    }


def _gated_on_every_client(route: Any) -> bool:
    dependant = getattr(route, "dependant", None)
    return dependant is not None and any(
        getattr(getattr(dep, "call", None), "__qualname__", "") == GATE
        for dep in _walk_dependant(dependant)
    )


def test_the_declared_list_and_the_router_agree(api_env: Any) -> None:
    routes = _write_routes(api_env.app)
    missing = sorted(set(GLOBAL_WRITES) - set(routes))
    assert not missing, f"listed global writes that no route serves: {missing}"
    stale = sorted(set(NOT_GLOBAL) - set(routes))
    assert not stale, f"NOT_GLOBAL names routes that no longer exist: {stale}"

    wrong = []
    for key, route in sorted(routes.items()):
        gated = _gated_on_every_client(route)
        if key in GLOBAL_WRITES:
            if not gated or route_permission(route) != GLOBAL_WRITES[key]:
                wrong.append(f"{key}: listed as global but not gated on every client")
        elif gated:
            wrong.append(f"{key}: gated on every client but not in GLOBAL_WRITES")
    assert not wrong, "\n".join(wrong)


def test_the_declared_reads_and_the_router_agree(api_env: Any) -> None:
    """`GLOBAL_READS` against the router, both directions: a listed read that
    lost its gate fails, and so does a read gated without being listed."""
    routes = _read_routes(api_env.app)
    missing = sorted(set(GLOBAL_READS) - set(routes))
    assert not missing, f"listed global reads that no route serves: {missing}"
    wrong = []
    for key, route in sorted(routes.items()):
        gated = _gated_on_every_client(route)
        if key in GLOBAL_READS:
            if not gated or route_permission(route) != GLOBAL_READS[key]:
                wrong.append(f"{key}: listed as a global read but not gated on every client")
        elif gated:
            wrong.append(f"{key}: gated on every client but not in GLOBAL_READS")
    assert not wrong, "\n".join(wrong)


def test_a_new_route_under_a_global_code_must_be_classified(api_env: Any) -> None:
    """A code that gates one global write is a module that HAS global
    configuration: every other write route under it must say why it is not."""
    codes = set(GLOBAL_WRITES.values())
    unclassified = sorted(
        key
        for key, route in _write_routes(api_env.app).items()
        if route_permission(route) in codes and key not in GLOBAL_WRITES and key not in NOT_GLOBAL
    )
    assert not unclassified, (
        "write routes under a code that gates global configuration, classified "
        f"neither way (add to GLOBAL_WRITES or NOT_GLOBAL): {unclassified}"
    )


async def _actor(env: Any, org_id: UUID, client_id: UUID | None) -> dict[str, str]:
    """A person holding EVERY permission code -- the platform-operator ones
    included, so every listed route's own gate passes -- on one client, or on
    every client when `client_id` is None."""
    sub = await env.create_user(org_id)
    async with AsyncSession(env.admin_engine) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
        role = Role(org_id=org_id, name=f"everything-{uuid4().hex[:8]}")
        session.add(role)
        await session.flush()
        for permission_id in (await session.execute(select(Permission.id))).scalars():
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
        session.add(UserRole(org_id=org_id, user_id=user_id, role_id=role.id, client_id=client_id))
        await session.commit()
    return await env.headers(org_id, sub)


def _url(path: str) -> str:
    while "{" in path:
        path = path[: path.index("{")] + str(uuid4()) + path[path.index("}") + 1 :]
    return path


def _refused_for_scope(response: Any) -> bool:
    if response.status_code != 403:
        return False
    detail: Any = response.json().get("detail")
    return bool(detail == EVERY_CLIENT)


async def test_every_global_write_needs_a_scope_of_every_client(api_env: Any) -> None:
    """Refused for a per-client holder of the code and for an every-client
    holder who narrowed with X-Client-Id; NOT refused for the same every-client
    holder un-narrowed -- which is what makes the refusal about the scope."""
    env = api_env
    org_id = await env.create_org("global-writes")
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as session:
        client = Client(org_id=org_id, name="X", kind="customer")
        session.add(client)
        await session.commit()
    per_client = await _actor(env, org_id, client.id)
    every = await _actor(env, org_id, None)
    narrowed = {**every, "X-Client-Id": str(client.id)}

    failures = []
    # A route that is let through runs on an empty body and a random id, and
    # may fail inside (an artefact store this test has none of): a 500 is
    # "not refused", so app exceptions become responses instead of raising.
    transport = httpx.ASGITransport(app=env.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as http:
        for method, path in sorted(GLOBAL_WRITES):
            url = _url(path)
            for who, headers in (("per-client", per_client), ("narrowed", narrowed)):
                response = await http.request(method, url, headers=headers, json={})
                if not _refused_for_scope(response):
                    failures.append(f"{method} {path}: {who} got {response.status_code}")
            response = await http.request(method, url, headers=every, json={})
            if _refused_for_scope(response):
                failures.append(f"{method} {path} refused for an every-client holder")
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("scope", ["me", "everyone"])
async def test_revoking_an_ai_app_for_everyone_needs_every_client(
    api_env: Any, scope: str
) -> None:
    """The one mixed route: revoking an AI app for yourself is personal,
    revoking it for EVERYONE writes the organization's revocation list."""
    from sqlalchemy import update

    from app.models import Organization

    env = api_env
    env.settings.public_console_url = "https://itms.example.com"
    org_id = await env.create_org("mcp-revoke")
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(Organization).where(Organization.id == org_id).values(
                settings={"mcp_enabled": True}
            )
        )
        await session.commit()
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as session:
        client = Client(org_id=org_id, name="X", kind="customer")
        session.add(client)
        await session.commit()
    every = await _actor(env, org_id, None)
    narrowed = {**every, "X-Client-Id": str(client.id)}
    url = "/api/v1/mcp/connections/some-ai-app/revoke"

    response = await env.client.post(url, headers=narrowed, json={"scope": scope})
    if scope == "everyone":
        assert _refused_for_scope(response), response.text
    else:
        assert not _refused_for_scope(response), response.text


async def test_every_global_read_needs_a_scope_of_every_client(api_env: Any) -> None:
    """The reads of every client's data: refused for a per-client holder of
    the code and for an every-client holder who narrowed with X-Client-Id;
    answered (200) for the same every-client holder un-narrowed."""
    env = api_env
    org_id = await env.create_org("global-reads")
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as session:
        client = Client(org_id=org_id, name="X", kind="customer")
        session.add(client)
        await session.commit()
    per_client = await _actor(env, org_id, client.id)
    every = await _actor(env, org_id, None)
    narrowed = {**every, "X-Client-Id": str(client.id)}

    failures = []
    for _method, path in sorted(GLOBAL_READS):
        for who, headers in (("per-client", per_client), ("narrowed", narrowed)):
            response = await env.client.get(path, headers=headers)
            if not _refused_for_scope(response):
                failures.append(f"GET {path}: {who} got {response.status_code}")
        response = await env.client.get(path, headers=every)
        if response.status_code != 200:
            failures.append(f"GET {path}: every-client holder got {response.status_code}")
    assert not failures, "\n".join(failures)


async def test_the_built_in_auditor_on_one_client_cannot_read_the_audit_log(
    api_env: Any,
) -> None:
    """The built-in `auditor` role holds `audit.read`. Granted on client X
    only, it would otherwise read every client's audit events through
    `GET /audit` (a G table RLS does not scope). Granted on every client, it
    reads them as before."""
    env = api_env
    org_id = await env.create_org("auditor-x")
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as session:
        client = Client(org_id=org_id, name="X", kind="customer")
        session.add(client)
        auditor = (
            await session.execute(select(Role).where(Role.name == "auditor", Role.org_id.is_(None)))
        ).scalar_one()
        await session.commit()

    async def _auditor(client_id: UUID | None) -> dict[str, str]:
        sub = await env.create_user(org_id)
        async with AsyncSession(env.admin_engine) as session:
            user_id = (
                await session.execute(select(User.id).where(User.external_subject == sub))
            ).scalar_one()
            session.add(
                UserRole(org_id=org_id, user_id=user_id, role_id=auditor.id, client_id=client_id)
            )
            await session.commit()
        return await env.headers(org_id, sub)

    on_x = await _auditor(client.id)
    everywhere = await _auditor(None)
    for path in ("/api/v1/audit", "/api/v1/audit/verify"):
        refused = await env.client.get(path, headers=on_x)
        assert _refused_for_scope(refused), (path, refused.status_code, refused.text)
        answered = await env.client.get(path, headers=everywhere)
        assert answered.status_code == 200, (path, answered.text)
