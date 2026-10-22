// TanStack Query hooks for the deploy module, built on the shared ApiClient
// (useApi()). There is no live deploy event stream — every mutation below
// invalidates the relevant query keys on success so the UI refetches, rather
// than subscribing to a websocket event that doesn't exist for this module
// (mirrors modules/patch/api.ts's rationale).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "../../lib/ApiContext";
import type { CurrentOrg, Me } from "../../lib/api";
import type {
  DeploymentCreateRequest,
  PackageCreateRequest,
  ScriptCreateRequest,
  ScriptPolicyCreateRequest,
  ScriptPolicyUpdateRequest,
  ScriptRunRequest,
  ScriptUpdateRequest,
} from "./types";

/**
 * The zone every date on a deploy screen is rendered in (cycle 5).
 *
 * READ FROM `/me`, NOT THE BROWSER — see `lib/datetime.ts` for why the
 * profile zone is the authority. Same `["me"]` key and `staleTime` the pages
 * already use, so this shares the cached response rather than issuing a
 * second request.
 *
 * It also gives `DeviceDeployTab` a zone: that tab is mounted by the devices
 * module with only a `deviceId` prop, and threading `timezone` down to it
 * would mean editing a file this module does not own.
 */
export function useUserTimezone(): string | null {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  return me.data?.timezone ?? null;
}

/** The organization's zone, then the operator's: schedule times are about the
 *  fleet's working day. */
export function useOrgZone(): string | null {
  const api = useApi();
  const profile = useUserTimezone();
  const org = useQuery<CurrentOrg>({
    queryKey: ["deploy-org-zone"],
    queryFn: () => api.getCurrentOrg(),
    staleTime: 300_000,
    retry: false,
  });
  return org.data?.settings.timezone || profile;
}

export function usePackages() {
  const api = useApi();
  return useQuery({ queryKey: ["deploy-packages"], queryFn: () => api.listPackages() });
}

/**
 * The endpoints a deployment or a script run can be aimed at (cycle 6).
 *
 * `retry: false` because this list is a CONVENIENCE on a dialog, not the
 * dialog's subject: `/devices` carries its own permission, and an operator who
 * may deploy but may not list devices should meet the picker's `emptyHint`
 * immediately rather than three silent retries later. Its own query key rather
 * than `["devices"]`, so a 403 here never poisons the devices page's cache.
 */
export function useTargetDevices() {
  const api = useApi();
  return useQuery({
    queryKey: ["deploy-target-devices"],
    queryFn: async () => (await api.listDevices()).items,
    retry: false,
    staleTime: 60_000,
  });
}

/**
 * The device groups a deployment or a script run can target (cycle 9).
 *
 * `retry: false` for the same reason `useTargetDevices` above gives: this is
 * a picker's list, not the dialog's subject, and an operator who may deploy
 * but may not list groups should meet the picker's `emptyHint` immediately.
 * Its own query key rather than `["device-groups"]`, so a 403 here never
 * poisons the Device groups page's cache.
 */
export function useDeviceGroups() {
  const api = useApi();
  return useQuery({
    queryKey: ["deploy-target-groups"],
    queryFn: async () => (await api.listDeviceGroups()).items,
    retry: false,
    staleTime: 60_000,
  });
}

/**
 * The org directory, for naming the person a deployment was requested by.
 *
 * REVIEW WAVE, round 2, /deploy (composition): the design's rows carry a BY
 * column and the console's did not, though `Deployment.requested_by` is on
 * every row. It is a user id, so a name needs this read.
 *
 * `retry: false` and the same `["users"]` key licenses and subscriptions
 * already use, so the three share one cached answer: `/users` carries its own
 * permission, and an operator who may deploy but may not read the directory
 * gets one 403 and the id in mono, not three retries and a blank column.
 */
export function useOrgUsers() {
  const api = useApi();
  return useQuery({ queryKey: ["users"], queryFn: () => api.listUsers(), retry: false });
}

