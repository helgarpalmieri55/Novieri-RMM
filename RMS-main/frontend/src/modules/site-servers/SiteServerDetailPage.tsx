// One site server, as a routed page. The container half of `SiteServerDetail`.
//
// See `SiteServersPage` for why these containers did not exist until
// 2026-08-14 and what that cost.
//
// ON THE v3 KIT (`P['site-servers/:siteServerId']` in
// docs/design/itms-prototype-v3.html): the head names the server and, under
// it, the device it runs on and what it declares; the facts strip and the
// panels are `SiteServerDetail`'s (every `data-testid` in
// `siteServers.test.tsx` depends on that component, so it keeps them).
//
// THE DEVICE IS JOINED HERE, NOT IN THE API. `SiteServerOut` carries the
// `device_id` and nothing else about the machine, because a site server IS a
// promoted device and the device's own record is the authority on its
// hostname and address. The join is one `getDevice` read, and a device the
// caller cannot read (no `devices.read`) leaves the sub-line at the
// declaration alone rather than failing the page.
//
// WHAT v3 DRAWS THAT IS NOT SERVED. "Sweep now" in the head and "Push
// installs 3 this week" in the facts belong to IPAM's sweep and to the push
// module; neither is a site-server API, so neither is drawn here.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";

import { useT } from "../../i18n/context";

import {
  useSiteServer,
  useSiteServerPermissions,
  useSiteServerSegmentCheck,
  useWakeJob,
  useWakeViaSiteServerMutation,
} from "./api";
import { SiteServerDetail } from "./SiteServerDetail";
import type { SiteWakeRequest } from "./types";
import type { ApiError, Device } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useOrgZone } from "../../lib/zone";
import { PageHead } from "../../shell/components";

export function SiteServerDetailPage() {
  const t = useT();
  const { siteServerId = "" } = useParams();
  const api = useApi();
  const { wake: canWake } = useSiteServerPermissions();
  const server = useSiteServer(siteServerId);
  const check = useSiteServerSegmentCheck(siteServerId);
  const wake = useWakeViaSiteServerMutation();
  const zone = useOrgZone();

  const deviceId = server.data?.device_id;
  const device = useQuery<Device, ApiError>({
    queryKey: ["devices", deviceId],
    queryFn: () => api.getDevice(deviceId as string),
    enabled: Boolean(deviceId),
    retry: false,
  });

  // The 202 from POST /wake describes a signed JOB, not a datagram. Holding the
  // job id here is what lets `useWakeJob` follow it to a terminal status, so
  // the pane can distinguish "we asked" from "the site server says it sent
  // one" -- the distinction `api.ts` is emphatic about.
  const [wakeJobId, setWakeJobId] = useState<string | null>(null);
  const wakeJob = useWakeJob(wakeJobId);

  const onWake = (body: SiteWakeRequest) => {
    wake.mutate(
      { id: siteServerId, body },
      { onSuccess: (result) => setWakeJobId(result.job_id ?? null) },
    );
  };

  const segments = server.data?.segments.length ?? 0;
  const description = [
    device.data?.hostname ?? null,
    device.data?.primary_ip ?? null,
    server.data ? t("siteServers.detail.declaredFor", { count: segments }) : null,
  ]
    .filter((part): part is string => part !== null)
    .join(" · ");

  return (
    <section>
      <PageHead
        title={server.data?.name ?? t("siteServers.detail.fallbackTitle")}
        description={description || undefined}
      />
      <SiteServerDetail
        server={server.data}
        device={device.data}
        check={check.data}
        checkLoading={check.isLoading}
        canWake={canWake}
        onWake={onWake}
        wakeResult={wake.data}
        wakeJob={wakeJob.data}
        wakeError={wake.error}
        zone={zone}
      />
    </section>
  );
}
