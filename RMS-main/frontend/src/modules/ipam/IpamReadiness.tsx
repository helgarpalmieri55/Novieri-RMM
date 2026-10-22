// "Before subnets can be swept": what a network sweep needs from outside this
// page (docs/design/cross-module-guidance.md §2, IP addresses).
//
//   1  A REGISTERED SUBNET IS AUTHORIZED FOR SCANNING. Deny-by-default: a sweep
//      with no covering scan authorization is refused before any job exists,
//      and registering a subnet authorizes nothing. Blocking.
//   2  AN ENROLLED AGENT SITS ON EACH SWEPT SEGMENT. A sweep runs FROM an agent
//      inside the segment -- the control plane never probes a network itself
//      (invariant 2) -- so a segment with no agent records `no_agent` and is
//      never swept. Read from `unsweepable_subnets` on the Discovered list,
//      the server's own count of subnets whose latest sweep found no agent.
//      Blocking.
//   3  A SITE SERVER SERVES EACH AUTHORIZED SUBNET. PREFERRED, NOT REQUIRED
//      (doc §0.5): `ipam/sweep.py` sorts a site server whose segment contains
//      the subnet ahead of an equally-live ordinary endpoint, and sweeps
//      without one. Advisory, and worded so nobody reads it as a refusal.
//   4  DRIFT REACHES SOMEONE. Drift findings and refused sweeps raise events;
//      with no notification rule for them they wait here until someone looks.
//      Advisory.
//
// Not drawn while no subnet is registered: the page's own first-run guidance
// covers that, and "authorize a subnet" means nothing before there is one.
//
// NOTHING HERE GATES ANYTHING (invariant 6). Authorization, sweeper selection
// and dispatch are all decided server-side on every request.

import { useQuery } from "@tanstack/react-query";

import { combineQueries, prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { ApiError, AssetCandidatePage } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { DEVICES_READ, joinWords, mayCheck, useRulesPrerequisite } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import { useSiteServers } from "../site-servers/api";
import { SITE_READ } from "../site-servers/types";
import { useIpamScanAuthorizations, useIpamSubnets } from "./api";
import { effectiveSweepScope, parsePrefix, prefixContains } from "./ipamState";
import type { SubnetOut } from "./types";

const IPAM_READ = "ipam.read";

/**
 * The CTA a no-agent segment needs: the agent package, installed by hand on one
 * machine there. The label is looked up when read, so it follows a language switch.
 */
export const INSTALL_ON_SEGMENT_CTA = {
  to: "/agent-packages",
  get label(): string {
    return translate("ipam.readiness.installCta");
  },
};

/** At most `max` prefixes named, then "and N more". */
function namePrefixes(prefixes: readonly string[], max = 4): string {
  if (prefixes.length <= max) return joinWords(prefixes);
  return translate("ipam.readiness.andMore", { list: prefixes.slice(0, max).join(", "), count: prefixes.length - max });
}

/**
 * Is `subnet` inside a segment of an enabled site server? The rule the sweeper
 * selection applies: a segment CONTAINING the subnet, not several segments
 * that happen to cover it between them.
 */
export function siteServerServes(
  subnet: Pick<SubnetOut, "prefix">,
  servers: readonly { readonly segments: readonly string[]; readonly enabled: boolean }[],
): boolean {
  const inner = parsePrefix(subnet.prefix);
  if (inner === null) return false;
  return servers
    .filter((server) => server.enabled)
    .some((server) =>
      server.segments.some((segment) => {
        const outer = parsePrefix(segment);
        return outer !== null && prefixContains(outer, inner);
      }),
    );
}

export function IpamReadiness() {
  const t = useT();
  const api = useApi();
  const me = useMePermissions();
  const subnets = useIpamSubnets();
  const scopes = useIpamScanAuthorizations();
  // The Discovered page's key and shape, so a sweep that refreshes it refreshes this.
  const candidates = useQuery<AssetCandidatePage, ApiError>({
    queryKey: ["asset-candidates"],
    queryFn: () => api.listAssetCandidates(),
    enabled: mayCheck(me, DEVICES_READ),
    retry: false,
  });
  const siteServers = useSiteServers({ enabled: true }, { enabled: mayCheck(me, SITE_READ) });

  const now = Date.now();
  const authorized = (subnets.data ?? []).filter(
    (subnet) => effectiveSweepScope(subnet, scopes.data ?? [], now).authorized,
  );
  const noneAuthorizedYet = subnets.isSuccess && scopes.isSuccess && authorized.length === 0;

  const authorizationItem = prerequisiteFromQuery({
    label: t("ipam.readiness.authLabel"),
    query: combineQueries(subnets, scopes),
    permission: { code: IPAM_READ, me },
    isDone: () => authorized.length > 0,
    done: ([list]) =>
      authorized.length === list.length
        ? t("ipam.readiness.authDoneAll", { count: list.length })
        : t("ipam.readiness.authDoneSome", { authorized: authorized.length, count: list.length }),
    missing: {
      severity: "crit",
      detail: t("ipam.readiness.authMissing"),
      cta: { to: "/ipam/scan-authorizations", label: t("ipam.readiness.authCta") },
    },
  });

  const agentItem = prerequisiteFromQuery({
    label: t("ipam.readiness.agentLabel"),
    query: candidates,
    permission: { code: DEVICES_READ, me },
    waitingOn: noneAuthorizedYet ? t("ipam.readiness.authorizeFirst") : null,
    isDone: (page) => page.unsweepable_subnets === 0,
    done: t("ipam.readiness.agentDone"),
    missing: (page) => ({
      severity: "crit",
      detail: t("ipam.readiness.agentMissing", { count: page.unsweepable_subnets }),
      cta: INSTALL_ON_SEGMENT_CTA,
    }),
  });

  const siteServerItem = prerequisiteFromQuery({
    label: t("ipam.readiness.siteLabel"),
    query: combineQueries(combineQueries(subnets, scopes), siteServers),
    permission: { code: SITE_READ, me },
    waitingOn: noneAuthorizedYet ? t("ipam.readiness.authorizeFirst") : null,
    isDone: ([, servers]) => authorized.every((subnet) => siteServerServes(subnet, servers)),
    done: t("ipam.readiness.siteDone"),
    missing: ([, servers]) => {
      const unserved = authorized.filter((subnet) => !siteServerServes(subnet, servers)).map((s) => s.prefix);
      return {
        severity: "warn",
        detail: t("ipam.readiness.siteMissing", { prefixes: namePrefixes(unserved) }),
        cta: { to: "/site-servers", label: t("ipam.readiness.siteCta") },
      };
    },
  });

  const driftItem = useRulesPrerequisite("ipam.", {
    label: t("ipam.readiness.driftLabel"),
    missingDetail: t("ipam.readiness.driftMissing"),
    ctaLabel: t("ipam.readiness.driftCta"),
  });

  if (subnets.isSuccess && subnets.data.length === 0) return null;

  return (
    <Prerequisites
      title={t("ipam.readiness.title")}
      subtitle={t("ipam.readiness.subtitle")}
      items={[authorizationItem, agentItem, siteServerItem, driftItem]}
    />
  );
}
