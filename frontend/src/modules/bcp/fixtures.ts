/**
 * Fixtures for the BCP/DR module: the continuity plans, their review
 * standing, the tests recorded against them, the steps and findings of one
 * test, and the two evidence reports.
 *
 * TWO PLANS, THE ONES THE DESIGN DRAWS. `Database failover` has been
 * exercised (a passed restore drill, one finding still being worked) and
 * `File server restore` never has and has never been reviewed, so the list
 * shows both the ok and the warn rows v3 shows, the review-overdue tile has
 * a real row behind it, and the never-reviewed case `reviewState.ts` exists
 * for is on screen. `plan-1` and `test-1` are the ids the gate opens. The
 * scope names the shell's worked fleet (`fixtures/core.ts`) and the
 * procedure names an article in `docs/fixtures.ts`, so the detail page
 * resolves both to names rather than ids.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { ORG_ID } from "../../fixtures/core";
import { ApiError } from "../../lib/api";
import type {
  BcpEvidence,
  FindingOut,
  PlanOut,
  PlanStatusOut,
  StepOut,
  TestOut,
} from "./types";

const MINUTE = 60_000;
const HOUR = 60;
const DAY = 24 * HOUR;
/** An ISO timestamp `minutes` before the call. */
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();
/** An ISO timestamp `minutes` after the call. */
const ahead = (minutes: number): string => new Date(Date.now() + minutes * MINUTE).toISOString();

const notFound = (what: string) => new ApiError(404, JSON.stringify({ detail: `${what} not found` }));

/* ------------------------------------------------------------------- plans */

const plans = (): PlanOut[] => [
  {
    id: "plan-1",
    client_id: ORG_ID,
    name: "Database failover",
    description:
      "Promote the standby PostgreSQL replica and repoint the application tier when the primary in rack B4 is lost.",
    scope: [
      { kind: "device", id: "dev-6" },
      { kind: "device", id: "dev-5" },
    ],
    rto_minutes: 30,
    rpo_minutes: 15,
    procedure_refs: ["a-1"],
    dependencies: [{ kind: "service", name: "DNS" }],
    review_interval_days: 90,
    last_reviewed_at: ago(20 * DAY),
    approved_by: "usr-helgar",
    state: "active",
    created_by: "usr-ines",
    created_at: ago(200 * DAY),
  },
  {
    id: "plan-2",
    client_id: ORG_ID,
    name: "File server restore",
    description: "Rebuild por-fs-01 from the last verified backup onto spare hardware in the Porto rack.",
    scope: [{ kind: "device", id: "dev-8" }],
    rto_minutes: 240,
    rpo_minutes: 60,
    procedure_refs: [],
    dependencies: [],
    review_interval_days: 180,
    last_reviewed_at: null,
    approved_by: null,
    state: "active",
    created_by: "usr-rui",
    created_at: ago(91 * DAY),
  },
  {
    id: "plan-3",
    client_id: ORG_ID,
    name: "Porto office WAN failover",
    description: "Retired when the LTE path became automatic; kept for the tests recorded against it.",
    scope: [{ kind: "device", id: "dev-8" }],
    rto_minutes: 60,
    rpo_minutes: null,
    procedure_refs: ["a-6"],
    dependencies: [],
    review_interval_days: 365,
    last_reviewed_at: ago(400 * DAY),
    approved_by: "usr-helgar",
    state: "archived",
    created_by: "usr-rui",
    created_at: ago(500 * DAY),
  },
];

function findPlan(id: string): PlanOut {
  const found = plans().find((plan) => plan.id === id);
  if (!found) throw notFound("plan");
  return found;
}

const listPlans: FixtureMap["listPlans"] = () => plans();
const getPlan: FixtureMap["getPlan"] = (id) => findPlan(id);

const createPlan: FixtureMap["createPlan"] = (body) => ({
  id: `plan-${Date.now()}`,
  client_id: body.client_id,
  name: body.name,
  description: body.description ?? "",
  scope: body.scope ?? [],
  rto_minutes: body.rto_minutes ?? null,
  rpo_minutes: body.rpo_minutes ?? null,
  procedure_refs: body.procedure_refs ?? [],
  dependencies: body.dependencies ?? [],
  review_interval_days: body.review_interval_days ?? 90,
  last_reviewed_at: null,
  approved_by: null,
  state: "active",
  created_by: "usr-helgar",
  created_at: ago(0),
});

const updatePlan: FixtureMap["updatePlan"] = (id, body) => ({ ...findPlan(id), ...body });

const reviewPlan: FixtureMap["reviewPlan"] = (id) => ({
  ...findPlan(id),
  last_reviewed_at: ago(0),
  approved_by: "usr-helgar",
});

/* ------------------------------------------------------------------- tests */

