/**
 * THE AUDIT LOG'S VOCABULARY STAYS COMPLETE.
 *
 * The backend writes an action code and a target kind with every audit entry.
 * The page words each one from `enums.AuditAction` / `enums.AuditTargetType`
 * (`labels.ts`), and a code the catalogue does not hold is split into English
 * words instead -- "IPAM sweep rejected" in the middle of a Spanish page. That
 * is silent: nothing fails when a backend change adds a code. This test is
 * what makes it fail.
 *
 * HOW THE CODES ARE FOUND. By reading `backend/app/**.py` as text, the way
 * `enums.coverage.test.ts` reads the TypeScript unions. Four forms are read
 * directly:
 *   - `action="x.y"` keyword arguments (and `action = "x.y"` assignments);
 *   - `*_ACTION = "x.y"` module constants;
 *   - `target_type="..."` keyword arguments;
 *   - `*TARGET_TYPE = "..."` module constants.
 * Codes the source does not spell out in one place are DYNAMIC, and each is
 * enumerated in `DYNAMIC` below with the closed set it ranges over and the text
 * in the source that proves the set. See the notes on that table.
 *
 * A BACKEND TREE THAT IS NOT THERE FAILS. It does not skip: a skipped guard
 * is a guard that reports green on a checkout that cannot run it. The CI
 * frontend job checks out the whole repository (`actions/checkout@v4` at the
 * root), so `../backend` is always present.
 */

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { en } from "../../i18n/en";
import { es } from "../../i18n/es";

// Relative to THIS FILE (frontend/src/modules/audit -> the repository root; not written as
// `new URL(.., import.meta.url)`, which Vite rewrites as an asset), not to
// the directory vitest was started in. `ITMS_AUDIT_BACKEND_APP` points the guard
// at another copy of backend/app; it exists so the guard can be mutation-tested
// against a scratch copy without touching the repository (see the fix-round
// notes in the task report). Nothing else sets it.
const BACKEND_APP = process.env.ITMS_AUDIT_BACKEND_APP
  ? resolve(process.env.ITMS_AUDIT_BACKEND_APP)
  : resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..", "backend", "app");

const CODE = "[a-z0-9_]+(?:\\.[a-z0-9_]+)+";

function pythonFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "__pycache__" ? [] : pythonFiles(path);
    return name.endsWith(".py") ? [path] : [];
  });
}

