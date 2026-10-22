// TanStack Query hooks for the health module, built on the shared ApiClient
// (useApi()). Mirrors modules/sla/api.ts: every mutation invalidates the query
// keys its server-side effects reach, and the UI refetches.
//
// SEVERAL MUTATIONS HAVE EFFECTS BEYOND THE ROW THEY EDIT, and the invalidation
// lists below are those effects, not a guess:
//
//   * A profile edit, a check replacement and an assignment create/delete each
//     run the policy RECONCILE server-side, which can put a signed
//     `health.policy.apply` job on every device whose acknowledged version is
//     now behind. `["health-device"]` is therefore invalidated by all of them,
//     so a device view open in another tab cannot keep showing a
//     `policy_version` the server has already moved past.
//   * Replacing a check set RETIRES the checks it drops and CLEARS their open
//     alerts as `policy_removed`. `["health-alerts"]` is invalidated for that
//     reason -- the alert list changed without anyone touching an alert.
//   * Granting or revoking a script exemption RE-DERIVES `destructive` on the
//     action, and revoking can DEMOTE an autonomous action to
//     `require_approval`. Both refetch the registry.
//
// There is no live health event stream -- the sweeps run on the worker's beat
// schedule -- so the alert console polls. The interval is deliberately modest
// and is stated at its one call site.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiError, Device, Me, Page } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import type {
  AlertDetailOut,
  AlertFilters,
  AlertOut,
  ApprovedActionCreate,
  ApprovedActionOut,
  ApprovedActionUpdate,
  AssignmentCreate,
  AssignmentOut,
  CheckIn,
  CheckTypeOut,
  DeviceHealthOut,
  ProfileChecksOut,
  ProfileCreate,
  ProfileOut,
  ProfileUpdate,
  SettingsOut,
  SettingsUpdate,
} from "./types";

// A genuine 403 (the caller lacks the code) should settle immediately rather
// than retry against the same permission wall -- the app-wide QueryClient
// default (App.tsx) is `retry: 1`.
const NO_RETRY = { retry: false } as const;

// The alert console's poll. The alert sweep runs every 60 s and the action
// worker every 30 s server-side, so anything faster than this only adds load
// without adding freshness.
export const ALERT_POLL_MS = 30_000;

// --------------------------------------------------------------------------
// The reader's timezone (UX remediation cycle 5)
// --------------------------------------------------------------------------

/**
 * The zone the signed-in operator works in, for `lib/datetime`.
 *
 * WHY A HOOK AND NOT A PROP, the same reasoning as `modules/sla/api.ts` and
 * `modules/ipam/api.ts`. Every instant this module renders sits deep inside a
 * table row or a `<dl>`, and `DeviceHealthPanel` is mounted by the DEVICES
 * page -- a component chain this module does not own. Threading `timezone`
 * down it means editing files that have nothing to do with health. This reads
 * the SAME `["me"]` query every page already keeps warm (`staleTime: 60_000`,
 * `HealthPage` included), so it costs no extra request.
 *
 * WHOSE ZONE THIS IS: the READER's, and that is right for every value here.
 * Everything this module renders through it -- `opened_at`, `last_seen_at`,
 * `muted_until`, `expires_at`, `granted_at`, `reported_at`,
 * `agent_last_seen_at` -- is an INSTANT, a single moment on a server clock.
 * There is no wall-clock-in-another-zone value in this module (SLA has those,
 * in a calendar's own zone, and deliberately does not convert them).
 *
 * `null` when the profile carries no preference; every helper in
 * `lib/datetime` falls back to the browser zone on `null`, which is the old
 * behaviour and is right for someone who never set one.
 */
export function useUserTimezone(): string | null {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  return me.data?.timezone ?? null;
}

// --------------------------------------------------------------------------
// Profiles, checks, assignments
// --------------------------------------------------------------------------

export function useHealthProfiles() {
  const api = useApi();
  return useQuery<ProfileOut[], ApiError>({
    queryKey: ["health-profiles"],
    queryFn: () => api.listHealthProfiles(),
    ...NO_RETRY,
  });
}

export function useHealthProfileChecks(profileId: string | undefined) {
  const api = useApi();
  return useQuery<ProfileChecksOut, ApiError>({
    queryKey: ["health-profile-checks", profileId],
    queryFn: () => api.getHealthProfileChecks(profileId as string),
    enabled: profileId != null && profileId !== "",
    ...NO_RETRY,
  });
}

