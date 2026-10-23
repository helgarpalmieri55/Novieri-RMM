import { describe, expect, it } from "vitest";

import { ApiError } from "../../../lib/api";
import {
  READINESS_CHIPS,
  countsSentence,
  isListChanged,
  mayCancel,
  mayResume,
  outcomeLabel,
  readinessChip,
  runIsLive,
  runProgress,
  runReasonLabel,
} from "../runState";
import type { PushRunCounts } from "../types";

const counts = (over: Partial<PushRunCounts> = {}): PushRunCounts => ({
  waiting: 0,
  in_flight: 0,
  installed: 0,
  failed: 0,
  refused: 0,
  cancelled: 0,
  ...over,
});

describe("run presentation", () => {
  it("counts everything terminal as progress, and nothing else", () => {
    expect(runProgress(counts({ installed: 2, failed: 1, refused: 1, waiting: 4, in_flight: 2 }), 10)).toBe(40);
    expect(runProgress(counts(), 0)).toBe(0);
  });

  it("is live while running, or while installs are still in flight", () => {
    expect(runIsLive({ state: "running", counts: counts() })).toBe(true);
    expect(runIsLive({ state: "cancelled", counts: counts({ in_flight: 1 }) })).toBe(true);
    expect(runIsLive({ state: "paused", counts: counts() })).toBe(false);
    expect(runIsLive({ state: "completed", counts: counts() })).toBe(false);
    // A stopped run still settles installs in flight, then goes quiet.
    expect(runIsLive({ state: "stopped_credential", counts: counts({ in_flight: 1 }) })).toBe(true);
    expect(runIsLive({ state: "stopped_credential", counts: counts() })).toBe(false);
  });

  it("lets a person resume a paused or stopped run, and cancel any active one", () => {
    expect(mayResume("paused")).toBe(true);
    expect(mayResume("stopped_credential")).toBe(true);
    expect(mayResume("running")).toBe(false);
    expect(mayResume("cancelled")).toBe(false);
    expect(mayCancel("stopped_credential")).toBe(true);
    expect(mayCancel("completed")).toBe(false);
  });

  it("gives every blocking reason the server can name a short chip", () => {
    for (const code of [
      "credential_not_scoped",
      "no_site_server",
      "site_server_too_old",
      "no_winrm_site_server",
      "no_dns_name",
      "invalid_dns_name",
      "wrong_credential_kind",
      "no_console_url",
      "outside_realm",
      "realm_not_recorded",
      "host_key_unconfirmed",
      "host_key_changed",
      "not_installable",
    ]) {
      expect(READINESS_CHIPS[code], code).toBeTruthy();
    }
    expect(readinessChip("host_key_unconfirmed")).toBe("Not probed");
    expect(readinessChip("something_new")).toBe("Not ready");
  });

  it("says a run's own refusals and outcomes in words, never as codes", () => {
    expect(runReasonLabel("already_enrolled")).not.toContain("_");
    expect(runReasonLabel("credential_missing")).not.toContain("_");
    expect(outcomeLabel("auth_failed")).toBe("The credential was refused");
    expect(outcomeLabel("never_seen")).toBe("Finished");
    expect(countsSentence(counts({ installed: 3, failed: 1, waiting: 2, in_flight: 1 }))).toBe(
      "3 installed · 1 failed · 0 refused · 3 to go",
    );
  });

  it("recognises the server's list-changed conflict and nothing else", () => {
    expect(isListChanged(new ApiError(409, JSON.stringify({ detail: { code: "list_changed", message: "x" } })))).toBe(true);
    expect(isListChanged(new ApiError(409, JSON.stringify({ detail: "one active run per organization" })))).toBe(false);
    expect(isListChanged(new ApiError(500, "boom"))).toBe(false);
    expect(isListChanged(new Error("nope"))).toBe(false);
  });
});
