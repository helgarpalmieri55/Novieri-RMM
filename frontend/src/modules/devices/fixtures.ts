/**
 * Fixtures for the devices module: groups, discovery candidates and the
 * suggestion queue. `listDevices` / `getDevice` live in `fixtures/core.ts`
 * (the shell's worked example); everything here is built from that same
 * fleet so the group counts agree with the rows on `/devices`.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { devices } from "../../fixtures/core";
import type {
  AssetCandidate,
  Device,
  DeviceGroup,
  DeviceGroupDetail,
  DeviceGroupPage,
  GroupRules,
  RulePreview,
} from "../../lib/api";
import { ApiError } from "../../lib/api";

const MINUTE = 60_000;
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();

/* ------------------------------------------------------------------ groups */

interface GroupSeed {
  id: string;
  name: string;
  description: string;
  dynamic: boolean;
  precedence: number;
  parent: string | null;
  archived: boolean;
  rules: GroupRules | null;
}

const GROUP_SEEDS: GroupSeed[] = [
  {
    id: "grp-servers",
    name: "Production servers",
    description: "Rule: asset type = server",
    dynamic: true,
    precedence: 10,
    parent: null,
    archived: false,
    rules: { all: [{ fact: "asset_type", op: "eq", value: "server" }] },
  },
  {
    id: "grp-workstations",
    name: "Workstations",
    description: "Rule: asset type = computer AND lifecycle = in service",
    dynamic: true,
    precedence: 20,
    parent: null,
    archived: false,
    rules: {
      all: [
        { fact: "asset_type", op: "eq", value: "computer" },
        { fact: "lifecycle", op: "eq", value: "in_service" },
      ],
    },
  },
  {
    id: "grp-build",
    name: "Build runners",
    description: "Manual membership",
    dynamic: false,
    precedence: 30,
    parent: "grp-servers",
    archived: false,
    rules: null,
  },
  {
    id: "grp-pilot",
    name: "Pilot ring (2025)",
    description: "Retired after the ring merged into Workstations",
    dynamic: false,
    precedence: 40,
    parent: null,
    archived: true,
    rules: null,
  },
];

function group(seed: GroupSeed): DeviceGroup {
  return {
    id: seed.id,
    name: seed.name,
    parent_group_id: seed.parent,
    description: seed.description,
    dynamic: seed.dynamic,
    device_count: devices().filter((d) => d.device_group_id === seed.id).length,
    precedence: seed.precedence,
    archived_at: seed.archived ? ago(60 * 24 * 60) : null,
  };
}

const listDeviceGroups: FixtureMap["listDeviceGroups"] = (params = {}) => {
  const items = GROUP_SEEDS.filter((seed) => params.include_archived || !seed.archived).map(group);
  const page: DeviceGroupPage = {
    items,
    total: items.length,
    ungrouped_device_count: devices().filter((d) => d.device_group_id === null).length,
    evaluation: {
      last_evaluated_at: ago(4),
      last_full_pass_at: ago(6 * 60),
      full_pass: false,
      groups_evaluated: 2,
      devices_evaluated: 7,
      devices_moved: 0,
      devices_without_inventory: 5,
      interval_seconds: 300,
    },
  };
  return page;
};

const getDeviceGroup: FixtureMap["getDeviceGroup"] = (id) => {
  const seed = GROUP_SEEDS.find((s) => s.id === id);
  if (!seed) throw new ApiError(404, JSON.stringify({ detail: "device group not found" }));
  const detail: DeviceGroupDetail = {
    group: group(seed),
    rules: seed.rules,
    references: {
      devices: group(seed).device_count,
      child_groups: GROUP_SEEDS.filter((s) => s.parent === seed.id).length,
      policies: 0,
      patch_policies: seed.id === "grp-servers" ? 1 : 0,
      backup_policies: 0,
      remote_policies: 0,
      health_assignments: 0,
      credentials: 0,
    },
  };
  return detail;
};

