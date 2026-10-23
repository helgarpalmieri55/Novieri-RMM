/**
 * THE SHARED DETECTORS: the four cross-module prerequisites that recur on most
 * console pages (`docs/design/cross-module-guidance.md` §0.3), built once.
 *
 *   FLEET      `listDevices({ limit: 1 }).fleet`         devices.read
 *   GROUPS     `listDeviceGroups().total`                devices.read
 *   INSTALLER  a published release with an artefact      agent.release.read
 *   RULES      an active notification rule for a prefix  notification.read
 *
 * Each comes in two layers:
 *
 *   `use…Query()`         the TanStack query, for a page that needs the numbers;
 *   `use…Prerequisite()`  a ready `Prerequisite` for `<Prerequisites>`, with
 *                         default wording a page may override per row.
 *
 * WHAT AN ACCOUNT WITHOUT THE CODE SEES. Each query runs only once `/me` says
 * the account holds the code (or `/me` itself failed, when the query is the
 * only way left to find out). An account without it gets "cannot be checked
 * from this account" and no request is made -- never "missing", which would
 * send an operator to set up something that may well exist.
 *
 * QUERY KEYS ARE THE ONES THE OWNING PAGES ALREADY USE, with the same data
 * shape, so a detector shares their cache entry and is refreshed by their
 * mutations' invalidations: `["device-groups"]` (Agent updates),
 * `["agent-releases"]` (Agent packages, onboarding), `["notification-rules"]`
 * (Notifications, Settings). FLEET has its own key under `["devices", …]`
 * because `limit: 1` is a different response from the Devices list; it still
 * sits under the prefix every device mutation invalidates.
 *
 * INVARIANT 6: NOTHING HERE GATES ANYTHING. These read lists the server
 * serves; every route checks its own permission on every request.
 */

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";

import {
  prerequisiteFromQuery,
  type Prerequisite,
  type PrerequisiteCta,
  type PrerequisiteSeverity,
} from "../components/prerequisites";
import { osLabel, platformLabel } from "../modules/agent-releases/platform";
import type { RuleOut } from "../modules/notification/types";
import { useT } from "../i18n/context";
import { translate, type MessageKey } from "../i18n/core";
import { ADD_COMPUTER_TO } from "../shell/nav";
import type { AgentRelease, ApiError, DeviceFleetCounts, DeviceGroupPage } from "./api";
import { useApi } from "./ApiContext";
import { joinWords } from "./joinWords";
import { useMePermissions, type MePermissions } from "./mePermissions";

export { joinWords };

/* --------------------------------------------------------------- shared */

export const DEVICES_READ = "devices.read";
export const AGENT_RELEASE_READ = "agent.release.read";
export const NOTIFICATION_READ = "notification.read";

/**
 * A call to action whose words are looked up when read: these are module-level
 * constants that other modules render, so a plain string would keep the
 * language the page started in.
 */
const cta_ = (to: string, label: MessageKey): PrerequisiteCta => ({ to, get label() { return translate(label); } });

export const ADD_COMPUTER_CTA: PrerequisiteCta = cta_(ADD_COMPUTER_TO, "lib.detectors.addComputer");
export const OFFLINE_COMPUTERS_CTA: PrerequisiteCta = cta_("/devices?state=offline", "lib.detectors.seeOfflineComputers");
export const CREATE_GROUP_CTA: PrerequisiteCta = cta_("/device-groups", "lib.detectors.createGroup");
export const PUBLISH_PACKAGE_CTA: PrerequisiteCta = cta_("/agent-packages#releases", "lib.detectors.publishPackage");
export const NOTIFY_RULES_CTA: PrerequisiteCta = cta_("/notifications#rules", "lib.detectors.notifyRules");

/**
 * Per-page overrides for a detector's item. The detector owns the check and
 * the CTA's destination; the page owns how the row reads and how bad a miss is
 * on that page (the doc's B rows are `crit`, its A rows `warn`).
 */
export interface DetectorOptions {
  /** The row's statement. Must be unique within one panel. */
  label?: string;
  /** Default `warn`. `crit` when the page cannot do its job without it. */
  severity?: PrerequisiteSeverity;
  /** Replaces the default sentence when the prerequisite is missing. */
  missingDetail?: ReactNode;
  /** Replaces the default sentence when it is in place. */
  doneDetail?: ReactNode;
  /** Replaces the CTA's label; its destination stays the detector's. */
  ctaLabel?: string;
  /** Set when this row depends on an earlier one that is not done (see `prerequisiteFromQuery`). */
  waitingOn?: ReactNode;
}

const NO_RETRY = { retry: false } as const;

/** Ask only once `/me` says the account may; fall back to asking if `/me` failed. */
export function mayCheck(me: MePermissions, code: string): boolean {
  return me.has(code) || me.status === "error";
}