export function useHealthCheckTypes() {
  const api = useApi();
  return useQuery<CheckTypeOut[], ApiError>({
    queryKey: ["health-check-types"],
    queryFn: () => api.listHealthCheckTypes(),
    // The registry is CLOSED and changes only with a deploy (spec §0.1), so
    // refetching it on every mount is pure waste.
    staleTime: 600_000,
    ...NO_RETRY,
  });
}

export function useCreateHealthProfileMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ProfileCreate) => api.createHealthProfile(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["health-profiles"] });
    },
  });
}

export function useUpdateHealthProfileMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ProfileUpdate }) =>
      api.updateHealthProfile(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["health-profiles"] });
      // Any write bumps `policy_version` and runs the reconcile.
      void queryClient.invalidateQueries({ queryKey: ["health-device"] });
    },
  });
}

export function useDeleteHealthProfileMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteHealthProfile(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["health-profiles"] });
      void queryClient.invalidateQueries({ queryKey: ["health-assignments"] });
      void queryClient.invalidateQueries({ queryKey: ["health-device"] });
    },
  });
}

export function useCloneHealthProfileMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => api.cloneHealthProfile(id, name),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["health-profiles"] });
    },
  });
}

export function useReplaceHealthChecksMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, checks }: { id: string; checks: CheckIn[] }) =>
      api.replaceHealthProfileChecks(id, checks),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["health-profile-checks"] });
      void queryClient.invalidateQueries({ queryKey: ["health-profiles"] });
      // A dropped check's open alerts are cleared `policy_removed` in the same
      // transaction, so the alert list moved without anyone touching an alert.
      void queryClient.invalidateQueries({ queryKey: ["health-alerts"] });
      void queryClient.invalidateQueries({ queryKey: ["health-device"] });
    },
  });
}

/**
 * The devices an assignment can name (UX remediation cycle 6).
 *
 * WHY THIS MODULE FETCHES DEVICES AT ALL. `EntityPicker` deliberately does not
 * fetch -- "each module knows its own endpoint, its own permissions and its
 * own idea of what is selectable" -- so the health module owns this read. It
 * is the fleet list every page already keeps warm under the same key, not a
 * second device index.
 *
 * GATED ON `device.read`, WHICH A HEALTH ADMIN IS NOT GUARANTEED. A 403 is not
 * retried and is not an error the picker hides behind: the panel says which
 * permission would populate the list, exactly as the SLA queue pickers do for
 * `ticket.read`.
 */
export function useAssignableDevices() {
  const api = useApi();
  // `Page<Device>` AND THE BARE `["devices"]` KEY, both deliberately: six other
  // modules already cache the fleet list under exactly this key and shape, and
  // a hook that stored `Device[]` there would hand every one of them an object
  // with no `.items`. Shared cache entries are shared contracts.
  return useQuery<Page<Device>, ApiError>({
    queryKey: ["devices"],
    queryFn: () => api.listDevices(),
    ...NO_RETRY,
  });
}

export function useHealthAssignments(profileId?: string) {
  const api = useApi();
  return useQuery<AssignmentOut[], ApiError>({
    queryKey: ["health-assignments", profileId ?? null],
    queryFn: () => api.listHealthAssignments(profileId),
    ...NO_RETRY,
  });
}

export function useCreateHealthAssignmentMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: AssignmentCreate) => api.createHealthAssignment(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["health-assignments"] });
      void queryClient.invalidateQueries({ queryKey: ["health-profiles"] });
      void queryClient.invalidateQueries({ queryKey: ["health-device"] });
    },
  });
}

export function useDeleteHealthAssignmentMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteHealthAssignment(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["health-assignments"] });
      void queryClient.invalidateQueries({ queryKey: ["health-profiles"] });
      void queryClient.invalidateQueries({ queryKey: ["health-device"] });
    },
  });
}

// --------------------------------------------------------------------------
// Alerts
// --------------------------------------------------------------------------

export function useHealthAlerts(filters: AlertFilters = {}) {
  const api = useApi();
  return useQuery<AlertOut[], ApiError>({
    queryKey: ["health-alerts", filters],
    queryFn: () => api.listHealthAlerts(filters),
    refetchInterval: ALERT_POLL_MS,
    ...NO_RETRY,
  });
}

