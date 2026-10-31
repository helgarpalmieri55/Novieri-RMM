/**
 * THE FIXTURES THE SHELL ITSELF NEEDS ON EVERY ROUTE, plus the worked example.
 *
 * `me` and `getCurrentOrg` draw the rail and the account menu;
 * `getHealthAlertSummary` and `getPlatformStatus` are the two reads behind the
 * top bar's glyphs (`shell/healthSignal.ts`); `getDashboardLayout` is the one
 * read `/dashboard` cannot render without. `listDevices` / `getDevice` /
 * `getDeviceInventory` are the worked example every module's `fixtures.ts`
 * copies the shape of: a small, internally consistent data set, filters
 * honoured the way the server honours them, a 404 for an id that is not in it.
 *
 * REALISTIC, NOT LOREM. The review wave reads screenshots of these pages
 * against the prototype, and a table of `foo`/`bar` cannot be judged for
 * hierarchy or truncation. Counts agree with each other (the fleet tiles are
 * derived from the same list the table shows). Timestamps are relative to
 * now, so "3 minutes ago" stays "3 minutes ago" on every run.
 *
 * DEV-ONLY BY CONSTRUCTION. Nothing imports this file except
 * `fixtures/client.ts`, which nothing reaches outside `FIXTURE_MODE`;
 * `preview.test.ts` asserts the production bundle carries none of it.
 */

import type { CurrentOrg, Device, DeviceInventory, DeviceState, Me } from "../lib/api";
import type { PlatformStatus } from "../modules/platform-status/types";
import { ApiError } from "../lib/api";
import { navItems } from "../shell/nav";
import type { FixtureMap } from "./client";

/* ------------------------------------------------------------------- time */

const MINUTE = 60_000;
const DAY = 24 * 60 * MINUTE;

/** An ISO timestamp `minutes` before the call. */
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();
/** An ISO timestamp `days` after the call. */
const inDays = (days: number): string => new Date(Date.now() + days * DAY).toISOString();

/* ------------------------------------------------------------ permissions */

/**
 * The backend's permission catalogue (`backend/app/core/rbac.py::PERMISSIONS`)
 * as of 2026-09-07, unioned at run time with every code the rail gates on so
 * a nav item added with a new code is visible in fixture mode without an
 * edit here. The fixture session holds ALL of them: the review wave needs to
 * see every control a page can draw, and the frontend gates nothing
 * (invariant 6), so a permission the server would refuse costs a screenshot
 * nothing.
 */
const CATALOGUE = [
  "agent.push.run", "agent.push.scope.manage", "agent.release.manage", "agent.release.read",
  "ai.act.confirm", "ai.act.propose", "ai.admin", "ai.use", "assets.manage", "audit.read",
  "backup.policy.manage", "backup.read", "backup.restore", "backup.run",
  "bcp.approve", "bcp.manage", "bcp.read", "budget.manage", "budget.read",
  "change.approve", "change.manage", "change.read", "change.request",
  "credentials.checkout", "credentials.manage", "credentials.read",
  "deploy.create", "deploy.execute", "deploy.package.manage", "deploy.read",
  "deploy.script.manage", "deploy.script.run", "devices.manage", "devices.read", "devices.review",
  "docs.document.purge", "docs.manage", "docs.read", "email_intake.admin", "email_intake.read",
  "enrollment.link.manage", "enrollment.manage", "expense.confirm", "expense.record",
  "health.action.admin", "health.admin", "health.alert.manage", "health.read", "health.remediate",
  "ipam.assign", "ipam.manage", "ipam.read", "ipam.scope.manage", "ipam.sweep.manage", "ipam.sweep.run",
  "jobs.dispatch", "jobs.read", "licenses.keys.read", "licenses.manage", "licenses.read",
  "mcp.use", "notification.admin", "notification.read", "orgs.manage",
  "patch.apply", "patch.approve", "patch.policy.manage", "patch.read", "patch.scan",
  "people.manage", "people.offboard", "people.read",
  "platform.errors.read", "platform.errors.write", "platform.status.read", "policies.manage",
  "portal.ticket.read", "portal.ticket.write",
  "provisioning.manage", "provisioning.read", "provisioning.run",
  "remote.control", "remote.policy.manage", "remote.recordings.read", "remote.supervise", "reporting.read",
  "roles.manage", "roles.read", "site.manage", "site.read", "site.wake", "sla.admin", "sla.read",
  "subscriptions.manage", "subscriptions.read",
  "ticket.admin", "ticket.approve", "ticket.assign", "ticket.read", "ticket.write",
  "users.manage", "users.read",
];

