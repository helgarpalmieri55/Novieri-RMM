/**
 * Fixtures for the agent-push module: push authorizations, credential scopes
 * and the probed host keys. NOTHING HERE CAN HOLD A SECRET, exactly as the wire types cannot:
 * a credential appears by id and name only.
 *
 * `listCredentials` (the bind form's picker) is the credentials module's
 * read and is answered by `modules/credentials/fixtures.ts`, which names the
 * push credentials the scopes below bind.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only.
 */

import type { FixtureMap } from "../../fixtures/client";
import { ORG_ID } from "../../fixtures/core";
import { ApiError } from "../../lib/api";
import type {
  HostKeyOut,
  PushAuthorizationOut,
  PushCredentialScopeOut,
  PushRunOut,
  PushRunPreviewOut,
  PushRunTargetOut,
} from "./types";

const DAY = 24 * 60 * 60_000;
/**
 * The fixture org's internal client: the org's own id (`fixtures/core.ts`'s
 * `me`), where every row here belongs until the client selector exists.
 */
const INTERNAL = ORG_ID;
const inDays = (days: number): string => new Date(Date.now() + days * DAY).toISOString();

const GRANTS: PushAuthorizationOut[] = [
  {
    id: "pa-1",
    client_id: INTERNAL,
    prefix: "10.20.4.0/24",
    address_count: 256,
    note: "Lisbon floor 3, via rack-b4-relay",
    authorized_by: "Helgar Palmieri",
    authorized_at: inDays(-12),
    expires_at: inDays(80),
    revoked_at: null,
    revoked_by: null,
  },
  {
    id: "pa-2",
    client_id: INTERNAL,
    prefix: "10.30.1.0/24",
    address_count: 256,
    note: "Porto office rollout",
    authorized_by: "Helgar Palmieri",
    authorized_at: inDays(-3),
    expires_at: null,
    revoked_at: null,
    revoked_by: null,
  },
  {
    id: "pa-3",
    client_id: INTERNAL,
    prefix: "10.20.9.0/24",
    address_count: 256,
    note: "Pilot, superseded",
    authorized_by: "Ana Ribeiro",
    authorized_at: inDays(-60),
    expires_at: inDays(-20),
    revoked_at: inDays(-25),
    revoked_by: "Helgar Palmieri",
  },
];

const SCOPES: PushCredentialScopeOut[] = [
  {
    id: "pcs-1",
    client_id: INTERNAL,
    credential_id: "cred-push-b4",
    credential_name: "Push install · rack B4",
    prefix: "10.20.4.0/24",
    address_count: 256,
    note: "Dedicated local admin, Lisbon",
    bound_by: "Helgar Palmieri",
    bound_at: inDays(-12),
    unbound_at: null,
    unbound_by: null,
  },
  {
    id: "pcs-2",
    client_id: INTERNAL,
    credential_id: "cred-push-porto",
    credential_name: "Push install · Porto",
    prefix: "10.30.1.0/24",
    address_count: 256,
    note: "",
    bound_by: "Helgar Palmieri",
    bound_at: inDays(-3),
    unbound_at: null,
    unbound_by: null,
  },
  {
    id: "pcs-3",
    client_id: INTERNAL,
    credential_id: "cred-push-pilot",
    credential_name: "Push install · pilot",
    prefix: "10.20.9.0/24",
    address_count: 256,
    note: "Pilot, superseded",
    bound_by: "Ana Ribeiro",
    bound_at: inDays(-60),
    unbound_at: inDays(-25),
    unbound_by: "Helgar Palmieri",
  },
];

const listPushAuthorizations: FixtureMap["listPushAuthorizations"] = (includeRevoked = false) =>
  GRANTS.filter((g) => includeRevoked || g.revoked_at === null);

const createPushAuthorization: FixtureMap["createPushAuthorization"] = (body) => ({
  id: `pa-${Date.now()}`,
  client_id: body.client_id,
  prefix: body.prefix,
  address_count: 256,
  note: body.note,
  authorized_by: "Helgar Palmieri",
  authorized_at: new Date().toISOString(),
  expires_at: body.expires_at,
  revoked_at: null,
  revoked_by: null,
});

const revokePushAuthorization: FixtureMap["revokePushAuthorization"] = () => undefined;

const listPushCredentialScopes: FixtureMap["listPushCredentialScopes"] = (includeUnbound = false) =>
  SCOPES.filter((s) => includeUnbound || s.unbound_at === null);

