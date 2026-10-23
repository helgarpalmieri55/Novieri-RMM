// TanStack Query hooks for the Subscriptions module, built on the shared
// ApiClient (useApi()). There is no live subscriptions event stream -- every
// mutation below invalidates the relevant query keys on success so the UI
// refetches, rather than subscribing to a websocket event that doesn't exist
// for this module (mirrors modules/licenses/api.ts's rationale -- this
// module dispatches no agent jobs either, per api.py's module docstring).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "../../lib/ApiContext";
import type { SeatAssignCreate, SubscriptionCreate, SubscriptionUpdate } from "./types";

export function useSubscriptions() {
  const api = useApi();
  return useQuery({ queryKey: ["subscriptions"], queryFn: () => api.listSubscriptions() });
}

/**
 * The two lookups the create form needs, read from the endpoints that already
 * serve them (cycle 6).
 *
 * VENDORS SHARE THE LICENSES MODULE'S CACHE KEY ON PURPOSE. There is one
 * `vendors` table -- VendorsPage's own description calls it "the shared vendor
 * record licenses and subscriptions both link against" -- so a second key would
 * let two screens hold two answers about one list, and a vendor added on one
 * would be missing on the other until a reload.
 *
 * BOTH CAN LEGITIMATELY 403. Vendor reads are `licenses.read`-gated and the
 * user directory is `users.read`-gated, and neither is implied by
 * `subscriptions.manage`. That is why the pickers' hints say "you may also not
 * have permission" rather than asserting the list is empty, and why a failure
 * here must never block the form: vendor and owner are optional fields, and a
 * subscription that cannot record its owner is still worth recording.
 */
export function useVendorDirectory() {
  const api = useApi();
  return useQuery({
    queryKey: ["license-vendors"],
    queryFn: () => api.listVendors(),
    retry: false,
  });
}

export function useOrgUsers() {
  const api = useApi();
  return useQuery({ queryKey: ["users"], queryFn: () => api.listUsers(), retry: false });
}

export function useCreateSubscriptionMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: SubscriptionCreate) => api.createSubscription(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["subscriptions"] });
      void queryClient.invalidateQueries({ queryKey: ["subscription-spend"] });
      // A new subscription with a `renews_at` date belongs on the renewal
      // calendar immediately (reporting.py's renewal_calendar selects every
      // subscription with a renewal date), so the calendar is as stale after
      // a create as the spend roll-ups are.
      void queryClient.invalidateQueries({ queryKey: ["subscription-renewals"] });
      // reporting.py's idle_seats reports one row per *active* subscription,
      // so a newly created one lands there with all of its seats idle.
      void queryClient.invalidateQueries({ queryKey: ["subscription-idle-seats"] });
    },
  });
}

export function useSubscription(id: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["subscription", id],
    queryFn: () => api.getSubscription(id as string),
    enabled: id != null,
  });
}

/**
 * The edit this module never had (cycle 7).
 *
 * The note that used to sit here said an update hook would be "an unused
 * mutation hook ... a maintenance liability" because the module shipped no edit
 * form. The audit turned that around: Auditor B's [HIGH] is that the renewal
 * date, notice period and owner -- the inputs to two of the three headline
 * tiles -- had no control anywhere. The create form now carries them, which
 * leaves every subscription recorded before today unfixable, and
 * `PATCH /subscriptions/{subscription_id}` (subscriptions/api.py's
 * `update_subscription_route`) has served exactly this since the module
 * shipped. Wired to the route that exists; nothing invented.
 */
export function useUpdateSubscriptionMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: SubscriptionUpdate }) =>
      api.updateSubscription(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["subscriptions"] });
      void queryClient.invalidateQueries({ queryKey: ["subscription", variables.id] });
      // `renews_at` / `notice_days` / `auto_renew` are precisely what
      // reporting.py's renewal_calendar reads, so an edit that touches them
      // makes the calendar and both deadline tiles stale.
      void queryClient.invalidateQueries({ queryKey: ["subscription-renewals"] });
      // The owner roll-up in the spend report is keyed on `owner_user_id`.
      void queryClient.invalidateQueries({ queryKey: ["subscription-spend"] });
    },
  });
}

export function useCancelSubscriptionMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.cancelSubscription(id),
    onSuccess: (_data, id) => {
      void queryClient.invalidateQueries({ queryKey: ["subscriptions"] });
      void queryClient.invalidateQueries({ queryKey: ["subscription", id] });
      void queryClient.invalidateQueries({ queryKey: ["subscription-events", id] });
      void queryClient.invalidateQueries({ queryKey: ["subscription-spend"] });
      void queryClient.invalidateQueries({ queryKey: ["subscription-renewals"] });
      // idle_seats reports only active subscriptions, so a cancel drops a row.
      void queryClient.invalidateQueries({ queryKey: ["subscription-idle-seats"] });
    },
  });
}

export function useRenewSubscriptionMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.renewSubscription(id),
    onSuccess: (_data, id) => {
      void queryClient.invalidateQueries({ queryKey: ["subscriptions"] });
      void queryClient.invalidateQueries({ queryKey: ["subscription", id] });
      void queryClient.invalidateQueries({ queryKey: ["subscription-events", id] });
      void queryClient.invalidateQueries({ queryKey: ["subscription-renewals"] });
    },
  });
}

export function useSubscriptionEvents(subscriptionId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["subscription-events", subscriptionId],
    queryFn: () => api.listEvents(subscriptionId as string),
    enabled: subscriptionId != null,
  });
}

export function useSeats(subscriptionId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["subscription-seats", subscriptionId],
    queryFn: () => api.listSeats(subscriptionId as string),
    enabled: subscriptionId != null,
  });
}

export function useAssignSeatMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      subscriptionId,
      body,
    }: {
      subscriptionId: string;
      body: SeatAssignCreate;
    }) => api.assignSeat(subscriptionId, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: ["subscription-seats", variables.subscriptionId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["subscription-events", variables.subscriptionId],
      });
      void queryClient.invalidateQueries({ queryKey: ["subscription-idle-seats"] });
    },
  });
}

export function useReleaseSeatMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ seatId }: { seatId: string; subscriptionId: string }) =>
      api.releaseSeat(seatId),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: ["subscription-seats", variables.subscriptionId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["subscription-events", variables.subscriptionId],
      });
      void queryClient.invalidateQueries({ queryKey: ["subscription-idle-seats"] });
    },
  });
}

export function useSpend() {
  const api = useApi();
  return useQuery({ queryKey: ["subscription-spend"], queryFn: () => api.spend() });
}

// `withinDays` is part of the query key so widening the window refetches
// instead of serving the cached default-window result. The mutations above
// invalidate the bare `["subscription-renewals"]` prefix, which TanStack
// Query matches against every windowed key beneath it.
export function useSubscriptionRenewals(withinDays?: number) {
  const api = useApi();
  return useQuery({
    queryKey: ["subscription-renewals", withinDays],
    queryFn: () => api.renewals(withinDays),
  });
}

export function useIdleSeats() {
  const api = useApi();
  return useQuery({ queryKey: ["subscription-idle-seats"], queryFn: () => api.idleSeats() });
}
