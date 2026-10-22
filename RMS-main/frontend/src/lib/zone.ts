/**
 * The zone the console shows times in: the organization's, then the
 * operator's profile, then (null) the browser's. One hook so a page does not
 * have to choose; several modules still carry their own copy of this.
 */

import { useQuery } from "@tanstack/react-query";

import type { CurrentOrg, Me } from "./api";
import { useApi } from "./ApiContext";

export function useOrgZone(): string | null {
  const api = useApi();
  const org = useQuery<CurrentOrg>({
    queryKey: ["org-zone"],
    queryFn: () => api.getCurrentOrg(),
    staleTime: 300_000,
    retry: false,
  });
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  return org.data?.settings.timezone || me.data?.timezone || null;
}
