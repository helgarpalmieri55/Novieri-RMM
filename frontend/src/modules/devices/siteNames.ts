/**
 * Site names for the fleet list's Site column and its filter, and for a
 * device's page (MSP clients phase 3, design §4.3).
 *
 * `GET /devices` sends each row's `site_id`, never the name. The names are
 * each client's own sites (`GET /clients/{id}/sites`), read once per client
 * on the page under the key IPAM and the site pickers already share, so the
 * cache is one. Read only for a client where the person holds `clients.read`
 * -- the route's code -- so the page never asks for a 403; without it the
 * name is unknown and says so.
 */
import { useQueries, useQuery } from "@tanstack/react-query";

import type { ClientSiteOut, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";

export function useSitesOf(clientIds: readonly string[]): {
  /** The client's live sites; `undefined` while they load, or when the person may not read them. */
  sitesOf(clientId: string): readonly ClientSiteOut[] | undefined;
  /** The site's name, or `null` when it is not one of those sites (or there is none). */
  siteName(siteId: string | undefined): string | null;
} {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const readable = new Set(
    (me.data?.clients ?? [])
      .filter((client) => client.permissions.includes("clients.read"))
      .map((client) => client.id),
  );
  const wanted = [...new Set(clientIds)].filter((clientId) => readable.has(clientId));
  const results = useQueries({
    queries: wanted.map((clientId) => ({
      queryKey: ["client-sites", clientId],
      queryFn: () => api.listClientSites(clientId),
      staleTime: 60_000,
      retry: false,
    })),
  });
  const byClient = new Map<string, readonly ClientSiteOut[]>();
  const names = new Map<string, string>();
  wanted.forEach((clientId, index) => {
    const data = results[index]?.data;
    if (!data) return;
    byClient.set(clientId, data);
    for (const site of data) names.set(site.id, site.name);
  });
  return {
    sitesOf: (clientId) => byClient.get(clientId),
    siteName: (siteId) => (siteId ? (names.get(siteId) ?? null) : null),
  };
}
