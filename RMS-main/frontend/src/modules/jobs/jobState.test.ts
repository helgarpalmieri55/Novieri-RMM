import { describe, expect, it } from "vitest";
import type { JobStatus } from "../../lib/api";
import { NO_DATE } from "../../lib/datetime";
import type { JobLifecycle } from "./jobState";
import {
  JOB_STATUSES,
  canRetry,
  canWithdraw,
  describeAge,
  describeCancelRequest,
  describeCancellation,
  describeLateCancel,
  describeRetry,
  describeSpan,
  describeStop,
  describeStopEffect,
  isStuck,
  statusVariant,
  stopKind,
  typeBreakdown,
  whyNotRetryable,
  whyNotStoppable,
} from "./jobState";

describe("statusVariant", () => {
  it("is total over every job status", () => {
    const statuses: JobStatus[] = [
      "queued",
      "dispatched",
      "running",
      "succeeded",
      "failed",
      "expired",
      "refused",
      "cancelled",
    ];
    expect([...JOB_STATUSES].sort()).toEqual([...statuses].sort());
    for (const status of statuses) {
      expect(statusVariant(status)).toBeTruthy();
    }
  });

  it("distinguishes terminal outcomes", () => {
    expect(statusVariant("succeeded")).toBe("success");
    expect(statusVariant("failed")).toBe("destructive");
    expect(statusVariant("refused")).toBe("destructive");
    expect(statusVariant("running")).toBe("info");
  });
});

/**
 * The "nothing needs attention" lie, as a pure function.
 *
 * Auditor A found the dashboard reporting "Jobs needing attention: 0" over two
 * `patch.scan` jobs that had been queued for more than a day against an agent
 * that had been offline the whole time. Auditor B found the same queue from
 * /jobs: "Jobs 5 / Running 0 / Failed 0". Both headline metrics were true and
 * both were useless, because neither counted WAITING as a state that can go
 * wrong.
 */
describe("isStuck", () => {
  const NOW = Date.parse("2026-08-20T10:00:00Z");
  const queued = (issued: string, status: JobStatus = "queued") => ({
    status,
    issued_at: issued,
  });

  it("flags a job queued for over a day — the audit's own rows", () => {
    expect(isStuck(queued("2026-08-19T09:00:00Z"), NOW)).toBe(true);
  });

  it("does not flag a job queued a minute ago", () => {
    // Without this the flag means "queued", every healthy queue lights up, and
    // the tile is ignored inside a week — which is how a real alarm dies.
    expect(isStuck(queued("2026-08-20T09:59:00Z"), NOW)).toBe(false);
  });

  it("uses the threshold as a boundary, not a suggestion", () => {
    expect(isStuck(queued("2026-08-20T09:45:00Z"), NOW)).toBe(true);
    expect(isStuck(queued("2026-08-20T09:45:01Z"), NOW)).toBe(false);
  });

  it("flags a dispatched job too — the endpoint still has not answered", () => {
    expect(isStuck(queued("2026-08-19T09:00:00Z", "dispatched"), NOW)).toBe(true);
  });

  it("never flags a job that has finished, however old", () => {
    for (const status of ["succeeded", "failed", "expired", "refused"] as JobStatus[]) {
      expect(isStuck(queued("2020-01-01T00:00:00Z", status), NOW)).toBe(false);
    }
    expect(isStuck(queued("2020-01-01T00:00:00Z", "running"), NOW)).toBe(false);
  });

  it("says nothing about a job with an unparseable issue time", () => {
    // A missing timestamp is not evidence of waiting, and reporting it as
    // stuck would put a permanent false alarm on the tile.
    expect(isStuck(queued(""), NOW)).toBe(false);
    expect(isStuck(queued("not a date"), NOW)).toBe(false);
  });
});

describe("describeAge", () => {
  const NOW = Date.parse("2026-08-20T10:00:00Z");

  it("answers 'is this stuck?' without date arithmetic", () => {
    expect(describeAge("2026-08-19T09:00:00Z", NOW)).toBe("25h ago");
    expect(describeAge("2026-08-20T09:30:00Z", NOW)).toBe("30m ago");
    expect(describeAge("2026-08-10T10:00:00Z", NOW)).toBe("10d ago");
  });

  it("does not report clock skew as the future", () => {
    // Server and browser clocks differ; "in -3 seconds" is not a thing to show
    // an operator.
    expect(describeAge("2026-08-20T10:00:20Z", NOW)).toBe("just now");
  });

  it("renders the missing-value token rather than 'Invalid Date'", () => {
    expect(describeAge(null, NOW)).toBe(NO_DATE);
    expect(describeAge("", NOW)).toBe(NO_DATE);
    expect(describeAge("not a date", NOW)).toBe(NO_DATE);
  });
});

