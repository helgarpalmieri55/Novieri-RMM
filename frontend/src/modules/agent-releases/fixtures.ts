/**
 * Fixtures for the agent-releases module: the published catalogue, the
 * one-machine install command and the managed-deployment package.
 *
 * NO SECRET IS REAL HERE. The token strings are fixture literals; they exist
 * so the reveal surfaces can be reviewed, not so anything can enrol.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only.
 */

import type { FixtureMap } from "../../fixtures/client";
import type { AgentRelease, InstallLinkSettings } from "../../lib/api";
import { ApiError } from "../../lib/api";

const HOUR = 60 * 60_000;
const at = (hours: number): string => new Date(Date.now() + hours * HOUR).toISOString();

const SHA = "8b3421e0c7a1f9d2e4b6a8c0d2f4e6a8b0c2d4e6f8a0b2c4d6e8f0a2b4c6d8e0";

/**
 * MUTABLE, so the publish dialog and "Mark as stable" can be walked in fixture
 * mode: a publish appends here and the table refetches it. The proxy clones
 * every result, so no page ever holds a reference into this array.
 */
const RELEASES: AgentRelease[] = [
  { id: "rel-1", version: "1.8.2", os: "windows", arch: "amd64", size_bytes: 18_874_368, sha256: SHA, signed: true, notes: "", published_at: at(-5 * 24), has_artifact: true, channel: "stable", release_signed: true },
  { id: "rel-2", version: "1.8.2", os: "linux", arch: "amd64", size_bytes: 16_777_216, sha256: SHA, signed: true, notes: "", published_at: at(-5 * 24), has_artifact: true, channel: "stable", release_signed: true },
  { id: "rel-3", version: "1.8.2", os: "darwin", arch: "arm64", size_bytes: 17_825_792, sha256: SHA, signed: true, notes: "", published_at: at(-5 * 24), has_artifact: true, channel: "stable", release_signed: true },
  { id: "rel-4", version: "1.8.1", os: "linux", arch: "amd64", size_bytes: 16_252_928, sha256: SHA, signed: true, notes: "", published_at: at(-40 * 24), has_artifact: true, channel: "stable", release_signed: false },
  { id: "rel-5", version: "1.9.0-rc1", os: "linux", arch: "arm64", size_bytes: 0, sha256: "", signed: false, notes: "", published_at: null, has_artifact: false, channel: "pilot", release_signed: false },
];

const find = (id: string): AgentRelease => {
  const found = RELEASES.find((r) => r.id === id);
  if (!found) throw new ApiError(404, JSON.stringify({ detail: "release not found" }));
  return found;
};

const listAgentReleases: FixtureMap["listAgentReleases"] = () => RELEASES;

const conflict = () =>
  new ApiError(409, JSON.stringify({ detail: "a release already exists for this version, OS and architecture" }));

const createAgentRelease: FixtureMap["createAgentRelease"] = (body) => {
  if (RELEASES.some((r) => r.version === body.version && r.os === body.os && r.arch === body.arch)) {
    throw conflict();
  }
  const created: AgentRelease = {
    id: `rel-${Date.now()}-${body.os}-${body.arch}`,
    version: body.version,
    os: body.os,
    arch: body.arch,
    size_bytes: 0,
    sha256: "",
    signed: body.signed,
    notes: body.notes,
    published_at: null,
    has_artifact: false,
    channel: "pilot",
    release_signed: false,
  };
  RELEASES.push(created);
  return created;
};

/** The REAL digest of what was "uploaded", so the dialog's comparison with a
 *  SIGNATURES file behaves in fixture mode as it does against the server. */
const uploadAgentReleaseArtifact: FixtureMap["uploadAgentReleaseArtifact"] = async (id, file) => {
  const release = find(id);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", await file.arrayBuffer()));
  release.sha256 = [...digest].map((b) => b.toString(16).padStart(2, "0")).join("");
  release.size_bytes = file.size;
  release.has_artifact = true;
  release.published_at = at(0);
  return release;
};

const setAgentReleaseSignature: FixtureMap["setAgentReleaseSignature"] = (id) => {
  find(id).release_signed = true;
};

const deleteAgentRelease: FixtureMap["deleteAgentRelease"] = (id) => {
  const index = RELEASES.findIndex((r) => r.id === id);
  if (index < 0) throw new ApiError(404, JSON.stringify({ detail: "no such agent release" }));
  RELEASES.splice(index, 1);
};

const promoteAgentReleaseVersion: FixtureMap["promoteAgentReleaseVersion"] = (version, channel) => {
  const rows = RELEASES.filter((r) => r.version === version);
  if (rows.length === 0) {
    throw new ApiError(404, JSON.stringify({ detail: `no release rows for version '${version}'` }));
  }
  for (const row of rows) row.channel = channel;
  return { version, channel, rows: rows.length };
};

const generateAgentInstallCommand: FixtureMap["generateAgentInstallCommand"] = (id, hours) => {
  const release = find(id);
  const token = "ENR-fixture-single-use-3nHc6dJf0gA5Qm2xVb9L";
  return {
    release,
    token,
    token_expires_at: at(hours),
    download_url: `https://console.toeshee.example/artifacts/its-agent-${release.os}-${release.arch}`,
    download_expires_in_s: 900,
    sha256: release.sha256,
    command: `curl -fsSL https://console.toeshee.example/artifacts/its-agent-${release.os}-${release.arch} -o its-agent && echo "${release.sha256}  its-agent" | sha256sum -c && sudo ./its-agent enroll --server console.toeshee.example --token ${token}`,
  };
};

