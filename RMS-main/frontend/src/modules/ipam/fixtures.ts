/**
 * Fixtures for IPAM: the Lisbon campus plan of record, what the fleet saw on
 * it, and where the two disagree.
 *
 * ONE STORY, TOLD CONSISTENTLY ACROSS EVERY ROUTE. `10.20.0.0/16` is a
 * container; under it the rack VLAN (`10.20.4.0/24`), the workstation VLAN
 * (`10.20.9.0/24`, the detail page's `subnet-1`) and a wireless VLAN nobody
 * has swept yet (`10.20.11.0/24`). Porto has its own `/24`. The drift list,
 * the dormant bucket, the sweep runs, the scan authorizations and the
 * address grid all describe the same addresses: the grid's `10.20.9.14` is
 * the conflict on the drift page, the workstation VLAN's 121 observed hosts
 * are the 121 the last sweep run reports, and the site server that swept
 * them is `rack-b4-relay` in the site-server fixtures. Counts are DERIVED
 * from the grid where a route shows both, so the tiles agree with the rows.
 *
 * Timestamps are relative to now, so an age reads the same on every run;
 * this module renders ages, never clock times (§0.10).
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { devices } from "../../fixtures/core";
import { ApiError } from "../../lib/api";
import type {
  AddressOut,
  DriftOut,
  GridEntryOut,
  HistoryEntryOut,
  ImportRowOut,
  OrphanOut,
  RangeOut,
  ScopeOut,
  SearchOut,
  SiteOut,
  SubnetOut,
  SweepRunOut,
  UtilizationOut,
  VlanOut,
  VrfOut,
} from "./types";

const MINUTE = 60_000;
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();
const ahead = (minutes: number): string => new Date(Date.now() + minutes * MINUTE).toISOString();

const notFound = (what: string): never => {
  throw new ApiError(404, JSON.stringify({ detail: `${what} not found` }));
};

/* --------------------------------------------------------------- registry */

const VRF_GLOBAL = "vrf-global";

const vrfs = (): VrfOut[] => [
  { id: VRF_GLOBAL, name: "global", rd: null, description: "The default VRF; every subnet lands here unless one is named." },
  { id: "vrf-lab", name: "lab", rd: "65000:20", description: "The isolated build lab. Overlaps campus space on purpose." },
];

const sites = (): SiteOut[] => [
  { id: "site-lisbon", name: "Lisbon", doc_record_id: "doc-lisbon-office", description: "Head office, floors 2 and 3, rack room B." },
  { id: "site-porto", name: "Porto", doc_record_id: null, description: "Branch office." },
];

const vlans = (): VlanOut[] => [
  { id: "vlan-10", vid: 10, site_id: null, name: "Management", description: "Org-wide management VLAN." },
  { id: "vlan-40", vid: 40, site_id: "site-lisbon", name: "Racks", description: "" },
  { id: "vlan-90", vid: 90, site_id: "site-lisbon", name: "Workstations", description: "" },
  { id: "vlan-110", vid: 110, site_id: "site-lisbon", name: "Wireless", description: "" },
];

/* ---------------------------------------------------------------- subnets */

const subnets = (): SubnetOut[] => [
  {
    id: "subnet-0",
    vrf_id: VRF_GLOBAL,
    prefix: "10.20.0.0/16",
    kind: "container",
    name: "Lisbon campus",
    description: "Everything at head office. A container: it holds no addresses.",
    site_id: "site-lisbon",
    vlan_id: null,
    gateway: null,
    dns_servers: [],
    parent_id: null,
    sweep_enabled: false,
    sweep_device_id: null,
    sweep_interval_hours: null,
    last_swept_at: null,
  },
  {
    id: "subnet-2",
    vrf_id: VRF_GLOBAL,
    prefix: "10.20.4.0/24",
    kind: "assignable",
    name: "Racks",
    description: "Rack room B, servers and appliances.",
    site_id: "site-lisbon",
    vlan_id: "vlan-40",
    gateway: "10.20.4.1",
    dns_servers: ["10.20.10.5"],
    parent_id: "subnet-0",
    sweep_enabled: true,
    sweep_device_id: "dev-5",
    sweep_interval_hours: 24,
    last_swept_at: ago(2 * 60 + 14),
  },
  {
    id: "subnet-1",
    vrf_id: VRF_GLOBAL,
    prefix: "10.20.9.0/24",
    kind: "assignable",
    name: "Workstations",
    description: "Floors 2 and 3, wired desks.",
    site_id: "site-lisbon",
    vlan_id: "vlan-90",
    gateway: "10.20.9.1",
    dns_servers: ["10.20.10.5", "10.20.10.6"],
    parent_id: "subnet-0",
    sweep_enabled: true,
    sweep_device_id: null,
    sweep_interval_hours: 24,
    last_swept_at: ago(2 * 60 + 14),
  },
  {
    id: "subnet-3",
    vrf_id: VRF_GLOBAL,
    prefix: "10.20.11.0/24",
    kind: "assignable",
    name: "Wireless",
    description: "Staff Wi-Fi. No agent lives here, so nothing can sweep it.",
    site_id: "site-lisbon",
    vlan_id: "vlan-110",
    gateway: "10.20.11.1",
    dns_servers: ["10.20.10.5"],
    parent_id: "subnet-0",
    sweep_enabled: false,
    sweep_device_id: null,
    sweep_interval_hours: null,
    last_swept_at: null,
  },
  {
    id: "subnet-4",
    vrf_id: VRF_GLOBAL,
    prefix: "10.30.1.0/24",
    kind: "assignable",
    name: "Porto office",
    description: "",
    site_id: "site-porto",
    vlan_id: null,
    gateway: "10.30.1.1",
    dns_servers: ["10.30.10.7"],
    parent_id: null,
    sweep_enabled: true,
    sweep_device_id: "dev-14",
    sweep_interval_hours: 168,
    last_swept_at: ago(3 * 24 * 60),
  },
];