function cta(base: PrerequisiteCta, options: DetectorOptions): PrerequisiteCta {
  return options.ctaLabel ? { to: base.to, label: options.ctaLabel } : base;
}

/* ---------------------------------------------------------------- FLEET */

export const FLEET_KEY = ["devices", "fleet-counts"] as const;

/** The whole estate's counts (`DevicePage.fleet`), from a one-row page. */
export function useFleetQuery() {
  const api = useApi();
  const me = useMePermissions();
  return useQuery({
    queryKey: FLEET_KEY,
    queryFn: () => api.listDevices({ limit: 1 }),
    select: (page): DeviceFleetCounts => page.fleet,
    enabled: mayCheck(me, DEVICES_READ),
    ...NO_RETRY,
  });
}

/**
 * `asset`  -- a computer is recorded at all (`fleet.total`);
 * `agent`  -- a computer runs the agent (`fleet.managed`);
 * `online` -- a computer running the agent is connected (`fleet.online`).
 */
export type FleetNeed = "asset" | "agent" | "online";

const FLEET_LABEL: Record<FleetNeed, MessageKey> = {
  asset: "lib.detectors.fleet.assetLabel",
  agent: "lib.detectors.fleet.agentLabel",
  online: "lib.detectors.fleet.onlineLabel",
};

export function useFleetPrerequisite(need: FleetNeed, options: DetectorOptions = {}): Prerequisite {
  useT();
  const me = useMePermissions();
  const query = useFleetQuery();
  const severity = options.severity ?? "warn";
  return prerequisiteFromQuery<DeviceFleetCounts>({
    label: options.label ?? translate(FLEET_LABEL[need]),
    query,
    permission: { code: DEVICES_READ, me },
    waitingOn: options.waitingOn,
    isDone: (fleet) => (need === "asset" ? fleet.total : need === "agent" ? fleet.managed : fleet.online) > 0,
    done: (fleet) =>
      options.doneDetail ??
      (need === "asset"
        ? translate("lib.detectors.fleet.assetDone", { count: fleet.total })
        : need === "agent"
          ? translate("lib.detectors.fleet.agentDone", { count: fleet.managed })
          : translate("lib.detectors.fleet.onlineDone", {
              count: fleet.online,
              online: fleet.online,
              computers: translate("lib.detectors.computers", { count: fleet.managed }),
            })),
    missing: (fleet) => {
      if (need === "online" && fleet.managed > 0) {
        return {
          severity,
          detail:
            options.missingDetail ??
            translate("lib.detectors.fleet.onlineMissing", { count: fleet.managed }),
          cta: cta(OFFLINE_COMPUTERS_CTA, options),
        };
      }
      const detail =
        need === "asset" || fleet.total === 0
          ? translate("lib.detectors.fleet.noneRecorded")
          : need === "agent"
            ? translate("lib.detectors.fleet.agentMissing", { count: fleet.total })
            : translate("lib.detectors.fleet.noneRunsAgent");
      return { severity, detail: options.missingDetail ?? detail, cta: cta(ADD_COMPUTER_CTA, options) };
    },
  });
}

/* --------------------------------------------------------------- GROUPS */

export const DEVICE_GROUPS_KEY = ["device-groups"] as const;

export function useDeviceGroupsQuery() {
  const api = useApi();
  const me = useMePermissions();
  return useQuery<DeviceGroupPage, ApiError>({
    queryKey: DEVICE_GROUPS_KEY,
    queryFn: () => api.listDeviceGroups(),
    enabled: mayCheck(me, DEVICES_READ),
    ...NO_RETRY,
  });
}

export function useGroupsPrerequisite(options: DetectorOptions = {}): Prerequisite {
  useT();
  const me = useMePermissions();
  const query = useDeviceGroupsQuery();
  return prerequisiteFromQuery<DeviceGroupPage>({
    label: options.label ?? translate("lib.detectors.groups.label"),
    query,
    permission: { code: DEVICES_READ, me },
    waitingOn: options.waitingOn,
    isDone: (groups) => groups.total > 0,
    done: (groups) => options.doneDetail ?? translate("lib.detectors.groups.done", { count: groups.total }),
    missing: {
      severity: options.severity ?? "warn",
      detail: options.missingDetail ?? translate("lib.detectors.groups.missing"),
      cta: cta(CREATE_GROUP_CTA, options),
    },
  });
}

/* ------------------------------------------------------------ INSTALLER */

export const AGENT_RELEASES_KEY = ["agent-releases"] as const;

/** A platform a page needs a package for. No `arch` means any processor. */
export interface ReleasePlatform {
  readonly os: string;
  readonly arch?: string;
}

