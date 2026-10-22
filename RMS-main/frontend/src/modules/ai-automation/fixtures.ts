/**
 * Fixtures for act mode: the prepared actions, one action's preview, the two
 * decisions a person can make on it, and the autonomy ceilings.
 *
 * ONE STORY, TOLD BY BOTH ASSISTANT MODULES. The action waiting for approval
 * here is the one the transcript in `modules/ai-admin/fixtures.ts` says the
 * assistant prepared ("approve KB5041585 into ring test"), so `/assistant`,
 * `/assistant/autonomy`, `/assistant/actions` and the action's own page agree
 * about what happened. Its target is a device from the fleet in
 * `fixtures/core.ts`, so the hostname on the confirmation screen is one the
 * device list also shows.
 *
 * THE RATIONALE IS WRITTEN AS THE MODEL WOULD WRITE IT and carries no
 * instruction: the point of the fixture is to show the two regions apart,
 * not to smuggle a laundered command into a screenshot.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { devices } from "../../fixtures/core";
import { ApiError } from "../../lib/api";
import type { AiActionOut, AiActionPreviewOut, AiCeilingOut } from "./types";

const MINUTE = 60_000;
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();
const inMinutes = (minutes: number): string => new Date(Date.now() + minutes * MINUTE).toISOString();

const notFound = () => new ApiError(404, JSON.stringify({ detail: "action not found" }));
const conflict = (detail: string) => new ApiError(409, JSON.stringify({ detail }));
const gone = () => new ApiError(410, JSON.stringify({ detail: "preview expired" }));

/** A device from the shared fleet, by position, so the target is a real row on `/devices`. */
function target(at: number): { id: string; hostname: string } {
  const fleet = devices();
  const device = fleet[at % fleet.length] ?? fleet[0];
  return { id: device?.id ?? "dev-1", hostname: device?.hostname ?? "unknown-host" };
}

const DIGEST = (seed: string): string =>
  Array.from({ length: 64 }, (_, i) => "0123456789abcdef"[(seed.charCodeAt(i % seed.length) + i) % 16]).join("");

/** Everything an action carries, in both the list and the preview shape. */
interface ActionSeed {
  action: AiActionOut;
  preview: Omit<AiActionPreviewOut, "ai_action_id" | "approved_action_key" | "job_type" | "target_device_id" | "risk_class" | "destructive" | "effective_autonomy" | "tainted" | "expires_at" | "state">;
}

const kb = target(2);
const runner = target(3);
const laptop = target(0);

const SEEDS: ActionSeed[] = [
  {
    action: {
      id: "act-1",
      session_id: "ai-sess-2",
      persona: "technician",
      approved_action_key: "patch.approve.ring",
      job_type: "patch.approve",
      target_device_id: kb.id,
      risk_class: "medium",
      destructive: false,
      effective_autonomy: "require_approval",
      tainted: false,
      state: "previewed",
      expires_at: inMinutes(25),
      confirmed_by: null,
      rejected_reason: null,
      job_id: null,
      created_at: ago(60 * 24),
    },
    preview: {
      approved_action_id: "aa-patch-approve-ring",
      name: "Approve KB5041585 into ring test",
      description: "Approve one Windows cumulative update into the test ring, so the next maintenance window installs it on the ring's members.",
      rendered_params: { kb: "KB5041585", ring: "test", devices: 12 },
      redacted_param_paths: [],
      script_name: null,
      script_version: null,
      target_hostname: kb.hostname,
      fuse_verdict: null,
      preview_digest: DIGEST("act-1"),
      rationale:
        "Windows compliance in the test ring is 84% and KB5041585 is the single largest gap: it is missing on 12 of 14 devices. Approving it into test is the ring policy's next step. I cannot approve into broad; that ring needs a person at every step.",
    },
  },
  {
    action: {
      id: "act-2",
      session_id: "ai-sess-3",
      persona: "admin",
      approved_action_key: "health.alert.acknowledge",
      job_type: "health.alert.ack",
      target_device_id: runner.id,
      risk_class: "low",
      destructive: false,
      effective_autonomy: "require_approval",
      tainted: false,
      state: "dispatched",
      expires_at: ago(60 * 24 * 3 - 30),
      confirmed_by: "Mateus Ribeiro",
      rejected_reason: null,
      job_id: "job-1",
      created_at: ago(60 * 24 * 3),
    },
    preview: {
      approved_action_id: "aa-alert-ack",
      name: "Acknowledge a health alert",
      description: "Mark one open alert as acknowledged by a person, which pauses its escalation.",
      rendered_params: { alert: "cpu.sustained", device: runner.hostname },
      redacted_param_paths: [],
      script_name: null,
      script_version: null,
      target_hostname: runner.hostname,
      fuse_verdict: null,
      preview_digest: DIGEST("act-2"),
      rationale: "The CPU alert has been open for 40 minutes while the build queue drains; acknowledging it stops the escalation without changing anything on the device.",
    },
  },
  {
    action: {
      id: "act-3",
      session_id: "ai-sess-1",
      persona: "technician",
      approved_action_key: "disk.cleanup.workstation",
      job_type: "deploy.script",
      target_device_id: laptop.id,
      risk_class: "medium",
      destructive: true,
      effective_autonomy: "require_approval",
      tainted: true,
      state: "rejected",
      expires_at: ago(60 * 24 * 6),
      confirmed_by: null,
      rejected_reason: "wrong_device",
      job_id: null,
      created_at: ago(60 * 24 * 6 + 20),
    },
    preview: {
      approved_action_id: "aa-disk-cleanup",
      name: "Clear temporary files on a workstation",
      description: "Run the catalogue's disk-cleanup script, which removes browser caches and the Windows update download cache.",
      rendered_params: { profile: "workstation-standard", repo_key: "[redacted: stored credential]" },
      redacted_param_paths: ["repo_key"],
      script_name: "disk-cleanup",
      script_version: 4,
      target_hostname: laptop.hostname,
      fuse_verdict: null,
      preview_digest: DIGEST("act-3"),
      rationale: "The ticket says the machine is out of space. Clearing the caches usually frees a few gigabytes.",
    },
  },
];

