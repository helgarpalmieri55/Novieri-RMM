// TanStack Query hooks for the site-server console, on the shared ApiClient.
// Mirrors modules/ipam/api.ts: every mutation invalidates the query keys its
// SERVER-SIDE EFFECTS reach.
//
// Those effects are small here, and one is worth naming: PROMOTING OR DEMOTING
// A SITE SERVER CHANGES WHICH DEVICE IPAM PREFERS FOR A SWEEP. That preference
// lives in one ORDER BY term inside `select_sweeper` and is evaluated per
// dispatch, so there is nothing cached on this side to invalidate — but a
// reader looking for "what else does this touch" should find the answer here
// rather than concluding it touches nothing.
//
// NOTHING POLLS THE MACHINE BEING WOKEN. A wake is fire-and-forget by
// construction (§0.9): the magic packet is unacknowledged, `SiteWakeResult`
// carries `sent` and deliberately not `woke`, and a UI that polled "is it up
// yet?" would be inventing the reachability signal the whole module declines to
// provide. Confirmation that a machine woke is its agent connecting, which the
// device list already shows.
//
// `useWakeJob` DOES follow the JOB, and that is a different question with a
// different answer. `POST /wake` returns 202 with a `job_id`: at that instant
// the job is `queued` and nothing has left any NIC. Whether the site server has
// taken the job, reported a packet, or let it expire is the CONTROL PLANE'S OWN
// record of work it dispatched — the same thing every other job view in this
// console reads — not a claim about the target host. Without it the UI has only
// the 202 and can only ever say "we asked", which is why `WakeAction` used to
// say something stronger and false.
//
// It stops at the first terminal status, so an operator who leaves the pane open
// is not holding a request loop against `/jobs/{id}` for the rest of the day.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiError, Device, Job, JobStatus, Me, Page } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import type {
  SegmentCheckOut,
  SiteServerCreate,
  SiteServerFilters,
  SiteServerOut,
  SiteServerUpdate,
  SiteWakeOut,
  SiteWakeRequest,
} from "./types";
import { SITE_MANAGE, SITE_READ, SITE_WAKE } from "./types";

// A genuine 403 should settle immediately rather than retry against the same
// permission wall.
const NO_RETRY = { retry: false } as const;

export const SITE_SERVERS_KEY = "site-servers";
export const SEGMENT_CHECK_KEY = "site-server-segment-check";

/**
 * The caller's permission codes, for deciding what RENDERS.
 *
 * INVARIANT #6: THIS GATES NOTHING. Every route carries its own
 * `require_permission` dependency and the server re-checks on every request.
 * Hiding the wake button saves an operator a 403; it prevents exactly nothing,
 * and a caller who forges this list gets the same 403 they would have got.
 */
export function useSiteServerPermissions(): {
  read: boolean;
  manage: boolean;
  wake: boolean;
} {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  return {
    read: permissions.includes(SITE_READ),
    manage: permissions.includes(SITE_MANAGE),
    wake: permissions.includes(SITE_WAKE),
  };
}

/** `enabled: false` holds the request back (e.g. until `/me` says the account holds `site.read`). */
export function useSiteServers(filters: SiteServerFilters = {}, options: { enabled?: boolean } = {}) {
  const api = useApi();
  return useQuery<SiteServerOut[], ApiError>({
    queryKey: [SITE_SERVERS_KEY, filters],
    queryFn: () => api.listSiteServers(filters),
    enabled: options.enabled ?? true,
    ...NO_RETRY,
  });
}

export function useSiteServer(id: string) {
  const api = useApi();
  return useQuery<SiteServerOut, ApiError>({
    queryKey: [SITE_SERVERS_KEY, id],
    queryFn: () => api.getSiteServer(id),
    ...NO_RETRY,
  });
}

export function useSiteServerSegmentCheck(id: string) {
  const api = useApi();
  return useQuery<SegmentCheckOut, ApiError>({
    queryKey: [SEGMENT_CHECK_KEY, id],
    queryFn: () => api.getSiteServerSegmentCheck(id),
    ...NO_RETRY,
  });
}

