/**
 * What the console offers, and what it can read.
 *
 * TWO FINDINGS, ONE TABLE. Auditor A: "[HIGH] Production job-type dropdown
 * exposes a developer test job called `hello`, pre-filled with `ping`."
 * Auditor B: "[LOW] A junk job type named `hello` sits in the production
 * queue … the UI gives no way to distinguish real job types from test ones."
 * The first says `hello` must not be dispatchable from here; the second says
 * it must still be legible when it turns up in the list. A test that only
 * checked the first would licence a job row rendering as nothing at all.
 *
 * THE THIRD ASSERTION IS ABOUT THE BACKEND. `DISPATCHABLE_JOB_TYPES` must stay
 * a subset of `backend/app/jobs/direct_dispatch.py#DIRECTLY_DISPATCHABLE_JOB_TYPES`
 * minus `hello` — every other registered type is refused by `POST /api/v1/jobs`
 * with a 403 naming the module route that owns it. Widening this list without
 * widening that allowlist ships a dropdown option that cannot work.
 */

import { describe, expect, it } from "vitest";

import { DISPATCHABLE_JOB_TYPES, isDispatchable, jobTypeLabel } from "./jobTypes";

/** `DIRECTLY_DISPATCHABLE_JOB_TYPES` as of this cycle, less the test fixture. */
const BACKEND_ALLOWS_LESS_HELLO = ["inventory.collect", "logs.ship"];

describe("DISPATCHABLE_JOB_TYPES", () => {
  it("does not offer the developer test job", () => {
    expect(DISPATCHABLE_JOB_TYPES.map((entry) => entry.type)).not.toContain("hello");
    expect(isDispatchable("hello")).toBe(false);
  });

  it("offers only what the kernel dispatch route will actually accept", () => {
    for (const entry of DISPATCHABLE_JOB_TYPES) {
      expect(BACKEND_ALLOWS_LESS_HELLO).toContain(entry.type);
    }
    // And is not empty — a dropdown with nothing in it is the blocked-flow
    // defect this pass exists to close, arriving by a different road.
    expect(DISPATCHABLE_JOB_TYPES.length).toBeGreaterThan(0);
  });

  it("gives every option a human name and a description of what it does", () => {
    for (const entry of DISPATCHABLE_JOB_TYPES) {
      expect(entry.label).not.toBe(entry.type);
      expect(entry.label.trim()).not.toBe("");
      // The audit asked for "a one-line description … ~30s, non-disruptive".
      expect(entry.description.length).toBeGreaterThan(20);
    }
  });
});

describe("jobTypeLabel", () => {
  it("reads the queue's job types as English, including the ones nobody may dispatch", () => {
    expect(jobTypeLabel("inventory.collect")).toBe("Collect inventory");
    expect(jobTypeLabel("patch.scan")).toBe("Scan for patches");
    expect(jobTypeLabel("health.policy.apply")).toBe("Apply health policy");
  });

  it("names the test fixture rather than hiding it", () => {
    // A `hello` row exists in the audited deployment. Rendering it blank, or
    // as "Unknown", would make the junk data harder to find and remove.
    expect(jobTypeLabel("hello")).toBe("Developer test job");
  });

  it("falls back to the wire type for a type this build has never heard of", () => {
    // A control plane outlives the console in front of it. The wire name is at
    // least a string the operator can search the backend for.
    expect(jobTypeLabel("module.invented.tomorrow")).toBe("module.invented.tomorrow");
    expect(jobTypeLabel("")).toBe("");
  });
});
