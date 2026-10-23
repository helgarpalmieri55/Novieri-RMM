"""Server-side RBAC. Authorization is evaluated here, inside the tenant session,
on every privileged endpoint and job dispatch — the frontend never gates security.

Grant semantics (MSP clients spec §2.1): a user_roles row with client_id NULL
grants the role's permissions for every client, including ones created later; a
row with a client_id grants them only for that client.
"""

from collections.abc import Iterator
from typing import Any
from uuid import UUID

from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Permission, Role, RolePermission, UserRole

# code -> human description; seeded by migration 0003. Format: "<module>.<action>".
PERMISSIONS: dict[str, str] = {
    "devices.read": "View devices and device groups",
    "devices.manage": "Create, edit, group, and decommission devices",
    # Technician tier (spec 2026-09-28 §3): deciding whether a machine that
    # joined through the install link belongs here.
    "devices.review": (
        "Approve or reject a computer that joined through the install link; "
        "rejecting it uninstalls the agent and revokes its certificate"
    ),
    "assets.manage": "Create and edit assets, including records with no agent",
    "jobs.read": "View jobs, job logs, and results",
    "jobs.dispatch": "Dispatch jobs to devices",
    "enrollment.manage": "Issue and revoke enrollment tokens; revoke agents",
    # The install link (spec 2026-09-28 §3). Admin tier: switching it on is
    # opening a door every machine on the listed networks can walk through.
    "enrollment.link.manage": (
        "Switch the install link on or off, choose the networks it answers, "
        "set its hourly limit and automatic switch-off, and regenerate it"
    ),
    # The MCP server. Admin tier:
    # an AI app connected under this code acts as the person, with every
    # permission that person holds, so who may connect one is an admin decision.
    "mcp.use": "Connect an AI app to the platform over MCP and act through it as yourself",
    "audit.read": "Read and verify the audit log",
    "platform.status.read": "View platform health, dependency status and history",
    "credentials.read": "See credential metadata",
    "credentials.checkout": "Decrypt and retrieve stored secrets (audited)",
    "credentials.manage": "Create and delete stored credentials",
    "policies.manage": "Create and edit policies",
    "orgs.manage": "Manage organization settings, users, and roles",
    # Access control administration (spec 2026-08-19 §1). DELIBERATELY NOT
    # `orgs.manage`: that code's description has always promised "settings,
    # users, and roles", but what it gates is the org name/settings PATCH and
    # remote-tunnel authorization. Attaching identity lifecycle to it would
    # upgrade every current holder from "can rename the org" to "can mint a
    # fleet admin" -- a privilege escalation delivered by a description string,
    # in a migration nobody would look twice at.
    "roles.read": "View roles and the permission matrix",
    "roles.manage": "Create and edit roles, and set what permissions they carry",
    "users.read": "View users, their roles, and their effective permissions",
    "users.manage": "Create, disable, delete users and assign their roles",
    "docs.read": (
        "View documentation articles and records, and download or view a "
        "stored document's bytes (audited on every call). Download reuses this "
        "code rather than getting its own: on the existing grants a separate "
        "download permission would leave an auditor able to see that a signed "
        "contract EXISTS and unable to read it, which is the opposite of an "
        "auditor. The control on reading a document is the audit event, not a "
        "second gate (spec-attachments.md §9)."
    ),
    "docs.manage": (
        "Create, edit, and archive documentation, including creating a "
        "document, uploading a new file version, and linking it to devices, "
        "records or credentials"
    ),
    "docs.document.purge": (
        "IRREVERSIBLY destroy a stored document version's bytes. The row "
        "survives as a tombstone and the destruction is audited, but the file "
        "is gone. Deliberately NOT folded into `docs.manage` -- editing the "
        "library and destroying evidence are different acts with different "
        "blast radii, the same reasoning `health.remediate` records for not "
        "folding into `health.admin`. A technician can archive; only an admin "
        "can destroy. Super-admin and org-admin only."
    ),
    "remote.control": "Start and operate remote-control sessions on devices",
    "remote.recordings.read": "View/replay remote-control session recordings",
    "remote.policy.manage": "Create and edit remote-control policies",
    "remote.unattended": (
        "Start unattended screen sessions (no consent prompt) where the device's "
        "remote policy also allows them"
    ),
    "remote.privacy_blank": "Blank the physical console during a screen session",
    "patch.read": "View patch catalog, device patch status, and patch runs",
    "patch.scan": "Trigger patch scans on devices",
    "patch.approve": "Approve or revoke patches for deployment rings",
    "patch.apply": "Apply approved patches to devices",
    "patch.policy.manage": "Create and edit patch policies",
    "deploy.read": "View packages, deployments, results, scripts, and script runs",
    "deploy.package.manage": "Create/edit/delete packages and upload artifacts",
    "deploy.create": "Create a deployment (targeting + scheduling)",
    "deploy.execute": "Dispatch/execute a deployment now",
    "deploy.script.manage": "Author/edit scripts",
    "deploy.script.run": "Execute a script",
    "deploy.script.schedule": "Create and edit scheduled scripts",
    "backup.read": "View backup repositories, policies, points, and status",
    "backup.policy.manage": "Create and edit backup repositories and policies",
    "backup.run": "Trigger an on-demand backup",
    "backup.restore": "Browse a backup point and restore/verify it",
    "backup.offsite.manage": "Set up off-site backup storage and turn off-site copies on or off",
    "backup.recovery.manage": "Add and confirm the backup recovery key",
    "provisioning.read": "View OS images, provisioning profiles, and runs",
    "provisioning.manage": "Create and edit OS images and provisioning profiles",
    "provisioning.run": "Start a provisioning run against a target",
    "change.read": "View change requests, approvals, links, calendar, and evidence",
    "change.request": "Create, edit, and submit change requests",
    "change.approve": "Approve or reject a change request",
    "change.manage": "Manage advisory links between changes and actions",
    "change.gate.manage": (
        "Choose which device groups need an approved change before patching or deploying"
    ),
    "bcp.read": "View BCP/DR plans, tests, findings, and reports",
    "bcp.manage": "Create and edit BCP/DR plans, record tests, and manage findings",
    "bcp.approve": "Sign off on (review) a BCP/DR plan",
    "licenses.read": "View license catalog, licenses, allocations, compliance, and reports",
    "licenses.manage": (
        "Create and edit vendors, products, licenses, allocations, and reconciliation"
    ),
    "licenses.keys.read": "Reveal a decrypted license key (audited)",
    "subscriptions.read": "View subscription registry, seats, and reports",
    "subscriptions.manage": (
        "Create, edit, cancel, and renew subscriptions; assign and release seats"
    ),
    "people.read": "View people, what they hold, and their starter and leaver checklists",
    "people.manage": (
        "Add and edit people, link their sign-in and computers, and run new "
        "starter checklists"
    ),
    "people.offboard": "Start, tick and complete leaver checklists",
    "budget.read": "View budgets, expenses, dashboards, and exports",
    "budget.manage": (
        "Create and edit budgets, lines, categories, cost centers, FX rates, "
        "and the org base currency"
    ),
    "expense.record": "Create and edit expenses, and run materialization",
    "expense.confirm": "Confirm an expected expense, or void an expense",
    "ticket.read": "View tickets, comments, attachments, queues, and categories",
    "ticket.write": (
        "Create tickets, comment, edit ticket fields, and upload attachments"
    ),
    "ticket.assign": "Assign or reassign a ticket, and move it between queues",
    "ticket.approve": "Approve or reject a service request",
    "ticket.admin": "Manage ticket queues and categories",
    "notification.read": "View notification channels, rules, queue membership, and delivery status",
    "notification.admin": (
        "Manage notification channels, rules, the webhook allowlist, and queue "
        "membership, and run test sends. HIGH PRIVILEGE: this grant controls "
        "where control-plane data is sent and which internal hosts the server "
        "will connect to (spec §8) -- admin tier, not technician tier."
    ),
    "portal.ticket.read": "Portal: view your OWN tickets and their public history",
    "portal.ticket.write": (
        "Portal: file a ticket, comment on your own, attach to your own. "
        "END-USER TIER: deliberately NOT `ticket.write`, which is org-wide and "
        "gates staff routes (spec §4)."
    ),
    "email_intake.read": "View mailbox config, poll status, and the inbound message log",
    "email_intake.admin": (
        "Create, modify and delete an inbound mailbox and its credential. "
        "High-privilege: it sets which mailbox the platform authenticates to "
        "and which category inbound tickets land in."
    ),
    "sla.read": "View SLA calendars, policies, and a ticket's SLA state",
    "sla.admin": (
        "Create/modify/delete SLA calendars and policies, and edit auto-close "
        "settings. HIGH PRIVILEGE: it sets what the organisation has promised "
        "its customers and can silence breach escalation entirely -- admin "
        "tier, never technician."
    ),
    "health.read": "View health profiles, checks, assignments, alerts, and device health",
    "health.alert.manage": (
        "Acknowledge, mute, and manually close health alerts, and ask a device "
        "to sample its checks now. Technician tier: it changes no configuration "
        "and runs no code on an endpoint, but `check-now` DOES mint a signed "
        "job, so it is not a read code -- see `health/api.py` for why that "
        "route moved off `health.read`."
    ),
    "health.admin": (
        "Create/modify/delete health profiles, checks, assignments, and org "
        "settings. HIGH PRIVILEGE: it decides what the entire fleet alarms on "
        "and can silence every alert in the organisation by deleting one "
        "assignment -- admin tier, never technician."
    ),
    "health.remediate": (
        "Dispatch a remediation, or approve/refuse one. HIGH PRIVILEGE: it puts "
        "a signed job on an endpoint that executes as SYSTEM/root. Deliberately "
        "NOT bundled into `health.admin` -- configuring what alarms and "
        "executing code on endpoints are different acts and different blast "
        "radii."
    ),
    "health.action.admin": (
        "Manage the approved-action registry, set the autonomy dial, and grant a "
        "script destructive-exemption. HIGH PRIVILEGE, and the highest grant in "
        "this module: it decides what MAY be run automatically as SYSTEM/root "
        "across the fleet -- super-admin/org-admin only, never technician."
    ),
    "ipam.read": "View subnets, addresses, drift, utilisation, sweep runs, and IPAM exports",
    "ipam.assign": (
        "Assign, reserve and release addresses; acknowledge and suppress drift"
    ),
    "ipam.manage": (
        "Create and edit VRFs, sites, VLANs, subnets and ranges, and run IPAM "
        "imports. The address plan is a plan of RECORD -- admin tier."
    ),
    "ipam.sweep.run": (
        "Dispatch a `net.sweep` job now, WITHIN already-authorized scope. "
        "Deliberately separate from `ipam.scope.manage`: running a sweep inside "
        "an authorized scope is routine technician work; deciding what may be "
        "scoped at all is not."
    ),
    "ipam.sweep.manage": (
        "Enable sweeps on a subnet, designate the sweeping agent, and set the "
        "sweep interval"
    ),
    "ipam.scope.manage": (
        "Grant or revoke authorization for the fleet to probe a prefix. HIGH "
        "PRIVILEGE: this grant decides where this platform emits network "
        "traffic -- the same class of grant as `notification.admin`, which "
        "decides where it emits webhooks. Admin tier, never technician."
    ),
    "ai.use": (
        "Open an AI conversation and ask questions about your org's estate. "
        "Grants NOTHING on its own: every answer is served by a loopback call "
        "to a real route carrying the caller's own token, so a user holding "
        "this and no module permission gets a 403 from every tool. "
        "Deliberately not implied by any read permission -- the AI surface has "
        "a cost dimension (tokens) and an egress dimension that plain reading "
        "does not."
    ),
    "ai.admin": (
        "Configure the AI provider selection, budgets, transcript retention, "
        "and the per-org kill switch. HIGH PRIVILEGE: this grant controls "
        "where control-plane data is sent -- selecting a hosted provider sends "
        "question text and tool results (hostnames, ticket bodies, inventory, "
        "audit action names) outside the customer's network. The same class of "
        "grant as `notification.admin`. Admin tier, never technician."
    ),
    "ai.act.propose": (
        "AI: open an act-capable session and mint action proposals. Grants NO "
        "ability to confirm or dispatch. A proposal is a persisted, "
        "server-rendered preview of a row an org admin already authored in the "
        "approved-action catalogue -- it signs nothing, reaches no agent, and "
        "creates no job."
    ),
    "ai.act.confirm": (
        "AI: confirm a previewed AI action. HIGH PRIVILEGE, and NOT sufficient "
        "on its own -- the confirming user must ALSO hold the permission the "
        "owning module's own dispatch route requires "
        "(`app/jobs/dispatch_permissions.py`), so a technician who cannot run "
        "`patch.apply` by hand cannot confirm one here. Admin tier."
    ),
    "reporting.read": (
        "View cross-module reports. Grants NOTHING on its own: every report "
        "additionally requires the read permission of every module it draws "
        "from, so a user holding this alone gets a 403 from every report."
    ),
    "site.read": "View site servers, their segments, roles, and segment checks",
    "site.manage": (
        "Promote a device to site server, demote it, and edit its segments, "
        "roles and enable state. HIGH PRIVILEGE: it decides that a host serves "
        "an entire LAN segment, and the segments it declares are what the "
        "control plane later computes a broadcast address from. Deliberately "
        "SEPARATE from `site.wake` -- collapsing them would let anyone who can "
        "wake a machine also decide what a site server is."
    ),
    "site.wake": (
        "Send a Wake-on-LAN magic packet via a site server, inside an already-"
        "declared segment. Technician tier: it changes no configuration and "
        "cannot widen what a site server serves, but it DOES mint a signed job "
        "that puts a datagram on somebody's LAN, so it is not a read code."
    ),
    # Read/write split, this project's convention (patch.read/patch.apply,
    # health.read/health.alert.manage). The split matters more than usual here:
    # `manage` publishes the bytes every endpoint in the fleet will execute, so
    # it is not a technician tier, while `read` is needed by anyone who installs
    # an agent. Migration 0070 is where that split stopped being a tier and
    # became a tenant boundary -- `manage` is a PLATFORM_OPERATOR_CODE below,
    # `read` stays with every org's own administrator.
    "agent.release.read": (
        "List agent releases and generate an install command, which mints a "
        "single-use enrollment token"
    ),
    "agent.release.manage": (
        "Publish, replace and unpublish agent artefacts -- the bytes every "
        "endpoint in the fleet will execute. PLATFORM OPERATOR ONLY "
        "(`super-admin`, migration 0070): `agent_releases` has no `org_id`, so "
        "this writes one catalogue every tenant's endpoints run from"
    ),
    # Agent push install (`docs/modules/agent-distribution/spec-push-install.md`
    # §8, under `CLAUDE.md` invariant 2a). ITS OWN CODES, NOT A REUSE: editing
    # an asset record is not executing code on a machine nobody has enrolled,
    # so `devices.manage` is deliberately not the gate; `deploy.script.run`
    # runs code on machines that ARE enrolled, from a pinned `scripts` row;
    # `agent.release.read` mints a token for a HUMAN to carry.
    "agent.push.run": (
        "Cause an unenrolled machine to run this platform's own agent "
        "installer, against explicitly named addresses a human selected. HIGH "
        "PRIVILEGE and NOT SUFFICIENT ON ITS OWN -- a push additionally "
        "requires `credentials.checkout`, checked at the service boundary, "
        "because a push USES a stored secret and without that conjunction this "
        "would be a side door around the credential module's own gate (the "
        "pattern `ai.act.confirm` records, and `provisioning`'s `wake_on_lan` "
        "additionally requiring `site.wake`). Admin tier, never technician: "
        "this is code execution against machines that are not under "
        "management, which is strictly larger than anything a technician does "
        "today. Never reachable by any automated, scheduled or AI-driven "
        "actor -- invariant 2a clause (e), enforced in both kernel gates."
    ),
    "agent.push.scope.manage": (
        "Grant or revoke a `push_authorization` prefix, and bind or unbind a "
        "`push_credential_scope`. HIGH PRIVILEGE: between them these two "
        "grants decide WHERE this platform may present an administrative "
        "credential to a machine it has never managed and cannot "
        "authenticate. The same class of grant as `ipam.scope.manage`, which "
        "decides where this platform emits packets, one notch up -- "
        "authorizing an ICMP echo is not authorizing an administrative login. "
        "Admin tier, never technician."
    ),
    # Agent self-update (spec-self-update.md §8.3). Org-scoped: rollouts act
    # on one org's devices, so none of these is a platform-operator code.
    # Promotion to `stable` stays with `agent.release.manage`.
    "agent.update.read": "View the fleet's agent versions and self-update rollouts",
    "agent.update.rollout": (
        "Create, start, pause, resume, advance, close a ring of and cancel an agent "
        "self-update rollout. Replaces the agent on this org's endpoints, in rings a "
        "person advances; never reachable by any automated or AI-driven actor"
    ),
    "agent.update.rollback": (
        "Additionally required for a rollback rollout, which installs a LOWER agent "
        "version than the devices run"
    ),
    "platform.errors.read": (
        "View error groups (fingerprinted failures) and their recent events' "
        "correlation ids"
    ),
    "platform.errors.write": (
        "Resolve an error group, or reopen one that was resolved. Deliberately "
        "separate from `platform.errors.read`: reading which code paths are "
        "failing and changing triage state are different privileges. Reopening "
        "shares this code rather than taking a weaker one -- undoing triage "
        "puts a group back in front of whoever is on call."
    ),
}


