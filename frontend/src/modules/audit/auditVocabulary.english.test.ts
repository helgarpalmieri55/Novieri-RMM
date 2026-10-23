/**
 * THE ENGLISH DID NOT MOVE.
 *
 * Before the audit vocabulary was catalogued, every code the console had no
 * entry for was shown as `humaniseEnum`'s derivation: "ipam.sweep_rejected"
 * read "IPAM sweep rejected". Cataloguing them gave the Spanish page words for
 * them, and the English page must read exactly as it always did. So each
 * English entry here IS that derivation -- except the thirteen the console
 * worded by hand before this table existed, listed explicitly below, which
 * keep the wording they always had, and the MCP revocations, worded by hand
 * when they were added (below).
 *
 * Without this test a typo in a 400-line English table is invisible: no
 * assertion names the code, and the page still renders something.
 */

import { describe, expect, it } from "vitest";

import { en } from "../../i18n/en";
import { es } from "../../i18n/es";
import { humaniseEnum } from "../../lib/labels";
import { auditActionLabel, auditActorTypeLabel, auditTargetTypeLabel } from "./labels";

/** The hand-worded originals (keys, as the catalogue spells them), with the English they have always had. */
const HAND_WORDED: Record<string, string> = {
  job_dispatch: "Job sent",
  job_retry: "Job retried",
  job_cancel: "Job cancelled",
  credential_checkout: "Credential checked out",
  credential_created: "Credential created",
  credential_use: "Credential used",
  remote_session_start: "Remote session started",
  remote_session_requested: "Remote session requested",
  remote_session_credential_used: "Credential used for a remote session",
  auth_signed_in: "Signed in",
  policy_change: "Policy changed",
  patch_approved: "Update approved",
  ai_action_proposed: "Assistant proposed an action",
  // New with the MCP server, never shown as the derivation on main. The page
  // names them as Settings -> AI connections does: what was revoked is an AI
  // app, and "MCP app" is the protocol's name for it, not the person's.
  mcp_app_revoked: "AI app revoked",
  mcp_app_revoke_incomplete: "AI app revoke incomplete",
};

const actions = en.enums.AuditAction as Record<string, string>;
const targets = en.enums.AuditTargetType as Record<string, string>;
const actors = en.enums.AuditActorType as Record<string, string>;

describe("the English audit vocabulary", () => {
  it("keeps the hand-worded actions exactly", () => {
    for (const [k, english] of Object.entries(HAND_WORDED)) {
      expect(actions[k], k).toBe(english);
    }
  });

  it("is the derivation for every other action", () => {
    const rest = Object.entries(actions).filter(([k]) => !(k in HAND_WORDED));
    expect(rest.length).toBeGreaterThan(300);
    for (const [k, english] of rest) {
      expect(english, `AuditAction.${k}`).toBe(humaniseEnum(k));
    }
  });

  it("is the derivation for every target kind and actor type", () => {
    expect(Object.keys(targets).length).toBeGreaterThan(90);
    for (const [k, english] of Object.entries(targets)) expect(english, `AuditTargetType.${k}`).toBe(humaniseEnum(k));
    for (const [k, english] of Object.entries(actors)) expect(english, `AuditActorType.${k}`).toBe(humaniseEnum(k));
  });

  it("reads through the page's label helpers as it always did", () => {
    expect(auditActionLabel("ipam.sweep_rejected")).toBe("IPAM sweep rejected");
    expect(auditActionLabel("jobs.dispatched")).toBe("Jobs dispatched");
    expect(auditActionLabel("agent.update.rollout_ring_advanced")).toBe("Agent update rollout ring advanced");
    expect(auditTargetTypeLabel("ipam_subnet")).toBe("IPAM subnet");
    expect(auditTargetTypeLabel("platform_error_group")).toBe("Platform error group");
    expect(auditActorTypeLabel("system")).toBe("System");
  });
});

describe("the Spanish audit vocabulary", () => {
  it("has exactly the English keys, each with a non-empty value", () => {
    for (const type of ["AuditAction", "AuditTargetType", "AuditActorType"] as const) {
      const english = en.enums[type] as Record<string, string>;
      const spanish = es.enums[type] as Record<string, string>;
      expect(Object.keys(spanish).sort()).toEqual(Object.keys(english).sort());
      for (const [k, value] of Object.entries(spanish)) expect(value.trim().length, `${type}.${k}`).toBeGreaterThan(2);
    }
  });
});