const bindPushCredentialScope: FixtureMap["bindPushCredentialScope"] = (body) => ({
  id: `pcs-${Date.now()}`,
  client_id: INTERNAL,
  credential_id: body.credential_id,
  credential_name: SCOPES.find((s) => s.credential_id === body.credential_id)?.credential_name ?? "Push install",
  prefix: body.prefix,
  address_count: 256,
  note: body.note,
  bound_by: "Helgar Palmieri",
  bound_at: new Date().toISOString(),
  unbound_at: null,
  unbound_by: null,
});

const unbindPushCredentialScope: FixtureMap["unbindPushCredentialScope"] = () => undefined;

/**
 * What the probes found: one Linux server's SSH host key, and two Windows
 * workstations probed over WinRM (design §2.4) -- one answering on HTTPS with a
 * domain controller reachable, one not answering at all -- so the readiness
 * panel draws both its done and its "the others need the Group Policy" copy.
 * Public material only; a Windows row has no host key.
 */
const HOST_KEYS: HostKeyOut[] = [
  {
    address: "10.20.4.12",
    client_id: INTERNAL,
    transport: "ssh",
    observed_fingerprint: "SHA256:q3Vd2mX0Jc8pZ1uYkT7rHs4nLw9aGf6eBi5oCxRtUvE",
    observed_key_type: "ssh-ed25519",
    observed_at: inDays(-2),
    confirmed_fingerprint: "SHA256:q3Vd2mX0Jc8pZ1uYkT7rHs4nLw9aGf6eBi5oCxRtUvE",
    confirmed_at: inDays(-2),
    confirmed_by: "Helgar Palmieri",
    installable: true,
    winrm_https: false,
    winrm_http: false,
    kdc_reachable: false,
  },
  {
    address: "10.20.4.31",
    client_id: INTERNAL,
    transport: "winrm",
    observed_fingerprint: "winrm",
    observed_key_type: "",
    observed_at: inDays(-1),
    confirmed_fingerprint: null,
    confirmed_at: null,
    confirmed_by: null,
    installable: false,
    winrm_https: true,
    winrm_http: false,
    kdc_reachable: true,
  },
  {
    address: "10.20.4.32",
    client_id: INTERNAL,
    transport: "winrm",
    observed_fingerprint: "winrm",
    observed_key_type: "",
    observed_at: inDays(-1),
    confirmed_fingerprint: null,
    confirmed_at: null,
    confirmed_by: null,
    installable: false,
    winrm_https: false,
    winrm_http: false,
    kdc_reachable: true,
  },
];

const listPushHostKeys: FixtureMap["listPushHostKeys"] = () => HOST_KEYS;

/** A probe or an install, accepted: one job per named address. Never a secret. */
const requestPush: FixtureMap["requestPush"] = (body) => ({
  batch_id: `b-${Date.now().toString(16)}`,
  kind: body.mode,
  targets: body.targets.map((address, at) => ({
    address,
    job_id: `job-push-${at + 1}`,
    site_server_device_id: "dev-5",
  })),
});

/** Confirming what the last probe observed, as the server requires. */
const confirmPushHostKey: FixtureMap["confirmPushHostKey"] = (body) => {
  const row = HOST_KEYS.find((k) => k.address === body.address && k.observed_fingerprint === body.fingerprint);
  if (!row) throw new ApiError(409, JSON.stringify({ detail: "that is not the fingerprint the last probe observed" }));
  return { ...row, confirmed_fingerprint: row.observed_fingerprint, installable: true, confirmed_by: "Helgar Palmieri" };
};

/** A run on the Lisbon floor, part-way through (design 2026-09-30 §1.7). */
const RUN: PushRunOut = {
  id: "run-1",
  client_id: INTERNAL,
  prefix: "10.20.4.0/24",
  credential_id: "cred-push-b4",
  credential_name: "Push install · rack B4",
  created_by: "u1",
  created_at: inDays(0),
  state: "running",
  target_count: 6,
  concurrency: 50,
  list_digest: "ab".repeat(32),
  finished_at: null,
  counts: { waiting: 1, in_flight: 2, installed: 1, failed: 1, refused: 1, cancelled: 0 },
  stopped_at: null,
  auth_failures: 1,
  auth_failure_limit: 3,
  signed_in: true,
};