# --------------------------------------------------------------------------- #
# The platform operator's codes.
# --------------------------------------------------------------------------- #

# Codes that belong to whoever runs the CONTROL PLANE, not to whoever
# administers an organisation inside it. `super-admin` ("Cross-org platform
# administration (SaaS operator)", migration 0003) holds them; no other seeded
# role does, and no org-scoped surface may grant them -- `app.core.rbac_admin`
# keeps them out of the permission matrix and refuses them on write, and
# migration 0070 refuses them again inside `set_role_permissions` for a caller
# that bypasses the service layer.
#
# THE PROPERTY THEY SHARE is that the resource behind them has no `org_id`, so
# nothing scopes the code by tenant: whoever holds it acts on every tenant at
# once. That is a description of the DATA, which is why it catches codes from
# two different modules and will catch the next one.
#
# WHY THIS SET EXISTS AT ALL. `GET /api/v1/platform/errors/{group_id}` reads
# `platform_error_event`, which carries `org_id`/`user_id`/`job_id`/`device_id`
# for every tenant and whose only RLS policy is `USING (true)` -- so the code
# gating that route is, by itself, the entire tenant boundary. Migration 0057
# granted it to `org-admin`, and audit 2026-08-21-tenancy-test-efficacy (F1)
# reproduced one tenant's administrator reading another tenant's identifiers
# through it. 0068 moves both codes to `super-admin`.
#
# `agent.release.manage` JOINED THEM IN 0070, AND IT IS THE MOST SERIOUS MEMBER.
# `agent_releases` carries no `org_id` either -- one catalogue every tenant
# shares (spec §4.2) -- and this code publishes, replaces and withdraws the
# artefact every endpoint on the deployment downloads and runs. 0060 seeded it
# to `org-admin` while its own docstring called it "the closest thing in this
# platform to arbitrary code execution on every managed machine". Where
# `platform.errors.read` disclosed other tenants' identifiers, this one
# executes code on their machines.
#
# WHY IT IS AN EXPLICIT LIST AND NOT A PREFIX RULE. Two codes sit one dot away
# from a member and are deliberately NOT here:
#
#   * `platform.status.read` discloses no tenant row at all -- probe reasons,
#     process heartbeats and the module registry, from tables that carry no
#     `org_id` -- so revoking it would take a working page away from the
#     single-tenant on-prem administrator this platform ships to today and
#     close no disclosure.
#   * `agent.release.read` is what anyone who INSTALLS an agent needs
#     (migration 0060): generating an install command is a read of the
#     catalogue plus a token mint. An org that cannot list releases cannot
#     onboard a machine.
#
# A prefix rule would also break an ordinary role edit, because the console
# posts back every code the role already has and both of those are codes an org
# may legitimately hold.
PLATFORM_OPERATOR_CODES: frozenset[str] = frozenset(
    {
        "platform.errors.read",
        "platform.errors.write",
        "agent.release.manage",
    }
)


