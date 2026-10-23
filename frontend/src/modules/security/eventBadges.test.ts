import { describe, expect, it } from "vitest";
import { eventBadgeVariant } from "./eventBadges";

describe("eventBadgeVariant", () => {
  it("is total over the v1 taxonomy", () => {
    expect(eventBadgeVariant("logon_failure")).toBe("destructive");
    expect(eventBadgeVariant("account_lockout")).toBe("destructive");
    expect(eventBadgeVariant("privilege_elevation")).toBe("warning");
    expect(eventBadgeVariant("admin_group_change")).toBe("warning");
    expect(eventBadgeVariant("policy_change")).toBe("warning");
    expect(eventBadgeVariant("logon_success")).toBe("muted");
    expect(eventBadgeVariant("logoff")).toBe("muted");
  });

  it("defaults unknown types to muted", () => {
    expect(eventBadgeVariant("future_event_type")).toBe("muted");
  });
});
