// Every read and write the Settings page makes, in one place.
//
// SHAPE, AND WHY IT IS THIS SHAPE. Everything is called through `useApi()`
// (the pattern `modules/notification/api.ts` set), the organization profile
// included: `GET/PATCH /api/v1/orgs/current` used to be the one exception,
// fetched here directly while it had no client method. It has one now
// (`ApiClient.getCurrentOrg` / `updateCurrentOrg`), so the local `orgRequest`
// that mirrored `ApiClient.request` is gone, and with it the one settings
// read that fixture mode could not answer and the one 401 that did not
// re-authenticate.
//
// QUERY KEYS ARE SHARED WITH THE OWNING MODULES ON PURPOSE. `ai-settings`,
// `notification-channels`, `sla-settings`, `health-settings` and
// `budget-settings` are the keys `../ai-admin/api.ts`, `../notification/api.ts`
// and the rest already use. Saving the assistant here must not leave the
// assistant's own page showing the value it replaced, and one cache entry per
// server resource is how that is guaranteed rather than remembered.
//
// EVERY MUTATION INVALIDATES ITS READ. The forms show what the server returned
// after the write, never what the operator typed — a save that the server
// silently normalised (or partially applied) shows up on screen.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type {
  ApiError,
  ClientOut,
  CurrentOrg,
  McpConnectionsOut,
  McpRevokeOut,
  McpRevokeScope,
  Me,
  OrgUpdateRequest,
} from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import type { AiProviderOut, AiSettingsOut, AiSettingsUpdate } from "../ai-admin/types";
import type { SettingsOut as BudgetSettingsOut, SettingsUpdate as BudgetSettingsUpdate } from "../budget/types";
import type { SettingsOut as HealthSettingsOut, SettingsUpdate as HealthSettingsUpdate } from "../health/types";
import type {
  ChannelCreate,
  ChannelOut,
  ChannelUpdate,
  RuleCreate,
  RuleOut,
  RuleUpdate,
  TestSendResult,
} from "../notification/types";
import type { CategoryOut, QueueOut } from "../service-desk/types";
import type { SettingsOut as SlaSettingsOut, SettingsUpdate as SlaSettingsUpdate } from "../sla/types";
import type {
  CreatedUserOut,
  CurrentOrgOut,
  CurrentOrgUpdate,
  EffectivePermissionsOut,
  EmailSettingsOut,
  EmailSettingsUpdate,
  MailResultOut,
  MeProfileUpdate,
  PermissionCatalogueOut,
  RoleAssign,
  RoleAssignmentOut,
  RoleCreate,
  RoleListOut,
  RoleOut,
  RolePermissionsOut,
  RoleUpdate,
  TestSendIn,
  TestSendOut,
  UserCreate,
  UserListOut,
  UserOut,
} from "./types";

// A 403 is an answer, not a fault: retrying a permission wall changes nothing
// and delays the "you do not hold this" message the operator needs.
const NO_RETRY = { retry: false } as const;

export const ORG_KEY = "settings-current-org";
export const AI_SETTINGS_KEY = "ai-settings";
export const AI_PROVIDERS_KEY = "ai-providers";
export const CHANNELS_KEY = "notification-channels";
export const RULES_KEY = "notification-rules";
export const SLA_SETTINGS_KEY = "sla-settings";
export const HEALTH_SETTINGS_KEY = "health-settings";
export const BUDGET_SETTINGS_KEY = "budget-settings";
export const EMAIL_SETTINGS_KEY = "email-settings";
export const MCP_CONNECTIONS_KEY = "mcp-connections";

// --------------------------------------------------------------------------
// Organization profile — GET/PATCH /api/v1/orgs/current
// --------------------------------------------------------------------------

/**
 * The org as this page's forms read it.
 *
 * `CurrentOrg.settings` is the client's typed mirror of the server allowlist;
 * this page renders WHATEVER KEYS THE SERVER RETURNED (see `OrganizationTab`),
 * so it reads them as an open record. A copy rather than a cast, because an
 * interface is not assignable to an index signature and the copy is what the
 * page would build anyway.
 */
function toOrgOut(org: CurrentOrg): CurrentOrgOut {
  return { id: org.id, name: org.name, settings: { ...org.settings } };
}

/**
 * The PATCH body, in the client's shape. The page sends each key back in the
 * type the server stored it as; the client's `OrgUpdateRequest` narrows the
 * same keys to the allowlist's declared types, and the server is the authority
 * on both, so the record is handed over as-is.
 */
