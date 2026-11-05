// Shared fixtures and render helper for the Settings tab tests.
//
// NOT a `*.test.tsx` file, so vitest's `include` does not pick it up as a
// suite of its own.
//
// The `ApiClient` is mocked per test FILE (`vi.mock` is hoisted per file), and
// every file points its mock at `apiClient()` here so all the tabs share one
// set of fixtures. `fetch` is stubbed too, even though no tab fetches directly
// any more (the organization profile moved onto `ApiClient` with its client
// methods): a request that reaches the network from a settings test is a
// defect, and a stub that answers nothing is how it shows up.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { RenderResult } from "@testing-library/react";
import { MemoryRouter, useLocation, type Location } from "react-router-dom";
import { vi } from "vitest";

import { ApiError, type ApiClient } from "../../../lib/api";
import { ProfilePage } from "../../profile/ProfilePage";
import { SecurityPage } from "../../profile/SecurityPage";
import { SettingsPage } from "../SettingsPage";

export const ALL_PERMISSIONS = [
  "orgs.manage",
  "roles.read",
  "roles.manage",
  "users.read",
  "users.manage",
  "ai.admin",
  "notification.read",
  "notification.admin",
  "sla.read",
  "sla.admin",
  "health.read",
  "health.admin",
  "budget.read",
  "budget.manage",
  "mcp.use",
] as const;

/**
 * A string that must never reach the DOM.
 *
 * Planted in the fixtures in the places a buggy or hostile server could put
 * credential material — an org settings key named like a password, a channel
 * `config` blob, an extra field on the AI settings response. Every "never
 * renders a secret" assertion in these tests is a search for this one string,
 * so a regression that starts echoing any of those places fails by name.
 */
export const PLANTED_SECRET = "PLANTED-SECRET-VALUE-must-never-render";

export const ORG_FIXTURE = {
  id: "org-1",
  name: "Toeshee Group",
  settings: {
    default_timezone: "America/Bogota",
    evidence_retention_days: 365,
    self_service_portal: true,
    // A server that allowlisted a credential-shaped key by mistake. The
    // console must name the key and never render the value.
    smtp_password: PLANTED_SECRET,
    // A structured value: named, not stringified into a text box.
    escalation_matrix: { tier1: "ops" },
  },
};

// **NO `enabled` AND NO `provider_key`** since spec §0.14 (2026-08-25): the
// server's `SettingsOut` no longer carries either, because whether the
// assistant runs and which model it uses are the deployment's `ITS_AI_*`.
export const AI_SETTINGS_FIXTURE = {
  transcript_retention_days: 30,
  max_turns_per_user_per_minute: 5,
  max_turns_per_org_per_hour: 100,
  daily_token_budget: null,
  // Not in `AiSettingsOut` — a server bug returning key material. It must not
  // appear on screen just because it arrived.
  api_key: PLANTED_SECRET,
};

export const AI_PROVIDERS_FIXTURE = [{ key: "primary", kind: "anthropic", model: "claude-x" }];

export const CHANNEL_FIXTURE = {
  id: "c1",
  kind: "email",
  name: "Operations mailbox",
  // `config` IS returned by the API. It is never rendered, so a secret that
  // lands in it (which the backend rejects, but this console does not rely on)
  // still never reaches a screen or a screenshot.
  config: { host: "smtp.internal", port: 587, from_address: "ops@example.com", token: PLANTED_SECRET },
  active: true,
  has_credential: true,
  created_at: "2026-08-01T00:00:00Z",
};

// A webhook channel: the only kind whose row takes a replacement token (an
// email channel's sign-in is edited on Notifications -> Channels).
export const WEBHOOK_CHANNEL_FIXTURE = {
  id: "c2",
  kind: "webhook",
  name: "Ops chat",
  config: { url: "https://chat.lan/hooks/x", token: PLANTED_SECRET },
  active: true,
  has_credential: true,
  created_at: "2026-08-01T00:00:00Z",
};

export const RULE_FIXTURE = {
  id: "r1",
  event_type: "ticket.assigned",
  channel_id: "c1",
  recipient_kind: "queue_members",
  queue_id: null,
  active: true,
};

