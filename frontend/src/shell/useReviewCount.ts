/**
 * THE NAV COUNT FOR DEVICES: computers waiting for review (install-link spec
 * §1.5). The server's own `state_counts.review`, asked for with a one-row page
 * so the count costs one aggregate, not a list. Only for someone who can act
 * on it (`devices.review`) and read the list (`devices.read`); for anyone else
 * nothing is asked and nothing is shown -- `null`, which the rail renders as
 * no badge, and which is not the same claim as zero.
 *
 * The key starts with `"devices"`, so every Approve and Reject (which
 * invalidate that prefix) refreshes it.
 */

import { useQuery } from "@tanstack/react-query";

import { useApi } from "../lib/ApiContext";

export const REVIEW_COUNT_KEY = ["devices", "review-count"] as const;

export function useReviewCount(permissions: readonly string[]): number | null {
  const api = useApi();
  const enabled = permissions.includes("devices.read") && permissions.includes("devices.review");
  const count = useQuery({
    queryKey: REVIEW_COUNT_KEY,
    queryFn: () => api.listDevices({ state: "review", limit: 1 }),
    enabled,
    staleTime: 30_000,
    refetchInterval: 60_000,
  });
  if (!enabled || count.data == null) return null;
  return count.data.state_counts.review ?? null;
}