function toOrgRequest(body: CurrentOrgUpdate): OrgUpdateRequest {
  return { name: body.name, settings: body.settings as unknown as OrgUpdateRequest["settings"] };
}

export function useCurrentOrg() {
  const api = useApi();
  return useQuery<CurrentOrgOut, ApiError>({
    queryKey: [ORG_KEY],
    queryFn: async () => toOrgOut(await api.getCurrentOrg()),
    ...NO_RETRY,
  });
}

export function useUpdateCurrentOrgMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<CurrentOrgOut, ApiError, CurrentOrgUpdate>({
    mutationFn: async (body) => toOrgOut(await api.updateCurrentOrg(toOrgRequest(body))),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [ORG_KEY] });
      // `mcp_enabled` is an org setting, and the connections read reports it.
      void queryClient.invalidateQueries({ queryKey: [MCP_CONNECTIONS_KEY] });
    },
  });
}

// --------------------------------------------------------------------------
// Session identity (the permission codes every tab is gated on)
// --------------------------------------------------------------------------

export function useMe() {
  const api = useApi();
  return useQuery<Me, ApiError>({
    queryKey: ["me"],
    queryFn: () => api.me(),
    staleTime: 60_000,
    ...NO_RETRY,
  });
}

// --------------------------------------------------------------------------
// Assistant — GET /ai/settings, GET /ai/providers, PUT /ai/settings
// --------------------------------------------------------------------------

export function useAiSettings(enabled: boolean) {
  const api = useApi();
  return useQuery<AiSettingsOut, ApiError>({
    queryKey: [AI_SETTINGS_KEY],
    queryFn: () => api.getAiSettings(),
    enabled,
    ...NO_RETRY,
  });
}

export function useAiProviders(enabled: boolean) {
  const api = useApi();
  return useQuery<AiProviderOut[], ApiError>({
    queryKey: [AI_PROVIDERS_KEY],
    queryFn: () => api.listAiProviders(),
    enabled,
    ...NO_RETRY,
  });
}

export function useUpdateAiSettingsMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<AiSettingsOut, ApiError, AiSettingsUpdate>({
    mutationFn: (body) => api.updateAiSettings(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [AI_SETTINGS_KEY] });
      // The chat panel renders from `GET /ai/status`, which changes when the
      // feature is enabled or disabled here.
      void queryClient.invalidateQueries({ queryKey: ["ai-status"] });
    },
  });
}

// --------------------------------------------------------------------------
// AI connections — GET /mcp/connections, POST /mcp/connections/{id}/revoke
// --------------------------------------------------------------------------
//
// THE SWITCH IS NOT HERE. `mcp_enabled` is an org setting, written through
// `useUpdateCurrentOrgMutation` above, which invalidates this read too.

export function useMcpConnections(enabled: boolean) {
  const api = useApi();
  return useQuery<McpConnectionsOut, ApiError>({
    queryKey: [MCP_CONNECTIONS_KEY],
    queryFn: () => api.getMcpConnections(),
    enabled,
    ...NO_RETRY,
  });
}

/**
 * A revoke. The result is kept on the mutation (`data`, `variables`) and
 * worded at render, so the outcome — including how many people's sign-in
 * could not be withdrawn — re-words on a language switch.
 */
export function useRevokeMcpConnection() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<McpRevokeOut, ApiError, { clientId: string; scope: McpRevokeScope }>({
    mutationFn: ({ clientId, scope }) => api.revokeMcpConnection(clientId, scope),
    onSuccess: () => {
      // The row's "revoked for you / for everyone" flags moved.
      void queryClient.invalidateQueries({ queryKey: [MCP_CONNECTIONS_KEY] });
    },
  });
}

// --------------------------------------------------------------------------
// Notifications — channels and rules
// --------------------------------------------------------------------------

export function useNotificationChannels(enabled: boolean) {
  const api = useApi();
  return useQuery<ChannelOut[], ApiError>({
    queryKey: [CHANNELS_KEY],
    queryFn: () => api.listNotificationChannels(),
    enabled,
    ...NO_RETRY,
  });
}

export function useCreateChannelMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ChannelOut, ApiError, ChannelCreate>({
    mutationFn: (body) => api.createNotificationChannel(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [CHANNELS_KEY] });
    },
  });
}

export function useUpdateChannelMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ChannelOut, ApiError, { id: string; body: ChannelUpdate }>({
    mutationFn: ({ id, body }) => api.updateNotificationChannel(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [CHANNELS_KEY] });
    },
  });
}