function allPermissions(): string[] {
  const held = new Set(CATALOGUE);
  for (const item of navItems()) if (item.permission) held.add(item.permission);
  held.add("ai.use");
  held.add("platform.status.read");
  return [...held].sort();
}

/* ---------------------------------------------------------------- identity */

/** The organisation -- and its internal client, whose id is the organisation's
 *  (MSP clients spec §7 point 2; `lib/internalClient.ts`). */
export const ORG_ID = "org-toeshee";

const me = (): Me => ({
  user_id: "usr-helgar",
  org_id: ORG_ID,
  email: "helgar.palmieri@toeshee.example",
  display_name: "Helgar Palmieri",
  permissions: allPermissions(),
  roles: ["org-admin"],
  phone: "+351 21 000 0000",
  timezone: "Europe/Lisbon",
  // No saved language, so the console resolves it from this browser's choice
  // (`itms.lang`), which is how the harmony gate walks every route in each
  // language (`scripts/harmony.mjs --lang`).
  locale: null,
});

const org = (): CurrentOrg => ({
  id: ORG_ID,
  name: "Toeshee",
  slug: "toeshee",
  status: "active",
  settings: {
    timezone: "Europe/Lisbon",
    locale: "en-GB",
    date_format: "dmy",
    support_email: "it@toeshee.example",
    default_page_size: 50,
    show_device_serials: true,
  },
});

/* ------------------------------------------------------------------ fleet */

interface Seed {
  id: string;
  host: string | null;
  domain: string;
  os: Device["os_family"];
  ver: string;
  arch: string;
  group: string | null;
  agent: Device["agent_status"];
  seen: number | null;
  cert: Device["cert_state"];
  certDays: number | null;
  revoked?: boolean;
  /** Its agent predates screen share (`screen_capable: false`). Default: it can share. */
  oldAgent?: boolean;
  lifecycle: Device["lifecycle"];
  type: Device["asset_type"];
  source: Device["source"];
  provenance: Device["provenance"];
  ip: string | null;
  owner: string | null;
  location: string | null;
  description: string | null;
  /** Joined through the install link and waits for review. Default: approved. */
  review?: "pending" | "rejected";
  joinedFrom?: string;
  joinedMinutesAgo?: number;
  /** How its rejection ended. Only on a `rejected` seed. */
  outcome?: NonNullable<Device["rejection_outcome"]>;
}

/**
 * Fifteen devices, chosen to exercise every column: three OS families, an
 * agent that is renewing, one that has been trying for days, one revoked, a
 * switch and a printer with no agent, a sweep row nobody has vouched for, a
 * laptop still in a box, and a desktop that joined through the install link
 * and waits for review. Plus one rejected desktop whose agent did not confirm
 * it removed itself: not a device (`devices()` leaves it out), listed only
 * under Waiting for review until someone says they removed it.
 */
