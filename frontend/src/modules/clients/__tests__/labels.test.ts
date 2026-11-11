import { describe, expect, it } from "vitest";

import type { AssignmentOut } from "../../health/types";
import { clientHealthDefault, clientKindLabel, parseDomains } from "../labels";

const assignment = (over: Partial<AssignmentOut>): AssignmentOut => ({
  id: "a",
  profile_id: "p",
  profile_name: "Baseline",
  device_id: null,
  device_group_id: null,
  client_id: null,
  level: "org",
  created_by: null,
  created_at: "2026-10-01T00:00:00Z",
  ...over,
});

describe("parseDomains", () => {
  it("takes one domain per line or comma, blanks dropped", () => {
    expect(parseDomains(" andes.example \n\nbodega.example, otra.example ")).toEqual([
      "andes.example",
      "bodega.example",
      "otra.example",
    ]);
    expect(parseDomains("   ")).toEqual([]);
  });
});

describe("clientHealthDefault", () => {
  it("finds the client's own default and the organisation's, and no other client's", () => {
    const rows = [
      assignment({ id: "org", level: "org", profile_name: "Baseline" }),
      assignment({ id: "mine", level: "client", client_id: "c-andes", profile_name: "Servers" }),
      assignment({ id: "theirs", level: "client", client_id: "c-other", profile_name: "Kiosks" }),
      assignment({ id: "group", level: "group", client_id: "c-andes", device_group_id: "g1" }),
    ];
    expect(clientHealthDefault(rows, "c-andes")).toEqual({ own: rows[1], org: rows[0] });
    expect(clientHealthDefault(rows, "c-none")).toEqual({ own: null, org: rows[0] });
    expect(clientHealthDefault([], "c-andes")).toEqual({ own: null, org: null });
  });
});

describe("clientKindLabel", () => {
  it("words the three kinds and passes an unknown one through", () => {
    expect(clientKindLabel("customer")).toBe("Customer");
    expect(clientKindLabel("internal")).toBe("Your own IT");
    expect(clientKindLabel("unassigned")).toBe("Unassigned");
    expect(clientKindLabel("partner")).toBe("partner");
  });
});