// Test-send writes no row this page reads, so it invalidates nothing; its
// result is ephemeral and rendered where it was triggered.
export function useTestChannelMutation() {
  const api = useApi();
  return useMutation<TestSendResult, ApiError, string>({
    mutationFn: (id) => api.testNotificationChannel(id),
  });
}

/**
 * Ticket queues, for scoping a notification rule.
 *
 * A CROSS-MODULE read: queues are `ticket.read`, which a `notification.admin`
 * holder is not guaranteed to hold. A 403 here must not block rule creation,
 * so the caller degrades to a free-text queue id — the same concession
 * `../notification/RulesPanel.tsx` makes, for the same reason.
 *
 * `includeInactive` defaults to `false` (this tab's own use: scoping a NEW
 * rule to a currently-usable queue). `HealthTab` passes `true` — an org's
 * stored *default* queue must resolve even after someone deactivates it
 * (`ServiceDeskAdminPage.tsx` fetches the same way, for the same reason: a
 * deactivated-but-present row is not a deleted one). The flag is part of the
 * query key so the two callers never serve each other's cached page.
 */
export function useTicketQueues(enabled: boolean, includeInactive = false) {
  const api = useApi();
  return useQuery<QueueOut[], ApiError>({
    queryKey: ["settings-ticket-queues", includeInactive],
    queryFn: () => api.listTicketQueues(includeInactive),
    enabled,
    ...NO_RETRY,
  });
}

/**
 * Ticket categories, for the endpoint-health default that decides whether an
 * alert can become a ticket.
 *
 * A CROSS-MODULE read, the same concession `useTicketQueues` above makes and
 * for the same reason: categories are `ticket.read`, which a `health.admin`
 * holder is not guaranteed to hold. A 403 here must not block editing the
 * health settings that do not depend on it, so the caller degrades to a
 * free-text category id — the same concession `../notification/RulesPanel.tsx`
 * makes for queues.
 *
 * `includeInactive` — see `useTicketQueues` above; `HealthTab` passes `true`
 * for the same reason.
 */
export function useTicketCategories(enabled: boolean, includeInactive = false) {
  const api = useApi();
  return useQuery<CategoryOut[], ApiError>({
    queryKey: ["settings-ticket-categories", includeInactive],
    queryFn: () => api.listTicketCategories(includeInactive),
    enabled,
    ...NO_RETRY,
  });
}

export function useNotificationRules(enabled: boolean) {
  const api = useApi();
  return useQuery<RuleOut[], ApiError>({
    queryKey: [RULES_KEY],
    queryFn: () => api.listNotificationRules(),
    enabled,
    ...NO_RETRY,
  });
}

export function useCreateRuleMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<RuleOut, ApiError, RuleCreate>({
    mutationFn: (body) => api.createNotificationRule(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [RULES_KEY] });
    },
  });
}

export function useUpdateRuleMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<RuleOut, ApiError, { id: string; body: RuleUpdate }>({
    mutationFn: ({ id, body }) => api.updateNotificationRule(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [RULES_KEY] });
    },
  });
}

// --------------------------------------------------------------------------
// Service levels — GET/PUT /sla/settings
// --------------------------------------------------------------------------

export function useSlaSettings(enabled: boolean) {
  const api = useApi();
  return useQuery<SlaSettingsOut, ApiError>({
    queryKey: [SLA_SETTINGS_KEY],
    queryFn: () => api.getSlaSettings(),
    enabled,
    ...NO_RETRY,
  });
}

export function useUpdateSlaSettingsMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<SlaSettingsOut, ApiError, SlaSettingsUpdate>({
    mutationFn: (body) => api.updateSlaSettings(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [SLA_SETTINGS_KEY] });
    },
  });
}

// --------------------------------------------------------------------------
// Endpoint health — GET/PUT /health/settings
// --------------------------------------------------------------------------

export function useHealthSettings(enabled: boolean) {
  const api = useApi();
  return useQuery<HealthSettingsOut, ApiError>({
    queryKey: [HEALTH_SETTINGS_KEY],
    queryFn: () => api.getHealthSettings(),
    enabled,
    ...NO_RETRY,
  });
}

export function useUpdateHealthSettingsMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<HealthSettingsOut, ApiError, HealthSettingsUpdate>({
    mutationFn: (body) => api.updateHealthSettings(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [HEALTH_SETTINGS_KEY] });
    },
  });
}

// --------------------------------------------------------------------------
// Budget — GET/PUT /budget/settings
// --------------------------------------------------------------------------

