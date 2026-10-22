import { describe, expect, it } from "vitest";
import {
  countsDown,
  describeTargetTiming,
  formatMinutes,
  formatWindow,
  formatWindowEnd,
  isMidnightEnd,
  parseTimeToMinutes,
  resolutionPreview,
  targetStateVariant,
  totalWindowMinutes,
  validateWindows,
  windowDurationMinutes,
  windowEndMinutes,
} from "../slaState";
import type { PolicyOut, TargetState } from "../types";

// --------------------------------------------------------------------------
// The midnight-end sentinel (spec §2.2, migration 0035)
// --------------------------------------------------------------------------
//
// `end_time = 00:00` means midnight at the END of the day: 1440 minutes, not
// zero. A UI that renders it as an empty or zero-length window is wrong, and
// these are the assertions that keep it from doing so.

describe("working windows: the midnight-end sentinel", () => {
  it("counts a 00:00 end as the end of the day, not the start", () => {
    expect(windowEndMinutes("00:00:00")).toBe(1440);
    expect(isMidnightEnd("00:00:00")).toBe(true);
    expect(isMidnightEnd("00:00")).toBe(true);
    expect(isMidnightEnd("17:00:00")).toBe(false);
  });

  it("gives an evening window ending at midnight its real duration", () => {
    expect(windowDurationMinutes({ start_time: "22:00:00", end_time: "00:00:00" })).toBe(120);
  });

  it("treats 00:00-00:00 as a full 24-hour day (1440 minutes)", () => {
    expect(windowDurationMinutes({ start_time: "00:00:00", end_time: "00:00:00" })).toBe(1440);
    // 00:00-23:59 is the near-miss the spec warns about: a minute a day short.
    expect(windowDurationMinutes({ start_time: "00:00:00", end_time: "23:59:00" })).toBe(1439);
  });

  it("renders the midnight end as 24:00 so it never reads as an empty window", () => {
    expect(formatWindowEnd("00:00:00")).toBe("24:00");
    expect(formatWindow({ start_time: "22:00:00", end_time: "00:00:00" })).toBe("22:00–24:00");
    expect(formatWindow({ start_time: "09:00:00", end_time: "17:30:00" })).toBe("09:00–17:30");
  });

  it("parses both HH:MM and HH:MM:SS, and refuses nonsense rather than defaulting to 0", () => {
    expect(parseTimeToMinutes("09:30")).toBe(570);
    expect(parseTimeToMinutes("09:30:00")).toBe(570);
    expect(parseTimeToMinutes("")).toBeNull();
    expect(parseTimeToMinutes("25:00")).toBeNull();
  });

  it("sums a week including midnight-ended windows", () => {
    expect(
      totalWindowMinutes([
        { start_time: "09:00:00", end_time: "17:00:00" },
        { start_time: "22:00:00", end_time: "00:00:00" },
      ]),
    ).toBe(480 + 120);
  });
});

describe("working windows: validation mirrors calendars._validate_windows", () => {
  it("accepts a midnight-ended window that a naive start<end check would reject", () => {
    expect(validateWindows([{ weekday: 4, start_time: "22:00", end_time: "00:00" }])).toEqual([]);
  });

  it("rejects an inverted window", () => {
    const problems = validateWindows([{ weekday: 0, start_time: "17:00", end_time: "09:00" }]);
    expect(problems).toHaveLength(1);
    expect(problems[0]).toContain("Monday");
  });

  it("rejects overlapping windows on the same weekday but allows touching ones", () => {
    expect(
      validateWindows([
        { weekday: 1, start_time: "09:00", end_time: "13:00" },
        { weekday: 1, start_time: "12:00", end_time: "17:00" },
      ]),
    ).toHaveLength(1);
    // Half-open [start, end): a lunch break split into two adjacent windows is
    // ordinary configuration, not an overlap.
    expect(
      validateWindows([
        { weekday: 1, start_time: "09:00", end_time: "13:00" },
        { weekday: 1, start_time: "13:00", end_time: "17:00" },
      ]),
    ).toEqual([]);
  });

  it("does not confuse two weekdays with each other", () => {
    expect(
      validateWindows([
        { weekday: 1, start_time: "09:00", end_time: "17:00" },
        { weekday: 2, start_time: "09:00", end_time: "17:00" },
      ]),
    ).toEqual([]);
  });
});

