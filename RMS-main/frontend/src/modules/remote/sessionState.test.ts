import { describe, expect, it } from "vitest";
import { consentModeLabel, formatDuration, remoteStateVariant } from "./sessionState";

describe("remoteStateVariant", () => {
  it("maps known states to their badge variant", () => {
    expect(remoteStateVariant("awaiting_consent")).toBe("warning");
    expect(remoteStateVariant("connecting")).toBe("info");
    expect(remoteStateVariant("active")).toBe("success");
    expect(remoteStateVariant("ended")).toBe("muted");
  });

  it("falls back to muted for unknown states", () => {
    expect(remoteStateVariant("some_future_state")).toBe("muted");
  });
});

describe("consentModeLabel", () => {
  it("labels attended and unattended modes", () => {
    expect(consentModeLabel("attended")).toBe("Attended");
    expect(consentModeLabel("unattended")).toBe("Unattended");
  });
});

describe("formatDuration", () => {
  it("returns an em dash when the session has not started", () => {
    expect(formatDuration(null, null)).toBe("—");
  });

  it("formats elapsed minutes and seconds between start and end", () => {
    const started = "2026-01-01T00:00:00.000Z";
    const ended = "2026-01-01T00:02:05.000Z";
    expect(formatDuration(started, ended)).toBe("2m 05s");
  });
});