/** A ticket queue, for the `EntityPicker` fields that replaced raw queue ids. */
export const QUEUE_FIXTURE = {
  id: "queue-7",
  key: "escalations",
  name: "Escalations",
  description: "",
  active: true,
  sort_order: 1,
  created_by: "admin",
  created_at: "2026-08-01T00:00:00Z",
};

/** A ticket category, for `HealthTab`'s default-ticket-category picker. */
export const CATEGORY_FIXTURE = {
  id: "cat-1",
  key: "incident",
  name: "Incident",
  type: "incident",
  requires_approval: false,
  approver_role_id: null,
  default_queue_id: null,
  active: true,
  sort_order: 1,
  created_by: "admin",
  created_at: "2026-08-01T00:00:00Z",
};

export const SLA_SETTINGS_FIXTURE = {
  auto_close_after_days: 7,
  configured: true,
  tickets_without_sla: 0,
  stale_ticket_sla: 0,
};

export const HEALTH_SETTINGS_FIXTURE = {
  flap_window_s: 3600,
  flap_threshold: 4,
  flap_cooldown_s: 3600,
  unavailable_clear_after_s: 86400,
  alert_retention_days: 400,
  escalate_after_s: 3600,
  offline_after_s: 900,
  approval_ttl_s: 3600,
  default_alert_queue_id: null,
  default_ticket_category_id: null,
  monitoring_deep_link_template: null,
  auto_resolve_alert_tickets: true,
  configured: true,
  alert_tickets_enabled: false,
  updated_at: "2026-08-01T00:00:00Z",
  updated_by: "admin",
};

/**
 * `GET /api/v1/settings/email`, on a deployment where a relay IS configured.
 *
 * `password` IS NOT A FIELD OF `EmailSettingsOut` — the server has no key that
 * can carry one. It is planted here anyway, for the same reason the org and
 * user fixtures plant one: the console must be tested against a server that
 * regressed, not against one that behaves. The value must never reach the DOM.
 */
export const EMAIL_SETTINGS_FIXTURE = {
  configured: true,
  host: "smtp.internal",
  port: "587",
  from_address: "itms@toeshee.com",
  from_display_name: "ITMS",
  reply_to: "support@toeshee.com",
  encryption: "starttls",
  username: "itms",
  authenticated: true,
  password: PLANTED_SECRET,
};

/** The state this platform ships in: no relay at all. */
export const EMAIL_UNCONFIGURED_FIXTURE = {
  configured: false,
  host: "",
  port: "",
  from_address: "",
  from_display_name: "",
  reply_to: "",
  encryption: "none",
  username: "",
  authenticated: false,
};

/**
 * `GET /api/v1/mcp/connections`: the switch on, an address configured, and
 * one AI app that called tools here. The client id is what the app
 * registered, recorded rather than translated.
 */
export const MCP_APP_FIXTURE = {
  client_id: "claude",
  people: 3,
  last_used_at: "2026-10-01T09:30:00Z",
  revoked_for_me: false,
  revoked_for_everyone: false,
};

export const MCP_CONNECTIONS_FIXTURE = {
  enabled: true,
  address: "https://itms.example.test/mcp",
  apps: [MCP_APP_FIXTURE],
};

export const BUDGET_SETTINGS_FIXTURE = {
  // The internal client's (its id is the organisation's, `/me`'s `org_id`).
  client_id: "org-1",
  base_currency: "USD",
  fiscal_year_start_month: 1,
  updated_by: "admin",
  updated_at: "2026-08-01T00:00:00Z",
};

// --------------------------------------------------------------------------
// Access control — roles, the permission catalogue, users, the profile
// --------------------------------------------------------------------------

/**
 * The catalogue as `GET /api/v1/permissions` serves it: four modules, seven
 * codes. NOT the platform's real 92 — the tests assert that the grid is built
 * from whatever the SERVER sent, so a fixture that matched `rbac.py` would
 * make a hardcoded frontend list pass.
 */
export const PERMISSION_CATALOGUE_FIXTURE = {
  permissions: [
    { code: "devices.read", module: "devices", action: "read", description: "View devices" },
    { code: "devices.manage", module: "devices", action: "manage", description: "Manage devices" },
    { code: "roles.read", module: "roles", action: "read", description: "View roles" },
    { code: "roles.manage", module: "roles", action: "manage", description: "Redefine roles" },
    { code: "users.read", module: "users", action: "read", description: "View users" },
    { code: "users.manage", module: "users", action: "manage", description: "Manage users" },
    { code: "budget.read", module: "budget", action: "read", description: "View budgets" },
  ],
};

