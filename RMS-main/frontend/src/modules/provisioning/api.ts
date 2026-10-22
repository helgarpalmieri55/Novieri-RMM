// TanStack Query hooks for the provisioning module, built on the shared
// ApiClient (useApi()). There is no live provisioning event stream — every
// mutation below invalidates the relevant query keys on success so the UI
// refetches, rather than subscribing to a websocket event that doesn't exist
// for this module (mirrors modules/backup/api.ts's rationale).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "../../lib/ApiContext";
import type { Me } from "../../lib/api";
import type {
  OsImageCreateRequest,
  ProfileUpsertRequest,
  RunStartRequest,
} from "./types";

/**
 * The zone every date on a provisioning screen is rendered in (cycle 5).
 *
 * READ FROM `/me`, NOT THE BROWSER. `lib/datetime.ts` explains why: an
 * operator who sets a timezone on their profile must see the same instant
 * spelled the same way from whichever machine they are sitting at, and two
 * people reading one provisioning run must be able to say which zone they are
 * each reading it in.
 *
 * Same `["me"]` key and `staleTime` the pages already use, so this shares the
 * cached response rather than issuing a second request — and it means
 * `DeviceProvisioningTab`, which is mounted by the devices module and cannot
 * be handed a `timezone` prop without editing a file this module does not
 * own, still gets the profile zone instead of falling back to the browser's.
 *
 * `null` (not `undefined`) when the profile carries no timezone: the helpers
 * treat both as "fall back to the browser zone", and being explicit about it
 * keeps the fallback visible at the call site.
 */
export function useUserTimezone(): string | null {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  return me.data?.timezone ?? null;
}

export function useOsImages() {
  const api = useApi();
  return useQuery({ queryKey: ["provisioning-images"], queryFn: () => api.listOsImages() });
}

export function useCreateOsImageMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: OsImageCreateRequest) => api.createOsImage(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["provisioning-images"] });
    },
  });
}

export function useProvisionProfiles() {
  const api = useApi();
  return useQuery({
    queryKey: ["provisioning-profiles"],
    queryFn: () => api.listProvisionProfiles(),
  });
}

export function useCreateProvisionProfileMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ProfileUpsertRequest) => api.createProvisionProfile(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["provisioning-profiles"] });
    },
  });
}

export function useUpdateProvisionProfileMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ProfileUpsertRequest }) =>
      api.updateProvisionProfile(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["provisioning-profiles"] });
    },
  });
}

export function useStartProvisionRunMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ profileId, body }: { profileId: string; body: RunStartRequest }) =>
      api.startProvisionRun(profileId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["provisioning-runs"] });
    },
  });
}

export function useProvisionRuns() {
  const api = useApi();
  return useQuery({ queryKey: ["provisioning-runs"], queryFn: () => api.listProvisionRuns() });
}

export function useProvisionRun(id: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["provisioning-run", id],
    queryFn: () => api.getProvisionRun(id as string),
    enabled: id != null,
  });
}

export function useDeviceProvisioning(deviceId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["provisioning-device", deviceId],
    queryFn: () => api.getDeviceProvisioning(deviceId as string),
    enabled: deviceId != null,
  });
}

export function useProvisioningReport() {
  const api = useApi();
  return useQuery({
    queryKey: ["provisioning-report"],
    queryFn: () => api.getProvisioningReport(),
  });
}
