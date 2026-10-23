/**
 * Fixtures for the Settings page: the email relay, the role catalogue and
 * matrix, the user lifecycle, and the one health write the health module's
 * own fixtures do not carry.
 *
 * WHAT IS DELIBERATELY NOT HERE. `me` and `getCurrentOrg` are the shell's
 * (`fixtures/core.ts`); `listUsers` and `listTicketQueues` are the service
 * desk's, because the ticket pages draw the same people and queues and one
 * seed is what keeps them the same person on both screens; the notification
 * channels and rules, the SLA and budget settings and their writes are their
 * modules' own. This file registers only what no other module answers, and
 * its roles are built over the service desk's directory so "7 hold
 * technician" on the Roles tab is the number of people the Users tab lists
 * with it.
 *
 * NOTHING CREDENTIAL-SHAPED IS SERVED (invariant 3): the relay answers
 * `authenticated: true` and no password, exactly as the API does.
 *
 * The AI connections read and its revoke are here too: no other module owns
 * them.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { ApiError } from "../../lib/api";
import type { McpAppOut } from "../../lib/api";
import type { SettingsOut as HealthSettingsOut } from "../health/types";
import { USERS } from "../service-desk/fixtures";
import type {
  EffectivePermissionsOut,
  EmailSettingsOut,
  PermissionOut,
  RoleAssignmentOut,
  RoleOut,
} from "./types";

const MINUTE = 60_000;
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();

const notFound = (what: string) => new ApiError(404, JSON.stringify({ detail: `${what} not found` }));
const conflict = (detail: string) => new ApiError(409, JSON.stringify({ detail }));

/* ------------------------------------------------------------------- email */

let email: EmailSettingsOut = {
  configured: true,
  host: "smtp.toeshee.example",
  port: "587",
  from_address: "itms@toeshee.example",
  from_display_name: "ITMS",
  reply_to: "it@toeshee.example",
  encryption: "starttls",
  username: "itms",
  authenticated: true,
  auth_type: "password",
  microsoft_client_id: "",
  microsoft_sign_in_url: "",
  microsoft_scope: "",
};

const getEmailSettings: FixtureMap["getEmailSettings"] = () => email;

const updateEmailSettings: FixtureMap["updateEmailSettings"] = (body) => {
  email = {
    configured: body.host !== "",
    host: body.host,
    port: String(body.port),
    from_address: body.from_address,
    from_display_name: body.from_display_name,
    reply_to: body.reply_to,
    encryption: body.encryption,
    username: body.username,
    // A password typed sets the credential; an omitted one keeps whatever was
    // stored; an empty username clears it, as the server does.
    authenticated: body.username !== "" && (body.password !== undefined || email.authenticated),
    auth_type: body.username !== "" && body.auth_type === "token" ? "token" : "password",
    microsoft_client_id: body.auth_type === "token" ? (body.token_client_id ?? "") : "",
    microsoft_sign_in_url: body.auth_type === "token" ? (body.token_url ?? "") : "",
    microsoft_scope: body.auth_type === "token" ? (body.token_scope ?? "") : "",
  };
  return email;
};

const sendTestEmail: FixtureMap["sendTestEmail"] = (body) => ({
  sent: true,
  recipient: body?.recipient || "support@toeshee.com",
});

/* --------------------------------------------------------------- catalogue */

/**
 * The catalogue as `GET /permissions` serves it: the platform's codes, each
 * split at the first dot into the module (the matrix's row) and the action
 * (its column). The description is derived from the action the way the
 * backend's registry phrases them.
 */
const CODES = [
  "agent.push.run", "agent.push.scope.manage", "agent.release.manage", "agent.release.read",
  "ai.act.confirm", "ai.act.propose", "ai.admin", "ai.use", "assets.manage", "audit.read",
  "backup.policy.manage", "backup.read", "backup.restore", "backup.run",
  "bcp.approve", "bcp.manage", "bcp.read", "budget.manage", "budget.read",
  "change.approve", "change.manage", "change.read", "change.request",
  "credentials.checkout", "credentials.manage", "credentials.read",
  "deploy.create", "deploy.execute", "deploy.package.manage", "deploy.read",
  "deploy.script.manage", "deploy.script.run", "devices.manage", "devices.read", "devices.review",
  "docs.document.purge", "docs.manage", "docs.read", "email_intake.admin", "email_intake.read",
  "enrollment.link.manage", "enrollment.manage", "expense.confirm", "expense.record",
  "health.action.admin", "health.admin", "health.alert.manage", "health.read", "health.remediate",
  "ipam.assign", "ipam.manage", "ipam.read", "ipam.scope.manage", "ipam.sweep.manage", "ipam.sweep.run",
  "jobs.dispatch", "jobs.read", "licenses.keys.read", "licenses.manage", "licenses.read",
  "mcp.use", "notification.admin", "notification.read", "orgs.manage",
  "patch.apply", "patch.approve", "patch.policy.manage", "patch.read", "patch.scan",
  "platform.errors.read", "platform.errors.write", "platform.status.read", "policies.manage",
  "portal.ticket.read", "portal.ticket.write",
  "provisioning.manage", "provisioning.read", "provisioning.run",
  "remote.control", "remote.policy.manage", "remote.recordings.read", "reporting.read",
  "roles.manage", "roles.read", "site.manage", "site.read", "site.wake", "sla.admin", "sla.read",
  "subscriptions.manage", "subscriptions.read",
  "ticket.admin", "ticket.approve", "ticket.assign", "ticket.read", "ticket.write",
  "users.manage", "users.read",
];

