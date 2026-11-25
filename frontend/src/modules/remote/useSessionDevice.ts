/**
 * THE SESSION AND THE MACHINE IT IS ON, for a session's record head (the
 * viewer and the recording player, redesign plan O3 3.6): the session by id,
 * and the device list it is named from -- the same two reads, under the same
 * keys, as the viewer always made, so the list page's cache answers them.
 */
import { useQuery } from "@tanstack/react-query";

import type { Device } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";

export function useSessionDevice(sessionId: string | undefined) {
  const api = useApi();
  const session = useQuery({
    queryKey: ["remote-sessions", sessionId],
    queryFn: () => api.getRemoteSession(sessionId as string),
    enabled: sessionId != null,
  });
  const devices = useQuery({ queryKey: ["devices"], queryFn: () => api.listDevices() });
  const device: Device | undefined = devices.data?.items.find((item) => item.id === session.data?.device_id);
  const deviceName = device?.hostname ?? device?.fqdn ?? null;
  return { session, device, deviceName };
}