const SEEDS: Seed[] = [
  { id: "dev-1", host: "lis-ws-041", domain: "corp.toeshee.example", os: "windows", ver: "11 24H2", arch: "amd64", group: "grp-workstations", agent: "online", seen: 2, cert: "ok", certDays: 212, lifecycle: "in_service", type: "computer", source: "agent", provenance: "agent", ip: "10.20.4.41", owner: "Ana Ribeiro", location: "Lisbon, floor 3", description: "Finance workstation" },
  { id: "dev-2", host: "lis-ws-042", domain: "corp.toeshee.example", os: "windows", ver: "11 23H2", arch: "amd64", group: "grp-workstations", agent: "online", seen: 1, cert: "ok", certDays: 198, lifecycle: "in_service", type: "computer", source: "agent", provenance: "agent", ip: "10.20.4.42", owner: "Tiago Sousa", location: "Lisbon, floor 3", description: null },
  { id: "dev-3", host: "lis-mbp-017", domain: "corp.toeshee.example", os: "macos", ver: "15.6", arch: "arm64", group: "grp-workstations", agent: "online", seen: 4, cert: "renewing", certDays: 9, lifecycle: "in_service", type: "computer", source: "agent", provenance: "agent", ip: "10.20.4.117", owner: "Marta Coelho", location: "Lisbon, floor 2", description: "Design" },
  { id: "dev-4", host: "por-ws-008", domain: "corp.toeshee.example", os: "windows", ver: "10 22H2", arch: "amd64", group: "grp-workstations", agent: "offline", seen: 3 * 24 * 60, cert: "expiring", certDays: 3, lifecycle: "in_service", type: "computer", source: "agent", provenance: "agent", ip: "10.30.1.8", owner: "Rui Almeida", location: "Porto", description: "Reception desk" },
  { id: "dev-5", host: "lis-app-02", domain: "dc.toeshee.example", os: "linux", ver: "Ubuntu 24.04", arch: "amd64", group: "grp-servers", agent: "online", seen: 1, cert: "ok", certDays: 301, lifecycle: "in_service", type: "server", source: "agent", provenance: "agent", ip: "10.20.10.12", owner: "Platform team", location: "Lisbon, rack B4", description: "Application server" },
  { id: "dev-6", host: "lis-db-01", domain: "dc.toeshee.example", os: "linux", ver: "Debian 12", arch: "amd64", group: "grp-servers", agent: "online", seen: 1, cert: "ok", certDays: 288, oldAgent: true, lifecycle: "in_service", type: "server", source: "agent", provenance: "agent", ip: "10.20.10.21", owner: "Platform team", location: "Lisbon, rack B4", description: "PostgreSQL primary" },
  { id: "dev-7", host: "lis-dc-01", domain: "dc.toeshee.example", os: "windows", ver: "Server 2022", arch: "amd64", group: "grp-servers", agent: "online", seen: 2, cert: "ok", certDays: 240, lifecycle: "in_service", type: "server", source: "agent", provenance: "agent", ip: "10.20.10.5", owner: "Platform team", location: "Lisbon, rack B3", description: "Domain controller" },
  { id: "dev-8", host: "por-fs-01", domain: "dc.toeshee.example", os: "windows", ver: "Server 2019", arch: "amd64", group: "grp-servers", agent: "offline", seen: 26 * 60, cert: "ok", certDays: 150, lifecycle: "in_service", type: "server", source: "agent", provenance: "agent", ip: "10.30.10.7", owner: "Platform team", location: "Porto, rack A1", description: "File server" },
  { id: "dev-9", host: "lis-ws-019", domain: "corp.toeshee.example", os: "windows", ver: "11 23H2", arch: "amd64", group: null, agent: "offline", seen: 41 * 24 * 60, cert: "expired", certDays: -12, revoked: true, lifecycle: "retired", type: "computer", source: "agent", provenance: "agent", ip: null, owner: null, location: "Storage", description: "Retired, certificate revoked" },
  { id: "dev-10", host: "lis-sw-core-1", domain: "net.toeshee.example", os: null, ver: "", arch: "", group: null, agent: null, seen: null, cert: null, certDays: null, lifecycle: "in_service", type: "network", source: "manual", provenance: "operator", ip: "10.20.0.2", owner: "Network team", location: "Lisbon, rack B1", description: "Core switch" },
  { id: "dev-11", host: "lis-prn-3f", domain: "corp.toeshee.example", os: null, ver: "", arch: "", group: null, agent: null, seen: null, cert: null, certDays: null, lifecycle: "in_service", type: "printer", source: "discovery", provenance: "promoted", ip: "10.20.4.200", owner: "Facilities", location: "Lisbon, floor 3", description: "Floor printer" },
  { id: "dev-12", host: null, domain: "", os: null, ver: "", arch: "", group: null, agent: null, seen: null, cert: null, certDays: null, lifecycle: "in_service", type: "other", source: "discovery", provenance: "sweep", ip: "10.20.4.231", owner: null, location: null, description: null },
  { id: "dev-13", host: "lis-mbp-018", domain: "corp.toeshee.example", os: "macos", ver: "15.6", arch: "arm64", group: null, agent: null, seen: null, cert: null, certDays: null, lifecycle: "stock", type: "computer", source: "manual", provenance: "operator", ip: null, owner: null, location: "Lisbon, IT store", description: "Spare, unboxed" },
  { id: "dev-14", host: "por-ws-011", domain: "corp.toeshee.example", os: "linux", ver: "Fedora 42", arch: "amd64", group: "grp-workstations", agent: "online", seen: 6, cert: "ok", certDays: 260, lifecycle: "in_service", type: "computer", source: "agent", provenance: "agent", ip: "10.30.1.11", owner: "Sofia Martins", location: "Porto", description: "Engineering" },
  { id: "dev-15", host: "DESKTOP-7Q2M4KD", domain: "", os: "windows", ver: "11 24H2", arch: "amd64", group: null, agent: "online", seen: 1, cert: "ok", certDays: 364, lifecycle: "in_service", type: "computer", source: "agent", provenance: "agent", ip: "10.20.4.77", owner: null, location: null, description: null, review: "pending", joinedFrom: "10.20.4.77", joinedMinutesAgo: 35 },
  // A Windows workstation promoted from Discovered that has never run the
  // agent, with its DNS name, OS and processor recorded by hand: what a push
  // install is FOR. Its address has a WinRM probe row in the agent-push
  // fixtures, so the push flow can be walked from Devices in fixture mode.
  { id: "dev-17", host: "lis-ws-055", domain: "corp.toeshee.example", os: "windows", ver: "", arch: "amd64", group: null, agent: null, seen: null, cert: null, certDays: null, lifecycle: "in_service", type: "computer", source: "discovery", provenance: "promoted", ip: "10.20.4.31", owner: null, location: "Lisbon, floor 3", description: "New starter's desk, no agent yet" },
  { id: "dev-16", host: "DESKTOP-J3K9P2L", domain: "", os: "windows", ver: "11 23H2", arch: "amd64", group: null, agent: "offline", seen: 26 * 60, cert: "ok", certDays: 363, revoked: true, lifecycle: "in_service", type: "computer", source: "agent", provenance: "agent", ip: "10.20.4.93", owner: null, location: null, description: null, review: "rejected", outcome: "agent_did_not_confirm", joinedFrom: "10.20.4.93", joinedMinutesAgo: 27 * 60 },
];

