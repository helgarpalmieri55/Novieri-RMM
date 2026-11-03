/**
 * The caller's permission codes (`GET /me`), for deciding what RENDERS.
 *
 * INVARIANT 6: THIS GATES NOTHING. Every route checks its own permission on
 * every request; knowing the list here lets a page say "this cannot be checked
 * from this account" instead of firing a request that can only be refused, and
 * instead of reading the refusal as an empty list.
 *
 * THREE STATES, NOT TWO. `has` and `lacks` are both false until `/me` answers
 * (and if it fails): a permission list that has not loaded is not a missing
 * permission, and a page that said "you cannot see this" during the first
 * paint would be wrong for every administrator on every visit.
 *
 * TWO LISTS SINCE PHASE 3. `useMePermissions` is the codes held in at least one
 * client of the request's scope -- what a page draws its controls for;
 * `useEverywherePermissions` the codes held on every client -- what a write to
 * configuration every client shares is offered for.
 *
 * Shares the `["me"]` query key every other page uses, so this is the same
 * cache entry and never a second request.
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import type { ApiError, Me } from "./api";
import { useApi } from "./ApiContext";

export interface MePermissions {
  /** `loading` until `/me` answers, `error` if it could not be read, `ready` after. */
  readonly status: "loading" | "error" | "ready";
  /** The codes `/me` returned; empty until `ready`. */
  readonly codes: readonly string[];
  /** True only once `/me` has answered and lists the code. */
  has(code: string): boolean;
  /** True only once `/me` has answered and does NOT list the code. Never true while loading or on error. */
  lacks(code: string): boolean;
}

function fromMe(me: UseQueryResult<Me, ApiError>, codes: readonly string[]): MePermissions {
  const status: MePermissions["status"] = me.isSuccess ? "ready" : me.isError ? "error" : "loading";
  const shown = status === "ready" ? codes : [];
  return {
    status,
    codes: shown,
    has: (code) => status === "ready" && shown.includes(code),
    lacks: (code) => status === "ready" && !shown.includes(code),
  };
}

function useMeQuery() {
  const api = useApi();
  return useQuery<Me, ApiError>({
    queryKey: ["me"],
    queryFn: () => api.me(),
    staleTime: 60_000,
    retry: false,
  });
}

/** The codes held in at least one client of the request's scope (`/me`'s `permissions`). */
export function useMePermissions(): MePermissions {
  const me = useMeQuery();
  return fromMe(me, me.data?.permissions ?? []);
}

/**
 * The codes held on EVERY client (`/me`'s `permissions_everywhere`, phase 3
 * design §1.3): what a screen of configuration every client shares offers its
 * writes on (§2). The server refuses those writes to a narrower scope with
 * 403 (`backend/tests/test_global_writes.py`); this saves the person a
 * control that can only answer that.
 *
 * AN API OLDER THAN PHASE 3 SENDS NO `permissions_everywhere`, and then
 * `permissions` IS this set: before phase 3, `/me` returned only the codes
 * held on every client.
 */
export function useEverywherePermissions(): MePermissions {
  const me = useMeQuery();
  return fromMe(me, me.data?.permissions_everywhere ?? me.data?.permissions ?? []);
}