const VERB: Record<string, string> = {
  read: "View", manage: "Manage", admin: "Administer", run: "Run", write: "Edit", approve: "Approve",
  confirm: "Confirm", propose: "Propose", use: "Use", restore: "Restore", request: "Request",
  checkout: "Check out", create: "Create", execute: "Execute", dispatch: "Dispatch", apply: "Apply",
  scan: "Scan", assign: "Assign", control: "Control", record: "Record", purge: "Purge", remediate: "Remediate",
  wake: "Wake", review: "Review",
};

function describe(module: string, action: string): string {
  const parts = action.split(".");
  const verb = VERB[parts[parts.length - 1] ?? ""] ?? "Use";
  const object = [module, ...parts.slice(0, -1)].join(" ").replace(/_/g, " ");
  return `${verb} ${object}`;
}

const PERMISSIONS: PermissionOut[] = CODES.map((code) => {
  const dot = code.indexOf(".");
  const module = code.slice(0, dot);
  const action = code.slice(dot + 1);
  return { code, module, action, description: describe(module, action) };
});

const listPermissions: FixtureMap["listPermissions"] = () => ({ permissions: PERMISSIONS });

/* ------------------------------------------------------------------- roles */

/** The codes each seeded role grants: derived from the catalogue by module, so they stay legal. */
const byModule = (...modules: string[]): string[] =>
  CODES.filter((code) => modules.includes(code.slice(0, code.indexOf("."))));

interface RoleSeed {
  id: string;
  name: string;
  description: string;
  is_global: boolean;
  codes: string[];
}

const ROLE_SEEDS: RoleSeed[] = [
  { id: "role-org-admin", name: "org-admin", description: "Everything in this organization", is_global: true, codes: [...CODES] },
  {
    id: "role-technician",
    name: "technician",
    description: "Day-to-day fleet operations",
    is_global: true,
    codes: [...byModule("devices", "patch", "deploy", "jobs", "health", "remote", "backup"), "ticket.read", "ticket.write", "ai.use"],
  },
  {
    id: "role-service-desk",
    name: "service-desk",
    description: "Tickets, the portal and the people behind them",
    is_global: true,
    codes: [...byModule("ticket", "portal", "sla", "email_intake", "notification"), "devices.read", "users.read", "ai.use"],
  },
  {
    id: "role-auditor",
    name: "auditor",
    description: "Read-only across the platform",
    is_global: true,
    codes: CODES.filter((code) => code.endsWith(".read")),
  },
  {
    id: "role-finance",
    name: "finance",
    description: "Licences, subscriptions and the budget",
    is_global: false,
    codes: [...byModule("budget", "expense", "licenses", "subscriptions"), "reporting.read"],
  },
];

/** Who holds what: `user_roles`, keyed by the service desk's user ids. */
const GRANTS: Map<string, RoleAssignmentOut[]> = new Map([
  ["usr-helgar", [{ assignment_id: "ur-1", role_id: "role-org-admin", role_name: "org-admin", device_group_id: null }]],
  ["usr-ao", [{ assignment_id: "ur-2", role_id: "role-technician", role_name: "technician", device_group_id: null }]],
  [
    "usr-mr",
    [
      { assignment_id: "ur-3", role_id: "role-technician", role_name: "technician", device_group_id: null },
      { assignment_id: "ur-4", role_id: "role-service-desk", role_name: "service-desk", device_group_id: null },
    ],
  ],
  ["usr-tk", [{ assignment_id: "ur-5", role_id: "role-technician", role_name: "technician", device_group_id: "grp-servers" }]],
  ["usr-lf", [{ assignment_id: "ur-6", role_id: "role-service-desk", role_name: "service-desk", device_group_id: null }]],
  ["usr-ic", [{ assignment_id: "ur-7", role_id: "role-finance", role_name: "finance", device_group_id: null }]],
  ["usr-rs", [{ assignment_id: "ur-8", role_id: "role-auditor", role_name: "auditor", device_group_id: null }]],
]);