const EVIDENCE: BcpEvidence = {
  captured_at: ago(26 * DAY + 2 * HOUR),
  devices: [
    {
      device_id: "dev-6",
      hostname: "lis-db-01",
      covered: true,
      last_backup_taken_at: ago(26 * DAY + 8 * HOUR),
      verified_at: ago(26 * DAY + 7 * HOUR),
      restore_tested_at: ago(26 * DAY + 3 * HOUR),
      restore_proven: true,
    },
    {
      device_id: "dev-5",
      hostname: "lis-app-02",
      covered: true,
      last_backup_taken_at: ago(26 * DAY + 9 * HOUR),
      verified_at: ago(26 * DAY + 7 * HOUR),
      restore_tested_at: ago(26 * DAY + 3 * HOUR),
      restore_proven: true,
    },
  ],
  summary: { total_n: 2, covered_n: 2, restore_proven_n: 2 },
};

const tests = (): TestOut[] => [
  {
    id: "test-1",
    plan_id: "plan-1",
    type: "restore_drill",
    scheduled_for: ago(26 * DAY + 3 * HOUR),
    executed_at: ago(26 * DAY + 2 * HOUR),
    executed_by: "usr-ines",
    outcome: "pass",
    rto_met: true,
    rpo_met: true,
    evidence: EVIDENCE,
    notes: "Replica promoted in 22 minutes. DNS TTL held the application tier back for a further 6 minutes.",
    created_at: ago(40 * DAY),
  },
  {
    id: "test-2",
    plan_id: "plan-1",
    type: "failover",
    scheduled_for: ahead(20 * DAY + 10 * HOUR),
    executed_at: null,
    executed_by: null,
    outcome: "pending",
    rto_met: null,
    rpo_met: null,
    evidence: {},
    notes: "",
    created_at: ago(3 * DAY),
  },
  {
    id: "test-3",
    plan_id: "plan-3",
    type: "tabletop",
    scheduled_for: ago(410 * DAY),
    executed_at: ago(410 * DAY),
    executed_by: "usr-rui",
    outcome: "partial",
    rto_met: false,
    rpo_met: null,
    evidence: {},
    notes: "The LTE path came up; the file server was unreachable over it for eleven minutes.",
    created_at: ago(420 * DAY),
  },
];

function findTest(id: string): TestOut {
  const found = tests().find((test) => test.id === id);
  if (!found) throw notFound("test");
  return found;
}

const listPlanTests: FixtureMap["listPlanTests"] = (planId) => {
  findPlan(planId);
  return tests().filter((test) => test.plan_id === planId);
};

const getTest: FixtureMap["getTest"] = (id) => findTest(id);

const createTest: FixtureMap["createTest"] = (planId, body) => ({
  id: `test-${Date.now()}`,
  plan_id: findPlan(planId).id,
  type: body.type,
  scheduled_for: body.scheduled_for ?? null,
  executed_at: null,
  executed_by: null,
  outcome: "pending",
  rto_met: null,
  rpo_met: null,
  evidence: {},
  notes: body.notes ?? "",
  created_at: ago(0),
});

const recordTest: FixtureMap["recordTest"] = (id, body) => ({
  ...findTest(id),
  executed_at: body.executed_at ?? ago(0),
  executed_by: "usr-helgar",
  outcome: body.outcome,
  rto_met: body.rto_met ?? null,
  rpo_met: body.rpo_met ?? null,
  notes: body.notes ?? "",
  evidence: { ...EVIDENCE, captured_at: ago(0) },
});

/* ------------------------------------------------------------------- steps */

const STEPS: Record<string, StepOut[]> = {
  "test-1": [
    { id: "step-1", test_id: "test-1", ordinal: 1, description: "Freeze application writes on lis-app-02", result: "pass", evidence_ref: "job-1" },
    { id: "step-2", test_id: "test-1", ordinal: 2, description: "Promote the standby on lis-db-01", result: "pass", evidence_ref: null },
    { id: "step-3", test_id: "test-1", ordinal: 3, description: "Repoint the application tier through DNS", result: "fail", evidence_ref: "tkt-1" },
    { id: "step-4", test_id: "test-1", ordinal: 4, description: "Run the smoke checks and hand back", result: "pass", evidence_ref: null },
  ],
};

const upsertStep: FixtureMap["upsertStep"] = (testId, body) => ({
  id: `step-${testId}-${body.ordinal}`,
  test_id: findTest(testId).id,
  ordinal: body.ordinal,
  description: body.description,
  result: body.result ?? "pending",
  evidence_ref: body.evidence_ref ?? null,
});

/* ---------------------------------------------------------------- findings */

