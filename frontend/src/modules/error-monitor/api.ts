// TanStack Query hooks for the error monitor page, built on the shared
// ApiClient (useApi()) -- mirrors modules/platform-status/api.ts for the
// queries and modules/subscriptions/api.ts for the resolve mutation's shape.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "../../lib/ApiContext";
import type { ApiError, Me } from "../../lib/api";
import type { ErrorGroupDetail, ErrorGroupList } from "./types";

/**
 * The zone every timestamp on this page is rendered in (cycle 5).
 *
 * READ FROM `/me`, NOT THE BROWSER — `lib/datetime.ts` records why the profile
 * zone is the authority. It matters more here than almost anywhere: the whole
 * point of this page is to hand the operator a first-seen and a last-seen they
 * then go and grep the logs for, and a page that reports those in one zone
 * while the logs are written in another sends them looking in the wrong hour.
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

export const ERROR_GROUPS_QUERY_KEY = ["error-groups"] as const;

// Explicit, rather than relying on the server's default (currently 100,
// errors.py's `list_error_groups`): the page needs to know this number so it
// can tell the operator when the list was truncated at it (see
// ErrorMonitorPage's truncation banner) instead of silently showing only the
// newest N of a larger fleet.
export const ERROR_GROUPS_LIST_LIMIT = 100;

export function useErrorGroups() {
  const api = useApi();
  return useQuery<ErrorGroupList, ApiError>({
    queryKey: ERROR_GROUPS_QUERY_KEY,
    queryFn: () => api.listErrorGroups(ERROR_GROUPS_LIST_LIMIT),
  });
}

export function useErrorGroupDetail(groupId: string | undefined) {
  const api = useApi();
  return useQuery<ErrorGroupDetail, ApiError>({
    queryKey: ["error-group", groupId],
    queryFn: () => api.getErrorGroup(groupId as string),
    enabled: groupId != null,
  });
}

/**
 * The caller's triage privilege, for deciding what RENDERS.
 *
 * INVARIANT #6: THIS GATES NOTHING. Both writes carry
 * `Depends(require_permission("platform.errors.write"))` server-side and the
 * server re-checks every request, so a caller who forges this list gets the
 * same 403 they would have got. What it prevents is a reader being offered
 * two controls that can only answer 403 — the console's own defect, not the
 * server's.
 *
 * ONE CODE FOR BOTH DIRECTIONS, matching `app/api/v1/errors.py`: resolving
 * and reopening are the same privilege, so drawing one without the other
 * would invent a distinction the API does not make.
 */
export function useErrorTriagePermission(): boolean {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  return (me.data?.permissions ?? []).includes("platform.errors.write");
}

export function useResolveErrorGroupMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (groupId: string) => api.resolveErrorGroup(groupId),
    onSuccess: (_data, groupId) => {
      void queryClient.invalidateQueries({ queryKey: ERROR_GROUPS_QUERY_KEY });
      void queryClient.invalidateQueries({ queryKey: ["error-group", groupId] });
    },
  });
}

/**
 * Reopen, invalidating exactly what resolve does.
 *
 * THE LIST IS THE ANSWER, not this mutation's response body. The route does
 * return the updated group, but the page renders the group out of the list
 * query — so a reopen that updated only the mutation's own cache would leave
 * a row reading "Resolved" over a group the server has reopened, and the Open
 * KPI counting it as resolved. Same two keys as resolve, for the same reason.
 */
export function useUnresolveErrorGroupMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (groupId: string) => api.unresolveErrorGroup(groupId),
    onSuccess: (_data, groupId) => {
      void queryClient.invalidateQueries({ queryKey: ERROR_GROUPS_QUERY_KEY });
      void queryClient.invalidateQueries({ queryKey: ["error-group", groupId] });
    },
  });
}
