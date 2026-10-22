import { describe, expect, it } from "vitest";
import {
  ACTION_STATE_PRESENTATION,
  ALERT_STATE_PRESENTATION,
  CHECK_STATE_PRESENTATION,
  CLEARED_REASON_PRESENTATION,
  AUTONOMY_PRESENTATION,
  blockedReason,
  checkLabel,
  checkStateBands,
  countsAsResolved,
  describeAlertStatus,
  describeDeviceHealth,
  describeDuration,
  describeObservation,
  describeThreshold,
  isAlertOpen,
  isAutonomous,
  isMachineResolved,
  severityLabel,
  summariseAlerts,
  unansweredFamilies,
} from "../healthState";
import type { AlertOut, DeviceHealthOut } from "../types";

// The presentation rules, tested as DISTINCTIONS rather than as labels.
//
// This file deliberately does NOT assert `Object.keys(SOME_MAP)` over a map
// defined in the same source tree: such a test cannot fail, and this project has
// shipped one. Every assertion below is of the form "these two inputs must
// produce DIFFERENT observable values", which is exactly what a renderer that
// collapsed them would break.

function alert(overrides: Partial<AlertOut> = {}): AlertOut {
  return {
    id: "a1",
    device_id: "d1",
    device_hostname: "WS-01",
    device_group_id: null,
    check_id: "c1",
    profile_id: "p1",
    check_type: "cpu.busy_pct",
    selector: "",
    severity: "warning",
    state: "active",
    observed_value: "97.4",
    threshold_display: "95",
    opened_at: "2026-03-01T09:00:00.000Z",
    opened_policy_version: 1,
    last_seen_at: "2026-03-01T09:05:00.000Z",
    occurrence_count: 2,
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

function device(overrides: Partial<DeviceHealthOut> = {}): DeviceHealthOut {
  return {
    device_id: "d1",
    hostname: "WS-01",
    os_family: "windows",
    has_profile: true,
    profile: {
      id: "p1",
      name: "Fleet baseline",
      description: "",
      tick_interval_s: 300,
      posture_interval_s: 3600,
      summary_interval_s: 1800,
      active: true,
      policy_version: 3,
      check_count: 2,
      assignment_count: 1,
      created_at: "2026-01-01T00:00:00.000Z",
      updated_at: "2026-01-01T00:00:00.000Z",
    },
    assignment: {
      id: "as1",
      profile_id: "p1",
      profile_name: "Fleet baseline",
      device_id: null,
      device_group_id: "g1",
      level: "group",
      created_by: null,
      created_at: "2026-01-01T00:00:00.000Z",
    },
    resolution_level: "group",
    acknowledged_policy_version: 3,
    policy_drift: false,
    checks: [],
    open_alerts: [],
    agent_last_seen_at: "2026-03-01T09:05:00.000Z",
    agent_online: true,
    offline_after_s: 900,
    ...overrides,
  };
}

describe("alert state", () => {
  it("keeps acknowledged, muted and cleared apart", () => {
    // The whole point: three states, three DIFFERENT observable answers to "is
    // this still an open incident?". A renderer that treated acknowledging or
    // muting as handling would make the first two agree with the third.
    expect(isAlertOpen(alert({ state: "acknowledged" }))).toBe(true);
    expect(isAlertOpen(alert({ state: "muted" }))).toBe(true);
    expect(isAlertOpen(alert({ state: "cleared" }))).toBe(false);

    const acknowledged = describeAlertStatus(alert({ state: "acknowledged" }));
    const muted = describeAlertStatus(alert({ state: "muted" }));
    const cleared = describeAlertStatus(
      alert({ state: "cleared", cleared_reason: "recovery" }),
    );
    expect(new Set([acknowledged, muted, cleared]).size).toBe(3);
  });

  it("says what acknowledging does NOT do", () => {
    // Spec §0.8: acknowledging does not stop the check, does not prevent
    // auto-clear and does not cancel a remediation in flight. That sentence is
    // the content of the state, so it is asserted rather than assumed.
    const meaning = ALERT_STATE_PRESENTATION.acknowledged.meaning.toLowerCase();
    expect(meaning).toContain("still open");
    expect(ALERT_STATE_PRESENTATION.muted.meaning.toLowerCase()).toContain("not resolved");
  });

  // GLOSSARY (`docs/design/settings-language.md`): "remediation" is the
  // engine's word; the operator-facing word is "fix". Same for
  // "suppress(es)" -> "pause(s)". This is rendered as the panel's subtitle
  // (`AlertDetail.tsx`), so it is exactly the kind of operator-facing text
  // the glossary governs.
  it("says 'fix' and 'pauses', not 'remediation' and 'suppresses', in what acknowledging does not do", () => {
    const meaning = ALERT_STATE_PRESENTATION.acknowledged.meaning;
    expect(meaning).toMatch(/fix/i);
    expect(meaning).not.toMatch(/remediation/i);
    expect(meaning).not.toMatch(/suppress/i);
  });

  // GLOSSARY: "flap/flapping" -> "repeating", "suppression" -> "paused". A
  // blocked action's label and reason are rendered verbatim as a badge and a
  // sub-line (`AlertDetail.tsx`'s `ActionRow`).
  it("names a blocked-by-repeat action as 'repeating' and 'paused', not 'flapping' and 'suppressed'", () => {
    const blocked = ACTION_STATE_PRESENTATION.blocked_flapping;
    expect(blocked.label).toBe("Blocked (repeating)");
    expect(blocked.label).not.toMatch(/flapping/i);
    expect(blocked.meaning).toMatch(/repeating/i);
    expect(blocked.meaning).toMatch(/paused/i);
    expect(blocked.meaning).not.toMatch(/flapping|suppress/i);
  });

  it("distinguishes clear reasons rather than showing only 'cleared'", () => {
    const recovered = describeAlertStatus(
      alert({ state: "cleared", cleared_reason: "recovery" }),
    );
    const removed = describeAlertStatus(
      alert({ state: "cleared", cleared_reason: "policy_removed" }),
    );
    // "It recovered" and "someone deleted the check" are different outcomes.
    expect(recovered).not.toEqual(removed);
  });
});

describe("resolution counting (spec §7.4)", () => {
  it("excludes unavailable_timeout, policy_removed and device_inactive", () => {
    for (const reason of ["unavailable_timeout", "policy_removed", "device_inactive"] as const) {
      expect(countsAsResolved(alert({ state: "cleared", cleared_reason: reason }))).toBe(false);
      expect(CLEARED_REASON_PRESENTATION[reason].countsAsResolution).toBe(false);
    }
    // ... while a genuine recovery does count, so "excludes everything" fails
    // this test too.
    expect(countsAsResolved(alert({ state: "cleared", cleared_reason: "recovery" }))).toBe(true);
  });

  it("separates machine-resolved from a human closing the row", () => {
    expect(isMachineResolved(alert({ state: "cleared", cleared_reason: "recovery" }))).toBe(true);
    expect(isMachineResolved(alert({ state: "cleared", cleared_reason: "manual" }))).toBe(false);
    // A manual close is still a resolution of the incident -- it is counted,
    // just not as a machine one.
    expect(countsAsResolved(alert({ state: "cleared", cleared_reason: "manual" }))).toBe(true);
  });

  it("an open alert has resolved nothing", () => {
    expect(countsAsResolved(alert({ state: "acknowledged" }))).toBe(false);
    expect(countsAsResolved(alert({ state: "muted" }))).toBe(false);
  });

  it("summarises volume, MTTR and the split with the exclusions applied", () => {
    // The fixture deliberately contains one of EACH interesting kind: without
    // the excluded rows an implementation that counted everything would pass,
    // and without the manual close one that counted all clears as machine
    // resolutions would pass.
    const rows = [
      alert({ id: "1", state: "active" }),
      alert({ id: "2", state: "muted" }),
      alert({
        id: "3",
        state: "cleared",
        cleared_reason: "recovery",
        opened_at: "2026-03-01T09:00:00.000Z",
        cleared_at: "2026-03-01T09:10:00.000Z",
      }),
      alert({
        id: "4",
        state: "cleared",
        cleared_reason: "manual",
        opened_at: "2026-03-01T09:00:00.000Z",
        cleared_at: "2026-03-01T09:30:00.000Z",
      }),
      alert({
        id: "5",
        state: "cleared",
        cleared_reason: "unavailable_timeout",
        opened_at: "2026-03-01T09:00:00.000Z",
        cleared_at: "2026-03-02T09:00:00.000Z",
      }),
    ];
    const summary = summariseAlerts(rows);
    expect(summary.total).toBe(5);
    expect(summary.open).toBe(2);
    expect(summary.machineResolved).toBe(1);
    expect(summary.humanClosed).toBe(1);
    expect(summary.excludedFromResolution).toBe(1);
    expect(summary.resolved).toBe(2);
    // (600 s + 1800 s) / 2. The 24-hour `unavailable_timeout` row is excluded:
    // including it would push MTTR to over 8 hours, which is the exact way a
    // conflating dashboard lies.
    expect(summary.meanTimeToResolveS).toBe(1200);
  });
});

describe("check state on a device", () => {
  it("gives unavailable and unknown their own bands", () => {
    const bands = checkStateBands([
      { state: "ok" },
      { state: "ok" },
      { state: "unavailable" },
      { state: "unknown" },
      { state: "breaching" },
    ]);
    expect(bands).toEqual({ ok: 2, breaching: 1, unavailable: 1, unknown: 1 });
  });

  it("renders unavailable as neither healthy nor alarming, and unknown as neither", () => {
    // Four states, four DIFFERENT badge variants. If `unavailable` folded into
    // `ok` (or into `breaching`), this set would shrink.
    const variants = new Set(
      (["ok", "breaching", "unavailable", "unknown"] as const).map(
        (state) => CHECK_STATE_PRESENTATION[state].variant,
      ),
    );
    expect(variants.size).toBe(4);
    expect(CHECK_STATE_PRESENTATION.unavailable.variant).not.toBe(
      CHECK_STATE_PRESENTATION.ok.variant,
    );
    expect(CHECK_STATE_PRESENTATION.unavailable.variant).not.toBe(
      CHECK_STATE_PRESENTATION.breaching.variant,
    );
  });
});

describe("device headline", () => {
  it("never says healthy for a device with no profile", () => {
    const headline = describeDeviceHealth(device({ has_profile: false, profile: null }));
    expect(headline.label).toMatch(/no health profile/i);
    expect(headline.label).not.toMatch(/healthy/i);
    expect(headline.detail).toMatch(/not the same as healthy/i);
  });

  it("never says healthy for a device that has gone quiet", () => {
    const quiet = describeDeviceHealth(
      device({ agent_online: false, checks: [{ state: "ok" } as never] }),
    );
    expect(quiet.label).toMatch(/offline/i);
    expect(quiet.label).not.toMatch(/healthy/i);
    // ... and the SAME device reporting IS healthy, so "never says healthy" is
    // not what makes the assertion above pass.
    const live = describeDeviceHealth(
      device({ agent_online: true, checks: [{ state: "ok" } as never] }),
    );
    expect(live.label).toMatch(/healthy/i);
  });

  it("keeps an offline device's open alerts as open alerts", () => {
    const headline = describeDeviceHealth(
      device({ agent_online: false, open_alerts: [alert({ state: "acknowledged" })] }),
    );
    // An open alert outranks "offline": the alert did not clear because the
    // device went quiet (spec §0.6).
    expect(headline.label).toMatch(/1 open alert/);
    expect(headline.detail).toMatch(/do not clear/i);
  });

  it("does not call a partially-answered device healthy", () => {
    const headline = describeDeviceHealth(
      device({ checks: [{ state: "ok" }, { state: "unavailable" }] as never }),
    );
    expect(headline.label).not.toMatch(/healthy/i);
  });
});

describe("durations (spec §0.3)", () => {
  it("renders both check count and wall-clock, and says how often the profile checks", () => {
    const text = describeDuration({ for_ticks: 3, for_seconds: 900, tick_interval_s: 300 });
    expect(text).toContain("3 checks in a row");
    expect(text).not.toMatch(/tick/);
    expect(text).toContain("15 min");
    expect(text).toContain("5 min");
  });

  it("changes when the profile's tick changes, for the same tick count", () => {
    // The whole reason both numbers are rendered: the same `for_ticks` means a
    // different duration on a different profile.
    const fast = describeDuration({ for_ticks: 3, for_seconds: 180, tick_interval_s: 60 });
    const slow = describeDuration({ for_ticks: 3, for_seconds: 900, tick_interval_s: 300 });
    expect(fast).not.toEqual(slow);
  });
});

describe("per-OS availability", () => {
  it("names the families that cannot answer an any-OS check", () => {
    expect(
      unansweredFamilies({ os_family: null, available_os_families: ["windows"] }),
    ).toEqual(["linux", "macos"]);
    expect(
      unansweredFamilies({
        os_family: null,
        available_os_families: ["windows", "linux", "macos"],
      }),
    ).toEqual([]);
  });

  it("flags a check pinned to a family that cannot answer it", () => {
    expect(
      unansweredFamilies({ os_family: "linux", available_os_families: ["windows"] }),
    ).toEqual(["linux"]);
  });
});

describe("actions", () => {
  it("returns the server's blocked reason verbatim, and only for blocked rows", () => {
    const fuse =
      "max_per_org_per_hour: 20 script.run job(s) already ran across this org in the last hour";
    expect(blockedReason({ state: "blocked_rate_limit", last_error: fuse })).toBe(fuse);
    // A non-blocked row has no "blocked" reason even when it carries an error.
    expect(blockedReason({ state: "dead", last_error: "boom" })).toBeNull();
  });

  it("marks an autonomous action distinctly", () => {
    expect(isAutonomous({ autonomy: "autonomous" })).toBe(true);
    expect(isAutonomous({ autonomy: "require_approval" })).toBe(false);
    expect(isAutonomous({ autonomy: "suggest" })).toBe(false);
  });
});

describe("severity, thresholds and observations", () => {
  it("names a severity in the same words the state pill beside it uses", () => {
    // The console drew "critical" / "warning" / "info" next to "Active" and
    // "Acknowledged": one row, two alphabets. The assertion is that the label
    // is NOT the wire word, which is what a renderer printing `alert.severity`
    // would fail.
    for (const severity of ["critical", "warning", "info"] as const) {
      expect(severityLabel(severity)).not.toBe(severity);
      expect(severityLabel(severity)).toBe(
        severity.charAt(0).toUpperCase() + severity.slice(1),
      );
    }
  });

  it("says what a check compares without printing the comparator", () => {
    expect(describeThreshold("gt", "90")).toBe("over 90");
    expect(describeThreshold("lte", "5")).toBe("at or under 5");
    expect(describeThreshold("is", "running")).toBe("expected running");
    for (const rendered of [
      describeThreshold("gt", "90"),
      describeThreshold("gte", "90"),
      describeThreshold("lt", "90"),
      describeThreshold("lte", "90"),
      describeThreshold("eq", "90"),
      describeThreshold("neq", "90"),
      describeThreshold("is", "true"),
    ]) {
      expect(rendered).not.toMatch(/\b(gt|gte|lt|lte|eq|neq|is)\b/);
    }
  });

  it("reads an alert's observation as a sentence, comparator and all", () => {
    expect(describeObservation({ observed_value: "96", threshold_display: "gt 95" })).toBe(
      "96 over 95",
    );
    expect(describeObservation({ observed_value: "3.1%", threshold_display: "lt 5" })).toBe(
      "3.1% under 5",
    );
    // An expectation is two clauses, not one: "stopped expected running" reads
    // as a typo.
    expect(
      describeObservation({ observed_value: "stopped", threshold_display: "is running" }),
    ).toBe("stopped, expected running");
  });

  it("never begins with its own operator when nothing was observed", () => {
    // An alert cleared by `unavailable_timeout` never got a value. This cell
    // read "vs is true".
    const rendered = describeObservation({ observed_value: "", threshold_display: "is true" });
    expect(rendered).toBe("Nothing observed, expected true");
    expect(rendered.startsWith("vs")).toBe(false);
  });

  it("degrades to prose rather than to nonsense on a format it does not know", () => {
    // The comparator is parsed out of a server-built string. A format this
    // file does not recognise is read as prose, not mangled.
    expect(describeObservation({ observed_value: "97.4", threshold_display: "95" })).toBe(
      "97.4 against 95",
    );
    expect(describeObservation({ observed_value: "97.4", threshold_display: "" })).toBe("97.4");
    expect(describeObservation({ observed_value: "", threshold_display: "" })).toBe(
      "Nothing observed",
    );
  });
});

describe("the words on the row are the console's own, not the wire's", () => {
  it("names a check as a phrase a person reads, not as its registry key", () => {
    expect(checkLabel("disk.free_pct")).toBe("Disk free");
    expect(checkLabel("cpu.busy_pct")).toBe("CPU busy");
    expect(checkLabel("posture.antivirus_healthy")).toBe("Antivirus health");
    for (const type of [
      "cpu.busy_pct",
      "memory.used_pct",
      "disk.free_pct",
      "agent.offline",
      "posture.antivirus_healthy",
      "service.running",
      "cert.days_left",
      // The rest of the server's registry (backend/app/modules/health/registry.py),
      // including the checks the default baseline profile carries.
      "disk.free_bytes",
      "disk.smart_ok",
      "process.present",
      "service.state",
      "posture.disk_encryption",
      "posture.firewall_enabled",
      "eventlog.disk_error",
      "eventlog.service_crash",
      "eventlog.kernel_fault",
      "backup.success_age_h",
      "backup.last_run_failed",
      "backup.offsite_lag_h",
      "backup.restore_test_age_d",
      "script.policy_failed",
    ]) {
      // The distinction, not the spelling: no label is still the key.
      expect(checkLabel(type)).not.toBe(type);
      expect(checkLabel(type)).not.toMatch(/[._]/);
    }
  });

  it("returns a type it does not know verbatim, rather than guessing at it", () => {
    // The registry is closed today. A name added to it later reads as its own
    // key -- which is honest -- and never as this file's theory of what the
    // key meant.
    expect(checkLabel("gpu.temp_c")).toBe("gpu.temp_c");
  });

  it("does not print source formatting at a reader", () => {
    // `health.remediate` is a permission code. In backticks it is a markdown
    // artefact rendered literally on the page.
    for (const entry of Object.values(AUTONOMY_PRESENTATION)) {
      expect(entry.meaning).not.toContain("`");
    }
    expect(AUTONOMY_PRESENTATION.require_approval.meaning).toContain("health.remediate");
  });
});