/**
 * The two supporting lines the KPI tiles are made of.
 *
 * v3 draws a second line under every figure and the port shipped four tiles
 * carrying nothing but the number, so a count said what it was made of
 * exactly when it happened to be zero (`zeroLabel`). These are the two
 * derivations that line needs; the page composes them.
 *
 * BREAKS AGAINST: `describeSpan` borrowing `describeAge`'s "ago" (a run in
 * flight has not happened yet), and against a breakdown that drops the tail
 * silently -- "18 patch.apply" over a figure of 40 is a wrong sentence, not a
 * short one.
 */
describe("describeSpan", () => {
  it("names a duration in the coarsest unit that is still true", () => {
    expect(describeSpan(12 * 60_000)).toBe("12m");
    expect(describeSpan(3 * 60 * 60_000)).toBe("3h");
    expect(describeSpan(4 * 24 * 60 * 60_000)).toBe("4d");
  });

  it("never says 'ago' — this is a span, not an age", () => {
    expect(describeSpan(90 * 60_000)).not.toContain("ago");
  });

  it("reports a skewed clock as a short span, not a negative one", () => {
    expect(describeSpan(-4000)).toBe("under a minute");
    expect(describeSpan(20_000)).toBe("under a minute");
  });
});

describe("typeBreakdown", () => {
  const job = (type: string) => ({ type });

  it("says what a count is made of, largest first", () => {
    expect(
      typeBreakdown([job("patch.apply"), job("inventory.collect"), job("patch.apply")]),
    ).toBe("2 patch.apply · 1 inventory.collect");
  });

  it("orders equal counts by name so a render does not shuffle them", () => {
    expect(typeBreakdown([job("b.two"), job("a.one")])).toBe("1 a.one · 1 b.two");
  });

  it("counts the tail rather than dropping it", () => {
    expect(typeBreakdown([job("a"), job("b"), job("c"), job("d")], 2)).toBe("1 a · 1 b · +2 more");
  });

  it("has nothing to say about nothing", () => {
    expect(typeBreakdown([])).toBeNull();
  });
});

/**
 * Cancellation copy: the console says only what the platform can prove.
 *
 * `docs/superpowers/specs/2026-08-22-job-lifecycle-design.md` §3.6. A UI
 * reading "Cancelled" when it means "asked to cancel, not confirmed" is the
 * defect class this programme has spent its cycles closing -- the same audit
 * found nine tiles reading `0` on a page where nothing had been measured.
 */