async def user_permission_codes(
    session: AsyncSession, user_id: UUID, *, client_id: UUID | None = None
) -> set[str]:
    """Permission codes granted to the user: all-clients grants always count; a
    client grant counts only when asking about that same client (MSP clients
    spec §2.1)."""
    rows = (
        await session.execute(
            select(Permission.code, UserRole.client_id)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .where(UserRole.user_id == user_id)
        )
    ).all()
    granted: set[str] = set()
    for code, scoped_client in rows:
        if scoped_client is None or (client_id is not None and scoped_client == client_id):
            granted.add(code)
    return granted


async def user_role_names(session: AsyncSession, user_id: UUID) -> list[str]:
    """The names of the roles a user holds, sorted and deduplicated.

    SEPARATE FROM `user_permission_codes` ON PURPOSE. Ninety-two permission
    codes are what the server enforces; a role name is what a person can hold
    in their head, and the console's account chip needs the second to answer
    "who am I acting as" before somebody acts on a whole fleet. It used to
    answer with the constant string "Operator" for everyone.

    Sorted, because the chip renders the first one and an order that depends on
    row order is a label that changes between page loads for no visible reason.
    """
    rows = (
        await session.execute(
            select(Role.name).join(UserRole, UserRole.role_id == Role.id).where(
                UserRole.user_id == user_id
            )
        )
    ).scalars()
    return sorted(set(rows))


