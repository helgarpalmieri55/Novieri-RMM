// TanStack Query hook for the platform status page, built on the shared
// ApiClient (useApi()) -- mirrors modules/subscriptions/api.ts.
//
// Polls every 15s, matching the heartbeat interval (Task 6), so a lost
// instance becomes visible within one cycle. The server caches probe
// results for 5s (health_registry), so this poll cannot turn the page into
// a load generator.

import { useQuery } from "@tanstack/react-query";
import { useApi } from "../../lib/ApiContext";
import type { ApiError, Me } from "../../lib/api";
import type { PlatformStatus } from "./types";

/**
 * The zone the freshness line is rendered in (cycle 12).
 *
 * READ FROM `/me`, NOT THE BROWSER — `lib/datetime.ts` records why the profile
 * zone is the authority. It matters here for the same reason it matters on the
 * error monitor: "probed at 10:14" is a claim an operator lines up against a
 * log line during an incident, and a page that reports it in a different zone
 * from the logs is worse than one that reports nothing.
 *
 * Same `["me"]` key and `staleTime` the rest of the console uses, so this
 * shares the cached response rather than issuing a second request. A null
 * timezone falls the helpers back to the browser zone.
 */
export function useProfileZone(): string | null {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  return me.data?.timezone ?? null;
}

export const PLATFORM_STATUS_POLL_MS = 15_000;

export function usePlatformStatus() {
  const api = useApi();
  return useQuery<PlatformStatus, ApiError>({
    queryKey: ["platform-status"],
    queryFn: () => api.getPlatformStatus(),
    refetchInterval: PLATFORM_STATUS_POLL_MS,
  });
}