/** Devices offerable for promotion — see `PromoteDialog` for the agent rule. */
export function usePromotableDevices() {
  const api = useApi();
  return useQuery<Page<Device>, ApiError>({
    queryKey: ["devices"],
    queryFn: () => api.listDevices(),
    ...NO_RETRY,
  });
}

export function usePromoteSiteServerMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<SiteServerOut, ApiError, SiteServerCreate>({
    mutationFn: (body) => api.promoteSiteServer(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [SITE_SERVERS_KEY] });
    },
  });
}

export function useUpdateSiteServerMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<SiteServerOut, ApiError, { id: string; body: SiteServerUpdate }>({
    mutationFn: ({ id, body }) => api.updateSiteServer(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: [SITE_SERVERS_KEY] });
      // Editing `segments` changes what the segment check compares against, so
      // its cached answer is stale the moment this succeeds.
      void queryClient.invalidateQueries({ queryKey: [SEGMENT_CHECK_KEY, variables.id] });
    },
  });
}

export function useDemoteSiteServerMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, { id: string; reason?: string }>({
    mutationFn: ({ id, reason }) => api.demoteSiteServer(id, reason ?? ""),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [SITE_SERVERS_KEY] });
    },
  });
}

/**
 * Send a wake.
 *
 * INVALIDATES NOTHING, and that is correct rather than an oversight: a wake
 * changes no site-server state. It mints a job whose result says only that a
 * datagram was sent (§0.9). There is no row here that a successful wake would
 * make stale.
 */
export function useWakeViaSiteServerMutation() {
  const api = useApi();
  return useMutation<SiteWakeOut, ApiError, { id: string; body: SiteWakeRequest }>({
    mutationFn: ({ id, body }) => api.wakeViaSiteServer(id, body),
  });
}

export const WAKE_JOB_KEY = "site-server-wake-job";

/** Statuses after which there is nothing further to learn about a wake. */
export const TERMINAL_WAKE_STATUSES: readonly JobStatus[] = [
  "succeeded",
  "failed",
  "expired",
  "refused",
] as const;

/** A wake is dead five minutes after dispatch (`WAKE_TTL_S`), so this is short. */
const WAKE_POLL_MS = 3_000;

/**
 * Follow the wake JOB — queued, then dispatched, then a reported result.
 *
 * The 202 from `POST /wake` describes a signed job, not a datagram. This is how
 * the UI learns the difference, and the reason `WakeAction` can distinguish
 * "we asked" from "the site server says it sent one".
 *
 * **REFETCHING STOPS AT A TERMINAL STATUS**, and the predicate is written over
 * `TERMINAL_WAKE_STATUSES` rather than as `!== "queued"`: a status this list
 * does not know is treated as NOT terminal and keeps polling, which is the safe
 * direction. Treating an unknown status as terminal would freeze the pane on
 * whatever it last saw and quietly reintroduce a stale claim.
 *
 * `GET /jobs/{id}` is gated on `jobs.read`, which the seeded `technician` role
 * holds alongside `site.wake` — but a bespoke role need not. A 403 here settles
 * immediately (`NO_RETRY`) and leaves `data` undefined, which `wakeStateOf`
 * renders as `queued`: "we could not look" and "nothing has been sent yet" are
 * both honest, and neither claims a packet. That is the whole reason the
 * unknown case collapses to the cautious state rather than the optimistic one.
 */
export function useWakeJob(jobId: string | null | undefined) {
  const api = useApi();
  return useQuery<Job, ApiError>({
    queryKey: [WAKE_JOB_KEY, jobId],
    queryFn: () => api.getJob(jobId as string),
    enabled: Boolean(jobId),
    refetchInterval: (query) =>
      query.state.data && TERMINAL_WAKE_STATUSES.includes(query.state.data.status)
        ? false
        : WAKE_POLL_MS,
    ...NO_RETRY,
  });
}
