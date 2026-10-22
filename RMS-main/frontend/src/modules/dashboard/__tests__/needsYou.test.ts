import { describe, expect, it } from "vitest";

import type { AlertOut } from "../../health/types";
import { needsYou } from "../needsYou";

function alert(overrides: Partial<AlertOut> = {}): AlertOut {
  return {
    id: "a1",
    device_id: "d1",
    device_hostname: "db-01",
    device_group_id: null,
    check_id: "c1",
    profile_id: "p1",
    check_type: "disk.free_pct",
    selector: "C:",
    severity: "critical",
    state: "active",
    observed_value: "3.1",
    threshold_display: "10",
    opened_at: "2026-09-23T00:00:00Z",
    opened_policy_version: 1,
    last_seen_at: "2026-09-23T00:00:00Z",
    occurrence_count: 1,
    flapping: false,
    flap_count: 0,
    escalated_at: null,
    acknowledged_at: null,
    acknowledged_by: null,
    muted_until: null,
    muted_by: null,
    cleared_at: null,
    cleared_reason: null,
    cleared_by: null,
    ticket_id: null,
    previous_alert_id: null,
    ...overrides,
  };
}

describe("needsYou", () => {
  it("is empty when nothing is wrong", () => {
    expect(
      needsYou({
        alerts: [],
        failedJobs: 0,
        stuckJobs: 0,
        fleet: { managed: 3, online: 3, cert_attention: 0 },
        untriagedTickets: 0,
        unverifiedBackups: 0,
      }),
    ).toEqual([]);
  });

  it("is empty when nothing could be read, rather than inventing zeros", () => {
    expect(needsYou({})).toEqual([]);
  });

  it("ranks critical, then warning, then routine work", () => {
    const items = needsYou({
      alerts: [alert({ id: "w1", severity: "warning" }), alert({ id: "c1" })],
      failedJobs: 2,
      stuckJobs: 1,
      fleet: { managed: 10, online: 7, cert_attention: 1 },
      untriagedTickets: 4,
      unverifiedBackups: 1,
    });
    expect(items.map((item) => item.kind)).toEqual([
      "crit",
      "crit",
      "warn",
      "warn",
      "warn",
      "warn",
      "info",
      "info",
    ]);
    expect(items.map((item) => item.title)).toEqual([
      "Disk free on db-01",
      "2 jobs failed",
      "3 devices are offline",
      "1 warning alert",
      "1 job is waiting too long",
      "1 device certificate needs attention",
      "4 new tickets to triage",
      "1 backup is not verified",
    ]);
  });

  it("links each item to the page where it is handled", () => {
    const items = needsYou({
      alerts: [alert({ id: "c 1" })],
      fleet: { managed: 2, online: 1, cert_attention: 0 },
    });
    expect(items.map((item) => item.to)).toEqual(["/health?alert=c%201", "/devices?state=offline"]);
  });

  it("names the first few critical alerts and sums the rest", () => {
    const items = needsYou({
      alerts: ["a", "b", "c", "d", "e"].map((id) => alert({ id })),
    });
    expect(items).toHaveLength(4);
    expect(items[3]?.title).toBe("2 more critical alerts");
  });
});
