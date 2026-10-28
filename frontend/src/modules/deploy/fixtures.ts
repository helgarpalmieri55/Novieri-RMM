/**
 * Fixtures for the deploy module: the package catalog, the deployments made
 * from it, the script library, and one device's deployment history.
 *
 * ONE CATALOG, EVERY ROW DERIVED FROM IT. A deployment names a package by id
 * and a target by id, so the deployments below point at the packages below
 * and at the shell's worked fleet (`fixtures/core.ts`): the deployments table
 * resolves "Google Chrome 128.0.6613 -> lis-ws-041" from the same two lists
 * the New deployment dialog offers, and the four tiles on `/deploy` are
 * counted from the same list the table shows. Timestamps are relative to
 * now, so "deployed this week" stays true on every run.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { ORG_ID } from "../../fixtures/core";
import { ApiError } from "../../lib/api";
import type { Deployment, DeviceDeploymentHistory, Package, Script, ScriptListItem } from "./types";

const MINUTE = 60_000;
const HOUR = 60;
const DAY = 24 * HOUR;
/** An ISO timestamp `minutes` before the call. */
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();

const notFound = (what: string) => new ApiError(404, JSON.stringify({ detail: `${what} not found` }));

/* ----------------------------------------------------------------- catalog */

interface PackageSeed {
  id: string;
  name: string;
  version: string;
  os: string;
  installer: string;
  sha: string | null;
  args: string | null;
  createdDays: number;
}

const PACKAGE_SEEDS: PackageSeed[] = [
  { id: "pkg-chrome-128", name: "Google Chrome", version: "128.0.6613", os: "windows", installer: "msi", sha: "9f3a4c1e7b2d8a6f0c5e3b1d9a7f2c4e6b8d0a2c4e6f8a0b2c4d6e8f0a1b3c5e", args: "/qn", createdDays: 5 },
  { id: "pkg-7zip-2408", name: "7-Zip", version: "24.08", os: "windows", installer: "msi", sha: "41b0c2d4e6f8a0b2c4d6e8f0a1b3c5d7e9f1a3b5c7d9e1f3a5b7c9d1e3f5a7d90", args: "/qn", createdDays: 24 },
  { id: "pkg-zoom-620", name: "Zoom Workplace", version: "6.2.0", os: "macos", installer: "pkg", sha: "c8e1f3a5b7d9e1f3a5b7c9d1e3f5a7b9c1d3e5f7a9b1c3d5e7f9a1b3c5d7e9f1a3", args: null, createdDays: 12 },
  { id: "pkg-agent-deb", name: "toeshee-agent", version: "1.8.2", os: "linux", installer: "deb", sha: null, args: null, createdDays: 1 },
];

function pkg(seed: PackageSeed): Package {
  return {
    id: seed.id,
    name: seed.name,
    version: seed.version,
    os_family: seed.os,
    installer_type: seed.installer,
    artifact_ref: seed.sha ? `sha256:${seed.sha}` : null,
    artifact_sha256: seed.sha,
    detection_rule: seed.sha ? { product: seed.name, min_version: seed.version } : {},
    uninstall_rule: {},
    install_args: seed.args,
    created_at: ago(seed.createdDays * DAY),
  };
}

const packages = (): Package[] => PACKAGE_SEEDS.map(pkg);

/* ------------------------------------------------------------- deployments */

interface DeploymentSeed {
  id: string;
  pkg: string;
  action: string;
  targetType: string;
  target: string;
  window: string | null;
  state: string;
  createdMinutes: number;
}

/**
 * Six deployments: one dispatching now, two scheduled inside a window, two
 * finished this week (one of them failed), and one from last month so the
 * "this week" tile and the total disagree by design.
 */
