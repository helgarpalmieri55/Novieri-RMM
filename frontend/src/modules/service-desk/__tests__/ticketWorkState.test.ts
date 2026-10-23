import { describe, expect, it } from "vitest";
import {
  canResolve,
  defaultReplyVisibility,
  describeDue,
  describeTarget,
  shortDuration,
} from "../serviceDeskState";
import type { TicketSlaSummary } from "../types";

const NOW = Date.parse("2026-09-23T12:00:00Z");
const MIN = 60_000;
const at = (offsetMinutes: number) => new Date(NOW + offsetMinutes * MIN).toISOString();

function sla(overrides: Partial<TicketSlaSummary> = {}): TicketSlaSummary {
  return {
    response_due_at: at(120),
    resolution_due_at: at(600),
    response_state: "running",
    resolution_state: "running",
    next_due_at: at(120),
    next_due_target: "response",
    breached: false,
    paused: false,
    ...overrides,
  };
}

describe("shortDuration", () => {
  it.each([
    [30 * 1000, "under a minute"],
    [1 * MIN, "1 min"],
    [45 * MIN, "45 min"],
    [60 * MIN, "1 h"],
    [150 * MIN, "2 h 30 min"],
    [23 * 60 * MIN, "23 h"],
    [26 * 60 * MIN, "1 d 2 h"],
    [3 * 24 * 60 * MIN, "3 d"],
  ])("renders %d ms as %s", (ms, expected) => {
    expect(shortDuration(ms)).toBe(expected);
  });
});

describe("describeDue", () => {
  it("says there is no SLA when the server sent none", () => {
    expect(describeDue(null, NOW)).toMatchObject({ label: "No SLA", variant: "muted" });
    expect(describeDue(undefined, NOW)).toMatchObject({ label: "No SLA" });
  });

  it("counts down to the next deadline and names which clock it is", () => {
    expect(describeDue(sla(), NOW)).toEqual({
      label: "Due in 2 h",
      detail: "First response",
      variant: "muted",
    });
    expect(
      describeDue(sla({ next_due_at: at(600), next_due_target: "resolution" }), NOW),
    ).toMatchObject({ label: "Due in 10 h", detail: "Resolution" });
  });

  it("warns inside the last hour", () => {
    expect(describeDue(sla({ next_due_at: at(30) }), NOW)).toMatchObject({
      label: "Due in 30 min",
      variant: "warning",
    });
  });

  it("says breached, and how long ago, once the server recorded it", () => {
    expect(
      describeDue(sla({ next_due_at: at(-30), breached: true, response_state: "breached" }), NOW),
    ).toMatchObject({ label: "Breached 30 min ago", variant: "destructive" });
  });

  it("does not call a deadline breached before the server has", () => {
    expect(describeDue(sla({ next_due_at: at(-5) }), NOW)).toMatchObject({
      label: "Overdue by 5 min",
      variant: "destructive",
    });
  });

  it("does not count down on a paused clock", () => {
    expect(describeDue(sla({ paused: true, next_due_at: null }), NOW)).toMatchObject({
      label: "Paused",
      variant: "muted",
    });
  });

  it("says met when every clock stopped on time, and breached when one did not", () => {
    expect(
      describeDue(
        sla({ next_due_at: null, response_state: "met", resolution_state: "met" }),
        NOW,
      ),
    ).toMatchObject({ label: "Met", variant: "success" });
    expect(
      describeDue(
        sla({
          next_due_at: null,
          response_state: "breached",
          resolution_state: "met",
          breached: true,
        }),
        NOW,
      ),
    ).toMatchObject({ label: "Breached", variant: "destructive" });
  });

  it("says nothing is owed on a void promise", () => {
    expect(
      describeDue(
        sla({ next_due_at: null, response_state: "void", resolution_state: "void" }),
        NOW,
      ),
    ).toMatchObject({ label: "No deadline", variant: "muted" });
  });
});

describe("canResolve", () => {
  it("offers Resolve from every state the server can resolve from", () => {
    for (const [type, status] of [
      ["incident", "new"],
      ["incident", "open"],
      ["incident", "pending"],
      ["request", "new"],
      ["request", "in_progress"],
    ] as const) {
      expect(canResolve(type, status)).toBe(true);
    }
  });

  it("offers nothing where resolving is not a legal next step", () => {
    for (const [type, status] of [
      ["incident", "resolved"],
      ["incident", "closed"],
      ["incident", "cancelled"],
      ["request", "pending_approval"],
      ["request", "fulfilled"],
      ["request", "rejected"],
    ] as const) {
      expect(canResolve(type, status)).toBe(false);
    }
  });
});

describe("defaultReplyVisibility", () => {
  it("is internal when nobody raised the ticket, so nothing is sent by accident", () => {
    expect(defaultReplyVisibility({ requester_id: null })).toBe("internal");
  });

  it("is public when there is a requester to answer", () => {
    expect(defaultReplyVisibility({ requester_id: "req-1" })).toBe("public");
  });
});

describe("describeTarget", () => {
  it("counts a running clock down, and never calls a passed one breached itself", () => {
    expect(describeTarget("running", at(90), NOW)).toEqual({ label: "Due in 1 h 30 min", variant: "muted" });
    expect(describeTarget("running", at(20), NOW)).toMatchObject({ variant: "warning" });
    expect(describeTarget("running", at(-20), NOW)).toEqual({
      label: "Overdue by 20 min",
      variant: "destructive",
    });
  });

  it("states a stopped clock's verdict", () => {
    expect(describeTarget("met", at(-5), NOW).label).toBe("Met");
    expect(describeTarget("breached", at(-5), NOW).label).toBe("Breached");
    expect(describeTarget("paused", at(5), NOW).label).toBe("Paused");
    expect(describeTarget("void", at(5), NOW).label).toBe("No deadline");
  });
});
