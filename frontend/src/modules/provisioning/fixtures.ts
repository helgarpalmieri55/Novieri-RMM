/**
 * Fixtures for the provisioning module: the OS images, the profiles that lay
 * them down, the runs those profiles produced, the fleet report over those
 * runs and one device's provisioning status.
 *
 * THE REPORT IS COUNTED FROM THE RUNS. `run_counts_by_state`, `success_rate`
 * and `per_profile` are computed from the run list below, so the tiles on
 * `/provisioning` and the table under them are one fact. The runs name the
 * shell's worked fleet where a machine has finished enrolling (`device_id`),
 * and a MAC or asset tag where it has not.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { ApiError } from "../../lib/api";
import type { FleetProvisioningReport, OsImage, ProvisionProfile, ProvisionRun } from "./types";

const MINUTE = 60_000;
const HOUR = 60;
const DAY = 24 * HOUR;
/** An ISO timestamp `minutes` before the call. */
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();

const notFound = (what: string) => new ApiError(404, JSON.stringify({ detail: `${what} not found` }));

/* ------------------------------------------------------------------ images */

const IMAGES: OsImage[] = [
  {
    id: "img-win11-23h2-ent",
    os_family: "windows",
    name: "Windows 11 23H2 · Enterprise",
    kind: "install",
    artifact_ref: "s3://itms-images/win11-23h2-enterprise.wim",
    artifact_sha256: "7c1d9e3f5a7b9c1d3e5f7a9b1c3d5e7f9a1b3c5d7e9f1a3b5c7d9e1f3a5b7c9e0a2",
    params: { edition: "Enterprise", language: "en-GB", size_bytes: 6_227_702_579 },
    state: "active",
    created_at: ago(28 * DAY),
  },
  {
    id: "img-ubuntu-2604-srv",
    os_family: "linux",
    name: "Ubuntu 26.04 LTS server",
    kind: "install",
    artifact_ref: "s3://itms-images/ubuntu-26.04-live-server-amd64.iso",
    artifact_sha256: "b83f1a3b5c7d9e1f3a5b7c9d1e3f5a7b9c1d3e5f7a9b1c3d5e7f9a1b3c5d7e9f19c4",
    params: { autoinstall: true, size_bytes: 2_254_857_830 },
    state: "active",
    created_at: ago(19 * DAY),
  },
  {
    id: "img-win11-24h2-ans",
    os_family: "windows",
    name: "Windows 11 24H2 answer file",
    kind: "answer_file",
    artifact_ref: null,
    artifact_sha256: null,
    params: { edition: "Enterprise", language: "en-GB" },
    state: "draft",
    created_at: ago(2 * DAY),
  },
];

/* ---------------------------------------------------------------- profiles */

const PROFILES: ProvisionProfile[] = [
  {
    id: "prof-workstation-std",
    name: "Workstation standard",
    image_id: "img-win11-23h2-ent",
    post_config: {
      hostname_pattern: "LIS-WS-{seq}",
      domain_join: { domain: "corp.toeshee.example", username: "svc-join" },
      local_admin: { username: "toeshee-admin" },
      first_jobs: ["deploy.install:pkg-chrome-128", "patch.scan"],
    },
    target_scope: { device_group_id: "grp-workstations", location: "Lisbon" },
    enabled: true,
    created_at: ago(27 * DAY),
    has_domain_join_credential: true,
    has_local_admin_credential: true,
  },
  {
    id: "prof-build-runner",
    name: "Build runner",
    image_id: "img-ubuntu-2604-srv",
    post_config: {
      hostname_pattern: "build-runner-{seq}",
      local_admin: { username: "ops" },
      first_jobs: ["deploy.install:pkg-agent-deb"],
    },
    target_scope: { device_group_id: "grp-build" },
    enabled: true,
    created_at: ago(18 * DAY),
    has_domain_join_credential: false,
    has_local_admin_credential: true,
  },
  {
    id: "prof-kiosk",
    name: "Reception kiosk",
    image_id: "img-win11-23h2-ent",
    post_config: { hostname_pattern: "POR-KIOSK-{seq}" },
    target_scope: { location: "Porto" },
    enabled: false,
    created_at: ago(9 * DAY),
    has_domain_join_credential: false,
    has_local_admin_credential: false,
  },
];

