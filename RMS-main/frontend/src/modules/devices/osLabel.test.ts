import { describe, expect, it } from "vitest";

import { osFamilyLabel, osLabel } from "./osLabel";

describe("osLabel", () => {
  it("writes the family as the vendor spells it, not as the wire stores it", () => {
    // The four the fleet list actually rendered: "macos 15.6",
    // "windows Server 2022", "linux Ubuntu 24.04", "linux Fedora 42".
    expect(osLabel("macos", "15.6")).toBe("macOS 15.6");
    expect(osLabel("windows", "Server 2022")).toBe("Windows Server 2022");
    expect(osLabel("linux", "Ubuntu 24.04")).toBe("Linux Ubuntu 24.04");
  });

  it("is null when there is no family, so the caller can say 'not known'", () => {
    // A switch is none of windows, linux or macos, and "undefined 11 23H2"
    // says something false about it.
    expect(osLabel(null, null)).toBeNull();
    expect(osLabel("", "15.6")).toBeNull();
  });

  it("renders a family without a version, and a family it has never seen", () => {
    expect(osLabel("windows", null)).toBe("Windows");
    expect(osLabel("freebsd", "14.1")).toBe("freebsd 14.1");
    expect(osFamilyLabel("freebsd")).toBe("freebsd");
  });
});