const sources = new Map<string, string>();
if (existsSync(BACKEND_APP)) {
  for (const file of pythonFiles(BACKEND_APP)) {
    // Whole-line comments go: they quote codes and kinds as examples ("actor_type='ai'"
    // in a note saying it is never recorded) and are not recordings.
    sources.set(relative(BACKEND_APP, file), readFileSync(file, "utf8").replace(/^[ \t]*#.*$/gm, ""));
  }
}

/** What the source spells out, in one of the four forms above. */
function extractStatic(): { actions: Set<string>; targets: Set<string>; actors: Set<string> } {
  const actions = new Set<string>();
  const targets = new Set<string>();
  const actors = new Set<string>();
  for (const source of sources.values()) {
    for (const m of source.matchAll(new RegExp(`\\baction\\s*=\\s*["'](${CODE})["']`, "g"))) actions.add(m[1]!);
    for (const m of source.matchAll(
      new RegExp(`^[A-Z][A-Z0-9_]*ACTION[A-Z0-9_]*(?:\\s*:\\s*[\\w\\[\\]., ]+)?\\s*=\\s*["'](${CODE})["']`, "gm"),
    )) {
      actions.add(m[1]!);
    }
    for (const m of source.matchAll(/\btarget_type\s*=\s*["']([a-z0-9_]+)["']/g)) targets.add(m[1]!);
    for (const m of source.matchAll(/^_?[A-Z0-9_]*TARGET_TYPE(?:\s*:\s*[\w\[\]., ]+)?\s*=\s*["']([a-z0-9_]+)["']/gm)) {
      targets.add(m[1]!);
    }
    for (const m of source.matchAll(/\bactor_type\s*=\s*["']([a-z_]+)["']/g)) actors.add(m[1]!);
    for (const m of source.matchAll(/^ActorType\s*=\s*Literal\[([^\]]+)\]/gm)) {
      for (const a of m[1]!.matchAll(/["']([a-z_]+)["']/g)) actors.add(a[1]!);
    }
  }
  return { actions, targets, actors };
}

/**
 * THE DYNAMIC CODES. Each entry is one place where a recorded code is not a
 * literal beside its `action=`, with the closed set it can take.
 *
 *   - A WRAPPER (`_audit(session, ..., "x.y", ...)`): the code is the literal
 *     the CALLERS pass. Every such literal is listed in `codes` and must still
 *     appear quoted in `file`.
 *   - A TERNARY or a TABLE (`"a" if x else "b"`, `_DECISION_TO_AUDIT_ACTION`,
 *     the Keycloak event map): the members are listed and must appear quoted.
 *   - An F-STRING OVER A CLOSED SET (`f"jobs.{state}"`): `needles` holds the
 *     f-string, which must still be in the file, and `quoted` the members of
 *     the closed set, which must still appear quoted there (or in the file
 *     that fixes the set, when `setFile` says so).
 *
 * If a needle or a quoted member goes missing the test fails and says which
 * entry to re-read: the enumeration is a claim about the source, and this is
 * how it is held to it.
 *
 * AND THE OTHER WAY. A new member is caught too: every quoted `<prefix>.<tail>`
 * literal in an entry's file must be one of its codes, a statically read action
 * or a listed `notActions`; the same for target kinds (`notTargets`); and a
 * closed set of bare tails (`closed`) fails on a call passing a new one.
 * Mutation-checked: a new wrapper literal, a new wrapper target kind and a new
 * `_close(.., "expired")` / `_audit(.., "rollout_expired")` member each fail.
 *
 * WHAT STAYS UNGUARDED, exactly: (1) `jobs/engine.py`'s `state` is guarded only by
 * the exact text of its `Literal["succeeded", "failed"]` needle, so widening the
 * Literal fails but a caller cannot be traced; (2) a `closed` set is read from
 * the call SHAPE in its regex, so a member passed through a variable or a
 * differently shaped call is not seen (the test does fail if NO call matches);
 * (3) a wrapper called from a file other than its own (none today; every wrapper
 * here is private) would pass its literal unseen; (4) the tripwire below counts
 * sites, so one site swapped for another in the same file goes unnoticed.
 */
interface Dynamic {
  file: string;
  /** What builds the code, in a phrase. */
  how: string;
  codes?: string[];
  targets?: string[];
  /** Text that must still be in `file` (the f-string, the table name). */
  needles?: string[];
  /** Strings that must appear quoted in `setFile ?? file`. Defaults to `codes`. */
  quoted?: string[];
  setFile?: string;
  /**
   * Quoted literals shaped like one of this entry's codes (same first segment)
   * that are NOT audit actions: a permission code, a job type, a health-check
   * name. Each needs a reason in `how` or beside the entry.
   */
  notActions?: string[];
  /** The same for target kinds: `<first segment>_*` literals that are not audit target kinds. */
  notTargets?: string[];
  /**
   * A CLOSED SET whose members are bare tails (`"completed"`) that a prefix is
   * added to in an f-string. `calls` matches every place a member is passed
   * (capture group 1); each captured value must be in `members`, and at least
   * one call must match, so a new member (`_close(..., "expired")`) fails here.
   */
  closed?: { file: string; calls: string; members: string[] };
}

const DYNAMIC: Dynamic[] = [
  {
    file: "jobs/engine.py",
    how: 'f"jobs.{state}" over state: Literal["succeeded", "failed"]',
    needles: ['f"jobs.{state}"', 'state: Literal["succeeded", "failed"]'],
    codes: ["jobs.succeeded", "jobs.failed"],
    quoted: ["succeeded", "failed"],
  },
  {
    file: "modules/agent_update/service.py",
    how: 'f"agent.update.{action}" over the literals the rollout functions pass to _audit',
    needles: ['f"agent.update.{action}"'],
    // `agent.update` is the CAPABILITY name this module registers under, not an action.
    notActions: ["agent.update"],
    codes: [
      "agent.update.rollout_created",
      "agent.update.rollout_started",
      "agent.update.rollout_paused",
      "agent.update.rollout_resumed",
      "agent.update.rollout_ring_advanced",
      "agent.update.rollout_ring_closed",
      "agent.update.rollout_cancelled",
    ],
    quoted: [
      "rollout_created",
      "rollout_started",
      "rollout_paused",
      "rollout_resumed",
      "rollout_ring_advanced",
      "rollout_ring_closed",
      "rollout_cancelled",
    ],
    closed: {
      file: "modules/agent_update/service.py",
      calls: '_audit\\(\\s*session,\\s*rollout,\\s*"([a-z_]+)"',
      members: [
        "rollout_created",
        "rollout_started",
        "rollout_paused",
        "rollout_resumed",
        "rollout_ring_advanced",
        "rollout_ring_closed",
        "rollout_cancelled",
      ],
    },
  },
  {
    file: "modules/backup/offsite.py",
    how: "a conditional expression over two literals",
    codes: ["backup.offsite_enabled", "backup.offsite_disabled"],
    // `purpose=` strings handed to the credential-use audit, not actions of their own.
    notActions: ["backup.offsite_test", "backup.replication"],
  },
  {
    file: "modules/change_mgmt/service.py",
    how: "_DECISION_TO_AUDIT_ACTION table",
    needles: ["_DECISION_TO_AUDIT_ACTION"],
    codes: ["change.approved", "change.rejected"],
  },
  {
    file: "modules/service_desk/approvals.py",
    how: "_DECISION_TO_AUDIT_ACTION table",
    needles: ["_DECISION_TO_AUDIT_ACTION"],
    codes: ["ticket.approved", "ticket.rejected"],
    // A permission code quoted in the module docstring, and a domain-event type (notification bus).
    notActions: ["ticket.approve", "ticket.approval_decided"],
  },
  {
    file: "modules/keycloak_events/mapping.py",
    how: "map_event(): _DIRECT table and the passkey branch",
    needles: ["_DIRECT"],
    codes: ["auth.identity_linked", "auth.identity_unlinked", "auth.passkey_registered", "auth.passkey_removed"],
  },
  {
    file: "modules/patch/reboot.py",
    how: 'f"patch.reboot_{state}" over the states _close() is called with, plus the _audit literals',
    needles: ['f"patch.reboot_{state}"'],
    codes: [
      "patch.reboot_completed",
      "patch.reboot_cancelled",
      "patch.reboot_failed",
      "patch.reboot_deferred",
      "patch.reboot_dispatched",
      "patch.reboot_pending",
      "patch.reboot_scheduled",
      "patch.reboot_verify_dispatched",
    ],
    quoted: [
      "completed",
      "cancelled",
      "failed",
      "patch.reboot_deferred",
      "patch.reboot_dispatched",
      "patch.reboot_pending",
      "patch.reboot_scheduled",
      "patch.reboot_verify_dispatched",
    ],
    // `patch.apply` / `patch.scan` / `patch.reboot` are JOB TYPES this file dispatches.
    notActions: ["patch.apply", "patch.scan", "patch.reboot"],
    closed: {
      file: "modules/patch/reboot.py",
      calls: '_close\\(\\s*session,\\s*row,\\s*"([a-z_]+)"',
      members: ["completed", "cancelled", "failed"],
    },
  },
  {
    file: "modules/people/checklists.py",
    how: 'f"people.checklist_{outcome}" over the two outcomes people/api.py passes to _close()',
    needles: ['f"people.checklist_{outcome}"'],
    codes: ["people.checklist_completed", "people.checklist_cancelled"],
    quoted: ["completed", "cancelled"],
    setFile: "modules/people/api.py",
    // `people.manage` / `people.offboard` / `people.read` are PERMISSION codes.
    notActions: ["people.manage", "people.offboard", "people.read"],
    closed: {
      file: "modules/people/api.py",
      calls: '_close\\(\\s*request,\\s*ctx,\\s*person_id,\\s*checklist_id,\\s*"([a-z_]+)"',
      members: ["completed", "cancelled"],
    },
  },
  {
    file: "modules/people/service.py",
    how: "_audit() wrapper literals and a conditional expression",
    targets: ["device", "org", "person", "seat_assignment"],
    // Metadata keys that share a first segment with a target kind, not kinds.
    notTargets: ["person_id", "seat_id", "device_id"],
    codes: [
      "people.account_linked",
      "people.account_unlinked",
      "people.accounts_mirrored",
      "people.created",
      "people.device_person_automatic",
      "people.device_person_detected",
      "people.device_person_set",
      "people.login_added",
      "people.login_removed",
      "people.seat_linked",
      "people.updated",
    ],
  },
  {
    file: "modules/agent_push/runs.py",
    how: "_audit_control() wrapper literals",
    codes: ["push.run.cancelled", "push.run.paused", "push.run.resumed"],
  },
  {
    file: "modules/deploy/script_policies.py",
    how: "_audit() wrapper literals",
    codes: [
      "script_policy.alert_unrouted",
      "script_policy.created",
      "script_policy.deleted",
      "script_policy.dispatched",
      "script_policy.updated",
    ],
  },
  {
    file: "modules/documentation/documents.py",
    how: "_audit() wrapper literals",
    codes: [
      "docs.document_archived",
      "docs.document_created",
      "docs.document_downloaded",
      "docs.document_link_added",
      "docs.document_link_removed",
      "docs.document_updated",
      "docs.document_version_purged",
      "docs.document_version_uploaded",
    ],
  },
  {
    file: "modules/documentation/service.py",
    how: "_audit() wrapper literals",
    codes: [
      "docs.article_archived",
      "docs.article_created",
      "docs.article_portal_visibility_changed",
      "docs.article_updated",
      "docs.link_added",
      "docs.link_removed",
      "docs.record_archived",
      "docs.record_created",
      "docs.record_updated",
    ],
  },
  {
    file: "modules/health/approved_actions.py",
    how: "_apply_exemption_change() literals",
    codes: ["health.action.destructive_exempted", "health.action.destructive_exemption_revoked"],
  },
  {
    file: "modules/health/profiles.py",
    how: "_audit() wrapper literals",
    targets: ["health_profile", "health_profile_assignment"],
    codes: [
      "health.assignment_created",
      "health.assignment_deleted",
      "health.checks_replaced",
      "health.profile_cloned",
      "health.profile_created",
      "health.profile_deleted",
      "health.profile_updated",
    ],
  },
  {
    file: "modules/ipam/addresses.py",
    how: "_audit_state_change() literals",
    codes: ["ipam.address_released", "ipam.address_reserved", "ipam.address_state_changed"],
  },
  {
    file: "modules/ipam/importer.py",
    how: "a conditional expression over two literals",
    codes: ["ipam.import_previewed", "ipam.import_applied"],
  },
  {
    file: "modules/ipam/registry.py",
    how: "_audit() wrapper literals",
    targets: ["ipam_site", "ipam_vlan", "ipam_vrf"],
    codes: [
      "ipam.site_created",
      "ipam.site_deleted",
      "ipam.site_updated",
      "ipam.vlan_created",
      "ipam.vlan_deleted",
      "ipam.vlan_updated",
      "ipam.vrf_created",
      "ipam.vrf_deleted",
      "ipam.vrf_updated",
    ],
  },
  {
    file: "modules/ipam/subnets.py",
    how: "_audit() wrapper literals",
    targets: ["ipam_range", "ipam_subnet"],
    codes: [
      "ipam.range_created",
      "ipam.range_deleted",
      "ipam.subnet_created",
      "ipam.subnet_deleted",
      "ipam.subnet_updated",
      "ipam.sweep_settings_changed",
    ],
  },
  {
    file: "modules/remote/screen_relay.py",
    how: "_session_event() literals and a conditional expression",
    codes: [
      "remote.session.consent_granted",
      "remote.session.consent_prompted",
      "remote.session.consent_timeout",
      "remote.session.ended_by_user",
      "remote.session.privacy_blank",
      "remote.session.input_enabled",
      "remote.session.input_disabled",
    ],
  },
  {
    file: "modules/service_desk/canned.py",
    how: "_record() wrapper literals",
    codes: ["ticket.canned_response_created", "ticket.canned_response_deleted", "ticket.canned_response_updated"],
  },
  {
    file: "modules/user_admin/service.py",
    how: "a conditional expression and _mail() literals",
    codes: ["user.enabled", "user.disabled", "user.enrolment_resent", "user.password_reset_requested"],
  },
  {
    file: "services/clients.py",
    how: "_audit_site() wrapper literals",
    codes: ["client.site_archived", "client.site_created", "client.site_updated"],
  },
  {
    file: "services/install_link.py",
    how: "_audit() wrapper literals",
    codes: [
      "enrollment.link_created",
      "enrollment.link_disabled",
      "enrollment.link_enabled",
      "enrollment.link_regenerated",
      "enrollment.link_settings_changed",
    ],
  },
];

/**
 * THE TRIPWIRE. Every `action=` whose value is not a string literal, counted
 * per file, for the files that write audit entries. A count that differs from
 * this table is a site nobody has read yet: a new wrapper, a new f-string, a
 * new table. Read it, add its closed set to `DYNAMIC` above (or, if it is not
 * an audit action, leave it here), and update the count.
 *
 * Counted, not listed by line, so ordinary edits do not move it; a new
 * dynamic site always does. Most of these are not audit actions at all
 * (`action=row.action` in a response model, `action=action` passing a
 * parameter on): the table is what was READ, not what is recorded.
 */
const REVIEWED_NON_LITERAL_ACTIONS: Record<string, number> = {
  "core/ai/act.py": 11,
  "core/errors/triage.py": 2,
  "core/identity/__init__.py": 1,
  "core/rbac_admin.py": 2,
  "jobs/engine.py": 1,
  "modules/agent_distribution/msi.py": 1,
  "modules/agent_push/runs.py": 1,
  "modules/agent_update/service.py": 1,
  "modules/backup/offsite.py": 1,
  "modules/change_mgmt/api.py": 1,
  "modules/change_mgmt/service.py": 4,
  "modules/deploy/api.py": 3,
  "modules/deploy/dispatch.py": 2,
  "modules/deploy/script_policies.py": 1,
  "modules/documentation/documents.py": 1,
  "modules/documentation/service.py": 1,
  "modules/health/actions.py": 3,
  "modules/health/approved_actions.py": 7,
  "modules/health/profiles.py": 1,
  "modules/ipam/addresses.py": 2,
  "modules/ipam/candidates.py": 1,
  "modules/ipam/importer.py": 1,
  "modules/ipam/registry.py": 1,
  "modules/ipam/subnets.py": 1,
  "modules/keycloak_events/sweep.py": 3,
  "modules/patch/reboot.py": 1,
  "modules/people/checklists.py": 1,
  "modules/people/service.py": 1,
  "modules/remote/screen_relay.py": 3,
  "modules/remote/service.py": 1,
  "modules/service_desk/approvals.py": 1,
  "modules/service_desk/canned.py": 1,
  "modules/user_admin/service.py": 1,
  "services/device_review.py": 1,
  "services/clients.py": 1,
  "services/install_link.py": 1,
};

function nonLiteralActionCounts(): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const [file, source] of sources) {
    if (!/audit_service\.record|AuditEntry\(|audit\.record|\b_audit\w*\(/.test(source)) continue;
    const n = [...source.matchAll(/\baction\s*=(?!=)\s*(?=[^\s"'])/g)].length;
    if (n > 0) counts[file] = n;
  }
  return counts;
}

function extract(): { actions: Set<string>; targets: Set<string>; actors: Set<string> } {
  const found = extractStatic();
  for (const d of DYNAMIC) {
    for (const c of d.codes ?? []) found.actions.add(c);
    for (const t of d.targets ?? []) found.targets.add(t);
  }
  return found;
}

const key = (code: string) => code.replace(/\./g, "_");
const catalogue = (language: typeof en | typeof es, type: "AuditAction" | "AuditTargetType" | "AuditActorType") =>
  language.enums[type] as Record<string, string>;

describe("the backend tree the audit vocabulary is read from", () => {
  it("is present: a checkout without it fails, it does not skip", () => {
    expect(existsSync(BACKEND_APP), `${BACKEND_APP} is missing; this guard reads backend/app/**.py`).toBe(true);
    expect(sources.size).toBeGreaterThan(100);
  });

  it("yields a vocabulary big enough to be real (a broken extraction finds almost nothing)", () => {
    const { actions, targets, actors } = extractStatic();
    expect(actions.size).toBeGreaterThan(250);
    expect(targets.size).toBeGreaterThan(60);
    expect([...actors].sort()).toEqual(["agent", "system", "user"]);
    const all = extract();
    expect(all.actions.size).toBeGreaterThan(actions.size);
  });
});

describe("the dynamic codes still come from where the table says", () => {
  for (const d of DYNAMIC) {
    it(`${d.file}: ${d.how}`, () => {
      const source = sources.get(d.file);
      expect(source, `${d.file} is gone or moved; re-read the dynamic site and update DYNAMIC`).toBeDefined();
      for (const needle of d.needles ?? []) {
        expect(source!.includes(needle), `${d.file} no longer contains ${needle}`).toBe(true);
      }
      const setSource = d.setFile ? sources.get(d.setFile) : source;
      expect(setSource, `${d.setFile} is gone or moved`).toBeDefined();
      for (const q of d.quoted ?? [...(d.codes ?? []), ...(d.targets ?? [])]) {
        const quoted = setSource!.includes(`"${q}"`) || setSource!.includes(`'${q}'`);
        expect(quoted, `${d.setFile ?? d.file} no longer has "${q}"; the closed set changed`).toBe(true);
      }
    });
  }

  // THE OTHER DIRECTION. The checks above prove the listed codes are still
  // there; they cannot see a NEW literal passed to an existing wrapper
  // (`_audit(session, ..., "ipam.zone_created", ...)`). So every quoted literal
  // shaped like one of an entry's codes -- same first segment -- must be one of
  // that entry's codes or a statically read action. Anything else is either a
  // new audit action (list it here AND add its words) or a non-action that
  // shares the prefix (a permission, a job type: `notActions`).
  const staticActions = extractStatic().actions;
  for (const d of DYNAMIC.filter((entry) => (entry.codes ?? []).length > 0)) {
    it(`${d.file}: no unlisted ${[...new Set(d.codes!.map((c) => c.split(".")[0]))].join("/")}.* literal`, () => {
      const source = sources.get(d.file)!;
      const prefixes = [...new Set(d.codes!.map((c) => c.split(".")[0]!))];
      const shape = new RegExp(`["']((?:${prefixes.join("|")})\\.[a-z0-9_]+(?:\\.[a-z0-9_]+)*)["']`, "g");
      const known = new Set([...d.codes!, ...staticActions, ...(d.notActions ?? [])]);
      const unlisted = [...new Set([...source.matchAll(shape)].map((m) => m[1]!))].filter((lit) => !known.has(lit));
      expect(
        unlisted,
        `${d.file} has quoted ${prefixes.join("/")}.* literal(s) that DYNAMIC does not list. If they are audit ` +
          `actions, add them to this entry's codes (and give them words in both audit vocabulary files); ` +
          `if they are permissions or job types, add them to notActions with the reason.`,
      ).toEqual([]);
    });
  }

  // The same for target kinds a wrapper is handed (`_audit(..., "ipam_site", ...)`).
  const staticTargets = extractStatic().targets;
  for (const d of DYNAMIC.filter((entry) => (entry.targets ?? []).length > 0)) {
    it(`${d.file}: no unlisted target kind literal`, () => {
      const source = sources.get(d.file)!;
      const prefixes = [...new Set(d.targets!.map((t) => t.split("_")[0]!))];
      const shape = new RegExp(`["']((?:${prefixes.join("|")})_[a-z0-9_]+)["']`, "g");
      const known = new Set([...d.targets!, ...staticTargets, ...(d.notTargets ?? [])]);
      const unlisted = [...new Set([...source.matchAll(shape)].map((m) => m[1]!)) ].filter((lit) => !known.has(lit));
      expect(
        unlisted,
        `${d.file} has quoted ${prefixes.join("/")}_* literal(s) that DYNAMIC does not list. If they are audit target ` +
          `kinds, add them to this entry's targets (and give them words in both audit vocabulary files); otherwise ` +
          `add them to notTargets with the reason.`,
      ).toEqual([]);
    });
  }

  for (const d of DYNAMIC.filter((entry) => entry.closed)) {
    it(`${d.file}: the closed set ${d.closed!.members.join("/")} has no new member`, () => {
      const { file, calls, members } = d.closed!;
      const source = sources.get(file);
      expect(source, `${file} is gone or moved`).toBeDefined();
      const passed = [...source!.matchAll(new RegExp(calls, "g"))].map((m) => m[1]!);
      expect(passed.length, `no call matches ${calls} in ${file}; the call shape changed, re-read the site`).toBeGreaterThan(0);
      const added = [...new Set(passed)].filter((member) => !members.includes(member));
      expect(
        added,
        `${file} now passes ${added.join(", ")} where DYNAMIC lists ${members.join(", ")}. Each new member is a new ` +
          `audit action code (prefix + member): add the codes to this entry and words for both languages.`,
      ).toEqual([]);
    });
  }

  it("has no unreviewed dynamic action= site (a new wrapper, f-string or table)", () => {
    const found = nonLiteralActionCounts();
    const differences = [...new Set([...Object.keys(found), ...Object.keys(REVIEWED_NON_LITERAL_ACTIONS)])]
      .sort()
      .filter((file) => found[file] !== REVIEWED_NON_LITERAL_ACTIONS[file])
      .map((file) => `  ${file}: reviewed ${REVIEWED_NON_LITERAL_ACTIONS[file] ?? 0}, now ${found[file] ?? 0}`);
    expect(
      differences,
      [
        "An `action=` that is not a string literal appeared, disappeared or moved in an audit-writing file.",
        "Read the file(s) below. If the value becomes an audit action code (a wrapper parameter, an f-string,",
        "a mapping table), list its closed set in DYNAMIC above and give each code words in",
        "src/i18n/{en,es}/auditVocabulary.ts; then update REVIEWED_NON_LITERAL_ACTIONS.",
        "Not every count is an audit action: model fields such as `action=` in core/ai/act.py and",
        "health/approved_actions.py (an approved action's key) are counted ON PURPOSE, so that a",
        "reviewer has read them once; they need no vocabulary.",
      ].join("\n"),
    ).toEqual([]);
  });
});

describe("every audit action, target kind and actor type the backend records has words in both languages", () => {
  const { actions, targets, actors } = extract();

  it("actions", () => {
    for (const language of [en, es]) {
      const names = catalogue(language, "AuditAction");
      const missing = [...actions].filter((code) => names[key(code)] === undefined).sort();
      expect(missing, `${missing.length} action code(s) have no enums.AuditAction entry`).toEqual([]);
    }
  });

  it("target kinds", () => {
    for (const language of [en, es]) {
      const names = catalogue(language, "AuditTargetType");
      const missing = [...targets].filter((kind) => names[kind] === undefined).sort();
      expect(missing, `${missing.length} target kind(s) have no enums.AuditTargetType entry`).toEqual([]);
    }
  });

  it("actor types", () => {
    for (const language of [en, es]) {
      const names = catalogue(language, "AuditActorType");
      const missing = [...actors].filter((kind) => names[kind] === undefined).sort();
      expect(missing, "actor type(s) with no enums.AuditActorType entry").toEqual([]);
    }
  });

  it("no two codes share a catalogue key (a `.` and a `_` both read as `_`)", () => {
    const seen = new Map<string, string>();
    for (const code of actions) {
      const k = key(code);
      expect(seen.get(k), `${code} and ${seen.get(k)} share the key ${k}`).toBeUndefined();
      seen.set(k, code);
    }
  });
});