/**
 * Review decisions made in fixture mode, so Approve and Reject can be walked.
 * A rejected row leaves every list, as it does on the server (plan D1).
 */
const REVIEWED = new Map<string, "approved" | "rejected">();
/** Rejected rows whose manual removal was acknowledged in fixture mode. */
const REMOVED = new Set<string>();

function device(seed: Seed): Device {
  return {
    id: seed.id,
    hostname: seed.host,
    fqdn: seed.host && seed.domain ? `${seed.host}.${seed.domain}` : null,
    os_family: seed.os,
    os_version: seed.ver,
    architecture: seed.arch,
    device_group_id: seed.group,
    status: "active",
    agent_status: seed.agent,
    last_seen_at: seed.seen === null ? null : ago(seed.seen),
    cert_not_after: seed.certDays === null ? null : inDays(seed.certDays),
    cert_state: seed.cert,
    cert_revoked: seed.revoked ?? false,
    screen_capable: seed.agent === null ? null : !seed.oldAgent,
    lifecycle: seed.lifecycle,
    asset_type: seed.type,
    source: seed.source,
    provenance: seed.provenance,
    description: seed.description,
    primary_ip: seed.ip,
    owner: seed.owner,
    location: seed.location,
    review_state: REVIEWED.get(seed.id) ?? seed.review ?? "approved",
    link_joined_from: seed.joinedFrom ?? null,
    link_joined_at: seed.joinedMinutesAgo === undefined ? null : ago(seed.joinedMinutesAgo),
    rejection_outcome: seed.outcome ?? null,
  };
}

/** Every device, rebuilt per call so the relative timestamps are fresh. */
export const devices = (): Device[] => SEEDS.map(device).filter((d) => d.review_state !== "rejected");

/**
 * Rejected rows whose agent may still be installed and that nobody has said
 * they removed: the one kind of rejected row the server lists, and only under
 * `review`.
 */
const leftBehind = (): Device[] =>
  SEEDS.map(device).filter(
    (d) =>
      d.review_state === "rejected" &&
      d.rejection_outcome != null &&
      d.rejection_outcome !== "agent_confirmed" &&
      !REMOVED.has(d.id),
  );

const needsCertAttention = (d: Device) =>
  d.cert_revoked || d.cert_state === "expiring" || d.cert_state === "expired";