export function useCreatePackageMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: PackageCreateRequest) => api.createPackage(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["deploy-packages"] });
    },
  });
}

export function useUploadPackageArtifactMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ packageId, file }: { packageId: string; file: File }) =>
      api.uploadPackageArtifact(packageId, file),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["deploy-packages"] });
    },
  });
}

export function useDeletePackageMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deletePackage(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["deploy-packages"] });
    },
  });
}

export function useDeployments() {
  const api = useApi();
  return useQuery({ queryKey: ["deploy-deployments"], queryFn: () => api.listDeployments() });
}

export function useCreateDeploymentMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: DeploymentCreateRequest) => api.createDeployment(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["deploy-deployments"] });
    },
  });
}

export function useExecuteDeploymentMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.executeDeployment(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["deploy-deployments"] });
      // A device's open DeviceDeployTab reads ["deploy-device", deviceId] —
      // refresh it too so an in-progress execute shows up there.
      void queryClient.invalidateQueries({ queryKey: ["deploy-device"] });
    },
  });
}

export function useDeviceDeployments(deviceId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["deploy-device", deviceId],
    queryFn: () => api.getDeviceDeployments(deviceId as string),
    enabled: deviceId != null,
  });
}

export function useScripts() {
  const api = useApi();
  return useQuery({ queryKey: ["deploy-scripts"], queryFn: () => api.listScripts() });
}

export function useScript(id: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["deploy-script", id],
    queryFn: () => api.getScript(id as string),
    enabled: id != null,
  });
}

export function useAuthorScriptMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ScriptCreateRequest) => api.authorScript(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["deploy-scripts"] });
    },
  });
}

export function useEditScriptMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ScriptUpdateRequest }) =>
      api.editScript(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["deploy-scripts"] });
      // Drop the cached single-script body too, so reopening Edit for this
      // script never prefills the pre-edit body from a stale cache entry.
      void queryClient.invalidateQueries({ queryKey: ["deploy-script", variables.id] });
    },
  });
}

export function useRunScriptMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ScriptRunRequest }) => api.runScript(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["deploy-scripts"] });
    },
  });
}

export const SCRIPT_POLICIES_KEY = "deploy-script-policies";

export function useScriptPolicies(enabled = true) {
  const api = useApi();
  return useQuery({
    queryKey: [SCRIPT_POLICIES_KEY],
    queryFn: () => api.listScriptPolicies(),
    enabled,
  });
}

export function useScriptPolicyRuns(id: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: [SCRIPT_POLICIES_KEY, id, "runs"],
    queryFn: () => api.listScriptPolicyRuns(id as string),
    enabled: id != null,
  });
}

/** Device groups a schedule can target. */
export function useScheduleGroups(enabled = true) {
  const api = useApi();
  return useQuery({
    queryKey: ["deploy-schedule-groups"],
    queryFn: async () => (await api.listDeviceGroups()).items,
    enabled,
    retry: false,
    staleTime: 60_000,
  });
}

export function useCreateScriptPolicyMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ScriptPolicyCreateRequest) => api.createScriptPolicy(body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [SCRIPT_POLICIES_KEY] }),
  });
}

export function useUpdateScriptPolicyMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ScriptPolicyUpdateRequest }) =>
      api.updateScriptPolicy(id, body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [SCRIPT_POLICIES_KEY] }),
  });
}

export function useDeleteScriptPolicyMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteScriptPolicy(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [SCRIPT_POLICIES_KEY] }),
  });
}

/** Changes a deployment can be linked to: approved, or being implemented. */
export function useAuthorizingChanges(enabled = true) {
  const api = useApi();
  return useQuery({
    queryKey: ["deploy-authorizing-changes"],
    queryFn: async () =>
      (await api.listChanges()).filter((c) => c.state === "approved" || c.state === "implementing"),
    enabled,
    retry: false,
    staleTime: 30_000,
  });
}