function findSubnet(id: string): SubnetOut {
  return subnets().find((subnet) => subnet.id === id) ?? notFound("subnet");
}

/** `/24` prefix of an address, for the search and orphan helpers. */
const base24 = (prefix: string): string => prefix.split("/")[0]?.replace(/\.\d+$/, "") ?? prefix;

const listIpamSubnets: FixtureMap["listIpamSubnets"] = (filters = {}) =>
  subnets().filter((subnet) => {
    if (filters.vrf && subnet.vrf_id !== filters.vrf) return false;
    if (filters.site && subnet.site_id !== filters.site) return false;
    if (filters.vlan && subnet.vlan_id !== filters.vlan) return false;
    if (filters.q) {
      const needle = filters.q.toLowerCase();
      if (!subnet.prefix.includes(needle) && !subnet.name.toLowerCase().includes(needle)) return false;
    }
    if (filters.contains) {
      const head = base24(subnet.prefix);
      if (subnet.kind === "container") {
        if (!filters.contains.startsWith(subnet.prefix.split(".").slice(0, 2).join(".") + ".")) return false;
      } else if (!filters.contains.startsWith(`${head}.`)) return false;
    }
    return true;
  });

/* ------------------------------------------------------------------- grid */

const FIRST_NAMES = ["ana", "tiago", "marta", "rui", "sofia", "ines", "mateus", "leila", "joao", "carla", "nuno", "beatriz"];

/** A deterministic MAC for address `i` in a subnet, so the same row has the same MAC on every run. */
const macFor = (seed: number, i: number): string =>
  ["a4", "83", "e7", (seed & 0xff).toString(16).padStart(2, "0"), ((i * 37) & 0xff).toString(16).padStart(2, "0"), ((i * 91 + 7) & 0xff).toString(16).padStart(2, "0")].join(":");

/** The workstation VLAN's 254 addresses: 119 documented, 121 seen, and the disagreements the drift page lists. */
function workstationGrid(): GridEntryOut[] {
  const head = "10.20.9";
  const rows: GridEntryOut[] = [];
  for (let i = 1; i <= 254; i += 1) {
    const address = `${head}.${i}`;
    const inPool = i >= 200 && i <= 219;
    const reservedRange = i >= 250;
    const name = FIRST_NAMES[i % FIRST_NAMES.length] ?? "desk";
    let entry: GridEntryOut = {
      address,
      status: "free",
      state: null,
      address_id: null,
      device_id: null,
      asset_label: "",
      mac: null,
      hostname: "",
      last_seen_at: null,
      range_kinds: inPool ? ["dhcp_pool"] : reservedRange ? ["reserved"] : [],
      allocation_method: "unknown",
      allocation_method_source: "unknown",
    };
    if (i === 1) {
      entry = { ...entry, status: "in_use", state: "assigned", address_id: `addr-9-${i}`, asset_label: "Core switch, VLAN 90 SVI", mac: "00:1e:8f:aa:03:41", hostname: "gw-vlan90", last_seen_at: ago(134), allocation_method: "static", allocation_method_source: "documented" };
    } else if (i <= 119) {
      const dormant = i % 23 === 0;
      const stale = i % 41 === 0;
      const conflict = i === 14;
      const device = i <= 3 ? `dev-${i}` : null;
      entry = {
        ...entry,
        status: conflict ? "conflicting" : dormant ? "dormant" : stale ? "stale" : "in_use",
        state: "assigned",
        address_id: `addr-9-${i}`,
        device_id: device,
        asset_label: device ? "" : `Desk ${Math.ceil(i / 2)}`,
        mac: macFor(9, i),
        hostname: `ws-${name}-${String(i).padStart(2, "0")}`,
        last_seen_at: dormant ? ago(9 * 24 * 60 + i) : stale ? ago(3 * 24 * 60) : ago(134),
        allocation_method: i % 7 === 0 ? "dhcp" : "static",
        allocation_method_source: i % 7 === 0 ? "observed" : "documented",
      };
    } else if (i === 120 || i === 121 || i === 207) {
      entry = { ...entry, status: "undocumented", mac: macFor(9, i), last_seen_at: ago(134), allocation_method: "dhcp", allocation_method_source: "observed" };
    } else if (inPool) {
      entry = { ...entry, status: "dhcp", mac: i % 3 === 0 ? macFor(9, i) : null, last_seen_at: i % 3 === 0 ? ago(134) : null, allocation_method: i % 3 === 0 ? "dhcp" : "unknown", allocation_method_source: i % 3 === 0 ? "observed" : "unknown" };
    } else if (i === 250) {
      entry = { ...entry, status: "reserved", state: "reserved", address_id: `addr-9-${i}`, hostname: "prn-3f", asset_label: "3rd floor printer", allocation_method: "static", allocation_method_source: "documented" };
    }
    rows.push(entry);
  }
  return rows;
}

