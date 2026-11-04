import { describe, expect, it } from "vitest";

import { groupGrantsByClient } from "../grantGroups";

const names = new Map([
  ["c-b", "Bodegas"],
  ["c-a", "Andes"],
]);
const name = (id: string) => names.get(id) ?? id;

describe("groupGrantsByClient", () => {
  it("puts the every-client grants first, then one group per client by name, codes sorted", () => {
    const groups = groupGrantsByClient(
      [
        { code: "ticket.read", via_role: "technician", client_id: "c-b" },
        { code: "devices.read", via_role: "technician", client_id: null },
        { code: "devices.manage", via_role: "technician", client_id: "c-b" },
        { code: "audit.read", via_role: "auditor", client_id: "c-a" },
      ],
      name,
    );
    expect(groups.map((group) => [group.clientId, group.grants.map((grant) => grant.code)])).toEqual([
      [null, ["devices.read"]],
      ["c-a", ["audit.read"]],
      ["c-b", ["devices.manage", "ticket.read"]],
    ]);
  });

  it("has no every-client group when nothing holds on every client", () => {
    expect(
      groupGrantsByClient([{ code: "x.read", via_role: "r", client_id: "c-a" }], name).map((g) => g.clientId),
    ).toEqual(["c-a"]);
    expect(groupGrantsByClient([], name)).toEqual([]);
  });
});