const FINDINGS: Record<string, FindingOut[]> = {
  "test-1": [
    {
      id: "fnd-1",
      test_id: "test-1",
      description: "DNS TTL of 15 minutes delayed the application tier; lower it to 60 seconds ahead of a failover.",
      severity: "medium",
      remediation_state: "in_progress",
      change_ref: "chg-3",
      created_at: ago(26 * DAY),
      resolved_at: null,
    },
    {
      id: "fnd-2",
      test_id: "test-1",
      description: "The runbook named the old replica hostname.",
      severity: "low",
      remediation_state: "resolved",
      change_ref: null,
      created_at: ago(26 * DAY),
      resolved_at: ago(24 * DAY),
    },
  ],
  "test-3": [
    {
      id: "fnd-3",
      test_id: "test-3",
      description: "File server unreachable over the LTE path for eleven minutes.",
      severity: "high",
      remediation_state: "resolved",
      change_ref: null,
      created_at: ago(410 * DAY),
      resolved_at: ago(380 * DAY),
    },
  ],
};

const listFindings: FixtureMap["listFindings"] = (testId) => {
  findTest(testId);
  return FINDINGS[testId] ?? [];
};

const createFinding: FixtureMap["createFinding"] = (testId, body) => ({
  id: `fnd-${Date.now()}`,
  test_id: findTest(testId).id,
  description: body.description,
  severity: body.severity,
  remediation_state: "open",
  change_ref: null,
  created_at: ago(0),
  resolved_at: null,
});

const updateFinding: FixtureMap["updateFinding"] = (id, body) => {
  const found = Object.values(FINDINGS)
    .flat()
    .find((finding) => finding.id === id);
  if (!found) throw notFound("finding");
  return {
    ...found,
    ...body,
    change_ref: body.change_ref === undefined ? found.change_ref : body.change_ref,
    resolved_at: body.remediation_state === "resolved" ? ago(0) : found.resolved_at,
  };
};

/* ------------------------------------------------------------------ status */

const openFindings = (planId: string): number =>
  tests()
    .filter((test) => test.plan_id === planId)
    .flatMap((test) => FINDINGS[test.id] ?? [])
    .filter((finding) => finding.remediation_state !== "resolved").length;

const lastOutcome = (planId: string): string | null => {
  const executed = tests()
    .filter((test) => test.plan_id === planId && test.executed_at !== null)
    .sort((a, b) => (b.executed_at as string).localeCompare(a.executed_at as string));
  return executed[0]?.outcome ?? null;
};

function statusRows(): PlanStatusOut[] {
  return plans().map((plan): PlanStatusOut => {
    const proven = plan.id === "plan-1" ? EVIDENCE.summary.restore_proven_n : 0;
    const overdue =
      plan.last_reviewed_at === null ||
      Date.parse(plan.last_reviewed_at) + plan.review_interval_days * DAY * MINUTE < Date.now();
    return {
      id: plan.id,
      name: plan.name,
      state: plan.state,
      review_overdue: overdue,
      last_reviewed_at: plan.last_reviewed_at,
      last_test_outcome: lastOutcome(plan.id),
      open_findings: openFindings(plan.id),
      restore_proven_coverage: { total: plan.scope.length, restore_proven: proven },
    };
  });
}

const planStatus: FixtureMap["planStatus"] = () => statusRows();

/* ----------------------------------------------------------------- reports */

const testReport: FixtureMap["testReport"] = (testId) => {
  const test = findTest(testId);
  return {
    test,
    plan: findPlan(test.plan_id),
    steps: STEPS[testId] ?? [],
    findings: FINDINGS[testId] ?? [],
    evidence: test.evidence,
  };
};

const periodReport: FixtureMap["periodReport"] = (from, to) => {
  const inPeriod = tests().filter(
    (test) => test.executed_at !== null && test.executed_at >= from && test.executed_at <= to,
  );
  const outcome_counts: Record<string, number> = {};
  for (const test of inPeriod) outcome_counts[test.outcome] = (outcome_counts[test.outcome] ?? 0) + 1;
  const open_findings_by_severity: Record<string, number> = {};
  for (const test of inPeriod) {
    for (const finding of FINDINGS[test.id] ?? []) {
      if (finding.remediation_state === "resolved") continue;
      open_findings_by_severity[finding.severity] = (open_findings_by_severity[finding.severity] ?? 0) + 1;
    }
  }
  return {
    outcome_counts,
    plans_tested: [...new Set(inPeriod.map((test) => test.plan_id))],
    open_findings_by_severity,
    review_overdue_plans: statusRows()
      .filter((row) => row.review_overdue && row.state === "active")
      .map((row) => row.id),
  };
};

export const fixtures: FixtureMap = {
  listPlans,
  getPlan,
  createPlan,
  updatePlan,
  reviewPlan,
  planStatus,
  listPlanTests,
  getTest,
  createTest,
  recordTest,
  upsertStep,
  listFindings,
  createFinding,
  updateFinding,
  testReport,
  periodReport,
};

export default fixtures;