/** A sparse grid for the other assignable subnets: the first `documented` addresses are in use. */
function sparseGrid(head: string, documented: number, observed: number, seed: number): GridEntryOut[] {
  const rows: GridEntryOut[] = [];
  for (let i = 1; i <= 254; i += 1) {
    const isDoc = i <= documented;
    const isSeen = i <= observed;
    rows.push({
      address: `${head}.${i}`,
      status: isDoc ? "in_use" : isSeen ? "undocumented" : "free",
      state: isDoc ? "assigned" : null,
      address_id: isDoc ? `addr-${seed}-${i}` : null,
      device_id: null,
      asset_label: isDoc ? `Rack unit ${i}` : "",
      mac: isSeen ? macFor(seed, i) : null,
      hostname: isDoc ? `srv-${seed}-${String(i).padStart(2, "0")}` : "",
      last_seen_at: isSeen ? ago(134) : null,
      range_kinds: [],
      allocation_method: isDoc ? "static" : "unknown",
      allocation_method_source: isDoc ? "documented" : "unknown",
    });
  }
  return rows;
}

function gridFor(id: string): GridEntryOut[] {
  const subnet = findSubnet(id);
  if (subnet.kind === "container") {
    throw new ApiError(400, JSON.stringify({ detail: "a container prefix has no address grid" }));
  }
  switch (id) {
    case "subnet-1":
      return workstationGrid();
    case "subnet-2":
      return sparseGrid("10.20.4", 28, 30, 4);
    case "subnet-3":
      return sparseGrid("10.20.11", 3, 0, 11);
    default:
      return sparseGrid("10.30.1", 41, 40, 30);
  }
}

const listIpamSubnetAddresses: FixtureMap["listIpamSubnetAddresses"] = (id, offset = 0, limit = 256) =>
  gridFor(id).slice(offset, offset + limit);

/** Derived from the grid, so the facts strip and the rows agree. */
const getIpamSubnetUtilization: FixtureMap["getIpamSubnetUtilization"] = (id) => {
  const subnet = findSubnet(id);
  if (subnet.kind === "container") {
    throw new ApiError(400, JSON.stringify({ detail: "a container prefix has no utilization" }));
  }
  const grid = gridFor(id);
  const assigned = grid.filter((e) => e.state === "assigned").length;
  const reserved = grid.filter((e) => e.state === "reserved").length;
  const deprecated = grid.filter((e) => e.state === "deprecated").length;
  const observed = grid.filter((e) => e.last_seen_at !== null).length;
  const used = assigned + reserved + deprecated;
  const capacity = 254;
  const util: UtilizationOut = {
    subnet_id: id,
    prefix: subnet.prefix,
    enumerable: true,
    capacity,
    free: capacity - used,
    used,
    percentage: Math.round((used / capacity) * 1000) / 10,
    assigned,
    reserved,
    deprecated,
    observed,
  };
  return util;
};

const nextFree = (id: string, count: number): string[] =>
  gridFor(id)
    .filter((e) => e.status === "free" && e.range_kinds.length === 0)
    .slice(0, count)
    .map((e) => e.address);

const getIpamSubnetNextFree: FixtureMap["getIpamSubnetNextFree"] = (id, count = 1) => nextFree(id, count);

const addressOut = (entry: GridEntryOut, subnetId: string): AddressOut => ({
  id: entry.address_id ?? `addr-new-${entry.address}`,
  vrf_id: VRF_GLOBAL,
  subnet_id: subnetId,
  address: entry.address,
  state: entry.state ?? "free",
  origin: "operator",
  allocation_method: entry.allocation_method,
  device_id: entry.device_id,
  asset_label: entry.asset_label,
  mac: entry.mac,
  hostname: entry.hostname,
  description: "",
  assigned_at: entry.state === "assigned" ? ago(30 * 24 * 60) : null,
  released_at: null,
});

