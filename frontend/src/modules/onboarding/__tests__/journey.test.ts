/**
 * The journey's state machine.
 *
 * EVERY CASE HERE IS TWO INPUTS THAT MUST PRODUCE DIFFERENT OUTPUT, following
 * `modules/ipam/__tests__/ipamState.test.ts`'s convention. A test that asserts
 * one shape passes just as happily against a function that returns that shape
 * unconditionally, and the distinctions this page exists to draw -- "nothing
 * answered" against "nothing could look", "not authorized" against "not swept",
 * "you may not" against "nobody has" -- are exactly the ones a single-shape
 * assertion would miss.
 */

import { describe, expect, it } from "vitest";

import type { ScopeOut, SubnetOut, SweepRunOut } from "../../ipam/types";
import { currentStep, journeySteps, type JourneyInput, type StepKey } from "../journey";

const NOW = Date.parse("2026-08-26T12:00:00Z");

const subnet = (over: Partial<SubnetOut> = {}): SubnetOut => ({
  client_id: "o1",
  id: "s1",
  vrf_id: "v1",
  prefix: "10.0.4.0/24",
  kind: "lan",
  name: "office",
  description: "",
  site_id: null,
  vlan_id: null,
  gateway: null,
  dns_servers: [],
  parent_id: null,
  sweep_enabled: true,
  sweep_device_id: null,
  sweep_interval_hours: 168,
  last_swept_at: null,
  ...over,
});

const scope = (over: Partial<ScopeOut> = {}): ScopeOut => ({
  client_id: "o1",
  id: "sc1",
  vrf_id: "v1",
  prefix: "10.0.0.0/16",
  note: "",
  authorized_by: "someone",
  authorized_at: "2026-08-01T00:00:00Z",
  expires_at: null,
  revoked_at: null,
  ...over,
});

const run = (over: Partial<SweepRunOut> = {}): SweepRunOut => ({
  client_id: "o1",
  id: "r1",
  subnet_id: "s1",
  job_id: null,
  device_id: null,
  state: "succeeded",
  trigger: "manual",
  target_count: 254,
  scope_digest: "d",
  hosts_found: 3,
  truncated: false,
  started_at: "2026-08-26T11:00:00Z",
  finished_at: "2026-08-26T11:01:00Z",
  error: "",
  ...over,
});

const ALL = [
  "ipam.manage",
  "ipam.scope.manage",
  "ipam.sweep.run",
  "devices.read",
  "agent.release.read",
];

const input = (over: Partial<JourneyInput> = {}): JourneyInput => ({
  permissions: ALL,
  subnets: [],
  scopes: [],
  selected: null,
  latestRun: null,
  candidates: { total: 0, unsweepable_subnets: 0 },
  releases: [],
  now: NOW,
  ...over,
});

const step = (over: Partial<JourneyInput>, key: StepKey) => {
  const found = journeySteps(input(over)).find((s) => s.key === key);
  if (!found) throw new Error(`no step ${key}`);
  return found;
};

describe("the sequence itself", () => {
  it("is five steps, in the order the work happens", () => {
    expect(journeySteps(input()).map((s) => s.key)).toEqual([
      "register",
      "authorize",
      "sweep",
      "review",
      "agent",
    ]);
  });

  it("points at the first unfinished step, and at nothing once everything is done", () => {
    const fresh = journeySteps(input());
    expect(currentStep(fresh)?.key).toBe("register");

    const finished = journeySteps(
      input({
        subnets: [subnet({ last_swept_at: "2026-08-26T11:00:00Z" })],
        scopes: [scope()],
        releases: [{ has_artifact: true }],
      }),
    );
    expect(currentStep(finished)).toBeNull();
  });

  it("treats a step whose query has not answered as unfinished, not as a place to stand", () => {
    // Otherwise the "you are here" marker walks down the page as each query
    // lands, which reads as progress being made rather than as loading.
    const loading = journeySteps(input({ subnets: undefined, scopes: undefined }));
    expect(loading[0]?.state).toBe("unknown");
    expect(currentStep(loading)?.key).toBe("register");
  });
});

