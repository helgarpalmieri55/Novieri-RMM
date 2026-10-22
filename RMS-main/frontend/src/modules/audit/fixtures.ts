/**
 * Fixtures for the immutable audit: the events `/audit` lists and the chain
 * walk it asks for.
 *
 * THE CHAIN VERIFIES, AND SAYS HOW MANY. `verifyAuditChain` answers
 * `ok: true` with a count equal to the log's total, so the "events in
 * verified chain" tile and the "audit events" tile agree with each other,
 * which is what a real intact chain looks like. Actor and target ids are the
 * core fleet's and the sibling modules' ids, so an operator reading a row can
 * find the thing it names elsewhere in the console.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import type { AuditEventOut } from "./types";

const MINUTE = 60_000;
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();

interface Seed {
  id: string;
  /** Minutes ago. */
  at: number;
  actorType: string;
  actor: string | null;
  action: string;
  targetType: string;
  target: string;
  metadata: Record<string, unknown>;
}

const SEEDS: Seed[] = [
  { id: "aud-1", at: 3, actorType: "user", actor: "usr-helgar", action: "remote.session.start", targetType: "device", target: "dev-1", metadata: { session_id: "sess-1", protocol: "rdp", consent_mode: "attended", reason: "INC-4127 VPN drops" } },
  { id: "aud-2", at: 3, actorType: "user", actor: "usr-helgar", action: "credential.checkout", targetType: "credential", target: "cred-1", metadata: { reason: "INC-4127 VPN drops", session_id: "sess-1" } },
  { id: "aud-3", at: 6, actorType: "user", actor: "usr-helgar", action: "job.retry", targetType: "job", target: "job-9", metadata: { retry_of: "job-1", type: "patch.apply", device_id: "dev-6" } },
  { id: "aud-4", at: 14, actorType: "system", actor: null, action: "job.dispatch", targetType: "job", target: "job-3", metadata: { type: "patch.apply", device_id: "dev-3", policy: "pol-workstations-monthly" } },
  { id: "aud-5", at: 44, actorType: "user", actor: "usr-ines", action: "patch.approved", targetType: "patch", target: "KB5041585", metadata: { ring: "test", policy: "pol-workstations-monthly" } },
  { id: "aud-6", at: 58, actorType: "system", actor: null, action: "job.dispatch", targetType: "job", target: "job-6", metadata: { type: "inventory.collect", device_id: "dev-1" } },
  { id: "aud-7", at: 2 * 60 + 18, actorType: "user", actor: "usr-helgar", action: "auth.signed_in", targetType: "user", target: "usr-helgar", metadata: { method: "oidc", ip: "10.20.4.41" } },
  { id: "aud-8", at: 3 * 60 + 12, actorType: "system", actor: null, action: "job.dispatch", targetType: "job", target: "job-2", metadata: { type: "inventory.collect", device_id: "dev-8" } },
  { id: "aud-9", at: 4 * 60 + 12, actorType: "user", actor: "usr-helgar", action: "remote.session.start", targetType: "device", target: "dev-5", metadata: { session_id: "sess-3", protocol: "ssh", consent_mode: "unattended", reason: "CHG-118 kernel update" } },
  { id: "aud-10", at: 4 * 60 + 12, actorType: "user", actor: "usr-helgar", action: "credential.checkout", targetType: "credential", target: "cred-3", metadata: { reason: "CHG-118 kernel update", session_id: "sess-3" } },
  { id: "aud-11", at: 4 * 60 + 20, actorType: "system", actor: null, action: "job.dispatch", targetType: "job", target: "job-1", metadata: { type: "patch.apply", device_id: "dev-6", policy: "pol-servers-weekly" } },
  { id: "aud-12", at: 26 * 60, actorType: "user", actor: "usr-helgar", action: "job.cancel", targetType: "job", target: "job-10", metadata: { kind: "interrupt", type: "script.run" } },
  { id: "aud-13", at: 26 * 60 + 30, actorType: "user", actor: "usr-ines", action: "policy.change", targetType: "patch_policy", target: "pol-servers-weekly", metadata: { field: "maintenance_window", before: "Sun 02:00-04:00", after: "Sun 01:00-05:00" } },
  { id: "aud-14", at: 2 * 24 * 60 + 40, actorType: "user", actor: "usr-helgar", action: "credential.created", targetType: "credential", target: "cred-2", metadata: { type: "push_install", name: "Push install · rack B4" } },
  { id: "aud-15", at: 3 * 24 * 60, actorType: "assistant", actor: "ai-assistant", action: "ai.action.proposed", targetType: "ai_action", target: "act-1", metadata: { summary: "Approve KB5041585 into ring test", autonomy: "propose" } },
  { id: "aud-16", at: 6 * 24 * 60 + 5, actorType: "user", actor: "usr-helgar", action: "auth.signed_in", targetType: "user", target: "usr-helgar", metadata: { method: "oidc", ip: "10.20.4.41" } },
];

/** A stable 64-hex digest per row, so a hash reads the same on every run. */
function hashOf(id: string): string {
  let h = 0x811c9dc5;
  const out: string[] = [];
  for (let block = 0; block < 8; block += 1) {
    for (const ch of `${id}:${block}`) {
      h ^= ch.charCodeAt(0);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    out.push(h.toString(16).padStart(8, "0"));
  }
  return out.join("");
}

function event(seed: Seed): AuditEventOut {
  return {
    id: seed.id,
    ts: ago(seed.at),
    actor_type: seed.actorType,
    actor_id: seed.actor,
    action: seed.action,
    target_type: seed.targetType,
    target_id: seed.target,
    metadata: seed.metadata,
    hash: hashOf(seed.id),
  };
}

const events = (): AuditEventOut[] => SEEDS.map(event);

const fixtures: FixtureMap = {
  listAuditEvents: (params = {}) => {
    const action = typeof params.action === "string" ? params.action : "";
    const matched = events().filter((e) => !action || e.action === action);
    const offset = Number(params.offset ?? 0);
    const limit = Number(params.limit ?? 200);
    return { items: matched.slice(offset, offset + limit), total: matched.length };
  },

  verifyAuditChain: () => ({ ok: true, count: SEEDS.length, broken_at: null }),
};

export default fixtures;