const allocateIpamAddress: FixtureMap["allocateIpamAddress"] = (id, body) => {
  const next = nextFree(id, 1)[0];
  if (!next) throw new ApiError(409, JSON.stringify({ detail: "no free address" }));
  return {
    ...addressOut({ ...workstationGrid()[0]!, address: next, address_id: null, state: "assigned" }, id),
    device_id: body.device_id ?? null,
    asset_label: body.asset_label ?? "",
    hostname: body.hostname ?? "",
    allocation_method: body.allocation_method ?? "static",
    state: "assigned",
    assigned_at: ago(0),
  };
};

function findAddress(id: string): { entry: GridEntryOut; subnetId: string } {
  for (const subnet of subnets()) {
    if (subnet.kind === "container") continue;
    const entry = gridFor(subnet.id).find((e) => e.address_id === id);
    if (entry) return { entry, subnetId: subnet.id };
  }
  return notFound("address");
}

const addressById = (id: string): AddressOut => {
  const { entry, subnetId } = findAddress(id);
  return addressOut(entry, subnetId);
};

const getIpamAddress: FixtureMap["getIpamAddress"] = (id) => addressById(id);

const createIpamAddress: FixtureMap["createIpamAddress"] = (body) => ({
  id: `addr-new-${body.address}`,
  vrf_id: body.vrf_id ?? VRF_GLOBAL,
  subnet_id: subnets().find((s) => s.kind === "assignable" && body.address.startsWith(`${base24(s.prefix)}.`))?.id ?? null,
  address: body.address,
  state: body.reserve ? "reserved" : "assigned",
  origin: "operator",
  allocation_method: body.allocation_method ?? "static",
  device_id: body.device_id ?? null,
  asset_label: body.asset_label ?? "",
  mac: body.mac ?? null,
  hostname: body.hostname ?? "",
  description: body.description ?? "",
  assigned_at: body.reserve ? null : ago(0),
  released_at: null,
});

const updateIpamAddress: FixtureMap["updateIpamAddress"] = (id, body) => {
  const current = addressById(id);
  return {
    ...current,
    hostname: body.hostname ?? current.hostname,
    description: body.description ?? current.description,
    asset_label: body.asset_label ?? current.asset_label,
    allocation_method: body.allocation_method ?? current.allocation_method,
  };
};

const releaseIpamAddress: FixtureMap["releaseIpamAddress"] = (id) => ({
  ...addressById(id),
  state: "free",
  device_id: null,
  asset_label: "",
  released_at: ago(0),
});

const getIpamAddressHistory: FixtureMap["getIpamAddressHistory"] = (id) => {
  const { entry } = findAddress(id);
  const history: HistoryEntryOut[] = [
    {
      ts: ago(30 * 24 * 60),
      actor_type: "user",
      actor_id: "usr-ines",
      action: "ipam.address_assigned",
      from_state: "free",
      to_state: entry.state ?? "assigned",
      detail: {
        address: entry.address,
        claimant: entry.device_id ?? entry.asset_label,
        claimant_type: entry.device_id ? "device" : "asset_label",
        allocation_method: entry.allocation_method,
      },
    },
    {
      ts: ago(31 * 24 * 60),
      actor_type: "user",
      actor_id: "usr-ines",
      action: "ipam.address_state_changed",
      from_state: null,
      to_state: "free",
      detail: { address: entry.address },
    },
  ];
  return history;
};

/* ----------------------------------------------------------------- ranges */

const ranges = (): RangeOut[] => [
  { id: "range-1", subnet_id: "subnet-1", kind: "dhcp_pool", start_address: "10.20.9.200", end_address: "10.20.9.219", name: "Visitor DHCP" },
  { id: "range-2", subnet_id: "subnet-1", kind: "reserved", start_address: "10.20.9.250", end_address: "10.20.9.254", name: "Printers" },
  { id: "range-3", subnet_id: "subnet-2", kind: "excluded", start_address: "10.20.4.250", end_address: "10.20.4.254", name: "Out-of-band management" },
];

const listIpamRanges: FixtureMap["listIpamRanges"] = (subnetId) => {
  findSubnet(subnetId);
  return ranges().filter((range) => range.subnet_id === subnetId);
};

const createIpamRange: FixtureMap["createIpamRange"] = (subnetId, body) => ({
  id: `range-${body.start_address}`,
  subnet_id: subnetId,
  kind: body.kind,
  start_address: body.start_address,
  end_address: body.end_address,
  name: body.name ?? "",
});

const deleteIpamRange: FixtureMap["deleteIpamRange"] = () => undefined;

/* ------------------------------------------------------------------ drift */

