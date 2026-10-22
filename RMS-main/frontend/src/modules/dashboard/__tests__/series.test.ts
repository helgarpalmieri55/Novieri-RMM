import { describe, expect, it } from "vitest";

import { dailySeries, deviceKind, hasSignal } from "../series";

const DAY = 86_400_000;
// Mid-day, so a day boundary is never a rounding accident.
const NOW = Date.parse("2026-09-07T12:00:00Z");

describe("dailySeries", () => {
  it("buckets timestamps by UTC day, oldest first, today last", () => {
    const series = dailySeries(
      [
        new Date(NOW).toISOString(),
        new Date(NOW - 1 * DAY).toISOString(),
        new Date(NOW - 1 * DAY + 3_600_000).toISOString(),
        new Date(NOW - 13 * DAY).toISOString(),
      ],
      14,
      NOW,
    );
    expect(series).toHaveLength(14);
    expect(series[13]).toBe(1);
    expect(series[12]).toBe(2);
    expect(series[0]).toBe(1);
    expect(series.reduce((a, b) => a + b, 0)).toBe(4);
  });

  it("drops what is outside the window rather than piling it on an edge", () => {
    const series = dailySeries(
      [new Date(NOW - 14 * DAY).toISOString(), new Date(NOW + DAY).toISOString()],
      14,
      NOW,
    );
    expect(hasSignal(series)).toBe(false);
  });

  it("counts an absent or unparseable timestamp nowhere", () => {
    expect(hasSignal(dailySeries([null, undefined, "not a date"], 14, NOW))).toBe(false);
  });
});

describe("deviceKind", () => {
  it("reads offline, a revoked or an expired identity as needing a person", () => {
    expect(deviceKind({ agent_status: "offline", cert_state: "ok", cert_revoked: false })).toBe("crit");
    expect(deviceKind({ agent_status: "online", cert_state: "ok", cert_revoked: true })).toBe("crit");
    expect(deviceKind({ agent_status: "online", cert_state: "expired", cert_revoked: false })).toBe("crit");
  });

  it("keeps degraded, working, agentless and healthy apart", () => {
    expect(deviceKind({ agent_status: "online", cert_state: "expiring", cert_revoked: false })).toBe("warn");
    expect(deviceKind({ agent_status: "online", cert_state: "renewing", cert_revoked: false })).toBe("run");
    expect(deviceKind({ agent_status: null, cert_state: null, cert_revoked: false })).toBe("mut");
    expect(deviceKind({ agent_status: "online", cert_state: "ok", cert_revoked: false })).toBe("ok");
  });
});