const getAgentDeploymentPackage: FixtureMap["getAgentDeploymentPackage"] = (id, channel) => {
  const release = find(id);
  return {
    release,
    channel,
    download_url: `https://console.toeshee.example/artifacts/its-agent-${release.os}-${release.arch}`,
    download_expires_in_s: 900,
    sha256: release.sha256,
    staging: [
      { title: "Download the artefact", detail: "Fetch the file once, on the administrator's machine.", command: `curl -fsSL -o its-agent https://console.toeshee.example/artifacts/its-agent-${release.os}-${release.arch}` },
      { title: "Verify the digest", detail: "Compare it against the value shown here before it reaches a share.", command: `echo "${release.sha256}  its-agent" | sha256sum -c` },
      { title: "Stage it on the distribution point", detail: channel === "intune" ? "Wrap it as a Win32 app package." : "Copy it to the SYSVOL share the startup script reads.", command: "" },
    ],
    install_command: "its-agent install --server console.toeshee.example",
    enrol_command: "its-agent enroll --server console.toeshee.example --token-from-env ITS_ENROLL_TOKEN",
    integrity: {
      sha256: release.sha256,
      verify_command: `echo "${release.sha256}  its-agent" | sha256sum -c`,
      verified_at_install: false,
      notes: [
        "The distribution point copies the file and runs it; it does not check a digest.",
        "Verify on the administrator's machine, before staging.",
      ],
    },
  };
};

const mintAgentDeploymentToken: FixtureMap["mintAgentDeploymentToken"] = (_id, body) => ({
  token: "ENR-fixture-managed-9Lk4pR8sT1wYz3nHc6dJf0gA5Qm2x",
  token_id: `tok-${Date.now()}`,
  token_expires_at: at(body.expires_in_hours),
  max_uses: body.max_uses,
  channel: body.channel,
  blast_radius: [
    `Any of up to ${body.max_uses} machines that read the distribution point can enrol with it until it expires.`,
    body.channel === "gpo"
      ? "Every machine account in the domain can read SYSVOL."
      : "Every enrolled Intune device assigned the app can read the package.",
  ],
});

/**
 * The install link. MUTABLE, so switching it off and on, and regenerating, can
 * be walked in fixture mode. The code below is 32 characters of the real
 * alphabet and enrols nothing anywhere.
 */
const FIXTURE_LINK = "https://console.toeshee.example/i/FIXTURExNOTxAxREALxCODEx00000000";
let LINK: InstallLinkSettings = {
  enabled: true,
  has_code: true,
  networks: ["10.20.0.0/16", "10.30.0.0/16"],
  disable_at: at(5 * 24),
  hourly_limit: 50,
  enabled_at: at(-26),
  joined_since_enabled: 4,
};

const getInstallLink: FixtureMap["getInstallLink"] = () => LINK;

// One link per site (MSP clients spec §1.5); fixture mode keeps the one the
// console manages, the internal client's "Principal" site's.
const saveInstallLink: FixtureMap["saveInstallLink"] = (_siteId, body) => {
  if (body.enabled && body.networks.length === 0) {
    throw new ApiError(422, JSON.stringify({
      detail: "at least one network is required to switch the link on; an empty list would not mean 'anywhere'",
    }));
  }
  const minted = body.enabled && !LINK.has_code;
  LINK = {
    ...LINK,
    ...body,
    has_code: LINK.has_code || body.enabled,
    enabled_at: body.enabled ? (LINK.enabled ? LINK.enabled_at : at(0)) : null,
    joined_since_enabled: body.enabled && LINK.enabled ? LINK.joined_since_enabled : 0,
  };
  return { ...LINK, url: minted ? FIXTURE_LINK : null };
};

const regenerateInstallLink: FixtureMap["regenerateInstallLink"] = () => {
  LINK = { ...LINK, has_code: true };
  return { ...LINK, url: FIXTURE_LINK };
};

/**
 * The Group Policy MSI. Refuses a code that is not the fixture link's, as the
 * server refuses one that is not the current link's; the bytes are a
 * placeholder and install nothing.
 */
const downloadInstallLinkMsi: FixtureMap["downloadInstallLinkMsi"] = (_siteId, body) => {
  if (!FIXTURE_LINK.endsWith(`/i/${body.code}`)) {
    throw new ApiError(409, JSON.stringify({
      detail: "That is not this organisation's current install link. Paste the link shown when it was last generated, or generate a new one.",
    }));
  }
  return {
    blob: new Blob(["fixture MSI"], { type: "application/x-msi" }),
    filename: `ITMS-Agent-console.toeshee.example-${body.arch}.msi`,
    sha256: "9c1e5b2f4a7d8e3c6b0a1f2e9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e1d0c",
  };
};

export const fixtures: FixtureMap = {
  downloadInstallLinkMsi,
  listAgentReleases,
  createAgentRelease,
  uploadAgentReleaseArtifact,
  setAgentReleaseSignature,
  deleteAgentRelease,
  promoteAgentReleaseVersion,
  generateAgentInstallCommand,
  getAgentDeploymentPackage,
  mintAgentDeploymentToken,
  getInstallLink,
  saveInstallLink,
  regenerateInstallLink,
};

export default fixtures;