export const TECHNICIAN_ROLE = {
  id: "11111111-1111-4111-8111-111111111111",
  name: "technician",
  description: "Day-to-day fleet operations",
  is_global: true,
  assigned_users: 7,
  codes: ["devices.read"],
};

export const AUDITOR_ROLE = {
  id: "22222222-2222-4222-8222-222222222222",
  name: "auditor",
  description: "Read-only across the platform",
  is_global: false,
  assigned_users: 0,
  codes: ["budget.read"],
};

/**
 * `super-admin` is cross-org platform administration (spec §1.3). The server
 * never lists it — this fixture puts it in the response ANYWAY, so the console
 * is tested against a server that regressed rather than against one that
 * behaves.
 */
export const SUPER_ADMIN_ROLE = {
  id: "33333333-3333-4333-8333-333333333333",
  name: "super-admin",
  description: "Cross-org platform administration (SaaS operator)",
  is_global: true,
  assigned_users: 1,
  codes: ["orgs.manage"],
};

export const ROLES_FIXTURE = { roles: [TECHNICIAN_ROLE, AUDITOR_ROLE] };

export const OPERATOR_USER = {
  id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  email: "operator@example.com",
  display_name: "Operator",
  status: "active",
  roles: [
    {
      assignment_id: "a1111111-1111-4111-8111-111111111111",
      role_id: TECHNICIAN_ROLE.id,
      role_name: "technician",
      client_id: null,
    },
  ],
};

export const CASEY_USER = {
  id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  email: "casey@example.com",
  display_name: "Casey Doe",
  status: "disabled",
  roles: [],
  // Not in `UserOut` — a server bug (or a hostile server) returning credential
  // material on the directory. It must not reach a screen just because it
  // arrived.
  temporary_password: PLANTED_SECRET,
};

export const USERS_FIXTURE = { users: [OPERATOR_USER, CASEY_USER] };

/**
 * The clients a grant's scope can name (`GET /api/v1/clients`), for the
 * picker that used to offer device groups: a grant is scoped to ONE CLIENT or
 * to every client now, and device groups no longer scope grants (MSP clients
 * spec §2.1). A customer, the organisation's internal client, and the
 * unassigned client -- which no grant may name (the server answers 422), so
 * the picker leaves it out.
 */
export const WAREHOUSE_CLIENT = {
  id: "44444444-4444-4444-8444-444444444444",
  name: "Warehouse",
  kind: "customer",
  status: "active",
  email_domains: [],
  phone: "",
  address: "",
  website: "",
  notes: "",
  enabled_modules: [],
  device_count: 6,
  site_count: 1,
};

export const INTERNAL_CLIENT = {
  ...WAREHOUSE_CLIENT,
  id: "org-1",
  name: "Toeshee Group",
  kind: "internal",
  enabled_modules: ["budget", "bcp", "change"],
  device_count: 40,
};

export const UNASSIGNED_CLIENT = {
  ...WAREHOUSE_CLIENT,
  id: "66666666-6666-4666-8666-666666666666",
  name: "Unassigned",
  kind: "unassigned",
  device_count: 0,
  site_count: 0,
};

export const CLIENTS_FIXTURE = [INTERNAL_CLIENT, WAREHOUSE_CLIENT, UNASSIGNED_CLIENT];

export const EFFECTIVE_PERMISSIONS_FIXTURE = {
  user_id: OPERATOR_USER.id,
  codes: [
    { code: "devices.read", via_role: "technician", client_id: null },
    {
      code: "devices.manage",
      via_role: "warehouse-technician",
      client_id: "44444444-4444-4444-8444-444444444444",
    },
  ],
};

export const ME_FIXTURE = {
  user_id: OPERATOR_USER.id,
  org_id: "org-1",
  email: "operator@example.com",
  display_name: "Operator",
  phone: "+57 601 555 0100",
  timezone: "America/Bogota",
  locale: "en-GB",
  permissions: [] as string[],
};

// --------------------------------------------------------------------------
// Your security (`/profile/security`), the recent confirmation, the invitation
// and the session settings (own authentication design §2, §7)
// --------------------------------------------------------------------------