const driftRows = (): DriftOut[] => [
  {
    id: "drift-1",
    vrf_id: VRF_GLOBAL,
    subnet_id: "subnet-1",
    address: "10.20.9.14",
    drift_class: "conflict",
    severity: "high",
    confidence: "high",
    detail: { macs: ["a4:83:e7:09:06:5c", "f0:18:98:aa:bb:cc"], claimant_mismatch: true, vrf_count: 1 },
    device_id: "dev-2",
    first_detected_at: ago(4 * 24 * 60),
    last_evaluated_at: ago(134),
    consecutive_detections: 4,
    resolved_at: null,
    acknowledged_at: null,
    suppressed_until: null,
    note: "",
  },
  {
    id: "drift-2",
    vrf_id: VRF_GLOBAL,
    subnet_id: "subnet-1",
    address: "10.20.9.207",
    drift_class: "present_undocumented",
    severity: "medium",
    confidence: "high",
    detail: { observed_macs: ["a4:83:e7:09:ef:2a"], source: "sweep", observed_by: "dev-5" },
    device_id: null,
    first_detected_at: ago(6 * 24 * 60),
    last_evaluated_at: ago(134),
    consecutive_detections: 6,
    resolved_at: null,
    acknowledged_at: null,
    suppressed_until: null,
    note: "",
  },
  {
    id: "drift-3",
    vrf_id: VRF_GLOBAL,
    subnet_id: "subnet-2",
    address: "10.20.4.99",
    drift_class: "documented_absent",
    severity: "medium",
    confidence: "medium",
    detail: { last_seen_days: 40, gate_e: true, gate_l: true, hostname: "old-backup-01" },
    device_id: null,
    first_detected_at: ago(33 * 24 * 60),
    last_evaluated_at: ago(134),
    consecutive_detections: 33,
    resolved_at: null,
    acknowledged_at: ago(20 * 24 * 60),
    suppressed_until: null,
    note: "Backup appliance decommissioned in July; waiting on the record.",
  },
  {
    id: "drift-4",
    vrf_id: VRF_GLOBAL,
    subnet_id: "subnet-1",
    address: "10.20.9.57",
    drift_class: "mac_mismatch",
    severity: "low",
    confidence: "medium",
    detail: { documented_mac: "a4:83:e7:09:2d:0e", observed_macs: ["a4:83:e7:09:2d:0f"] },
    device_id: null,
    first_detected_at: ago(2 * 24 * 60),
    last_evaluated_at: ago(134),
    consecutive_detections: 2,
    resolved_at: null,
    acknowledged_at: null,
    suppressed_until: null,
    note: "",
  },
  {
    id: "drift-5",
    vrf_id: VRF_GLOBAL,
    subnet_id: "subnet-2",
    address: "10.20.4.31",
    drift_class: "device_moved",
    severity: "medium",
    confidence: "high",
    detail: { documented_at: "10.20.4.31", observed_at: "10.20.9.31", device_id: "dev-3", observed_subnet_id: "subnet-1" },
    device_id: "dev-3",
    first_detected_at: ago(24 * 60 + 12),
    last_evaluated_at: ago(134),
    consecutive_detections: 1,
    resolved_at: null,
    acknowledged_at: null,
    suppressed_until: null,
    note: "",
  },
  {
    id: "drift-6",
    vrf_id: VRF_GLOBAL,
    subnet_id: "subnet-4",
    address: "10.30.1.77",
    drift_class: "claimant_mismatch",
    severity: "high",
    confidence: "medium",
    detail: { documented_claimant: "dev-4", observed_device_id: "dev-14" },
    device_id: "dev-14",
    first_detected_at: ago(3 * 24 * 60),
    last_evaluated_at: ago(3 * 24 * 60),
    consecutive_detections: 3,
    resolved_at: null,
    acknowledged_at: null,
    suppressed_until: ahead(10 * 24 * 60),
    note: "Sofia moved desks; waiting for the Porto sweep to confirm.",
  },
  {
    id: "drift-7",
    vrf_id: VRF_GLOBAL,
    subnet_id: "subnet-1",
    address: "10.20.9.88",
    drift_class: "present_undocumented",
    severity: "medium",
    confidence: "high",
    detail: { observed_macs: ["a4:83:e7:09:c8:41"], source: "inventory" },
    device_id: "dev-14",
    first_detected_at: ago(12 * 24 * 60),
    last_evaluated_at: ago(5 * 24 * 60),
    consecutive_detections: 7,
    resolved_at: ago(5 * 24 * 60),
    acknowledged_at: null,
    suppressed_until: null,
    note: "Registered after the fact.",
  },
];

const dormantRows = (): DriftOut[] =>
  [23, 46, 69].map((i) => ({
    id: `dormant-${i}`,
    vrf_id: VRF_GLOBAL,
    subnet_id: "subnet-1",
    address: `10.20.9.${i}`,
    drift_class: "dormant",
    severity: "low",
    confidence: "low",
    detail: { state: "assigned", device_id: null, gate_e: false, gate_l: true },
    device_id: null,
    first_detected_at: ago(9 * 24 * 60 + i),
    last_evaluated_at: ago(134),
    consecutive_detections: 9,
    resolved_at: null,
    acknowledged_at: null,
    suppressed_until: null,
    note: "",
  }));

