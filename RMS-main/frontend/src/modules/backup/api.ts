// TanStack Query hooks for the backup module, built on the shared ApiClient
// (useApi()). There is no live backup event stream — every mutation below
// invalidates the relevant query keys on success so the UI refetches, rather
// than subscribing to a websocket event that doesn't exist for this module
// (mirrors modules/deploy/api.ts's rationale).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "../../lib/ApiContext";
import type {
  OffsiteTargetCreate,
  OffsiteTargetUpdate,
  PolicyUpsertRequest,
  RepositoryCreateRequest,
  RestoreRequest,
} from "./types";

/**
 * The endpoints a backup can be targeted at (cycle 6).
 *
 * WHY IT LIVES HERE. `components/picker.tsx` takes its options rather than
 * fetching them, so each module supplies its own list from its own endpoint —
 * the picker would otherwise have to know every module's permissions and
 * every module's idea of what is selectable.
 *
 * SAME KEY AND SAME FETCHER as the devices module's own list
 * (`modules/devices/DevicesPage.tsx`: `["devices"]` / `api.listDevices()`),
 * so opening this page after that one reuses the cached response instead of
 * issuing a second request, and a device added there refreshes here.
 */
export function useDevices() {
  const api = useApi();
  return useQuery({ queryKey: ["devices"], queryFn: () => api.listDevices() });
}

/**
 * The device groups a backup policy can scope to (cycle 9).
 *
 * `retry: false` -- the same concession `useDevices` above does not need
 * (that read is the page's own subject) but this one does: it feeds a
 * picker, not the policy list, and an operator who may manage policies but
 * may not list groups should meet the picker's `emptyHint` immediately.
 */
export function useDeviceGroups() {
  const api = useApi();
  return useQuery({
    queryKey: ["backup-target-groups"],
    queryFn: async () => (await api.listDeviceGroups()).items,
    retry: false,
    staleTime: 60_000,
  });
}

export function useBackupRepositories() {
  const api = useApi();
  return useQuery({
    queryKey: ["backup-repositories"],
    queryFn: () => api.listBackupRepositories(),
  });
}

export function useCreateBackupRepositoryMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: RepositoryCreateRequest) => api.createBackupRepository(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["backup-repositories"] });
    },
  });
}

export function useBackupPolicies() {
  const api = useApi();
  return useQuery({ queryKey: ["backup-policies"], queryFn: () => api.listBackupPolicies() });
}

export function useUpsertBackupPolicyMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: PolicyUpsertRequest) => api.upsertBackupPolicy(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["backup-policies"] });
    },
  });
}

export function useBackupPoints() {
  const api = useApi();
  return useQuery({ queryKey: ["backup-points"], queryFn: () => api.listBackupPoints() });
}

export function useRunBackupMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (deviceId: string) => api.runBackup(deviceId),
    onSuccess: (_data, deviceId) => {
      void queryClient.invalidateQueries({ queryKey: ["backup-points"] });
      // A device's open DeviceBackupTab reads ["backup-device", deviceId] —
      // refresh it too so an in-progress run shows up there.
      void queryClient.invalidateQueries({ queryKey: ["backup-device", deviceId] });
    },
  });
}

export function useBackupManifest(pointId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["backup-manifest", pointId],
    queryFn: () => api.getBackupManifest(pointId as string),
    enabled: pointId != null,
  });
}

export function useDeviceBackup(deviceId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["backup-device", deviceId],
    queryFn: () => api.getDeviceBackup(deviceId as string),
    enabled: deviceId != null,
  });
}

export function useRestoreBackupMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ pointId, body }: { pointId: string; body: RestoreRequest }) =>
      api.restoreBackupPoint(pointId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["backup-points"] });
    },
  });
}

export function useVerifyBackupMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (pointId: string) => api.verifyBackupPoint(pointId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["backup-points"] });
    },
  });
}

export function useBackupReport() {
  const api = useApi();
  return useQuery({ queryKey: ["backup-report"], queryFn: () => api.getBackupReport() });
}

// ---- Off-site copy, recovery key and coverage ----

export function useOffsiteTargets() {
  const api = useApi();
  return useQuery({
    queryKey: ["backup-offsite-targets"],
    queryFn: () => api.listOffsiteTargets(),
    // A requested test finishes in the worker within about a minute; poll
    // only while one is outstanding.
    refetchInterval: (query) =>
      (query.state.data ?? []).some((t) => t.state === "testing") ? 5_000 : false,
  });
}

export function useCreateOffsiteTargetMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: OffsiteTargetCreate) => api.createOffsiteTarget(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["backup-offsite-targets"] });
    },
  });
}

export function useUpdateOffsiteTargetMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: OffsiteTargetUpdate }) =>
      api.updateOffsiteTarget(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["backup-offsite-targets"] });
    },
  });
}

export function useTestOffsiteTargetMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.testOffsiteTarget(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["backup-offsite-targets"] });
    },
  });
}

export function useSetRepositoryOffsiteMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ repositoryId, targetId }: { repositoryId: string; targetId: string | null }) =>
      api.setRepositoryOffsite(repositoryId, targetId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["backup-repositories"] });
    },
  });
}

export function useRecoveryKeys() {
  const api = useApi();
  return useQuery({ queryKey: ["backup-recovery-keys"], queryFn: () => api.listRecoveryKeys() });
}

export function useRegisterRecoveryKeyMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (publicKey: string) => api.registerRecoveryKey(publicKey),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["backup-recovery-keys"] });
    },
  });
}

export function useNewRecoveryKeyChallengeMutation() {
  const api = useApi();
  return useMutation({ mutationFn: (keyId: string) => api.newRecoveryKeyChallenge(keyId) });
}

export function useProveRecoveryKeyMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ keyId, answer }: { keyId: string; answer: string }) =>
      api.proveRecoveryKey(keyId, answer),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["backup-recovery-keys"] });
    },
  });
}

export function useBackupCoverage(coverage: string) {
  const api = useApi();
  const params: Record<string, string> = { limit: "500" };
  return useQuery({
    queryKey: ["backup-coverage"],
    queryFn: () => api.runReport("backup-coverage", params),
    select: (page) =>
      coverage === "" ? page : { ...page, rows: page.rows.filter((r) => r.coverage === coverage) },
  });
}