/**
 * `GET /api/v1/me/security`: two passkeys (one synced), the app, eight codes
 * left, two remembered devices (this one and another), two sessions (this one
 * and another).
 */
export const MY_SECURITY_FIXTURE = {
  has_totp: true,
  passkeys: [
    { id: "pk-1", name: "Work laptop", created_at: "2026-10-01T09:00:00Z", last_used_at: "2026-11-03T08:00:00Z", backup_state: false },
    { id: "pk-2", name: "Phone", created_at: "2026-10-02T09:00:00Z", last_used_at: null, backup_state: true },
  ],
  recovery_codes_left: 8,
  trusted_devices: [
    { id: "td-1", label: "Firefox on Windows", created_at: "2026-11-01T09:00:00Z", expires_at: "2026-11-16T09:00:00Z", last_used_at: "2026-11-04T08:00:00Z", current: true },
    { id: "td-2", label: "Safari on macOS", created_at: "2026-10-25T09:00:00Z", expires_at: "2026-11-09T09:00:00Z", last_used_at: null, current: false },
  ],
  sessions: [
    { id: "s-1", created_at: "2026-11-04T07:00:00Z", last_seen_at: "2026-11-04T08:00:00Z", expires_at: "2026-11-04T19:00:00Z", user_agent: "Mozilla/5.0 (Windows NT 10.0) Firefox/140.0", ip: "10.0.0.5", current: true },
    { id: "s-2", created_at: "2026-11-03T15:00:00Z", last_seen_at: "2026-11-03T16:00:00Z", expires_at: "2026-11-04T03:00:00Z", user_agent: "Mozilla/5.0 (Macintosh) Safari/605.1", ip: "10.0.0.9", current: false },
  ],
  // Not in `MySecurity` -- a server bug returning the authenticator secret.
  // The page renders named fields only, so it must never reach the screen.
  totp_secret: PLANTED_SECRET,
};

/** What `POST /auth/enroll/totp/start` answers. Its key is `JBSWY3DPEHPK3PXP`. */
export const OTPAUTH_URI_FIXTURE =
  "otpauth://totp/Novieri:operator%40example.com?secret=JBSWY3DPEHPK3PXP&issuer=Novieri";

/** W3C creation options, as py_webauthn serves them (`POST /auth/enroll/passkey/options`). */
export const CREATION_OPTIONS_FIXTURE = {
  rp: { name: "Novieri", id: "itms.example.test" },
  user: { id: "dXNlcg", name: "operator@example.com", displayName: "Operator" },
  challenge: "Y2hhbGxlbmdl",
  pubKeyCredParams: [{ type: "public-key", alg: -7 }],
  authenticatorSelection: { residentKey: "preferred", userVerification: "required" },
  attestation: "none",
};

/** W3C request options for the recent confirmation (`POST /auth/step-up/passkey/options`). */
export const REQUEST_OPTIONS_FIXTURE = {
  challenge: "c3RlcC11cA",
  rpId: "itms.example.test",
  allowCredentials: [],
  userVerification: "required",
};

/** Ten fresh codes in the Crockford alphabet (no I, L, O or U). */
export const RECOVERY_CODES_FIXTURE = [
  "7KQ2M-X9D4P", "3HV8N-QW2ZR", "M4T6C-8YJKD", "R2XPA-5N7GE", "Z9W3K-HT6QV",
  "C8D5M-2PRXN", "Q6J4T-W8ZAH", "N3F7V-K2MXD", "E5Y9R-6TQPC", "W2H8K-D4NZJ",
];

/** The server's refusal of a change whose second factor is not fresh (own authentication design §2.5). */
export function stepUpRequired(): ApiError {
  return new ApiError(403, JSON.stringify({ detail: { code: "step_up_required" } }));
}

/** `GET /api/v1/settings/sessions`, at the defaults (design §2.4, §2.6, §3). */
export const SESSION_SETTINGS_FIXTURE = {
  staff_idle_minutes: 30,
  staff_max_hours: 12,
  remember_device_enabled: true,
  remember_device_days: 15,
  remembered_max_hours: 168,
  contact_idle_minutes: 60,
  contact_max_days: 7,
  updated_at: "2026-11-01T00:00:00Z",
};

/** Invited and not yet accepted (own authentication design §7). Not in `USERS_FIXTURE`: a test that wants it says so. */
export const INVITED_USER = {
  id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  email: "invitee@example.com",
  display_name: "Invitee",
  status: "active",
  roles: [],
  invite_pending: true,
};