const DEPLOYMENT_SEEDS: DeploymentSeed[] = [
  { id: "dep-chrome-ws", pkg: "pkg-chrome-128", action: "install", targetType: "group", target: "grp-workstations", window: null, state: "dispatching", createdMinutes: 25 },
  { id: "dep-7zip-041", pkg: "pkg-7zip-2408", action: "update", targetType: "device", target: "dev-1", window: "0 2-4 * * 6", state: "pending", createdMinutes: 3 * HOUR },
  { id: "dep-zoom-017", pkg: "pkg-zoom-620", action: "install", targetType: "device", target: "dev-3", window: "0 22-23 * * 1-5", state: "pending", createdMinutes: DAY + 2 * HOUR },
  { id: "dep-chrome-042", pkg: "pkg-chrome-128", action: "install", targetType: "device", target: "dev-2", window: null, state: "complete", createdMinutes: 2 * DAY },
  { id: "dep-agent-build", pkg: "pkg-agent-deb", action: "install", targetType: "group", target: "grp-build", window: null, state: "failed", createdMinutes: 3 * DAY + 4 * HOUR },
  { id: "dep-7zip-008", pkg: "pkg-7zip-2408", action: "uninstall", targetType: "device", target: "dev-4", window: null, state: "complete", createdMinutes: 34 * DAY },
];

function deployment(seed: DeploymentSeed): Deployment {
  return {
    id: seed.id,
    // Every target of the worked fleet is the internal client's (`ORG_ID`,
    // the id convention), and a deployment is its target's client's.
    client_id: ORG_ID,
    package_id: seed.pkg,
    action: seed.action,
    target_type: seed.targetType,
    target_id: seed.target,
    window_cron: seed.window,
    requested_by: "usr-helgar",
    state: seed.state,
    created_at: ago(seed.createdMinutes),
  };
}

const deployments = (): Deployment[] => DEPLOYMENT_SEEDS.map(deployment);

/* ----------------------------------------------------------------- scripts */

interface ScriptSeed {
  id: string;
  name: string;
  os: string;
  interpreter: string;
  version: number;
  createdDays: number;
  body: string;
}

const SCRIPT_SEEDS: ScriptSeed[] = [
  { id: "scr-rotate-admin", name: "Rotate local admin password", os: "windows", interpreter: "powershell", version: 4, createdDays: 41, body: "$p = New-RandomPassword -Length 24\nSet-LocalUser -Name Administrator -Password (ConvertTo-SecureString $p -AsPlainText -Force)\n" },
  { id: "scr-spooler", name: "Clear print spooler", os: "windows", interpreter: "powershell", version: 2, createdDays: 18, body: "Stop-Service Spooler\nRemove-Item -Recurse -Force $env:SystemRoot\\System32\\spool\\PRINTERS\\*\nStart-Service Spooler\n" },
  { id: "scr-apt-clean", name: "Clean apt cache", os: "linux", interpreter: "bash", version: 1, createdDays: 7, body: "#!/usr/bin/env bash\nset -euo pipefail\napt-get clean\njournalctl --vacuum-time=14d\n" },
];

function scriptRow(seed: ScriptSeed): ScriptListItem {
  return {
    id: seed.id,
    name: seed.name,
    os_family: seed.os,
    interpreter: seed.interpreter,
    version: seed.version,
    created_by: "usr-helgar",
    created_at: ago(seed.createdDays * DAY),
  };
}

const scripts = (): ScriptListItem[] => SCRIPT_SEEDS.map(scriptRow);

/* ---------------------------------------------------------------- fixtures */

const listPackages: FixtureMap["listPackages"] = () => packages();

const createPackage: FixtureMap["createPackage"] = (body) => ({
  id: `pkg-${Date.now()}`,
  name: body.name,
  version: body.version,
  os_family: body.os_family,
  installer_type: body.installer_type,
  artifact_ref: null,
  artifact_sha256: null,
  detection_rule: body.detection_rule ?? {},
  uninstall_rule: body.uninstall_rule ?? {},
  install_args: body.install_args ?? null,
  created_at: ago(0),
});

const uploadPackageArtifact: FixtureMap["uploadPackageArtifact"] = (packageId) => {
  const found = packages().find((p) => p.id === packageId);
  if (!found) throw notFound("package");
  const sha = "d2f4a6b8c0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4c6d8e0f2a4";
  return { ...found, artifact_ref: `sha256:${sha}`, artifact_sha256: sha };
};

