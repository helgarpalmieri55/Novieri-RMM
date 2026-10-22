// TanStack Query hooks for the Notification module, built on the shared
// ApiClient (useApi()). There is no live delivery event stream -- every
// mutation below invalidates the relevant query keys on success so the UI
// refetches, mirroring modules/service-desk/api.ts's rationale (this module
// dispatches no agent jobs either -- control-plane only, per api.py's module
// docstring).
//
// Every hook defined here is wired into NotificationAdminPage's panels; none
// are left dangling (the Budget lesson service-desk/api.ts's own header
// documents).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "../../lib/ApiContext";
import type { ApiError } from "../../lib/api";
import type {
  AllowlistEntryCreate,
  ChannelCreate,
  ChannelUpdate,
  DeliveryListFilters,
  DeliveryListResponse,
  RuleCreate,
  RuleUpdate,
} from "./types";

// --------------------------------------------------------------------------
// Channels
// --------------------------------------------------------------------------

export function useNotificationChannels() {
  const api = useApi();
  return useQuery({
    queryKey: ["notification-channels"],
    queryFn: () => api.listNotificationChannels(),
    // A genuine 403 (caller lacks notification.read) should settle
    // immediately rather than retry against the same permission wall --
    // the app-wide QueryClient default (App.tsx) is `retry: 1`.
    retry: false,
  });
}

export function useCreateNotificationChannelMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ChannelCreate) => api.createNotificationChannel(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["notification-channels"] });
    },
  });
}

export function useUpdateNotificationChannelMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ChannelUpdate }) =>
      api.updateNotificationChannel(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["notification-channels"] });
    },
  });
}

export function useDeleteNotificationChannelMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteNotificationChannel(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["notification-channels"] });
    },
  });
}

// Test-send has no query key to invalidate -- it writes no channel row, no
// delivery row, and no outbox event (spec §5/§9's TestSendResult is
// ephemeral, rendered inline where it was triggered).
export function useTestNotificationChannelMutation() {
  const api = useApi();
  return useMutation({
    mutationFn: (id: string) => api.testNotificationChannel(id),
  });
}

// --------------------------------------------------------------------------
// Rules
// --------------------------------------------------------------------------

export function useNotificationRules() {
  const api = useApi();
  return useQuery({
    queryKey: ["notification-rules"],
    queryFn: () => api.listNotificationRules(),
    retry: false,
  });
}

export function useCreateNotificationRuleMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: RuleCreate) => api.createNotificationRule(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["notification-rules"] });
    },
  });
}

export function useUpdateNotificationRuleMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: RuleUpdate }) =>
      api.updateNotificationRule(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["notification-rules"] });
    },
  });
}

export function useDeleteNotificationRuleMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteNotificationRule(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["notification-rules"] });
    },
  });
}

// --------------------------------------------------------------------------
// Allowlist
// --------------------------------------------------------------------------

export function useNotificationAllowlist() {
  const api = useApi();
  return useQuery({
    queryKey: ["notification-allowlist"],
    queryFn: () => api.listNotificationAllowlist(),
    retry: false,
  });
}

export function useCreateAllowlistEntryMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: AllowlistEntryCreate) => api.createNotificationAllowlistEntry(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["notification-allowlist"] });
    },
  });
}

export function useDeleteAllowlistEntryMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteNotificationAllowlistEntry(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["notification-allowlist"] });
    },
  });
}

// --------------------------------------------------------------------------
// Queue members
// --------------------------------------------------------------------------

export function useNotificationQueueMembers(queueId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["notification-queue-members", queueId],
    queryFn: () => api.listNotificationQueueMembers(queueId as string),
    enabled: queueId != null && queueId !== "",
    retry: false,
  });
}

export function useAddNotificationQueueMemberMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ queueId, userId }: { queueId: string; userId: string }) =>
      api.addNotificationQueueMember(queueId, userId),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: ["notification-queue-members", variables.queueId],
      });
    },
  });
}

export function useRemoveNotificationQueueMemberMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ queueId, userId }: { queueId: string; userId: string }) =>
      api.removeNotificationQueueMember(queueId, userId),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: ["notification-queue-members", variables.queueId],
      });
    },
  });
}

// --------------------------------------------------------------------------
// Deliveries
// --------------------------------------------------------------------------

export function useNotificationDeliveries(filters: DeliveryListFilters = {}) {
  const api = useApi();
  return useQuery<DeliveryListResponse, ApiError>({
    queryKey: ["notification-deliveries", filters],
    queryFn: () => api.listNotificationDeliveries(filters),
    retry: false,
  });
}

// A dedicated `status=dead` query, `limit: 1` -- only `total` is needed for
// the prominent dead-letter count (spec §7: "GET .../deliveries?status=dead
// and a count surfaced in the admin UI"); fetching one row's worth avoids
// pulling the whole dead set just to display a number, and keeps the count
// correct regardless of whatever status filter the main list is showing.
export function useNotificationDeadCount() {
  const api = useApi();
  return useQuery<DeliveryListResponse, ApiError>({
    queryKey: ["notification-deliveries", { status: "dead", limit: 1 }],
    queryFn: () => api.listNotificationDeliveries({ status: "dead", limit: 1 }),
    retry: false,
  });
}