async def has_permission(
    session: AsyncSession,
    user_id: UUID,
    code: str,
    *,
    client_id: UUID | None = None,
) -> bool:
    if code not in PERMISSIONS:
        raise ValueError(  # i18n-exempt: programmer error, unhandled 500
            f"unknown permission code {code!r}"
        )
    return code in await user_permission_codes(session, user_id, client_id=client_id)


def _walk_dependant(dependant: Any) -> Iterator[Any]:
    """Every sub-dependency of `dependant`, recursively -- a `Depends(...)`
    parameter can itself declare further `Depends(...)` parameters (exactly
    how `require_permission(code)`'s returned closure depends on
    `auth_context`), so a single-level scan of `dependant.dependencies` is
    not enough to find one buried under another."""
    for dep in dependant.dependencies:
        yield dep
        yield from _walk_dependant(dep)


# Qualnames of dependency-factory closures that `route_permission` recognises
# as "this route is gated on a permission code, recoverable from this exact
# closure". `require_permission` (`app/api/deps.py`) is the staff-route
# factory; `require_portal_permission` (`app/modules/portal/principal.py`) is
# the portal's, added so the portal's router-derived RBAC completeness test
# can use the same mechanism instead of a second, hand-maintained one. This is
# an explicit, additive set -- not a generic "any closure with a `code` free
# variable" match -- to keep false positives (some unrelated dependency that
# happens to close over a variable named `code`) impossible.
_PERMISSION_DEPENDENCY_QUALNAMES = frozenset(
    {
        "require_permission.<locals>.dependency",
        "require_portal_permission.<locals>.dependency",
    }
)