export function useBudgetSettings(enabled: boolean) {
  const api = useApi();
  return useQuery<BudgetSettingsOut, ApiError>({
    queryKey: [BUDGET_SETTINGS_KEY],
    queryFn: () => api.getBudgetSettings(),
    enabled,
    ...NO_RETRY,
  });
}

export function useUpdateBudgetSettingsMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<BudgetSettingsOut, ApiError, BudgetSettingsUpdate>({
    mutationFn: (body) => api.updateBudgetSettings(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [BUDGET_SETTINGS_KEY] });
    },
  });
}

// --------------------------------------------------------------------------
// Email — GET/PUT /settings/email, POST /settings/email/test
// --------------------------------------------------------------------------
//
// ONE CACHE KEY, READ BY TWO TABS. The Email tab edits it; the Users tab reads
// it to decide whether to warn that invitations cannot be sent. Sharing the key
// is what makes saving a relay remove that banner without a reload — the same
// reason the assistant and notification keys are shared with their own modules.

export function useEmailSettings(enabled: boolean) {
  const api = useApi();
  return useQuery<EmailSettingsOut, ApiError>({
    queryKey: [EMAIL_SETTINGS_KEY],
    queryFn: () => api.getEmailSettings(),
    enabled,
    ...NO_RETRY,
  });
}

export function useUpdateEmailSettingsMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<EmailSettingsOut, ApiError, EmailSettingsUpdate>({
    mutationFn: (body) => api.updateEmailSettings(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [EMAIL_SETTINGS_KEY] });
    },
  });
}

/**
 * The test send.
 *
 * Writes no row this page reads, so it invalidates nothing; its result is
 * ephemeral and is rendered where it was triggered — the same shape
 * `useTestChannelMutation` uses, for the same reason.
 */
export function useTestEmailMutation() {
  const api = useApi();
  return useMutation<TestSendOut, ApiError, TestSendIn>({
    mutationFn: (body) => api.sendTestEmail(body),
  });
}

// --------------------------------------------------------------------------
// Access control — roles, the permission matrix, users, the account profile
// --------------------------------------------------------------------------
//
// These DO go through `ApiClient`: the routes shipped with this cycle and the
// client methods with them, so there is no reason to repeat `orgRequest`'s
// workaround. The only route still fetched directly on this page is
// `/orgs/current`, above.
//
// TWO CACHE KEYS, AND EVERY WRITE INVALIDATES BOTH WHERE IT HAS TO. A role's
// `assigned_users` count is what the matrix editor shows BEFORE applying a
// change, and a user's role assignments are what the directory shows — so a
// role assignment invalidates the USERS list and the ROLES list, because it
// moved a number on each.

export const PERMISSIONS_KEY = "permission-catalogue";
export const ROLES_KEY = "roles";
export const USERS_KEY = "users";
export const EFFECTIVE_PERMISSIONS_KEY = "user-effective-permissions";
export const CLIENTS_KEY = "settings-clients";

export function usePermissionCatalogue(enabled: boolean) {
  const api = useApi();
  return useQuery<PermissionCatalogueOut, ApiError>({
    queryKey: [PERMISSIONS_KEY],
    queryFn: () => api.listPermissions(),
    enabled,
    // The catalogue changes when the PLATFORM changes, not when an operator
    // edits anything, so it is worth holding for the length of a session.
    staleTime: 5 * 60_000,
    ...NO_RETRY,
  });
}

export function useRoles(enabled: boolean) {
  const api = useApi();
  return useQuery<RoleListOut, ApiError>({
    queryKey: [ROLES_KEY],
    queryFn: () => api.listRoles(),
    enabled,
    ...NO_RETRY,
  });
}

export function useCreateRoleMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<RoleOut, ApiError, RoleCreate>({
    mutationFn: (body) => api.createRole(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [ROLES_KEY] });
    },
  });
}

export function useUpdateRoleMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<RoleOut, ApiError, { roleId: string; body: RoleUpdate }>({
    mutationFn: ({ roleId, body }) => api.updateRole(roleId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [ROLES_KEY] });
    },
  });
}

/**
 * The matrix write.
 *
 * It invalidates the roles list (the code set and the affected-user count both
 * moved), the users list and every effective-permission view — redefining a
 * role changes what its holders can do, and an effective-permission panel left
 * showing the old answer is exactly the "simulate it in your head" failure
 * spec §1.5 exists to remove. `/me` too: the operator may have just changed
 * their own reach, and the tab strip is drawn from it.
 */