const listIpamDrift: FixtureMap["listIpamDrift"] = (filters = {}) => {
  const rows = filters.class === "dormant" ? dormantRows() : driftRows();
  return rows.filter((row) => {
    if (filters.class && filters.class !== "dormant" && row.drift_class !== filters.class) return false;
    if (filters.subnet && row.subnet_id !== filters.subnet) return false;
    if (!filters.include_resolved && row.resolved_at !== null) return false;
    return true;
  });
};

const acknowledgeIpamDrift: FixtureMap["acknowledgeIpamDrift"] = (id, note) => {
  const row = [...driftRows(), ...dormantRows()].find((r) => r.id === id) ?? notFound("drift finding");
  return { ...row, acknowledged_at: ago(0), note };
};

const suppressIpamDrift: FixtureMap["suppressIpamDrift"] = (id, until, note) => {
  const row = [...driftRows(), ...dormantRows()].find((r) => r.id === id) ?? notFound("drift finding");
  return { ...row, suppressed_until: until, note };
};

const listIpamOrphans: FixtureMap["listIpamOrphans"] = () => {
  const orphans: OrphanOut[] = [
    { prefix: "10.20.7.0/24", count: 6, last_seen_at: ago(134) },
    { prefix: "192.168.50.0/24", count: 2, last_seen_at: ago(26 * 60) },
  ];
  return orphans;
};

/* ------------------------------------------------- sweep governance */

const scopes = (): ScopeOut[] => [
  { id: "scope-1", vrf_id: VRF_GLOBAL, prefix: "10.20.4.0/24", note: "Rack room, swept nightly from rack-b4-relay.", authorized_by: "ines.caetano", authorized_at: ago(36 * 24 * 60), expires_at: null, revoked_at: null },
  { id: "scope-2", vrf_id: VRF_GLOBAL, prefix: "10.20.9.0/24", note: "Workstation VLAN.", authorized_by: "ines.caetano", authorized_at: ago(36 * 24 * 60), expires_at: null, revoked_at: null },
  { id: "scope-3", vrf_id: VRF_GLOBAL, prefix: "10.20.11.0/24", note: "Wi-Fi audit for the September review. No agent can reach it yet.", authorized_by: "helgar.palmieri", authorized_at: ago(12 * 24 * 60), expires_at: ahead(30 * 24 * 60), revoked_at: null },
  { id: "scope-4", vrf_id: VRF_GLOBAL, prefix: "10.30.1.0/24", note: "Porto office.", authorized_by: "ines.caetano", authorized_at: ago(36 * 24 * 60), expires_at: null, revoked_at: null },
  { id: "scope-5", vrf_id: VRF_GLOBAL, prefix: "10.30.0.0/16", note: "Porto migration window.", authorized_by: "helgar.palmieri", authorized_at: ago(90 * 24 * 60), expires_at: null, revoked_at: ago(60 * 24 * 60) },
];

const listIpamScanAuthorizations: FixtureMap["listIpamScanAuthorizations"] = (includeRevoked = false) =>
  scopes().filter((scope) => includeRevoked || scope.revoked_at === null);

const createIpamScanAuthorization: FixtureMap["createIpamScanAuthorization"] = (body) => ({
  id: `scope-${body.prefix}`,
  vrf_id: body.vrf_id ?? VRF_GLOBAL,
  prefix: body.prefix,
  note: body.note ?? "",
  authorized_by: "helgar.palmieri",
  authorized_at: ago(0),
  expires_at: body.expires_at ?? null,
  revoked_at: null,
});

const revokeIpamScanAuthorization: FixtureMap["revokeIpamScanAuthorization"] = () => undefined;

const sweepRuns = (): SweepRunOut[] => {
  const run = (id: string, subnet: string, minutesAgo: number, state: string, found: number, targets: number, trigger = "scheduled", device: string | null = "dev-5", error = ""): SweepRunOut => ({
    id,
    subnet_id: subnet,
    job_id: state === "no_agent" || state === "refused" ? null : `job-${id}`,
    device_id: device,
    state,
    trigger,
    target_count: targets,
    scope_digest: "sha256:4f1c9e",
    hosts_found: found,
    truncated: false,
    started_at: ago(minutesAgo),
    finished_at: state === "queued" || state === "dispatched" ? null : ago(minutesAgo - 3),
    error,
  });
  return [
    run("run-1", "subnet-1", 2 * 60 + 14, "succeeded", 121, 234),
    run("run-2", "subnet-1", 26 * 60 + 14, "succeeded", 119, 234),
    run("run-3", "subnet-1", 50 * 60 + 14, "succeeded", 118, 234),
    run("run-4", "subnet-1", 3 * 24 * 60 + 40, "succeeded", 120, 234, "manual"),
    run("run-5", "subnet-1", 4 * 24 * 60 + 14, "failed", 0, 234, "scheduled", "dev-5", "Result rejected: scope digest mismatch."),
    run("run-6", "subnet-2", 2 * 60 + 14, "succeeded", 30, 249),
    run("run-7", "subnet-3", 24 * 60, "no_agent", 0, 0, "scheduled", null),
    run("run-8", "subnet-4", 3 * 24 * 60, "succeeded", 40, 254, "scheduled", "dev-14"),
  ];
};