export function useAgentReleasesQuery() {
  const api = useApi();
  const me = useMePermissions();
  return useQuery<AgentRelease[], ApiError>({
    queryKey: AGENT_RELEASES_KEY,
    queryFn: () => api.listAgentReleases(),
    enabled: mayCheck(me, AGENT_RELEASE_READ),
    ...NO_RETRY,
  });
}

/** Installable: published, with its file uploaded. A row without the bytes installs nothing. */
export function isInstallable(release: Pick<AgentRelease, "published_at" | "has_artifact">): boolean {
  return release.published_at !== null && release.has_artifact;
}

function matches(release: AgentRelease, platform: ReleasePlatform): boolean {
  return release.os === platform.os && (platform.arch === undefined || release.arch === platform.arch);
}

function describePlatform(platform: ReleasePlatform): string {
  return platform.arch === undefined ? osLabel(platform.os) : platformLabel(platform.os, platform.arch);
}

/** The installable releases, narrowed to `platforms` when given. */
export function installableReleases(
  releases: readonly AgentRelease[],
  platforms?: readonly ReleasePlatform[],
): AgentRelease[] {
  return releases.filter(
    (release) => isInstallable(release) && (!platforms || platforms.some((platform) => matches(release, platform))),
  );
}

export function useInstallerPrerequisite(
  options: DetectorOptions & { platforms?: readonly ReleasePlatform[] } = {},
): Prerequisite {
  useT();
  const me = useMePermissions();
  const query = useAgentReleasesQuery();
  const { platforms } = options;
  return prerequisiteFromQuery<AgentRelease[]>({
    label: options.label ?? translate("lib.detectors.installer.label"),
    query,
    permission: { code: AGENT_RELEASE_READ, me },
    waitingOn: options.waitingOn,
    isDone: (releases) => installableReleases(releases, platforms).length > 0,
    done: (releases) => {
      if (options.doneDetail !== undefined) return options.doneDetail;
      const usable = installableReleases(releases, platforms);
      if (!platforms) {
        const covered = [...new Set(usable.map((release) => platformLabel(release.os, release.arch)))].sort();
        return translate("lib.detectors.installer.published", { platforms: joinWords(covered) });
      }
      const covered = platforms.filter((platform) => usable.some((release) => matches(release, platform)));
      const uncovered = platforms.filter((platform) => !covered.includes(platform));
      const lead = translate("lib.detectors.installer.published", { platforms: joinWords(covered.map(describePlatform)) });
      return uncovered.length === 0
        ? lead
        : `${lead} ${translate("lib.detectors.installer.nothingYet", { platforms: joinWords(uncovered.map(describePlatform)) })}`;
    },
    missing: {
      severity: options.severity ?? "warn",
      detail:
        options.missingDetail ??
        (platforms
          ? translate("lib.detectors.installer.missingFor", { platforms: joinWords(platforms.map(describePlatform), "or") })
          : translate("lib.detectors.installer.missing")),
      cta: cta(PUBLISH_PACKAGE_CTA, options),
    },
  });
}

/* ---------------------------------------------------------------- RULES */

export const NOTIFICATION_RULES_KEY = ["notification-rules"] as const;

export function useNotificationRulesQuery() {
  const api = useApi();
  const me = useMePermissions();
  return useQuery<RuleOut[], ApiError>({
    queryKey: NOTIFICATION_RULES_KEY,
    queryFn: () => api.listNotificationRules(),
    enabled: mayCheck(me, NOTIFICATION_READ),
    ...NO_RETRY,
  });
}

/** Active rules whose event type starts with `eventPrefix` (e.g. `"health."`). */
export function activeRulesFor(rules: readonly RuleOut[], eventPrefix: string): RuleOut[] {
  return rules.filter((rule) => rule.active && rule.event_type.startsWith(eventPrefix));
}

/**
 * Someone is told when these events happen. `eventPrefix` is matched against
 * `event_type` and never rendered: event types are wire names.
 */
export function useRulesPrerequisite(eventPrefix: string, options: DetectorOptions = {}): Prerequisite {
  useT();
  const me = useMePermissions();
  const query = useNotificationRulesQuery();
  return prerequisiteFromQuery<RuleOut[]>({
    label: options.label ?? translate("lib.detectors.rules.label"),
    query,
    permission: { code: NOTIFICATION_READ, me },
    waitingOn: options.waitingOn,
    isDone: (rules) => activeRulesFor(rules, eventPrefix).length > 0,
    done: (rules) =>
      options.doneDetail ?? translate("lib.detectors.rules.done", { count: activeRulesFor(rules, eventPrefix).length }),
    missing: {
      severity: options.severity ?? "warn",
      detail: options.missingDetail ?? translate("lib.detectors.rules.missing"),
      cta: cta(NOTIFY_RULES_CTA, options),
    },
  });
}