export function useHealthAlert(alertId: string | undefined) {
  const api = useApi();
  return useQuery<AlertDetailOut, ApiError>({
    queryKey: ["health-alert", alertId],
    queryFn: () => api.getHealthAlert(alertId as string),
    enabled: alertId != null && alertId !== "",
    ...NO_RETRY,
  });
}

function alertMutationOptions(queryClient: ReturnType<typeof useQueryClient>) {
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["health-alerts"] });
    void queryClient.invalidateQueries({ queryKey: ["health-alert"] });
    void queryClient.invalidateQueries({ queryKey: ["health-device"] });
  };
}

export function useAcknowledgeAlertMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.acknowledgeHealthAlert(id),
    onSuccess: alertMutationOptions(queryClient),
  });
}

export function useMuteAlertMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, mutedUntil }: { id: string; mutedUntil: string }) =>
      api.muteHealthAlert(id, mutedUntil),
    onSuccess: alertMutationOptions(queryClient),
  });
}

export function useCloseAlertMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.closeHealthAlert(id),
    onSuccess: alertMutationOptions(queryClient),
  });
}

export function useRemediateAlertMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, actionKey }: { id: string; actionKey: string }) =>
      api.remediateHealthAlert(id, actionKey),
    onSuccess: alertMutationOptions(queryClient),
  });
}

export function useApproveAlertActionMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ alertId, actionId }: { alertId: string; actionId: string }) =>
      api.approveHealthAlertAction(alertId, actionId),
    onSuccess: alertMutationOptions(queryClient),
  });
}

export function useRefuseAlertActionMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      alertId,
      actionId,
      reason,
    }: {
      alertId: string;
      actionId: string;
      reason: string;
    }) => api.refuseHealthAlertAction(alertId, actionId, reason),
    onSuccess: alertMutationOptions(queryClient),
  });
}

// --------------------------------------------------------------------------
// Device health
// --------------------------------------------------------------------------

export function useDeviceHealth(deviceId: string | undefined) {
  const api = useApi();
  return useQuery<DeviceHealthOut, ApiError>({
    queryKey: ["health-device", deviceId],
    queryFn: () => api.getDeviceHealth(deviceId as string),
    enabled: deviceId != null && deviceId !== "",
    ...NO_RETRY,
  });
}

export function useCheckDeviceNowMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (deviceId: string) => api.checkDeviceHealthNow(deviceId),
    onSuccess: () => {
      // The device's answer arrives on its own schedule (the agent samples, then
      // reports), so this refetch is optimistic: the point is that the panel
      // reflects whatever HAS arrived rather than a stale render.
      void queryClient.invalidateQueries({ queryKey: ["health-device"] });
    },
  });
}

// --------------------------------------------------------------------------
// Settings and the approved-action registry
// --------------------------------------------------------------------------

export function useHealthSettings() {
  const api = useApi();
  return useQuery<SettingsOut, ApiError>({
    queryKey: ["health-settings"],
    queryFn: () => api.getHealthSettings(),
    ...NO_RETRY,
  });
}

export function useUpdateHealthSettingsMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: SettingsUpdate) => api.updateHealthSettings(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["health-settings"] });
    },
  });
}

export function useApprovedActions() {
  const api = useApi();
  return useQuery<ApprovedActionOut[], ApiError>({
    queryKey: ["approved-actions"],
    queryFn: () => api.listApprovedActions(),
    ...NO_RETRY,
  });
}

export function useCreateApprovedActionMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ApprovedActionCreate) => api.createApprovedAction(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["approved-actions"] });
    },
  });
}

export function useUpdateApprovedActionMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ApprovedActionUpdate }) =>
      api.updateApprovedAction(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["approved-actions"] });
    },
  });
}

export function useDeleteApprovedActionMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteApprovedAction(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["approved-actions"] });
    },
  });
}

export function useExemptScriptMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) =>
      api.exemptApprovedActionScript(id, reason),
    onSuccess: () => {
      // Granting re-derives `destructive` on the action, so the row itself
      // changed and not only the exemption.
      void queryClient.invalidateQueries({ queryKey: ["approved-actions"] });
    },
  });
}

export function useRevokeScriptExemptionMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.revokeApprovedActionScriptExemption(id),
    onSuccess: () => {
      // Revoking can DEMOTE an autonomous action to `require_approval`
      // server-side, which is a change to the row nobody asked for and which
      // must be visible immediately.
      void queryClient.invalidateQueries({ queryKey: ["approved-actions"] });
    },
  });
}
