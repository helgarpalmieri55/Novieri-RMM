/** The client's assets, as the rail names them (design §1.1): licenses, subscriptions, networks. */
import { useQuery } from "@tanstack/react-query";

import { useT } from "../../i18n/context";
import type { ClientOut } from "../../lib/api";
import { StatCard, StatsGrid, useNarrowedApi } from "../../shell/components";

export function AssetsTab({ client, codes, enabled }: { client: ClientOut; codes: ReadonlySet<string>; enabled: boolean }) {
  const t = useT();
  const { api, client: sent, ready } = useNarrowedApi(client.id);
  const licenses = useQuery({
    queryKey: ["client360", client.id, "licenses", sent],
    queryFn: () => api.listLicenses(),
    enabled: enabled && ready && codes.has("licenses.read"),
  });
  const subscriptions = useQuery({
    queryKey: ["client360", client.id, "subscriptions", sent],
    queryFn: () => api.listSubscriptions(),
    enabled: enabled && ready && codes.has("subscriptions.read"),
  });
  const subnets = useQuery({
    queryKey: ["client360", client.id, "subnets", sent],
    queryFn: () => api.listIpamSubnets(),
    enabled: enabled && ready && codes.has("ipam.read"),
  });
  return (
    <StatsGrid columns={3}>
      {codes.has("licenses.read") ? (
        <StatCard label={t("clients.assets.licenses")} value={String(licenses.data?.length ?? 0)} pending={licenses.data === undefined} to={`/licenses?client=${client.id}`} icon="license" />
      ) : null}
      {codes.has("subscriptions.read") ? (
        <StatCard label={t("clients.assets.subscriptions")} value={String(subscriptions.data?.length ?? 0)} pending={subscriptions.data === undefined} to={`/subscriptions?client=${client.id}`} icon="subscription" />
      ) : null}
      {codes.has("ipam.read") ? (
        <StatCard label={t("clients.assets.networks")} value={String(subnets.data?.length ?? 0)} pending={subnets.data === undefined} to={`/ipam?client=${client.id}`} icon="network" />
      ) : null}
    </StatsGrid>
  );
}