const RUN_TARGETS: PushRunTargetOut[] = [
  { position: 0, device_id: "dev-1", address: "10.20.4.12", state: "installed", job_id: "job-1", outcome: "installed", detail: "", reasons: [], updated_at: inDays(0) },
  { position: 1, device_id: "dev-2", address: "10.20.4.13", state: "failed", job_id: "job-2", outcome: "auth_failed", detail: "Permission denied (publickey,password).", reasons: [], updated_at: inDays(0) },
  { position: 2, device_id: "dev-3", address: "10.20.4.14", state: "refused", job_id: null, outcome: "", detail: "", reasons: ["host_key_changed"], updated_at: inDays(0) },
  { position: 3, device_id: "dev-4", address: "10.20.4.15", state: "in_flight", job_id: "job-3", outcome: "", detail: "", reasons: [], updated_at: inDays(0) },
  { position: 4, device_id: "dev-5", address: "10.20.4.16", state: "in_flight", job_id: "job-4", outcome: "", detail: "", reasons: [], updated_at: inDays(0) },
  { position: 5, device_id: "dev-6", address: "10.20.4.17", state: "waiting", job_id: null, outcome: "", detail: "", reasons: [], updated_at: inDays(0) },
];

const PREVIEW: PushRunPreviewOut = {
  prefix: "10.20.4.0/24",
  client_id: INTERNAL,
  credential_id: "cred-push-b4",
  credential_name: "Push install · rack B4",
  list_digest: "cd".repeat(32),
  ready_count: 1,
  blocked_count: 2,
  unvouched_count: 4,
  awaiting_review_count: 1,
  ambiguous_count: 0,
  run_max: 1024,
  concurrency: 50,
  rows: [
    { device_id: "dev-1", address: "10.20.4.12", hostname: "lis-srv-12", os_family: "linux", ready: true, reasons: [] },
    { device_id: "dev-7", address: "10.20.4.31", hostname: "lis-ws-31", os_family: "windows", ready: false, reasons: ["wrong_credential_kind"] },
    { device_id: "dev-8", address: "10.20.4.40", hostname: "lis-srv-40", os_family: "linux", ready: false, reasons: ["host_key_unconfirmed"] },
  ],
  reason_help: {},
};

const listPushRunPrefixes: FixtureMap["listPushRunPrefixes"] = () =>
  GRANTS.filter((g) => g.revoked_at === null).map((g) => ({
    prefix: g.prefix,
    client_id: g.client_id,
    address_count: g.address_count,
    expires_at: g.expires_at,
  }));
const previewPushRun: FixtureMap["previewPushRun"] = (prefix) => ({ ...PREVIEW, prefix });
const createPushRun: FixtureMap["createPushRun"] = (body) => ({ ...RUN, prefix: body.prefix });
const listPushRuns: FixtureMap["listPushRuns"] = () => [
  RUN,
  { ...RUN, id: "run-0", prefix: "10.30.1.0/24", state: "completed", created_at: inDays(-3), finished_at: inDays(-3), counts: { waiting: 0, in_flight: 0, installed: 5, failed: 1, refused: 0, cancelled: 0 } },
];
const getPushRun: FixtureMap["getPushRun"] = (id) => ({ ...RUN, id });
const listPushRunTargets: FixtureMap["listPushRunTargets"] = (_id, { offset, limit }) => ({
  total: RUN_TARGETS.length,
  items: RUN_TARGETS.slice(offset, offset + limit),
  reason_help: {},
});
const pausePushRun: FixtureMap["pausePushRun"] = (id) => ({ ...RUN, id, state: "paused" });
const resumePushRun: FixtureMap["resumePushRun"] = (id) => ({ ...RUN, id, state: "running" });
const cancelPushRun: FixtureMap["cancelPushRun"] = (id) => ({ ...RUN, id, state: "cancelled" });

export const fixtures: FixtureMap = {
  listPushAuthorizations,
  createPushAuthorization,
  revokePushAuthorization,
  listPushCredentialScopes,
  bindPushCredentialScope,
  unbindPushCredentialScope,
  listPushHostKeys,
  requestPush,
  confirmPushHostKey,
  listPushRunPrefixes,
  previewPushRun,
  createPushRun,
  listPushRuns,
  getPushRun,
  listPushRunTargets,
  pausePushRun,
  resumePushRun,
  cancelPushRun,
};

export default fixtures;