const matchesState = (d: Device, state: DeviceState) => {
  switch (state) {
    case "online":
      return d.agent_status === "online";
    case "offline":
      return d.agent_status === "offline";
    case "assets":
      return d.agent_status === null;
    case "certs":
      return needsCertAttention(d);
    case "review":
      return d.review_state === "pending";
    default:
      return true;
  }
};

const matchesQuery = (d: Device, q: string) => {
  const needle = q.trim().toLowerCase();
  if (!needle) return true;
  return [d.hostname, d.fqdn, d.primary_ip, d.owner, d.location, d.description]
    .filter((v): v is string => typeof v === "string")
    .some((v) => v.toLowerCase().includes(needle));
};

const listDevices: FixtureMap["listDevices"] = (params = {}) => {
  const all = devices();
  const filtered = (rows: Device[]) =>
    rows.filter(
      (d) =>
        matchesQuery(d, params.q ?? "") &&
        (!params.provenance || d.provenance === params.provenance) &&
        (!params.osFamily || d.os_family === params.osFamily) &&
        // Every fixture machine is the internal client's (id = the org's).
        (!params.clientId || params.clientId === ORG_ID),
    );
  const searched = filtered(all);
  // In the review tab and its count, and nowhere else -- as on the server.
  const review = [...searched.filter((d) => matchesState(d, "review")), ...filtered(leftBehind())];
  const state_counts = {
    all: searched.length,
    online: searched.filter((d) => matchesState(d, "online")).length,
    offline: searched.filter((d) => matchesState(d, "offline")).length,
    assets: searched.filter((d) => matchesState(d, "assets")).length,
    certs: searched.filter((d) => matchesState(d, "certs")).length,
    review: review.length,
  };
  const inState =
    params.state === "review" ? review : searched.filter((d) => matchesState(d, params.state ?? "all"));
  const offset = params.offset ?? 0;
  const limit = params.limit ?? 50;
  return {
    items: inState.slice(offset, offset + limit),
    total: inState.length,
    state_counts,
    fleet: {
      total: all.length,
      managed: all.filter((d) => d.agent_status !== null).length,
      online: all.filter((d) => d.agent_status === "online").length,
      cert_attention: all.filter(needsCertAttention).length,
    },
  };
};

/** The row for `id`, or the 404 the server would answer with. */
function findDevice(id: string): Device {
  const found = devices().find((d) => d.id === id);
  if (!found) throw new ApiError(404, JSON.stringify({ detail: "device not found" }));
  return found;
}

const getDevice: FixtureMap["getDevice"] = (id) => findDevice(id);

const approveDevice: FixtureMap["approveDevice"] = (id) => {
  REVIEWED.set(findDevice(id).id, "approved");
  return findDevice(id);
};

const rejectDevice: FixtureMap["rejectDevice"] = (id) => {
  REVIEWED.set(findDevice(id).id, "rejected");
  return {
    device_id: id,
    uninstall: "dispatched",
    job_id: "job-fixture-uninstall",
    message: "The agent has been told to uninstall itself. Its certificate is revoked as soon as it confirms, or in 24 hours if it does not.",
  };
};

/** Approve all: only the listed machines still pending, as on the server. */
const approveAllDevices: FixtureMap["approveAllDevices"] = (body) => {
  const pending = new Set(devices().filter((d) => d.review_state === "pending").map((d) => d.id));
  const listed = [...new Set(body.device_ids)];
  const approved = listed.filter((id) => pending.has(id));
  for (const id of approved) REVIEWED.set(id, "approved");
  return {
    bulk_id: "bulk-fixture",
    approved: approved.length,
    not_pending: listed.length - approved.length,
    still_waiting: devices().filter((d) => d.review_state === "pending").length,
  };
};

const acknowledgeDeviceRemoval: FixtureMap["acknowledgeDeviceRemoval"] = (id) => {
  if (!leftBehind().some((d) => d.id === id)) {
    throw new ApiError(409, JSON.stringify({ detail: "this computer was not rejected" }));
  }
  REMOVED.add(id);
};

