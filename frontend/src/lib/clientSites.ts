/**
 * A client's sites (`GET /clients/{id}/sites`, phase 1), archived ones left
 * out by the server, for the Site field of a form that files something under
 * one (phase 3 design §3.2). The key is IPAM's (`useIpamSites`): the same
 * answer, one cache entry.
 *
 * LOADING until the client is known, not empty (`loadingUntilClient`); `null`
 * reads nothing.
 */

import { useQuery } from "@tanstack/react-query";

import type { ApiError, ClientSiteOut } from "./api";
import { useApi } from "./ApiContext";
import { loadingUntilClient, requireKnown } from "./clientSelection";

export function useClientSites(clientId: string | null) {
  const api = useApi();
  return loadingUntilClient(
    useQuery<ClientSiteOut[], ApiError>({
      queryKey: ["client-sites", clientId],
      queryFn: () => api.listClientSites(requireKnown(clientId)),
      enabled: clientId !== null,
      retry: false,
    }),
    clientId,
  );
}