const roles = new Map<string, RoleSeed>(ROLE_SEEDS.map((seed) => [seed.id, seed]));

function holders(roleId: string): number {
  let count = 0;
  for (const grants of GRANTS.values()) if (grants.some((g) => g.role_id === roleId)) count += 1;
  return count;
}

const roleOut = (seed: RoleSeed): RoleOut => ({
  id: seed.id,
  name: seed.name,
  description: seed.description,
  is_global: seed.is_global,
  assigned_users: holders(seed.id),
  codes: [...seed.codes].sort(),
});

const listRoles: FixtureMap["listRoles"] = () => ({ roles: [...roles.values()].map(roleOut) });

let created = 0;
const createRole: FixtureMap["createRole"] = (body) => {
  if ([...roles.values()].some((r) => r.name === body.name)) throw conflict("name must be unique in this organization");
  created += 1;
  const seed: RoleSeed = { id: `role-new-${created}`, name: body.name, description: body.description, is_global: false, codes: [...body.codes] };
  roles.set(seed.id, seed);
  return roleOut(seed);
};

const updateRole: FixtureMap["updateRole"] = (roleId, body) => {
  const seed = roles.get(roleId);
  if (!seed) throw notFound("role");
  if (body.name != null) seed.name = body.name;
  if (body.description != null) seed.description = body.description;
  return roleOut(seed);
};

/**
 * Editing a platform-wide role copies it into the org first (spec §1.3), and
 * the copy's id comes back, which is what the Roles tab reports.
 */
const setRolePermissions: FixtureMap["setRolePermissions"] = (roleId, codes) => {
  const seed = roles.get(roleId);
  if (!seed) throw notFound("role");
  let target = seed;
  if (seed.is_global) {
    target = { ...seed, id: `${seed.id}-org`, is_global: false, codes: [...codes] };
    roles.set(target.id, target);
    for (const grants of GRANTS.values()) {
      for (const grant of grants) if (grant.role_id === seed.id) grant.role_id = target.id;
    }
    roles.delete(seed.id);
  } else {
    target.codes = [...codes];
  }
  return { role_id: target.id, codes: [...codes].sort(), affected_users: holders(target.id) };
};

const deleteRole: FixtureMap["deleteRole"] = (roleId) => {
  const seed = roles.get(roleId);
  if (!seed) throw notFound("role");
  if (holders(roleId) > 0) throw conflict("role is still assigned; remove its holders first");
  roles.delete(roleId);
  return undefined;
};

/* ------------------------------------------------------------------- users */

const userById = (userId: string) => {
  const user = USERS.find((u) => u.id === userId);
  if (!user) throw notFound("user");
  return user;
};

const getUserEffectivePermissions: FixtureMap["getUserEffectivePermissions"] = (userId): EffectivePermissionsOut => {
  userById(userId);
  const codes: EffectivePermissionsOut["codes"] = [];
  for (const grant of GRANTS.get(userId) ?? []) {
    const role = roles.get(grant.role_id);
    for (const code of role?.codes ?? []) {
      codes.push({ code, via_role: grant.role_name, device_group_id: grant.device_group_id });
    }
  }
  return { user_id: userId, codes: codes.sort((a, b) => a.code.localeCompare(b.code)) };
};

let createdUsers = 0;
const createUser: FixtureMap["createUser"] = (body) => {
  if (USERS.some((u) => u.email === body.email)) throw conflict("a user with that email already exists");
  createdUsers += 1;
  const id = `usr-new-${createdUsers}`;
  const grants: RoleAssignmentOut[] = body.role_ids.flatMap((roleId, at) => {
    const role = roles.get(roleId);
    return role ? [{ assignment_id: `ur-new-${createdUsers}-${at}`, role_id: role.id, role_name: role.name, device_group_id: null }] : [];
  });
  GRANTS.set(id, grants);
  return {
    id,
    email: body.email,
    display_name: body.display_name,
    status: "active",
    roles: grants,
    enrolment_email_sent: email.configured,
    enrolment_email_error: email.configured ? null : "No mail relay is configured on this server.",
  };
};

