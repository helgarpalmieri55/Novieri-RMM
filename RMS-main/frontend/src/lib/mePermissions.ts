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
 * Shares the `["me"]` query key every other page uses, so this is the same
 * cache entry and never a second request.
 */

import { useQuery } from "@tanstack/react-query";

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

export function useMePermissions(): MePermissions {
  const api = useApi();
  const me = useQuery<Me, ApiError>({
    queryKey: ["me"],
    queryFn: () => api.me(),
    staleTime: 60_000,
    retry: false,
  });
  const status: MePermissions["status"] = me.isSuccess ? "ready" : me.isError ? "error" : "loading";
  const codes = me.data?.permissions ?? [];
  return {
    status,
    codes,
    has: (code) => status === "ready" && codes.includes(code),
    lacks: (code) => status === "ready" && !codes.includes(code),
  };
}