/** A candidate rule, evaluated over the fixture fleet's flat facts only. */
const previewDeviceGroupRules: FixtureMap["previewDeviceGroupRules"] = (rules) => {
  const holds = (d: Device, fact: string, op: string, value: unknown) => {
    const actual = (d as unknown as Record<string, unknown>)[fact];
    if (op === "eq") return String(actual) === String(value);
    if (op === "ne") return String(actual) !== String(value);
    if (op === "contains") return String(actual ?? "").includes(String(value));
    return false;
  };
  const matched = devices().filter((d) =>
    d.agent_status !== null && rules.all.every((c) => holds(d, c.fact, c.op, c.value)),
  );
  const groupName = (id: string | null) => GROUP_SEEDS.find((s) => s.id === id)?.name ?? null;
  const preview: RulePreview = {
    matched: matched.length,
    devices_without_inventory: devices().filter((d) => d.agent_status === null).length,
    sample: matched.slice(0, 5).map((d) => ({
      device_id: d.id,
      hostname: d.hostname,
      current_group_id: d.device_group_id,
      current_group_name: groupName(d.device_group_id),
    })),
    sample_truncated: matched.length > 5,
  };
  return preview;
};

/* -------------------------------------------------------------- discovery */

const CANDIDATES: AssetCandidate[] = [
  {
    address: "10.20.9.201",
    mac: "00:80:77:5a:12:9c",
    vendor: "Brother Industries",
    oui_prefix: "00:80:77",
    hostname: "brn-3f-copier",
    first_seen_at: ago(9 * 24 * 60),
    last_seen_at: ago(48),
    sightings: 14,
    confidence: "medium",
    subnet_id: "subnet-2",
  },
  {
    address: "10.20.11.57",
    mac: null,
    vendor: null,
    oui_prefix: "",
    hostname: "",
    first_seen_at: ago(2 * 24 * 60),
    last_seen_at: ago(48),
    sightings: 2,
    confidence: "low",
    subnet_id: "subnet-3",
  },
];

const listAssetCandidates: FixtureMap["listAssetCandidates"] = () => ({
  items: CANDIDATES,
  total: CANDIDATES.length,
  truncated: false,
  unsweepable_subnets: 1,
});

const promoteAssetCandidates: FixtureMap["promoteAssetCandidates"] = (body) => ({
  results: body.addresses.map((address, at) => ({
    address,
    created: true,
    device_id: `dev-new-${at + 1}`,
    reason: "created",
  })),
});

/** Nothing is waiting on a person: the panel renders nothing, as designed. */
const listAssetSuggestions: FixtureMap["listAssetSuggestions"] = () => ({ items: [] });
const resolveAssetSuggestion: FixtureMap["resolveAssetSuggestion"] = () => ({ items: [] });

const createAsset: FixtureMap["createAsset"] = (body) => ({
  id: `dev-${Date.now()}`,
  hostname: body.hostname ?? null,
  fqdn: body.fqdn ? body.fqdn.toLowerCase() : null,
  os_family: body.os_family ?? null,
  os_version: "",
  architecture: body.architecture ?? "",
  device_group_id: null,
  status: "active",
  agent_status: null,
  last_seen_at: null,
  cert_not_after: null,
  cert_state: null,
  cert_revoked: false,
  lifecycle: body.lifecycle ?? "in_service",
  asset_type: body.asset_type ?? "other",
  source: "manual",
  provenance: "operator",
  description: body.description ?? null,
  primary_ip: body.primary_ip ?? null,
  owner: body.owner ?? null,
  location: body.location ?? null,
});

/** An asset edit (the push-details dialog): the fleet row with what was sent applied. */
const updateAsset: FixtureMap["updateAsset"] = (id, body) => {
  const row = devices().find((device) => device.id === id);
  if (!row) throw new ApiError(404, JSON.stringify({ detail: "no such device" }));
  return {
    ...row,
    ...(body.fqdn !== undefined ? { fqdn: body.fqdn ? body.fqdn.toLowerCase() : null } : {}),
    ...(body.os_family !== undefined ? { os_family: body.os_family ?? null } : {}),
    ...(body.architecture !== undefined ? { architecture: body.architecture ?? "" } : {}),
  };
};

export const fixtures: FixtureMap = {
  updateAsset,
  listDeviceGroups,
  getDeviceGroup,
  previewDeviceGroupRules,
  listAssetCandidates,
  promoteAssetCandidates,
  listAssetSuggestions,
  resolveAssetSuggestion,
  createAsset,
};

export default fixtures;