export const calls = {
  me: vi.fn(),
  getCurrentOrg: vi.fn(),
  updateCurrentOrg: vi.fn(),
  getAiSettings: vi.fn(),
  listAiProviders: vi.fn(),
  updateAiSettings: vi.fn(),
  listNotificationChannels: vi.fn(),
  createNotificationChannel: vi.fn(),
  updateNotificationChannel: vi.fn(),
  testNotificationChannel: vi.fn(),
  listNotificationRules: vi.fn(),
  createNotificationRule: vi.fn(),
  updateNotificationRule: vi.fn(),
  listTicketQueues: vi.fn(),
  listTicketCategories: vi.fn(),
  getSlaSettings: vi.fn(),
  updateSlaSettings: vi.fn(),
  getHealthSettings: vi.fn(),
  updateHealthSettings: vi.fn(),
  getBudgetSettings: vi.fn(),
  updateBudgetSettings: vi.fn(),
  getEmailSettings: vi.fn(),
  updateEmailSettings: vi.fn(),
  sendTestEmail: vi.fn(),
  listPermissions: vi.fn(),
  listRoles: vi.fn(),
  createRole: vi.fn(),
  updateRole: vi.fn(),
  setRolePermissions: vi.fn(),
  deleteRole: vi.fn(),
  listUsers: vi.fn(),
  listClients: vi.fn(),
  getUserEffectivePermissions: vi.fn(),
  assignUserRole: vi.fn(),
  removeUserRole: vi.fn(),
  setUserEnabled: vi.fn(),
  deleteUser: vi.fn(),
  updateMe: vi.fn(),
  getMySecurity: vi.fn(),
  changePassword: vi.fn(),
  removePasskey: vi.fn(),
  removeTotp: vi.fn(),
  regenerateRecoveryCodes: vi.fn(),
  revokeTrustedDevice: vi.fn(),
  endMySession: vi.fn(),
  stepUpTotp: vi.fn(),
  stepUpPasskeyOptions: vi.fn(),
  stepUpPasskey: vi.fn(),
  stepUpRecovery: vi.fn(),
  enrollTotpStart: vi.fn(),
  enrollTotpConfirm: vi.fn(),
  enrollPasskeyOptions: vi.fn(),
  enrollPasskey: vi.fn(),
  inviteUser: vi.fn(),
  resendInvite: vi.fn(),
  cancelInvite: vi.fn(),
  resetUserFactors: vi.fn(),
  endUserSessions: vi.fn(),
  getSessionSettings: vi.fn(),
  putSessionSettings: vi.fn(),
  getMcpConnections: vi.fn(),
  revokeMcpConnection: vi.fn(),
};

export function apiClient(): ApiClient {
  return calls as unknown as ApiClient;
}

export const fetchMock = vi.fn();