const getDeviceInventory = (deviceId: string): DeviceInventory => {
  const d = findDevice(deviceId);
  return {
    summary: {
      hostname: d.hostname,
      os_family: d.os_family,
      os_version: d.os_version,
      cpu: d.os_family === "macos" ? "Apple M3 Pro" : "Intel Core i5-13500",
      ram_bytes: d.asset_type === "server" ? 64 * 1024 ** 3 : 16 * 1024 ** 3,
      serial: `TSH-${d.id.toUpperCase().replace("-", "")}-7K2Q`,
    },
    hardware: [
      { type: "disk", identifier: "nvme0n1", attributes: { size_bytes: 512 * 1024 ** 3, model: "Samsung PM9A1", encrypted: true } },
      { type: "nic", identifier: "eth0", attributes: { mac: "3c:7c:3f:2a:91:0b", speed_mbps: 1000, ip: d.primary_ip } },
      { type: "gpu", identifier: "gpu0", attributes: { model: "Intel Iris Xe", vram_bytes: 0 } },
    ],
    software: [
      { name: "Google Chrome", version: "129.0.6668.70", publisher: "Google LLC", install_date: ago(9 * 24 * 60) },
      { name: "7-Zip", version: "24.08", publisher: "Igor Pavlov", install_date: ago(120 * 24 * 60) },
      { name: "Microsoft 365 Apps", version: "16.0.17928", publisher: "Microsoft", install_date: ago(30 * 24 * 60) },
      { name: "Zoom Workplace", version: "6.2.0", publisher: "Zoom Video Communications", install_date: ago(14 * 24 * 60) },
    ],
    facts: {
      domain_joined: d.os_family === "windows",
      secure_boot: true,
      disk_encryption: d.os_family === "macos" ? "FileVault" : "BitLocker",
      last_boot: ago(3 * 24 * 60 + 17),
      timezone: "Europe/Lisbon",
    },
    snapshot_taken_at: ago(38),
  };
};

/* ------------------------------------------------------- the shell's reads */

export const fixtures: FixtureMap = {
  me,
  getCurrentOrg: org,

  /** Behind the health glyph and the rail's `/health` count. */
  getHealthAlertSummary: () => ({
    open: 12,
    by_state: { active: 9, acknowledged: 2, muted: 1 },
    by_severity: { info: 3, warning: 8, critical: 1 },
    worst_severity: "critical",
  }),

  /** Behind the platform glyph and `/platform-status`. */
  getPlatformStatus: (): PlatformStatus => ({
    overall: "ok",
    probes: [
      { name: "database", status: "ok", duration_ms: 3, reason: null, detail: { rls: "enforced" } },
      { name: "redis", status: "ok", duration_ms: 1, reason: null, detail: null },
      { name: "vault", status: "ok", duration_ms: 11, reason: null, detail: { sealed: false } },
      { name: "object_store", status: "ok", duration_ms: 24, reason: null, detail: { bucket: "itms-backups" } },
      { name: "identity", status: "ok", duration_ms: 42, reason: null, detail: null },
    ],
    processes: [
      { process: "api", instances: 3, healthy_instances: 3, oldest_age_s: 86_412, status: "ok", version: "8b3421e" },
      { process: "gateway", instances: 2, healthy_instances: 2, oldest_age_s: 86_390, status: "ok", version: "8b3421e" },
      { process: "worker", instances: 2, healthy_instances: 2, oldest_age_s: 12_044, status: "ok", version: "8b3421e" },
      { process: "beat", instances: 1, healthy_instances: 1, oldest_age_s: 86_402, status: "ok", version: "8b3421e" },
    ],
    modules: [
      { name: "inventory", routes: 14, job_types: 2 },
      { name: "patch", routes: 19, job_types: 3 },
      { name: "deploy", routes: 16, job_types: 3 },
      { name: "backup", routes: 15, job_types: 4 },
      { name: "health", routes: 21, job_types: 1 },
      { name: "remote", routes: 9, job_types: 2 },
      { name: "service_desk", routes: 27, job_types: 0 },
    ],
  }),

  /** Never arranged: `null`, so the dashboard renders its stock order. */
  getDashboardLayout: () => ({ widgets: null }),
  saveDashboardLayout: (widgets) => ({ widgets }),
  resetDashboardLayout: () => undefined,

  listDevices,
  getDevice,
  approveDevice,
  approveAllDevices,
  rejectDevice,
  acknowledgeDeviceRemoval,
  getDeviceInventory,
};

export default fixtures;
