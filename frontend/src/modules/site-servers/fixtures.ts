/**
 * Fixtures for the site-server module: the two promoted devices, their
 * segment checks, and the four mutations the console can send.
 *
 * Built against the core fleet (`fixtures/core.ts`): `rack-b4-relay` is
 * `dev-5` (lis-app-02, online, rack B4) and serves the two Lisbon segments
 * IPAM's fixtures sweep from it; `por-branch-relay` is `dev-8` (por-fs-01,
 * offline for a day) and is DEMOTED WITHOUT DELETE, which is the one
 * distinction `SiteServerList` exists to draw. The segment check for the
 * Lisbon server has one declared segment inventory has not yet placed the
 * machine on, so the detail page shows the designed "declared, not yet
 * observed" state rather than a clean sheet.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { ApiError } from "../../lib/api";
import type { SegmentCheckOut, SiteServerOut } from "./types";

const MINUTE = 60_000;
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();

const servers = (): SiteServerOut[] => [
  {
    id: "ss-1",
    device_id: "dev-5",
    name: "rack-b4-relay",
    segments: ["10.20.4.0/24", "10.20.9.0/24"],
    roles: ["wake"],
    enabled: true,
    notes: "Lisbon office relay. Sweeps the rack and workstation VLANs.",
    ipam_site_id: "site-lisbon",
    agent_status: "online",
    agent_last_seen_at: ago(1),
  },
  {
    id: "ss-2",
    device_id: "dev-8",
    name: "por-branch-relay",
    segments: ["10.30.1.0/24"],
    roles: ["wake"],
    enabled: false,
    notes: "Demoted 2026-08-30 while the Porto file server is rebuilt.",
    ipam_site_id: "site-porto",
    agent_status: "offline",
    agent_last_seen_at: ago(26 * 60),
  },
];

function findServer(id: string): SiteServerOut {
  const found = servers().find((server) => server.id === id);
  if (!found) throw new ApiError(404, JSON.stringify({ detail: "site server not found" }));
  return found;
}

const listSiteServers: FixtureMap["listSiteServers"] = (filters = {}) =>
  servers().filter(
    (server) =>
      (filters.enabled === undefined || server.enabled === filters.enabled) &&
      (!filters.ipam_site_id || server.ipam_site_id === filters.ipam_site_id) &&
      (!filters.segment || server.segments.includes(filters.segment)),
  );

const getSiteServer: FixtureMap["getSiteServer"] = (id) => findServer(id);

const getSiteServerSegmentCheck: FixtureMap["getSiteServerSegmentCheck"] = (id) => {
  const server = findServer(id);
  const check: SegmentCheckOut =
    server.id === "ss-1"
      ? {
          site_server_id: server.id,
          device_id: server.device_id,
          evidence: "ipam",
          matched: ["10.20.4.0/24"],
          declared_unobserved: ["10.20.9.0/24"],
          observed_undeclared: ["10.20.10.12"],
        }
      : {
          site_server_id: server.id,
          device_id: server.device_id,
          evidence: "ipam",
          matched: [],
          declared_unobserved: ["10.30.1.0/24"],
          observed_undeclared: ["10.30.10.7"],
        };
  return check;
};

const promoteSiteServer: FixtureMap["promoteSiteServer"] = (body) => ({
  id: `ss-${body.device_id}`,
  device_id: body.device_id,
  name: body.name ?? body.device_id,
  segments: body.segments,
  roles: body.roles ?? ["wake"],
  enabled: true,
  notes: body.notes ?? "",
  ipam_site_id: body.ipam_site_id ?? null,
  agent_status: "online",
  agent_last_seen_at: ago(0),
});

const updateSiteServer: FixtureMap["updateSiteServer"] = (id, body) => {
  const server = findServer(id);
  return {
    ...server,
    name: body.name ?? server.name,
    segments: body.segments ?? server.segments,
    roles: body.roles ?? server.roles,
    notes: body.notes ?? server.notes,
    enabled: body.enabled ?? server.enabled,
    ipam_site_id: body.ipam_site_id === undefined ? server.ipam_site_id : body.ipam_site_id,
  };
};

const demoteSiteServer: FixtureMap["demoteSiteServer"] = (id) => {
  findServer(id);
  return undefined;
};

/** The 202: a signed job, queued. Nothing has left any NIC. */
const wakeViaSiteServer: FixtureMap["wakeViaSiteServer"] = (id, body) => {
  const server = findServer(id);
  const segment = body.segment ?? server.segments[0] ?? "10.20.4.0/24";
  const broadcast = segment.replace(/\.\d+\/\d+$/, ".255");
  return {
    job_id: "job-wake-fixture",
    device_id: server.device_id,
    broadcast,
    target_mac: body.target_mac,
    expires_at: new Date(Date.now() + 5 * MINUTE).toISOString(),
  };
};

const fixtures: FixtureMap = {
  listSiteServers,
  getSiteServer,
  getSiteServerSegmentCheck,
  promoteSiteServer,
  updateSiteServer,
  demoteSiteServer,
  wakeViaSiteServer,
};

export default fixtures;