/** A `Response` as far as `../api.ts`'s `orgRequest` is concerned. */
export function httpResponse(body: unknown, status = 200): Response {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(text),
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

/** Reset every mock and install the happy-path fixtures. Call in `beforeEach`. */
export function installDefaults(permissions: readonly string[] = ALL_PERMISSIONS): void {
  for (const fn of Object.values(calls)) fn.mockReset();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);

  calls.me.mockResolvedValue({ ...ME_FIXTURE, permissions: [...permissions] });
  calls.getCurrentOrg.mockResolvedValue(ORG_FIXTURE);
  calls.updateCurrentOrg.mockResolvedValue(ORG_FIXTURE);
  calls.getAiSettings.mockResolvedValue(AI_SETTINGS_FIXTURE);
  calls.listAiProviders.mockResolvedValue(AI_PROVIDERS_FIXTURE);
  calls.updateAiSettings.mockResolvedValue(AI_SETTINGS_FIXTURE);
  calls.listNotificationChannels.mockResolvedValue([CHANNEL_FIXTURE]);
  calls.createNotificationChannel.mockResolvedValue(CHANNEL_FIXTURE);
  calls.updateNotificationChannel.mockResolvedValue(CHANNEL_FIXTURE);
  calls.testNotificationChannel.mockResolvedValue({ delivered: true, status_code: 200 });
  calls.listNotificationRules.mockResolvedValue([RULE_FIXTURE]);
  calls.createNotificationRule.mockResolvedValue(RULE_FIXTURE);
  calls.updateNotificationRule.mockResolvedValue({ ...RULE_FIXTURE, active: false });
  calls.listTicketQueues.mockResolvedValue([]);
  calls.listTicketCategories.mockResolvedValue([]);
  calls.getSlaSettings.mockResolvedValue(SLA_SETTINGS_FIXTURE);
  calls.updateSlaSettings.mockResolvedValue(SLA_SETTINGS_FIXTURE);
  calls.getHealthSettings.mockResolvedValue(HEALTH_SETTINGS_FIXTURE);
  calls.updateHealthSettings.mockResolvedValue(HEALTH_SETTINGS_FIXTURE);
  calls.getBudgetSettings.mockResolvedValue(BUDGET_SETTINGS_FIXTURE);
  calls.updateBudgetSettings.mockResolvedValue(BUDGET_SETTINGS_FIXTURE);
  // The CONFIGURED state is the default, so a test about the unconfigured one
  // has to say so — a fixture that shipped `configured: false` by default would
  // make the Users tab's banner appear in every unrelated test and stop being
  // evidence of anything.
  calls.getEmailSettings.mockResolvedValue(EMAIL_SETTINGS_FIXTURE);
  calls.updateEmailSettings.mockResolvedValue(EMAIL_SETTINGS_FIXTURE);
  calls.sendTestEmail.mockResolvedValue({ sent: true, recipient: "support@toeshee.com" });
  calls.listPermissions.mockResolvedValue(PERMISSION_CATALOGUE_FIXTURE);
  calls.listRoles.mockResolvedValue(ROLES_FIXTURE);
  calls.createRole.mockResolvedValue(AUDITOR_ROLE);
  calls.updateRole.mockResolvedValue(AUDITOR_ROLE);
  calls.setRolePermissions.mockResolvedValue({
    role_id: TECHNICIAN_ROLE.id,
    codes: ["devices.read", "devices.manage"],
    affected_users: 7,
  });
  calls.deleteRole.mockResolvedValue(undefined);
  calls.listUsers.mockResolvedValue(USERS_FIXTURE);
  calls.listClients.mockResolvedValue(CLIENTS_FIXTURE);
  calls.getUserEffectivePermissions.mockResolvedValue(EFFECTIVE_PERMISSIONS_FIXTURE);
  calls.assignUserRole.mockResolvedValue({
    assignment_id: "a2222222-2222-4222-8222-222222222222",
    role_id: AUDITOR_ROLE.id,
    role_name: "auditor",
    client_id: null,
  });
  calls.removeUserRole.mockResolvedValue(undefined);
  calls.setUserEnabled.mockResolvedValue({ ...OPERATOR_USER, status: "disabled" });
  calls.deleteUser.mockResolvedValue(undefined);
  calls.updateMe.mockResolvedValue({ ...ME_FIXTURE, permissions: [...permissions] });
  calls.getMySecurity.mockResolvedValue(MY_SECURITY_FIXTURE);
  calls.changePassword.mockResolvedValue(undefined);
  calls.removePasskey.mockResolvedValue(undefined);
  calls.removeTotp.mockResolvedValue(undefined);
  calls.regenerateRecoveryCodes.mockResolvedValue({ recovery_codes: [...RECOVERY_CODES_FIXTURE] });
  calls.revokeTrustedDevice.mockResolvedValue(undefined);
  calls.endMySession.mockResolvedValue(undefined);
  calls.stepUpTotp.mockResolvedValue(undefined);
  calls.stepUpPasskeyOptions.mockResolvedValue(REQUEST_OPTIONS_FIXTURE);
  calls.stepUpPasskey.mockResolvedValue(undefined);
  calls.stepUpRecovery.mockResolvedValue(undefined);
  calls.enrollTotpStart.mockResolvedValue({ otpauth_uri: OTPAUTH_URI_FIXTURE });
  calls.enrollTotpConfirm.mockResolvedValue(undefined);
  calls.enrollPasskeyOptions.mockResolvedValue(CREATION_OPTIONS_FIXTURE);
  calls.enrollPasskey.mockResolvedValue(undefined);
  // The CONFIGURED state is the default here too: the invitation is emailed,
  // so a test about the link shown once has to say so.
  calls.inviteUser.mockResolvedValue({ user_id: INVITED_USER.id, invite_link: null });
  calls.resendInvite.mockResolvedValue({ invite_link: null });
  calls.cancelInvite.mockResolvedValue(undefined);
  calls.resetUserFactors.mockResolvedValue(undefined);
  calls.endUserSessions.mockResolvedValue(undefined);
  calls.getSessionSettings.mockResolvedValue(SESSION_SETTINGS_FIXTURE);
  calls.putSessionSettings.mockResolvedValue(SESSION_SETTINGS_FIXTURE);
  calls.getMcpConnections.mockResolvedValue(MCP_CONNECTIONS_FIXTURE);
  calls.revokeMcpConnection.mockResolvedValue({ revoked: true, keycloak_failures: 0 });

  fetchMock.mockImplementation(() => Promise.reject(new Error("a settings test reached fetch()")));
}

