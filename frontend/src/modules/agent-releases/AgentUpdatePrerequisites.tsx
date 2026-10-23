// "Before an agent update can roll out": what a rollout needs from other
// modules, and from this page's own release channels
// (`docs/design/cross-module-guidance.md` §2, Agent packages).
//
//   1  a computer runs the agent -- a rollout updates agents, and with none
//      there is nothing to update (advisory);
//   2  a device group of 1 to 25 computers exists for the pilot ring -- the
//      first ring must name a group (`agent_update/service.py`), capped at
//      `PILOT_MAX_DEVICES`; "Start" stays disabled until one is picked, so
//      without one no rollout can begin (blocking);
//   3  a release is marked stable -- only a stable release goes past the
//      pilot ring (`_require_stable`). Marking one is done in "Release
//      channels" below, which only a publisher is offered; anyone else is told
//      who can, rather than sent to a section that is not there.
//
// Rendered by the page for holders of `agent.update.read` only, like the
// updates section it prepares for. NOTHING HERE GATES ANYTHING (invariant 6).

import { useQuery } from "@tanstack/react-query";

import { prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import type { ApiError, DeviceGroupPage, FleetVersions } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import {
  CREATE_GROUP_CTA,
  DEVICES_READ,
  mayCheck,
  useDeviceGroupsQuery,
  useFleetPrerequisite,
} from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";

const AGENT_UPDATE_READ = "agent.update.read";
const AGENT_RELEASE_MANAGE = "agent.release.manage";

/** `PILOT_MAX_DEVICES` in `backend/app/modules/agent_update/service.py`. */
export const PILOT_MAX_DEVICES = 25;

/** The same key `AgentUpdatesSection` reads, so this is one request. */
export const FLEET_VERSIONS_KEY = ["agent-fleet-versions"] as const;

/** Groups a pilot ring could name: live, with at least one and at most 25 computers. */
export function pilotCandidates(page: DeviceGroupPage) {
  return page.items.filter(
    (group) => group.archived_at === null && group.device_count > 0 && group.device_count <= PILOT_MAX_DEVICES,
  );
}

export function AgentUpdatePrerequisites() {
  const t = useT();
  const api = useApi();
  const me = useMePermissions();
  const groups = useDeviceGroupsQuery();
  const versions = useQuery<FleetVersions, ApiError>({
    queryKey: FLEET_VERSIONS_KEY,
    queryFn: () => api.getFleetVersions(),
    enabled: mayCheck(me, AGENT_UPDATE_READ),
    retry: false,
  });

  const agentItem = useFleetPrerequisite("agent", {
    missingDetail: t("agentReleases.prerequisites.noComputer"),
  });

  const pilotItem = prerequisiteFromQuery({
    label: t("agentReleases.prerequisites.pilotLabel", { max: PILOT_MAX_DEVICES }),
    query: groups,
    permission: { code: DEVICES_READ, me },
    isDone: (page) => pilotCandidates(page).length > 0,
    done: (page) => t("agentReleases.prerequisites.pilotDone", { count: pilotCandidates(page).length }),
    missing: (page) => {
      const live = page.items.filter((group) => group.archived_at === null);
      const detail =
        live.length === 0
          ? t("agentReleases.prerequisites.pilotNone")
          : live.every((group) => group.device_count === 0)
            ? t("agentReleases.prerequisites.pilotEmpty")
            : t("agentReleases.prerequisites.pilotTooBig", { max: PILOT_MAX_DEVICES });
      return {
        severity: "crit",
        detail: t("agentReleases.prerequisites.pilotSuffix", { detail }),
        cta: { to: CREATE_GROUP_CTA.to, label: t("agentReleases.prerequisites.pilotCta", { max: PILOT_MAX_DEVICES }) },
      };
    },
  });

  const canMarkStable = me.has(AGENT_RELEASE_MANAGE);
  const stableItem = prerequisiteFromQuery({
    label: t("agentReleases.prerequisites.stableLabel"),
    query: versions,
    permission: { code: AGENT_UPDATE_READ, me },
    isDone: (fleet) => fleet.latest_stable !== null,
    done: (fleet) => t("agentReleases.prerequisites.stableDone", { version: fleet.latest_stable ?? "" }),
    missing: {
      severity: "warn",
      detail: canMarkStable
        ? t("agentReleases.prerequisites.stableMissing")
        : `${t("agentReleases.prerequisites.stableMissing")} ${t("agentReleases.prerequisites.stableMissingNote")}`,
      ...(canMarkStable ? { cta: { to: "/agent-packages#channels", label: t("agentReleases.prerequisites.stableCta") } } : {}),
    },
  });

  return (
    <Prerequisites
      title={t("agentReleases.prerequisites.title")}
      subtitle={t("agentReleases.prerequisites.subtitle")}
      items={[agentItem, pilotItem, stableItem]}
    />
  );
}
