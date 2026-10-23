// "Before a site server can be promoted": what other modules have to hold for
// this page to have anything to promote, and for a promoted server's segments
// to be checked (`docs/design/cross-module-guidance.md` §2, Site servers).
//
//   1  a computer runs the agent -- a site server is an enrolled device
//      promoted in place, and the promote dialog offers only devices with an
//      agent (blocking);
//   2  a subnet is registered in IP addresses -- sweeps run per registered
//      subnet (a site server is only the preferred sweeper on one), so with
//      none registered a site server has nothing to sweep; wakes do not need
//      one (advisory).
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { useQuery } from "@tanstack/react-query";

import { prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import type { ApiError } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { mayCheck, useFleetPrerequisite } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import { SUBNETS_KEY } from "../ipam/api";
import { IPAM_READ, type SubnetOut } from "../ipam/types";

export function SiteServerPrerequisites() {
  const t = useT();
  const api = useApi();
  const me = useMePermissions();
  // The IP addresses page's own key and unfiltered call, so the two share a cache entry.
  const subnets = useQuery<SubnetOut[], ApiError>({
    queryKey: [SUBNETS_KEY, {}],
    queryFn: () => api.listIpamSubnets({}),
    enabled: mayCheck(me, IPAM_READ),
    retry: false,
  });

  const agentItem = useFleetPrerequisite("agent", {
    severity: "crit",
    missingDetail: t("siteServers.prerequisites.agentMissing"),
  });

  const subnetItem = prerequisiteFromQuery({
    label: t("siteServers.prerequisites.subnetLabel"),
    query: subnets,
    permission: { code: IPAM_READ, me },
    isDone: (list) => list.length > 0,
    done: (list) => t("siteServers.prerequisites.subnetDone", { count: list.length }),
    missing: {
      severity: "warn",
      detail: t("siteServers.prerequisites.subnetMissing"),
      cta: { to: "/ipam?register=1", label: t("siteServers.prerequisites.subnetCta") },
    },
  });

  return (
    <Prerequisites
      title={t("siteServers.prerequisites.title")}
      subtitle={t("siteServers.prerequisites.subtitle")}
      items={[agentItem, subnetItem]}
    />
  );
}