/** The profile page, with the same mocked API surface as the settings tabs.
 *
 * It moved out of Settings on 2026-08-20 (its credential steps return to a
 * URL, and a tab held in React state is not one), but it still talks to the
 * same `/me` endpoint through the same client, so it keeps using this harness
 * rather than growing a second one to maintain in step. */
export function renderProfilePage(): RenderResult {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ProfilePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** A render, and the QueryClient it ran against, for asserting what the caches still hold. */
export type HarnessRender = RenderResult & { queryClient: QueryClient };

/**
 * Everything TanStack's mutation cache still holds -- each mutation's answer
 * and the body it was sent -- as one string. A value shown once (an
 * invitation link, an authenticator key, recovery codes, a password) must not
 * be in it once the person has it: `reset()` only detaches the observer, and a
 * detached mutation is collected after its `gcTime` (5 minutes by default).
 */
export function mutationCacheText(queryClient: QueryClient): string {
  return JSON.stringify(
    queryClient
      .getMutationCache()
      .getAll()
      .map((mutation) => ({ data: mutation.state.data, variables: mutation.state.variables })),
  );
}

/** Your security (`/profile/security`), with the same mocked API surface. */
export function renderSecurityPage(): HarnessRender {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return {
    ...render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/profile/security"]}>
          <SecurityPage />
          <LocationProbe />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
    queryClient,
  };
}

/**
 * The router's current location, for asserting what the page wrote to the
 * URL. Set by `LocationProbe`, which renders nothing.
 */
export const currentLocation: { value: Location | null } = { value: null };

function LocationProbe() {
  currentLocation.value = useLocation();
  return null;
}

/**
 * The Settings page under a router, at `/settings` unless `path` says
 * otherwise (`/settings?tab=users`): the active tab lives in `?tab=`.
 */
export function renderSettings(path = "/settings"): HarnessRender {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return {
    ...render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[path]}>
          <SettingsPage />
          <LocationProbe />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
    queryClient,
  };
}

/** The body of the nth `fetch` call, parsed. */
export function fetchBody(index = 0): unknown {
  const call = fetchMock.mock.calls[index];
  const init = call?.[1] as RequestInit | undefined;
  return init?.body == null ? undefined : JSON.parse(String(init.body));
}

/**
 * A short, human-readable tag for a failed "is this control grouped?"
 * assertion — not load-bearing for the assertion itself, just what a reader
 * sees when it fails. `aria-label` covers the controls that carry one
 * directly (a `Chips` `role="group"`, a table row's per-item input);
 * `.labels` covers a `Field`'s wrapping `<label>`. Falling back to a clipped
 * `outerHTML` rather than throwing keeps the guard from depending on either
 * working perfectly.
 *
 * EACH STEP FALLS THROUGH ON EMPTY, NOT JUST ON ABSENT. `??` only skips a
 * `null`/`undefined` left side, so an `aria-label=""` or a label whose text
 * trims to "" would win here and hand back an empty identifier — the one
 * outcome this helper exists to avoid, since producing a usable identifier
 * is its only job. `||` falls through on any falsy value, `""` included,
 * which is what every step below needs.
 */
export function describeControl(control: Element): string {
  const labels = "labels" in control ? (control as HTMLInputElement).labels : null;
  return (
    control.getAttribute("aria-label") ||
    labels?.[0]?.textContent?.trim() ||
    control.outerHTML.slice(0, 120)
  );
}