def route_permission(route: APIRoute) -> str | None:
    """The permission code `route` is gated on, or `None` if it carries no
    `require_permission(...)`/`require_portal_permission(...)` dependency at
    all.

    THIS IS WHAT MAKES RBAC COMPLETENESS DERIVABLE FROM THE ROUTER (invariant
    #6), rather than from a hand-maintained list of "every route and its
    permission" that a later route addition could silently miss. A test that
    instead hand-lists routes only proves each LISTED route is gated -- it
    says nothing about a route nobody remembered to add to the list. Walking
    `app.routes` and calling this on each one is a completeness check that
    cannot go stale: a route registered without `require_permission` returns
    `None` here and fails the assertion, whatever module added it.

    HOW: `require_permission(code)` (`app/api/deps.py`) returns a closure
    (`dependency`) over `code`, and does not stash `code` anywhere else
    inspectable -- by design, it is an ordinary dependency-injection
    factory, not a registry. This walks `route.dependant`'s dependency tree
    (every `Depends(...)` on the endpoint, and every `Depends(...)` those
    sub-dependencies themselves declare, recursively -- see
    `_walk_dependant`) for the one whose `.call` is exactly that closure --
    identified by its `__qualname__`, since a fresh closure object is minted
    on every `require_permission(...)` call and no two routes share one --
    then reads `code` back out of the closure's own cell via
    `__closure__`/`co_freevars`. There is no other place `code` lives once
    the dependency has been built, so this reflection is the only way to
    recover it without changing `require_permission`'s own signature into a
    registry."""
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return None
    for dep in _walk_dependant(dependant):
        call = getattr(dep, "call", None)
        if call is None or not hasattr(call, "__code__"):
            continue
        if call.__qualname__ not in _PERMISSION_DEPENDENCY_QUALNAMES:
            continue
        freevars = call.__code__.co_freevars
        if call.__closure__ is None or "code" not in freevars:
            continue
        return str(call.__closure__[freevars.index("code")].cell_contents)
    return None