const actions = new Map<string, ActionSeed>(SEEDS.map((seed) => [seed.action.id, seed]));

const bySeed = (id: string): ActionSeed => {
  const found = actions.get(id);
  if (!found) throw notFound();
  return found;
};

const previewOf = (seed: ActionSeed): AiActionPreviewOut => ({
  ai_action_id: seed.action.id,
  approved_action_key: seed.action.approved_action_key,
  job_type: seed.action.job_type,
  target_device_id: seed.action.target_device_id,
  risk_class: seed.action.risk_class,
  destructive: seed.action.destructive,
  effective_autonomy: seed.action.effective_autonomy,
  tainted: seed.action.tainted,
  expires_at: seed.action.expires_at,
  state: seed.action.state,
  ...seed.preview,
});

const listAiActions: FixtureMap["listAiActions"] = (sessionId, limit = 50) =>
  [...actions.values()]
    .map((seed) => seed.action)
    .filter((action) => sessionId === undefined || action.session_id === sessionId)
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .slice(0, limit);

const getAiAction: FixtureMap["getAiAction"] = (id) => previewOf(bySeed(id));

let dispatched = 0;
const confirmAiAction: FixtureMap["confirmAiAction"] = (id, body) => {
  const seed = bySeed(id);
  if (Date.parse(seed.action.expires_at) < Date.now()) throw gone();
  if (seed.action.state !== "previewed") throw conflict(`action is already ${seed.action.state}`);
  if (body.digest !== seed.preview.preview_digest) throw conflict("the preview changed; look again");
  dispatched += 1;
  const jobId = `job-ai-${dispatched}`;
  seed.action = { ...seed.action, state: "dispatched", job_id: jobId, confirmed_by: "Helgar Palmieri" };
  return { ai_action_id: id, job_id: jobId, state: "dispatched" };
};

const rejectAiAction: FixtureMap["rejectAiAction"] = (id, body) => {
  const seed = bySeed(id);
  if (seed.action.state !== "previewed") throw conflict(`action is already ${seed.action.state}`);
  seed.action = { ...seed.action, state: "rejected", rejected_reason: body.reason };
  return seed.action;
};

/* ---------------------------------------------------------------- ceilings */

const ceilings = new Map<string, AiCeilingOut>(
  (
    [
      ["technician", "low", "suggest"],
      ["technician", "medium", "require_approval"],
      ["admin", "low", "require_approval"],
      ["admin", "medium", "require_approval"],
      ["admin", "high", "suggest"],
    ] as const
  ).map(([persona, risk_class, level]) => [
    `${persona}:${risk_class}`,
    { persona, risk_class, level, updated_by: "Helgar Palmieri", updated_at: ago(60 * 24 * 12) },
  ]),
);

const listAiCeilings: FixtureMap["listAiCeilings"] = () => [...ceilings.values()];

const setAiCeiling: FixtureMap["setAiCeiling"] = (body) => {
  const row: AiCeilingOut = {
    persona: body.persona,
    risk_class: body.risk_class,
    level: body.level,
    updated_by: "Helgar Palmieri",
    updated_at: ago(0),
  };
  ceilings.set(`${body.persona}:${body.risk_class}`, row);
  return row;
};

const fixtures: FixtureMap = {
  listAiActions,
  getAiAction,
  confirmAiAction,
  rejectAiAction,
  listAiCeilings,
  setAiCeiling,
};

export default fixtures;