/* -------------------------------------------------------------------- runs */

interface RunSeed {
  id: string;
  profile: string;
  device: string | null;
  target: string | null;
  state: string;
  startedMinutes: number | null;
  durationMinutes: number | null;
  error: string | null;
}

/**
 * Nine runs: one installing now, one queued, six completed this week across
 * both profiles, and one that timed out at PXE.
 */
const RUN_SEEDS: RunSeed[] = [
  { id: "run-ws-0119", profile: "prof-workstation-std", device: null, target: "LIS-WS-0119", state: "awaiting_enrollment", startedMinutes: 38, durationMinutes: null, error: null },
  { id: "run-ws-0120", profile: "prof-workstation-std", device: null, target: "3c:7c:3f:2a:91:44", state: "pending", startedMinutes: null, durationMinutes: null, error: null },
  { id: "run-ws-041", profile: "prof-workstation-std", device: "dev-1", target: "LIS-WS-041", state: "complete", startedMinutes: DAY + 2 * HOUR, durationMinutes: 47, error: null },
  { id: "run-ws-042", profile: "prof-workstation-std", device: "dev-2", target: "LIS-WS-042", state: "complete", startedMinutes: DAY + 3 * HOUR, durationMinutes: 52, error: null },
  { id: "run-ws-019", profile: "prof-workstation-std", device: "dev-9", target: "LIS-WS-019", state: "complete", startedMinutes: 2 * DAY + 5 * HOUR, durationMinutes: 44, error: null },
  { id: "run-app-02", profile: "prof-build-runner", device: "dev-5", target: "3c:7c:3f:1a:02:9e", state: "complete", startedMinutes: 3 * DAY, durationMinutes: 23, error: null },
  { id: "run-ws-011", profile: "prof-workstation-std", device: "dev-14", target: "POR-WS-011", state: "complete", startedMinutes: 4 * DAY + HOUR, durationMinutes: 58, error: null },
  { id: "run-ws-008", profile: "prof-workstation-std", device: "dev-4", target: "POR-WS-008", state: "complete", startedMinutes: 5 * DAY + 2 * HOUR, durationMinutes: 61, error: null },
  { id: "run-ws-0118", profile: "prof-workstation-std", device: null, target: "3c:7c:3f:2a:91:3f", state: "failed", startedMinutes: 6 * DAY, durationMinutes: 30, error: "PXE never answered: no DHCP offer from the target segment within 30 minutes." },
];

function run(seed: RunSeed): ProvisionRun {
  const started = seed.startedMinutes === null ? null : ago(seed.startedMinutes);
  const finished =
    seed.startedMinutes === null || seed.durationMinutes === null
      ? null
      : ago(seed.startedMinutes - seed.durationMinutes);
  return {
    id: seed.id,
    profile_id: seed.profile,
    device_id: seed.device,
    target_ref: seed.target,
    state: seed.state,
    answer_ref: seed.state === "pending" ? null : `s3://itms-images/answers/${seed.id}.xml`,
    started_at: started,
    finished_at: finished,
    error: seed.error,
  };
}

const runs = (): ProvisionRun[] => RUN_SEEDS.map(run);

/* ---------------------------------------------------------------- fixtures */

const listOsImages: FixtureMap["listOsImages"] = () => IMAGES;

const getOsImage: FixtureMap["getOsImage"] = (id) => {
  const found = IMAGES.find((image) => image.id === id);
  if (!found) throw notFound("image");
  return found;
};

const createOsImage: FixtureMap["createOsImage"] = (body) => ({
  id: `img-${Date.now()}`,
  os_family: body.os_family,
  name: body.name,
  kind: body.kind,
  artifact_ref: null,
  artifact_sha256: null,
  params: body.params ?? {},
  state: "draft",
  created_at: ago(0),
});

const listProvisionProfiles: FixtureMap["listProvisionProfiles"] = () => PROFILES;

const getProvisionProfile: FixtureMap["getProvisionProfile"] = (id) => {
  const found = PROFILES.find((profile) => profile.id === id);
  if (!found) throw notFound("profile");
  return found;
};