describe("formatMinutes", () => {
  it("reads a stored target as a duration", () => {
    expect(formatMinutes(480)).toBe("8h");
    expect(formatMinutes(90)).toBe("1h 30m");
    expect(formatMinutes(1440)).toBe("1d");
    expect(formatMinutes(45)).toBe("45m");
    expect(formatMinutes(0)).toBe("0m");
  });
});

// --------------------------------------------------------------------------
// Target state -- the server's verdict, rendered
// --------------------------------------------------------------------------

const NOW = Date.parse("2026-08-02T12:00:00Z");

function facts(overrides: Partial<Parameters<typeof describeTargetTiming>[0]> = {}) {
  return {
    state: "running" as TargetState,
    dueAt: null,
    metAt: null,
    breachedAt: null,
    pausedAt: null,
    pausedMinutesTotal: null,
    voidedAt: null,
    ...overrides,
  };
}

describe("describeTargetTiming", () => {
  it("counts a running clock down to its deadline", () => {
    const timing = describeTargetTiming(
      facts({ state: "running", dueAt: "2026-08-02T14:30:00Z" }),
      NOW,
    );
    expect(timing.headline).toBe("2h 30m remaining");
    expect(timing.ticking).toBe(true);
  });

  it("says a running clock is past due WITHOUT calling it breached", () => {
    const timing = describeTargetTiming(
      facts({ state: "running", dueAt: "2026-08-02T11:00:00Z" }),
      NOW,
    );
    expect(timing.headline).toBe("1h past due");
    expect(timing.detail).toContain("has not recorded a breach yet");
    // Spec §9.1: breach state is never computed in the browser.
    expect(timing.headline.toLowerCase()).not.toContain("breach");
  });

  // A PAUSED CLOCK MUST NOT APPEAR TO BE COUNTING DOWN. The stored due instant
  // is extended on resume, so any "remaining" shown now would be wrong.
  it("never renders a countdown for a paused clock", () => {
    const timing = describeTargetTiming(
      facts({
        state: "paused",
        dueAt: "2026-08-02T14:00:00Z",
        pausedAt: "2026-08-02T10:00:00Z",
        pausedMinutesTotal: 30,
      }),
      NOW,
    );
    expect(timing.headline).toContain("Paused for 2h");
    expect(timing.headline).toContain("clock stopped");
    expect(timing.headline).not.toContain("remaining");
    expect(timing.detail).toContain("extends");
    expect(timing.detail).toContain("30m of earlier pause already credited");
    expect(timing.ticking).toBe(false);
    expect(countsDown("paused")).toBe(false);
  });

  // A VOIDED PROMISE MUST NOT RENDER AS A BREACH, even when the row carries a
  // breach instant -- the server's precedence already put void on top.
  it("renders a voided promise as neither met nor breached", () => {
    const timing = describeTargetTiming(
      facts({
        state: "void",
        dueAt: "2026-08-01T09:00:00Z",
        breachedAt: "2026-08-01T09:00:00Z",
        voidedAt: "2026-08-01T15:00:00Z",
      }),
      NOW,
    );
    expect(timing.headline).toContain("voided");
    expect(timing.headline).not.toContain("Breached");
    expect(timing.detail).not.toContain("Breached");
    expect(timing.ticking).toBe(false);
    // And it must not carry the breach colour.
    expect(targetStateVariant("void")).not.toBe(targetStateVariant("breached"));
    expect(targetStateVariant("void")).toBe("muted");
  });

  // Breached outranks met server-side and the two genuinely co-occur: a reply
  // landing after the deadline stamps met_at on an already-breached row.
  it("reports a late answer as breached, not met", () => {
    const timing = describeTargetTiming(
      facts({
        state: "breached",
        dueAt: "2026-08-02T09:00:00Z",
        breachedAt: "2026-08-02T09:00:00Z",
        metAt: "2026-08-02T11:00:00Z",
      }),
      NOW,
    );
    expect(timing.headline).toBe("Breached 3h ago");
    expect(timing.detail).toContain("after the deadline");
    expect(timing.ticking).toBe(false);
  });

  it("reports a met target with the room it had to spare", () => {
    const timing = describeTargetTiming(
      facts({
        state: "met",
        dueAt: "2026-08-02T12:00:00Z",
        metAt: "2026-08-02T10:00:00Z",
      }),
      NOW,
    );
    expect(timing.headline).toBe("Met 2h ago");
    expect(timing.detail).toContain("2h inside");
    expect(timing.ticking).toBe(false);
  });

  it("renders no SLA as a real state, not a missing value", () => {
    const timing = describeTargetTiming(facts({ state: "none" }), NOW);
    expect(timing.headline).toBe("No SLA target");
    expect(timing.detail).toContain("real state");
    expect(timing.ticking).toBe(false);
  });

  it("only a running clock ticks", () => {
    expect(countsDown("running")).toBe(true);
    for (const state of ["none", "void", "breached", "met", "paused"] as TargetState[]) {
      expect(countsDown(state)).toBe(false);
    }
  });
});