export function useSetRolePermissionsMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<RolePermissionsOut, ApiError, { roleId: string; codes: string[] }>({
    mutationFn: ({ roleId, codes }) => api.setRolePermissions(roleId, codes),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [ROLES_KEY] });
      void queryClient.invalidateQueries({ queryKey: [USERS_KEY] });
      void queryClient.invalidateQueries({ queryKey: [EFFECTIVE_PERMISSIONS_KEY] });
      void queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

export function useDeleteRoleMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (roleId) => api.deleteRole(roleId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [ROLES_KEY] });
      void queryClient.invalidateQueries({ queryKey: [USERS_KEY] });
    },
  });
}

export function useUsers(enabled: boolean) {
  const api = useApi();
  return useQuery<UserListOut, ApiError>({
    queryKey: [USERS_KEY],
    queryFn: () => api.listUsers(),
    enabled,
    ...NO_RETRY,
  });
}

/**
 * The clients a role grant's scope can name (MSP clients spec §2.1). It was
 * the device groups until then (cycle 9); device groups no longer scope
 * grants.
 *
 * `retry: false`, the same concession `useRoles` above makes: an operator
 * who holds `users.manage` but not `clients.read` should meet the picker's
 * `emptyHint` immediately rather than three silent retries.
 */
export function useClients(enabled: boolean) {
  const api = useApi();
  return useQuery<ClientOut[], ApiError>({
    queryKey: [CLIENTS_KEY],
    queryFn: () => api.listClients(),
    enabled,
    ...NO_RETRY,
  });
}

export function useEffectivePermissions(userId: string | null) {
  const api = useApi();
  return useQuery<EffectivePermissionsOut, ApiError>({
    queryKey: [EFFECTIVE_PERMISSIONS_KEY, userId],
    queryFn: () => api.getUserEffectivePermissions(userId as string),
    enabled: userId != null,
    ...NO_RETRY,
  });
}

export function useCreateUserMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<CreatedUserOut, ApiError, UserCreate>({
    mutationFn: (body) => api.createUser(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [USERS_KEY] });
      void queryClient.invalidateQueries({ queryKey: [ROLES_KEY] });
    },
  });
}

export function useAssignRoleMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<RoleAssignmentOut, ApiError, { userId: string; body: RoleAssign }>({
    mutationFn: ({ userId, body }) => api.assignUserRole(userId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [USERS_KEY] });
      void queryClient.invalidateQueries({ queryKey: [ROLES_KEY] });
      void queryClient.invalidateQueries({ queryKey: [EFFECTIVE_PERMISSIONS_KEY] });
    },
  });
}

export function useRemoveRoleMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, { userId: string; assignmentId: string }>({
    mutationFn: ({ userId, assignmentId }) => api.removeUserRole(userId, assignmentId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [USERS_KEY] });
      void queryClient.invalidateQueries({ queryKey: [ROLES_KEY] });
      void queryClient.invalidateQueries({ queryKey: [EFFECTIVE_PERMISSIONS_KEY] });
    },
  });
}

export function useSetUserEnabledMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<UserOut, ApiError, { userId: string; enabled: boolean }>({
    mutationFn: ({ userId, enabled }) => api.setUserEnabled(userId, enabled),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [USERS_KEY] });
    },
  });
}

/**
 * The two mails.
 *
 * Neither writes a row this page reads, so neither invalidates anything; the
 * result is ephemeral and is rendered where it was triggered — the same shape
 * `useTestChannelMutation` above uses, for the same reason.
 */
export function useResetPasswordMutation() {
  const api = useApi();
  return useMutation<MailResultOut, ApiError, string>({
    mutationFn: (userId) => api.resetUserPassword(userId),
  });
}

export function useResendEnrolmentMutation() {
  const api = useApi();
  return useMutation<MailResultOut, ApiError, string>({
    mutationFn: (userId) => api.resendUserEnrolment(userId),
  });
}

export function useDeleteUserMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (userId) => api.deleteUser(userId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [USERS_KEY] });
      void queryClient.invalidateQueries({ queryKey: [ROLES_KEY] });
    },
  });
}

/**
 * The account profile — `PATCH /api/v1/me`.
 *
 * Invalidates `me`, which the tab strip and the shell's identity chip both
 * read: a display name changed here must not leave the old one in the corner
 * of the screen.
 */
export function useUpdateMeMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<Me, ApiError, MeProfileUpdate>({
    mutationFn: (body) => api.updateMe(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  });
}