describe("cancellation", () => {
  const QUEUED: JobLifecycle = {
    status: "queued",
    expires_at: "2026-08-20T11:00:00Z",
    cancel_outcome: null,
  };

  it("offers withdrawal only for a job that has not been sent", () => {
    expect(canWithdraw(QUEUED)).toBe(true);
    expect(stopKind(QUEUED)).toBe("withdraw");
    for (const status of ["dispatched", "running", "succeeded", "failed", "expired", "refused",
      "cancelled"] as JobStatus[]) {
      expect(canWithdraw({ ...QUEUED, status })).toBe(false);
    }
  });

  it("offers interruption for a job that has reached a device, and for nothing else", () => {
    // THE TWO MECHANISMS ARE NOT INTERCHANGEABLE. Withdrawal is the server's
    // own authority; interruption is a signed request to a machine that
    // already has the work. Reporting "withdraw" for a running job would put
    // the wrong promise in the confirmation dialog.
    expect(stopKind({ ...QUEUED, status: "dispatched" })).toBe("interrupt");
    expect(stopKind({ ...QUEUED, status: "running" })).toBe("interrupt");
    for (const status of ["succeeded", "failed", "expired", "refused", "cancelled"] as JobStatus[]) {
      expect(stopKind({ ...QUEUED, status })).toBeNull();
    }
  });

  it("offers nothing once a cancel has already been asked for", () => {
    // The server answers a second cancel with a 409 naming the first request,
    // and it does NOT mint a second signed command. A console that kept
    // offering the button would be offering a control that cannot work.
    expect(
      stopKind({
        ...QUEUED,
        status: "running",
        cancel_requested_at: "2026-08-20T10:02:00Z",
      }),
    ).toBeNull();
  });

  it("promises different things for a withdrawal and an interruption", () => {
    // The one difference that matters to somebody about to press the button:
    // whether the device is contacted at all.
    expect(describeStop("withdraw").body).toContain("device is not contacted");
    expect(describeStop("interrupt").body).toContain("asks the device to stop");
    expect(describeStop("interrupt").body).toContain("half-done");
    expect(describeStop("withdraw").title).not.toBe(describeStop("interrupt").title);
    expect(describeStop("withdraw").confirm).not.toBe(describeStop("interrupt").confirm);

    const expiry = "20 Aug 2026, 07:00";
    expect(describeStopEffect("withdraw", expiry)).toContain("has not been sent");
    expect(describeStopEffect("interrupt", expiry)).toContain("already has this job");
    for (const kind of ["withdraw", "interrupt"] as const) {
      expect(describeStopEffect(kind, expiry)).toContain(expiry);
    }
  });

  it("says a cancel was ASKED FOR while the endpoint has not answered", () => {
    // **THE SENTENCE THE WHOLE DESIGN EXISTS TO MAKE POSSIBLE.** A job can be
    // `running` AND carry a cancel request; both are true and the console shows
    // both. Saying "Cancelled" here would claim something only the endpoint can
    // know.
    const asked = "20 Aug 2026, 07:02";
    for (const status of ["dispatched", "running"] as JobStatus[]) {
      const line = describeCancelRequest(
        { ...QUEUED, status, cancel_requested_at: "2026-08-20T10:02:00Z", cancel_outcome: "requested" },
        asked,
      );
      expect(line).toContain("Cancel requested");
      expect(line).toContain(asked);
      expect(line).toContain("has not confirmed");
      expect(line).not.toContain("Cancelled.");
    }
    expect(describeCancelRequest(QUEUED, asked)).toBeNull();
  });

  it("says plainly when an endpoint is too old to be interrupted", () => {
    // The mixed-version case, in front of the operator rather than in a log
    // file. An agent built before the interruption cycle refuses the signed
    // `job.cancel` as an unknown job type, and the server records
    // `unsupported`.
    const line = describeCancelRequest(
      {
        ...QUEUED,
        status: "running",
        cancel_requested_at: "2026-08-20T10:02:00Z",
        cancel_outcome: "unsupported",
      },
      "20 Aug 2026, 07:02",
    );
    expect(line).toContain("too old to be interrupted");
    expect(line).toContain("run or expire");
  });

  it("says a withdrawn job was withdrawn before it was sent", () => {
    expect(
      describeCancellation({ ...QUEUED, status: "cancelled", cancel_outcome: "withdrawn" }),
    ).toBe("Withdrawn before it was sent to the device.");
  });

  it("distinguishes a job that never started from one that was stopped mid-flight", () => {
    // `dropped` and `interrupted` are not the same fact, and only the second
    // can have left work half-done on the machine. Collapsing them would delete
    // the distinction the outcome column exists to carry.
    const dropped = describeCancellation({
      ...QUEUED,
      status: "cancelled",
      cancel_outcome: "dropped",
    });
    const interrupted = describeCancellation({
      ...QUEUED,
      status: "cancelled",
      cancel_outcome: "interrupted",
    });
    expect(dropped).toContain("before it started");
    expect(dropped).toContain("nothing ran");
    expect(interrupted).toContain("while it was running");
    expect(interrupted).toContain("was not undone");
    expect(dropped).not.toBe(interrupted);
  });

  it("claims nothing for a cancelled job whose outcome it does not recognise", () => {
    // The falsifier for the lines above. A control plane may add outcomes this
    // console has never seen, and rendering an unknown one as "withdrawn" would
    // assert that the job never reached the device when the endpoint may have
    // run it.
    expect(
      describeCancellation({ ...QUEUED, status: "cancelled", cancel_outcome: "something_new" }),
    ).toBe("Cancelled.");
    expect(describeCancellation({ ...QUEUED, status: "cancelled", cancel_outcome: null })).toBe(
      "Cancelled.",
    );
  });

  it("has nothing to say about a job nobody cancelled", () => {
    expect(describeCancellation(QUEUED)).toBeNull();
    expect(describeCancellation({ ...QUEUED, status: "succeeded" })).toBeNull();
    expect(describeLateCancel(QUEUED)).toBeNull();
    expect(describeLateCancel({ ...QUEUED, status: "succeeded" })).toBeNull();
  });

  it("says so when the cancel arrived too late", () => {
    // The case the design exists to tell the truth about. "Cancelled" here
    // would be a lie about a machine that did the work; silence would leave
    // somebody who pressed the button wondering whether it did anything.
    expect(
      describeLateCancel({
        ...QUEUED,
        status: "succeeded",
        cancel_requested_at: "2026-08-20T10:02:00Z",
      }),
    ).toContain("too late");
    expect(
      describeLateCancel({
        ...QUEUED,
        status: "expired",
        cancel_requested_at: "2026-08-20T10:02:00Z",
      }),
    ).toContain("never confirmed");
  });

  it("explains why a finished job cannot be stopped", () => {
    const expiry = "20 Aug 2026, 07:00";
    for (const status of ["succeeded", "failed", "expired", "refused", "cancelled"] as JobStatus[]) {
      expect(whyNotStoppable({ ...QUEUED, status }, expiry)).toContain("has finished");
    }
    // A job that CAN be stopped gets no refusal line -- the button is there
    // instead.
    expect(whyNotStoppable(QUEUED, expiry)).toBeNull();
    expect(whyNotStoppable({ ...QUEUED, status: "running" }, expiry)).toBeNull();
    // Nor does one with a cancel already in flight: `describeCancelRequest`
    // owns that sentence, and two of them would contradict each other.
    expect(
      whyNotStoppable(
        { ...QUEUED, status: "running", cancel_requested_at: "2026-08-20T10:02:00Z" },
        expiry,
      ),
    ).toBeNull();
  });
});

