// The person using the portal (own-auth phase 2, Task 10, decision 3): a
// client's contact or a technician with a portal code. The portal's pages
// read their permissions and time zone from here, never from `GET /me`,
// which a contact does not have.
import { useQuery } from "@tanstack/react-query";

import type { ApiError, PortalMe } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";

export const PORTAL_ME_KEY = "portal-me";

export function usePortalMe() {
  const api = useApi();
  return useQuery<PortalMe, ApiError>({
    queryKey: [PORTAL_ME_KEY],
    queryFn: () => api.portalMe(),
    staleTime: 60_000,
    retry: false,
  });
}