let assigned = 0;
const assignUserRole: FixtureMap["assignUserRole"] = (userId, body) => {
  userById(userId);
  const role = roles.get(body.role_id);
  if (!role) throw notFound("role");
  assigned += 1;
  const grant: RoleAssignmentOut = { assignment_id: `ur-assigned-${assigned}`, role_id: role.id, role_name: role.name, device_group_id: body.device_group_id };
  GRANTS.set(userId, [...(GRANTS.get(userId) ?? []), grant]);
  return grant;
};

const removeUserRole: FixtureMap["removeUserRole"] = (userId, assignmentId) => {
  const grants = GRANTS.get(userId) ?? [];
  if (!grants.some((g) => g.assignment_id === assignmentId)) throw notFound("assignment");
  GRANTS.set(userId, grants.filter((g) => g.assignment_id !== assignmentId));
  return undefined;
};

const setUserEnabled: FixtureMap["setUserEnabled"] = (userId, enabled) => {
  const user = userById(userId);
  return { ...user, status: enabled ? "active" : "disabled", roles: GRANTS.get(userId) ?? [] };
};

const resetUserPassword: FixtureMap["resetUserPassword"] = (userId) => {
  userById(userId);
  return { sent: email.configured };
};

const resendUserEnrolment: FixtureMap["resendUserEnrolment"] = (userId) => {
  userById(userId);
  return { sent: email.configured };
};

const deleteUser: FixtureMap["deleteUser"] = (userId) => {
  userById(userId);
  GRANTS.delete(userId);
  return undefined;
};

/* ---------------------------------------------------------- AI connections */

/**
 * `GET /mcp/connections` over the shell's org, whose settings carry no
 * `mcp_enabled` — the state an organization starts in, so `enabled` is false
 * here as it is on the switch. The apps are ones used before it was turned
 * off: the list is derived from audited use, not from the switch.
 */
const MCP_APPS: McpAppOut[] = [
  { client_id: "claude", people: 4, last_used_at: ago(12), revoked_for_me: false, revoked_for_everyone: false },
  {
    client_id: "helpdesk-assistant",
    people: 1,
    last_used_at: ago(3 * 24 * 60),
    revoked_for_me: true,
    revoked_for_everyone: false,
  },
];

const getMcpConnections: FixtureMap["getMcpConnections"] = () => ({
  enabled: false,
  address: "https://itms.toeshee.example/mcp",
  apps: MCP_APPS.map((app) => ({ ...app })),
});

const revokeMcpConnection: FixtureMap["revokeMcpConnection"] = (clientId, scope) => {
  const app = MCP_APPS.find((candidate) => candidate.client_id === clientId);
  if (!app) throw notFound("AI app");
  if (scope === "everyone") app.revoked_for_everyone = true;
  else app.revoked_for_me = true;
  return { revoked: true, keycloak_failures: 0 };
};

/* ------------------------------------------------------------------ health */

/**
 * The write half of the health settings. The read is the health module's
 * (`modules/health/fixtures.ts`); the answer here is the body as the server
 * would store it, over the same defaults that read serves.
 */
const updateHealthSettings: FixtureMap["updateHealthSettings"] = (body): HealthSettingsOut => ({
  flap_window_s: body.flap_window_s ?? 3600,
  flap_threshold: body.flap_threshold ?? 4,
  flap_cooldown_s: body.flap_cooldown_s ?? 3600,
  unavailable_clear_after_s: body.unavailable_clear_after_s ?? 86_400,
  alert_retention_days: body.alert_retention_days ?? 400,
  escalate_after_s: body.escalate_after_s ?? 3600,
  offline_after_s: body.offline_after_s ?? 900,
  approval_ttl_s: body.approval_ttl_s ?? 3600,
  default_alert_queue_id: body.default_alert_queue_id ?? null,
  default_ticket_category_id: body.default_ticket_category_id ?? null,
  monitoring_deep_link_template: body.monitoring_deep_link_template ?? null,
  auto_resolve_alert_tickets: body.auto_resolve_alert_tickets ?? true,
  configured: true,
  alert_tickets_enabled: body.default_alert_queue_id != null && body.default_ticket_category_id != null,
  updated_at: ago(0),
  updated_by: "Helgar Palmieri",
});

const fixtures: FixtureMap = {
  getEmailSettings,
  updateEmailSettings,
  sendTestEmail,
  listPermissions,
  listRoles,
  createRole,
  updateRole,
  setRolePermissions,
  deleteRole,
  getUserEffectivePermissions,
  createUser,
  assignUserRole,
  removeUserRole,
  setUserEnabled,
  resetUserPassword,
  resendUserEnrolment,
  deleteUser,
  updateHealthSettings,
  getMcpConnections,
  revokeMcpConnection,
};

export default fixtures;