/**
 * A cancelled job is terminal, so it is never "waiting too long".
 *
 * The KPI tile counts jobs the platform is still waiting on an endpoint for.
 * A withdrawn job from last week is not one, and counting it would put the
 * console straight back into the state the audit found: a red number nobody
 * can act on.
 */
describe("a cancelled job is not waiting", () => {
  it("is excluded from the stuck count however old it is", () => {
    const NOW = Date.parse("2026-08-20T10:00:00Z");
    expect(isStuck({ status: "cancelled", issued_at: "2026-08-01T00:00:00Z" }, NOW)).toBe(false);
    // NOT VACUOUS: the same job, still queued, IS stuck.
    expect(isStuck({ status: "queued", issued_at: "2026-08-01T00:00:00Z" }, NOW)).toBe(true);
  });
});

/**
 * Retry copy: a NEW job, and the console never implies otherwise.
 *
 * `docs/superpowers/specs/2026-08-22-job-lifecycle-design.md` §4.1, §4.2.
 */
describe("retry", () => {
  const FAILED: JobLifecycle = {
    status: "failed",
    expires_at: "2026-08-20T11:00:00Z",
  };

  it("offers a retry from the four unhappy terminal states and nowhere else", () => {
    for (const status of ["failed", "expired", "refused", "cancelled"] as JobStatus[]) {
      expect(canRetry({ ...FAILED, status })).toBe(true);
    }
    // `succeeded` is absent ON PURPOSE: re-running work that worked is a new
    // dispatch, and it belongs on the route that owns the record of what was
    // asked for. The unfinished states are absent because the platform's own
    // sweep already re-sends a job that never reached its device.
    for (const status of ["succeeded", "queued", "dispatched", "running"] as JobStatus[]) {
      expect(canRetry({ ...FAILED, status })).toBe(false);
    }
  });

  it("mirrors the server's two refusals rather than hiding the control", () => {
    expect(whyNotRetryable({ ...FAILED, status: "succeeded" })).toContain(
      "new job rather than a retry",
    );
    expect(whyNotRetryable({ ...FAILED, status: "running" })).toContain("nothing to re-run yet");
    expect(whyNotRetryable(FAILED)).toBeNull();
  });

  it("says a retry does not re-send the original job", () => {
    // The operator's mental model is the thing at stake. A dialog implying
    // this row comes back to life would be describing a replay, and a replay
    // is exactly what the server refuses to do.
    expect(describeRetry(FAILED).body).toContain("does not re-send the original job");
    expect(describeRetry(FAILED).body).toContain("NEW job");
    expect(describeRetry(FAILED).body).toContain("left exactly as it is");
  });

  it("names an earlier cancellation before re-running what somebody stopped", () => {
    const cancelled = describeRetry({ ...FAILED, status: "cancelled" });
    expect(cancelled.body).toContain("Somebody cancelled this job");
    expect(cancelled.body).not.toBe(describeRetry(FAILED).body);
  });
});