describe("step 1 — a registered network", () => {
  it("distinguishes 'none registered' from 'not loaded'", () => {
    expect(step({ subnets: [] }, "register").state).toBe("blocked");
    expect(step({ subnets: undefined }, "register").state).toBe("unknown");
    // And the difference is visible, not just internal.
    expect(step({ subnets: [] }, "register").status).toMatch(/No network is registered/);
    expect(step({ subnets: undefined }, "register").status).toMatch(/Loading/);
  });

  it("names the permission when the caller cannot register one, and still shows the step", () => {
    const denied = step({ subnets: [], permissions: [] }, "register");
    expect(denied.missingPermission).toBe("ipam.manage");
    // NOT HIDDEN. A hidden prerequisite is indistinguishable from a broken
    // feature, which is the failure this whole journey exists to fix.
    expect(denied.status).toMatch(/No network is registered/);
    expect(step({ subnets: [] }, "register").missingPermission).toBeNull();
  });
});

describe("step 2 — the authorization nobody discovers on their own", () => {
  it("separates 'registered' from 'authorized', which is the point of the step", () => {
    // A subnet exists and is NOT authorized: this is the case an operator hits
    // as a bare 403 from the sweep button today.
    const unauthorized = step({ subnets: [subnet()], scopes: [] }, "authorize");
    expect(unauthorized.state).toBe("blocked");
    expect(unauthorized.remedy).toMatch(/deny-by-default/);

    const authorized = step({ subnets: [subnet()], scopes: [scope()] }, "authorize");
    expect(authorized.state).toBe("done");
  });

  it("reports a partial authorization as partial rather than as done", () => {
    const partial = step(
      {
        subnets: [subnet(), subnet({ id: "s2", prefix: "192.168.9.0/24" })],
        scopes: [scope()],
      },
      "authorize",
    );
    expect(partial.state).toBe("partial");
    expect(partial.status).toMatch(/1 of 2/);
  });

  it("does not count a revoked or expired grant", () => {
    const revoked = step(
      { subnets: [subnet()], scopes: [scope({ revoked_at: "2026-08-20T00:00:00Z" })] },
      "authorize",
    );
    expect(revoked.state).toBe("blocked");
    const expired = step(
      { subnets: [subnet()], scopes: [scope({ expires_at: "2026-08-01T00:00:00Z" })] },
      "authorize",
    );
    expect(expired.state).toBe("blocked");
  });

  it("deep-links to the admin-tier screen rather than offering the grant here", () => {
    // The scan-authorization grant decides where this platform puts packets on
    // somebody's wire. It stays where it is, under its own permission.
    const s = step({ subnets: [subnet()], scopes: [] }, "authorize");
    expect(s.to).toBe("/ipam/scan-authorizations");
    expect(step({ subnets: [subnet()], scopes: [], permissions: [] }, "authorize").missingPermission).toBe(
      "ipam.scope.manage",
    );
  });
});

describe("step 3 — swept, and the segment nothing could look at", () => {
  it("refuses to ask for a sweep before there is anything to sweep", () => {
    const s = step({ subnets: [subnet()], scopes: [] }, "sweep");
    expect(s.state).toBe("blocked");
    expect(s.status).toMatch(/No authorized network/);
  });

  it("tells 'never swept' apart from 'swept'", () => {
    expect(step({ subnets: [subnet()], scopes: [scope()] }, "sweep").state).toBe("blocked");
    expect(
      step(
        { subnets: [subnet({ last_swept_at: "2026-08-26T11:00:00Z" })], scopes: [scope()] },
        "sweep",
      ).state,
    ).toBe("done");
  });

  it("gives no_agent its own remedy, and it is not 'try again'", () => {
    // THE ONE AN OPERATOR WOULD NEVER GUESS. A sweep is performed BY an agent
    // inside the segment, because the control plane never probes an endpoint.
    // So a brand-new segment cannot be swept until one machine on it has been
    // installed by hand -- and nothing else in the console says so.
    const blind = step(
      {
        subnets: [subnet()],
        scopes: [scope()],
        selected: subnet(),
        latestRun: run({ state: "no_agent" }),
      },
      "sweep",
    );
    expect(blind.state).toBe("blocked");
    expect(blind.status).toMatch(/no enrolled agent/i);
    expect(blind.remedy).toMatch(/never probes/);
    // And it sends them somewhere that helps, rather than back to the button
    // that just failed.
    expect(blind.to).toBe("/agent-packages");

    const fine = step(
      {
        subnets: [subnet({ last_swept_at: "2026-08-26T11:00:00Z" })],
        scopes: [scope()],
        selected: subnet(),
        latestRun: run({ state: "succeeded" }),
      },
      "sweep",
    );
    expect(fine.state).toBe("done");
    expect(fine.to).not.toBe("/agent-packages");
  });
});