const listIpamSweepRuns: FixtureMap["listIpamSweepRuns"] = (subnetId, limit = 50) => {
  findSubnet(subnetId);
  return sweepRuns()
    .filter((run) => run.subnet_id === subnetId)
    .slice(0, limit);
};

const dispatchIpamSweep: FixtureMap["dispatchIpamSweep"] = (subnetId) => {
  const subnet = findSubnet(subnetId);
  const run: SweepRunOut = {
    id: `run-${subnetId}-now`,
    subnet_id: subnetId,
    job_id: `job-sweep-${subnetId}`,
    device_id: subnet.sweep_device_id ?? "dev-5",
    state: "queued",
    trigger: "manual",
    target_count: 234,
    scope_digest: "sha256:4f1c9e",
    hosts_found: 0,
    truncated: false,
    started_at: ago(0),
    finished_at: null,
    error: "",
  };
  return run;
};

const updateIpamSweepSettings: FixtureMap["updateIpamSweepSettings"] = (subnetId, body) => ({
  ...findSubnet(subnetId),
  sweep_enabled: body.sweep_enabled,
  sweep_device_id: body.sweep_device_id ?? null,
  sweep_interval_hours: body.sweep_interval_hours ?? null,
});

/* ----------------------------------------------------------------- search */

const searchIpam: FixtureMap["searchIpam"] = (q) => {
  const query = q.trim();
  const kind = query.includes("/")
    ? "cidr"
    : /^([0-9a-f]{2}:){5}[0-9a-f]{2}$/i.test(query)
      ? "mac"
      : /^\d{1,3}(\.\d{1,3}){3}$/.test(query)
        ? "address"
        : "text";
  const lower = query.toLowerCase();
  const matchingSubnets = subnets().filter((s) =>
    kind === "cidr"
      ? s.prefix === query
      : kind === "address"
        ? s.kind === "assignable" && query.startsWith(`${base24(s.prefix)}.`)
        : s.name.toLowerCase().includes(lower) || s.prefix.includes(lower),
  );
  const grids = subnets()
    .filter((s) => s.kind === "assignable")
    .flatMap((s) => gridFor(s.id).filter((e) => e.address_id !== null));
  const addresses = grids
    .filter((e) =>
      kind === "address"
        ? e.address === query
        : kind === "mac"
          ? e.mac?.toLowerCase() === lower
          : kind === "text"
            ? e.hostname.toLowerCase().includes(lower) || e.asset_label.toLowerCase().includes(lower)
            : false,
    )
    .map((e) => e.address);
  const matchedDevices = devices()
    .filter((d) =>
      kind === "address"
        ? d.primary_ip === query
        : kind === "text"
          ? (d.hostname ?? "").toLowerCase().includes(lower)
          : false,
    )
    .map((d) => d.id);
  const out: SearchOut = {
    kind,
    query,
    subnets: matchingSubnets.map((s) => s.id),
    addresses,
    observations: addresses.length + matchedDevices.length,
    devices: matchedDevices,
  };
  return out;
};

/* ------------------------------------------------------ registry writes */

const createIpamVrf: FixtureMap["createIpamVrf"] = (body) => ({ id: `vrf-${body.name}`, name: body.name, rd: body.rd ?? null, description: body.description ?? "" });
const createIpamSite: FixtureMap["createIpamSite"] = (body) => ({ id: `site-${body.name.toLowerCase()}`, name: body.name, doc_record_id: body.doc_record_id ?? null, description: body.description ?? "" });
const createIpamVlan: FixtureMap["createIpamVlan"] = (body) => ({ id: `vlan-${body.vid}`, vid: body.vid, site_id: body.site_id ?? null, name: body.name ?? "", description: body.description ?? "" });

const createIpamSubnet: FixtureMap["createIpamSubnet"] = (body) => ({
  id: `subnet-${body.prefix}`,
  vrf_id: body.vrf_id ?? VRF_GLOBAL,
  prefix: body.prefix,
  kind: body.kind ?? "assignable",
  name: body.name ?? "",
  description: body.description ?? "",
  site_id: body.site_id ?? null,
  vlan_id: body.vlan_id ?? null,
  gateway: body.gateway ?? null,
  dns_servers: body.dns_servers ?? [],
  parent_id: body.prefix.startsWith("10.20.") ? "subnet-0" : null,
  sweep_enabled: false,
  sweep_device_id: null,
  sweep_interval_hours: null,
  last_swept_at: null,
});

const getIpamSubnet: FixtureMap["getIpamSubnet"] = (id) => findSubnet(id);