const deletePackage: FixtureMap["deletePackage"] = (id) => {
  if (!packages().some((p) => p.id === id)) throw notFound("package");
  return undefined;
};

const listDeployments: FixtureMap["listDeployments"] = () => deployments();

const createDeployment: FixtureMap["createDeployment"] = (body) => ({
  id: `dep-${Date.now()}`,
  // The server derives it from the target; the body never carries it.
  client_id: ORG_ID,
  package_id: body.package_id,
  action: body.action,
  target_type: body.target_type,
  target_id: body.target_id,
  window_cron: body.window_cron ?? null,
  requested_by: "usr-helgar",
  state: "pending",
  created_at: ago(0),
});

const executeDeployment: FixtureMap["executeDeployment"] = (id) => {
  const found = deployments().find((d) => d.id === id);
  if (!found) throw notFound("deployment");
  return { job_ids: found.target_type === "group" ? ["job-9a1", "job-9a2", "job-9a3"] : ["job-9a1"] };
};

/** One machine's history: the worked device `dev-1` has three entries; the rest, one or none. */
const getDeviceDeployments: FixtureMap["getDeviceDeployments"] = (deviceId) => {
  const rows: DeviceDeploymentHistory[] = deployments()
    .filter((d) => d.target_type === "device" && d.target_id === deviceId)
    .map((d) => ({
      deployment_id: d.id,
      client_id: d.client_id,
      package_id: d.package_id,
      action: d.action,
      state: d.state === "complete" ? "installed" : d.state === "failed" ? "failed" : "pending",
      exit_code: d.state === "complete" ? 0 : d.state === "failed" ? 1603 : null,
      log_ref: d.state === "complete" || d.state === "failed" ? `logs/${d.id}/${deviceId}.txt` : null,
      finished_at: d.state === "complete" || d.state === "failed" ? ago(DAY + 40) : null,
      created_at: d.created_at,
    }));
  if (deviceId === "dev-1") {
    rows.push({
      deployment_id: "dep-chrome-041-old",
      client_id: ORG_ID,
      package_id: "pkg-chrome-128",
      action: "install",
      state: "installed",
      exit_code: 0,
      log_ref: "logs/dep-chrome-041-old/dev-1.txt",
      finished_at: ago(6 * DAY),
      created_at: ago(6 * DAY + 30),
    });
  }
  return rows;
};

const listScripts: FixtureMap["listScripts"] = () => scripts();

const getScript: FixtureMap["getScript"] = (id) => {
  const seed = SCRIPT_SEEDS.find((s) => s.id === id);
  if (!seed) throw notFound("script");
  const detail: Script = { ...scriptRow(seed), body: seed.body };
  return detail;
};

const authorScript: FixtureMap["authorScript"] = (body) => ({
  id: `scr-${Date.now()}`,
  name: body.name,
  os_family: body.os_family,
  interpreter: body.interpreter,
  version: 1,
  created_by: "usr-helgar",
  created_at: ago(0),
  body: body.body,
});

const editScript: FixtureMap["editScript"] = (id, body) => {
  const seed = SCRIPT_SEEDS.find((s) => s.id === id);
  if (!seed) throw notFound("script");
  return { ...scriptRow(seed), version: seed.version + 1, body: body.body };
};

const runScript: FixtureMap["runScript"] = (id, body) => {
  if (!SCRIPT_SEEDS.some((s) => s.id === id)) throw notFound("script");
  return { job_ids: body.target_type === "group" ? ["job-7c1", "job-7c2"] : ["job-7c1"] };
};

const listScriptPolicies: FixtureMap["listScriptPolicies"] = () => [];
const listScriptPolicyRuns: FixtureMap["listScriptPolicyRuns"] = () => [];

export const fixtures: FixtureMap = {
  listPackages,
  createPackage,
  uploadPackageArtifact,
  deletePackage,
  listDeployments,
  createDeployment,
  executeDeployment,
  getDeviceDeployments,
  listScripts,
  getScript,
  authorScript,
  editScript,
  runScript,
  listScriptPolicies,
  listScriptPolicyRuns,
};

export default fixtures;