// --------------------------------------------------------------------------
// Policy resolution preview (spec §3, mirroring policies.resolve_policy)
// --------------------------------------------------------------------------

function policy(overrides: Partial<PolicyOut>): PolicyOut {
  return {
    id: "p",
    queue_id: null,
    priority: null,
    calendar_id: "cal",
    response_target_minutes: 60,
    resolution_target_minutes: 480,
    escalate_priority: false,
    escalate_to_queue_id: null,
    active: true,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("resolutionPreview", () => {
  const exact = policy({ id: "exact", queue_id: "q1", priority: "urgent" });
  const queueWildcard = policy({ id: "queue-any-priority", queue_id: "q1", priority: null });
  const priorityWildcard = policy({ id: "any-queue-urgent", queue_id: null, priority: "urgent" });
  const catchAll = policy({ id: "catch-all", queue_id: null, priority: null });

  it("prefers the most specific level and marks the rest shadowed", () => {
    const preview = resolutionPreview(
      [catchAll, priorityWildcard, queueWildcard, exact],
      "q1",
      "urgent",
    );
    expect(preview.winner?.level).toBe(1);
    expect(preview.winner?.policy?.id).toBe("exact");
    expect(preview.levels.filter((level) => level.wins)).toHaveLength(1);
    expect(preview.levels[1]?.policy?.id).toBe("queue-any-priority");
    expect(preview.levels[1]?.wins).toBe(false);
  });

  // The confusing case this panel exists for: two overlapping wildcards, and
  // only the queue-scoped one is ever used.
  it("resolves queue+any-priority above any-queue+priority", () => {
    const preview = resolutionPreview([priorityWildcard, queueWildcard], "q1", "urgent");
    expect(preview.winner?.level).toBe(2);
    expect(preview.winner?.policy?.id).toBe("queue-any-priority");
    expect(preview.levels[2]?.policy?.id).toBe("any-queue-urgent");
    expect(preview.levels[2]?.wins).toBe(false);
  });

  it("falls through to the catch-all when nothing more specific matches", () => {
    const preview = resolutionPreview([catchAll], "q1", "low");
    expect(preview.winner?.level).toBe(4);
  });

  it("ignores inactive policies exactly as resolve_policy does", () => {
    const preview = resolutionPreview(
      [policy({ id: "exact-inactive", queue_id: "q1", priority: "urgent", active: false }), catchAll],
      "q1",
      "urgent",
    );
    expect(preview.winner?.level).toBe(4);
    expect(preview.levels[0]?.policy).toBeNull();
  });

  it("reports no match rather than inventing a default", () => {
    const preview = resolutionPreview([queueWildcard], "q2", "low");
    expect(preview.winner).toBeNull();
    expect(preview.levels.every((level) => level.policy === null)).toBe(true);
  });

  it("does not let a different queue's policy match", () => {
    const preview = resolutionPreview([exact], "q2", "urgent");
    expect(preview.winner).toBeNull();
  });
});

// --------------------------------------------------------------------------
// Instants inside the timing sentences (UX remediation cycle 5)
// --------------------------------------------------------------------------
//
// `describeTargetTiming` builds sentences a technician prioritises from, and
// half of each sentence is a time. It used to build them with the module's own
// `formatInstant` -- `new Date(x).toLocaleString()` -- which rendered in
// whatever zone the machine in front of the technician happened to be set to
// and never said which. Two people reading one ticket got two different
// answers about the SAME deadline, and neither could tell.
describe("describeTargetTiming renders instants in the reader's zone", () => {
  const DUE = "2026-08-02T14:30:00Z";

  it("puts the deadline in the zone the profile asks for, not the browser's", () => {
    const inNewYork = describeTargetTiming(
      facts({ state: "running", dueAt: DUE }),
      NOW,
      "America/New_York",
    );
    // 14:30Z is 10:30 in New York in August (-04:00, DST -- and the offset is
    // derived per instant precisely so this is not -05:00).
    expect(inNewYork.detail).toBe("Due 02 Aug 2026, 10:30.");
  });

  it("gives two readers in two zones two renderings of the one instant", () => {
    const london = describeTargetTiming(facts({ state: "running", dueAt: DUE }), NOW, "Europe/London");
    const kolkata = describeTargetTiming(facts({ state: "running", dueAt: DUE }), NOW, "Asia/Kolkata");
    expect(london.detail).toBe("Due 02 Aug 2026, 15:30.");
    expect(kolkata.detail).toBe("Due 02 Aug 2026, 20:00.");
  });

  it("falls back to the browser zone when the profile carries no preference", () => {
    // `timezone` is nullable on /me and most operators never set it. Passing
    // nothing must behave exactly as before, not render an empty zone or throw.
    const timing = describeTargetTiming(facts({ state: "running", dueAt: DUE }), NOW);
    expect(timing.detail).toBe("Due 02 Aug 2026, 14:30.");
  });

  // `formatInstant` echoed an unparseable value back to the screen verbatim,
  // and `new Date(x).toLocaleString()` on one produces the literal string
  // "Invalid Date". Neither is a time, and the em dash says so honestly.
  it("renders an unusable instant as the absent marker, never as 'Invalid Date'", () => {
    const timing = describeTargetTiming(
      facts({ state: "breached", breachedAt: "2026-08-02T11:00:00Z", metAt: "not-a-timestamp" }),
      NOW,
      "Europe/London",
    );
    expect(timing.detail).toContain("answered —, after the deadline");
    expect(timing.detail).not.toContain("Invalid Date");
    expect(timing.detail).not.toContain("not-a-timestamp");
  });
});

// THERE IS NO `describeError` TEST HERE ANY MORE, because there is no
// `describeError`. It was the module's own copy of the error-to-sentence
// translation and it is deleted (cycle 4); the two behaviours these tests
// pinned -- unwrapping FastAPI's `{"detail": "..."}` body, and passing a plain
// message through -- are covered by `components/form.test.tsx`, which also
// covers the case this one could not: an `ApiError` whose body is empty,
// where `describeError` returned "" and every call site rendered nothing.
