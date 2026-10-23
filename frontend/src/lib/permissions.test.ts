import { describe, expect, it } from "vitest";

import { permissionPhrase, permissionsSentence } from "./permissions";

describe("permission phrases", () => {
  it("says what a code lets a person do", () => {
    expect(permissionPhrase("devices.read")).toBe("view devices");
    expect(permissionsSentence(["devices.read", "patch.read"])).toBe(
      "permission to view devices and view updates",
    );
    expect(permissionsSentence(["ipam.manage"])).toBe("permission to manage the IP address plan");
  });

  it("never falls back to the dotted code", () => {
    expect(permissionPhrase("widgets.read")).toBe("view widgets");
    expect(permissionPhrase("email_intake.admin")).toBe("manage email intake");
    expect(permissionPhrase("email_intake.admin")).not.toMatch(/\./);
  });
});