describe("step 4 — review, which reports and never gates", () => {
  it("is never blocked, whatever the sweep found", () => {
    // Whether a sweep's findings become asset records automatically or only on
    // promotion is the asset module's decision and is being revisited. A
    // journey that gated step 5 on "you have promoted these" would be wrong the
    // day that changes, and would read as broken to an operator whose records
    // already exist.
    for (const candidates of [
      { total: 0, unsweepable_subnets: 0 },
      { total: 42, unsweepable_subnets: 0 },
      { total: 0, unsweepable_subnets: 3 },
      { total: 42, unsweepable_subnets: 3 },
    ]) {
      expect(step({ candidates }, "review").state).not.toBe("blocked");
    }
  });

  it("does not let step 5 depend on it", () => {
    const withUnreviewed = journeySteps(
      input({
        subnets: [subnet({ last_swept_at: "2026-08-26T11:00:00Z" })],
        scopes: [scope()],
        candidates: { total: 42, unsweepable_subnets: 0 },
        releases: [{ has_artifact: true }],
      }),
    );
    expect(withUnreviewed.find((s) => s.key === "agent")?.state).toBe("done");
  });

  it("says 'nothing answered' and 'nothing could look' in different words", () => {
    const clean = step({ candidates: { total: 0, unsweepable_subnets: 0 } }, "review");
    const blind = step({ candidates: { total: 0, unsweepable_subnets: 2 } }, "review");
    expect(clean.status).not.toEqual(blind.status);
    expect(blind.status).toMatch(/could not be swept/);
    expect(blind.state).toBe("partial");
    expect(clean.state).toBe("done");
  });

  it("does not read an empty Discovered list as 'nothing was found'", () => {
    // A sweep that writes its findings straight into the fleet list leaves
    // Discovered empty. If this step said "nothing was found" there, it would
    // be asserting the opposite of the truth in the regime the asset module is
    // moving to -- so it reports what is WAITING FOR REVIEW HERE, and names
    // the other place findings land, whichever regime is in force.
    const empty = step({ candidates: { total: 0, unsweepable_subnets: 0 } }, "review");
    expect(empty.status).toMatch(/waiting for review/);
    expect(empty.status).not.toMatch(/nothing was (found|seen)/i);
    expect(empty.remedy).toMatch(/straight into the fleet list/);

    // And it is said in the populated case too, not only the empty one.
    const found = step({ candidates: { total: 3, unsweepable_subnets: 0 } }, "review");
    expect(found.remedy).toMatch(/straight into the fleet list/);
  });

  it("warns that a ping reply is not a machine before anything is installed on it", () => {
    const found = step({ candidates: { total: 9, unsweepable_subnets: 0 } }, "review");
    expect(found.status).toMatch(/9 addresses/);
    expect(found.remedy).toMatch(/not a machine/);
  });
});

describe("step 5 — something to install", () => {
  it("distinguishes an empty catalogue from a catalogue of rows with no bytes", () => {
    // A release row without an uploaded artefact cannot produce an install
    // command at all, so counting it would promise something that 409s.
    expect(step({ releases: [] }, "agent").state).toBe("blocked");
    expect(step({ releases: [{ has_artifact: false }] }, "agent").state).toBe("blocked");
    expect(step({ releases: [{ has_artifact: true }] }, "agent").state).toBe("done");
  });

  it("does not claim the platform will install anything", () => {
    const ready = step({ releases: [{ has_artifact: true }] }, "agent");
    expect(ready.remedy).toMatch(/One machine at a time/);
    expect(ready.remedy).toMatch(/Managed deployment/);
  });

  it("says why an empty catalogue is not a fault", () => {
    expect(step({ releases: [] }, "agent").remedy).toMatch(/never builds or fetches/);
  });
});
