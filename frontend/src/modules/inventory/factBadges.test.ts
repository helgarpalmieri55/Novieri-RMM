import { describe, expect, it } from "vitest";
import { factBadge } from "./factBadges";

describe("factBadge", () => {
  it("maps healthy facts to success", () => {
    expect(factBadge("disk_encryption", true)).toEqual({
      label: "Encrypted",
      variant: "success",
    });
    expect(factBadge("firewall_enabled", true)).toEqual({
      label: "Firewall on",
      variant: "success",
    });
    expect(factBadge("domain_joined", true)).toEqual({
      label: "Domain joined",
      variant: "success",
    });
  });

  it("maps unhealthy facts to warning", () => {
    expect(factBadge("disk_encryption", false)).toEqual({
      label: "Not encrypted",
      variant: "warning",
    });
    expect(factBadge("firewall_enabled", false)).toEqual({
      label: "Firewall off",
      variant: "warning",
    });
  });

  it("returns null for unknown keys and non-boolean values", () => {
    expect(factBadge("some_future_fact", true)).toBeNull();
    expect(factBadge("disk_encryption", "yes")).toBeNull();
  });
});