/** The server never returns a credential value: only whether one is set. */
function redacted(body: Parameters<NonNullable<FixtureMap["createProvisionProfile"]>>[0], id: string, createdAt: string): ProvisionProfile {
  const postConfig = { ...(body.post_config ?? {}) } as Record<string, unknown>;
  const domainJoin = { ...((postConfig.domain_join as Record<string, unknown> | undefined) ?? {}) };
  const localAdmin = { ...((postConfig.local_admin as Record<string, unknown> | undefined) ?? {}) };
  const hasDomain = typeof domainJoin.password === "string" && domainJoin.password.length > 0;
  const hasLocal = typeof localAdmin.password === "string" && localAdmin.password.length > 0;
  delete domainJoin.password;
  delete localAdmin.password;
  if (Object.keys(domainJoin).length > 0) postConfig.domain_join = domainJoin;
  else delete postConfig.domain_join;
  if (Object.keys(localAdmin).length > 0) postConfig.local_admin = localAdmin;
  else delete postConfig.local_admin;
  return {
    id,
    name: body.name,
    image_id: body.image_id,
    post_config: postConfig,
    target_scope: body.target_scope ?? {},
    enabled: body.enabled ?? true,
    created_at: createdAt,
    has_domain_join_credential: hasDomain,
    has_local_admin_credential: hasLocal,
  };
}

const createProvisionProfile: FixtureMap["createProvisionProfile"] = (body) =>
  redacted(body, `prof-${Date.now()}`, ago(0));

const updateProvisionProfile: FixtureMap["updateProvisionProfile"] = (id, body) => {
  const existing = PROFILES.find((profile) => profile.id === id);
  if (!existing) throw notFound("profile");
  const next = redacted(body, id, existing.created_at);
  return {
    ...next,
    has_domain_join_credential: next.has_domain_join_credential || existing.has_domain_join_credential,
    has_local_admin_credential: next.has_local_admin_credential || existing.has_local_admin_credential,
  };
};

const startProvisionRun: FixtureMap["startProvisionRun"] = (profileId) => {
  if (!PROFILES.some((profile) => profile.id === profileId)) throw notFound("profile");
  return { run_id: `run-${Date.now()}`, state: "pending" };
};

const listProvisionRuns: FixtureMap["listProvisionRuns"] = () => runs();

const getProvisionRun: FixtureMap["getProvisionRun"] = (id) => {
  const found = runs().find((item) => item.id === id);
  if (!found) throw notFound("run");
  return found;
};

const getDeviceProvisioning: FixtureMap["getDeviceProvisioning"] = (deviceId) => {
  const mine = runs().filter((item) => item.device_id === deviceId);
  const latest = mine[0] ?? null;
  const profile = latest ? PROFILES.find((item) => item.id === latest.profile_id) : null;
  return {
    device_id: deviceId,
    profile_id: latest?.profile_id ?? null,
    image_id: profile?.image_id ?? null,
    enrolled_at: latest?.finished_at ?? null,
    latest_run_state: latest?.state ?? null,
  };
};

const getProvisioningReport: FixtureMap["getProvisioningReport"] = () => {
  const all = runs();
  const counts: Record<string, number> = { pending: 0, awaiting_enrollment: 0, enrolled: 0, complete: 0, failed: 0 };
  for (const item of all) counts[item.state] = (counts[item.state] ?? 0) + 1;
  const complete = counts.complete ?? 0;
  const failed = counts.failed ?? 0;
  const perProfile: Record<string, unknown> = {};
  for (const profile of PROFILES) {
    const mine = all.filter((item) => item.profile_id === profile.id);
    perProfile[profile.id] = {
      name: profile.name,
      runs: mine.length,
      complete: mine.filter((item) => item.state === "complete").length,
      failed: mine.filter((item) => item.state === "failed").length,
    };
  }
  const report: FleetProvisioningReport = {
    run_counts_by_state: counts,
    success_rate: complete + failed === 0 ? 0 : complete / (complete + failed),
    machines_provisioned: new Set(all.filter((item) => item.device_id).map((item) => item.device_id)).size,
    per_profile: perProfile,
  };
  return report;
};

export const fixtures: FixtureMap = {
  listOsImages,
  getOsImage,
  createOsImage,
  listProvisionProfiles,
  getProvisionProfile,
  createProvisionProfile,
  updateProvisionProfile,
  startProvisionRun,
  listProvisionRuns,
  getProvisionRun,
  getDeviceProvisioning,
  getProvisioningReport,
};

export default fixtures;