const updateIpamSubnet: FixtureMap["updateIpamSubnet"] = (id, body) => {
  const subnet = findSubnet(id);
  return {
    ...subnet,
    name: body.name ?? subnet.name,
    description: body.description ?? subnet.description,
    site_id: body.site_id === undefined ? subnet.site_id : body.site_id,
    vlan_id: body.vlan_id === undefined ? subnet.vlan_id : body.vlan_id,
    gateway: body.gateway === undefined ? subnet.gateway : body.gateway,
    dns_servers: body.dns_servers ?? subnet.dns_servers,
  };
};

const deleteIpamSubnet: FixtureMap["deleteIpamSubnet"] = (id, cascade = false) => {
  const subnet = findSubnet(id);
  if (subnet.kind === "assignable" && !cascade) {
    throw new ApiError(409, JSON.stringify({ detail: `${subnet.prefix} still holds addresses, ranges and sweep history; delete with cascade to release them` }));
  }
  return undefined;
};

/* ---------------------------------------------------------- import/export */

const previewRows = (): ImportRowOut[] => [
  { line: 2, action: "create", address: "10.20.9.130", error: "", detail: { hostname: "ws-new-130", asset_label: "Desk 65" } },
  { line: 3, action: "update", address: "10.20.9.14", error: "", detail: { hostname: "ws-tiago-14" } },
  { line: 4, action: "conflict", address: "10.20.9.207", error: "", detail: { reason: "observed with a different MAC" } },
  { line: 5, action: "error", address: "", error: "line 5: '10.20.9.999' is not an IPv4 or IPv6 address", detail: {} },
];

const previewIpamImport: FixtureMap["previewIpamImport"] = () => ({
  digest: "3b7f0a9c4e1d5b6a8f2c9d0e1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b",
  total_rows: 4,
  creates: 1,
  updates: 1,
  conflicts: 1,
  errors: 1,
  rows: previewRows(),
});

const applyIpamImport: FixtureMap["applyIpamImport"] = (_file, _mapping, digest) => ({
  digest,
  total_rows: 4,
  applied: 2,
  errors: 2,
  rows: previewRows(),
});

const convertIpamDocRecords: FixtureMap["convertIpamDocRecords"] = (dryRun = true) => ({
  dry_run: dryRun,
  converted: [
    { record_id: "doc-net-guest", name: "Guest Wi-Fi", prefix: "10.20.12.0/24", subnet_id: dryRun ? null : "subnet-10.20.12.0/24", vlan_id: null, warnings: [] },
  ],
  skipped: [{ record_id: "doc-net-legacy", name: "Legacy lab", reason: "prefix 10.20.0.0/16 is already registered" }],
});

const csv = (header: string, rows: string[][]): string =>
  [header, ...rows.map((row) => row.join(","))].join("\n");

const exportIpamSubnetsCsv: FixtureMap["exportIpamSubnetsCsv"] = () =>
  csv("prefix,kind,name,site,vlan,gateway", subnets().map((s) => [s.prefix, s.kind, s.name, s.site_id ?? "", s.vlan_id ?? "", s.gateway ?? ""]));

const exportIpamAddressesCsv: FixtureMap["exportIpamAddressesCsv"] = () =>
  csv("address,state,hostname,mac", workstationGrid().filter((e) => e.address_id !== null).map((e) => [e.address, e.state ?? "", e.hostname, e.mac ?? ""]));

const exportIpamDriftCsv: FixtureMap["exportIpamDriftCsv"] = () =>
  csv("address,class,severity", driftRows().map((d) => [d.address, d.drift_class, d.severity]));

const fixtures: FixtureMap = {
  listIpamVrfs: vrfs,
  createIpamVrf,
  listIpamSites: sites,
  createIpamSite,
  listIpamVlans: (siteId) => vlans().filter((v) => !siteId || v.site_id === siteId || v.site_id === null),
  createIpamVlan,
  listIpamSubnets,
  createIpamSubnet,
  getIpamSubnet,
  updateIpamSubnet,
  deleteIpamSubnet,
  listIpamSubnetAddresses,
  getIpamSubnetUtilization,
  getIpamSubnetNextFree,
  allocateIpamAddress,
  listIpamRanges,
  createIpamRange,
  deleteIpamRange,
  getIpamAddress,
  createIpamAddress,
  updateIpamAddress,
  releaseIpamAddress,
  getIpamAddressHistory,
  searchIpam,
  listIpamDrift,
  acknowledgeIpamDrift,
  suppressIpamDrift,
  listIpamOrphans,
  listIpamScanAuthorizations,
  createIpamScanAuthorization,
  revokeIpamScanAuthorization,
  dispatchIpamSweep,
  updateIpamSweepSettings,
  listIpamSweepRuns,
  previewIpamImport,
  applyIpamImport,
  convertIpamDocRecords,
  exportIpamSubnetsCsv,
  exportIpamAddressesCsv,
  exportIpamDriftCsv,
};

export default fixtures;
