import { describe, expect, it } from "vitest";

import type { MeClient } from "../lib/api";
import { newMenuEntries, type NewMenuInput } from "./newMenu";

const A = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57";
const B = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const ALL = ["ticket.write", "clients.manage", "devices.read", "docs.manage", "change.request"];

function client(id: string, permissions: string[], kind: MeClient["kind"] = "customer"): MeClient {
  return { id, name: id, kind, enabled_modules: [], permissions };
}

function input(over: Partial<NewMenuInput> = {}): NewMenuInput {
  return {
    permissions: ALL,
    everywhere: ALL,
    clients: [client(A, ALL), client(B, ALL)],
    selected: null,
    modules: new Set(["change"]),
    ...over,
  };
}

describe("the New menu (redesign design §1.2: only real destinations, each with its permission)", () => {
  it("offers every destination, in the design's order, to someone who may create all of it", () => {
    expect(newMenuEntries(input()).map((entry) => [entry.kind, entry.to])).toEqual([
      ["ticket", "/service-desk?new=ticket"],
      ["client", "/clients?new=client"],
      ["contact", "/clients?new=contact"],
      ["device", "/devices/onboard"],
      ["document", "/docs?new=article"],
      ["change", "/changes?new=change"],
    ]);
  });

  it("offers nothing to someone who may create nothing, so there is no menu at all", () => {
    expect(newMenuEntries(input({ permissions: [], everywhere: [], clients: [client(A, [])] }))).toEqual([]);
  });

  it("offers a client only to someone who may manage clients everywhere, as the Clients page does", () => {
    const kinds = newMenuEntries(input({ everywhere: ["ticket.write"] })).map((entry) => entry.kind);
    expect(kinds).not.toContain("client");
  });

  it("takes a contact straight to the chosen client's contacts", () => {
    const contact = newMenuEntries(input({ selected: A })).find((entry) => entry.kind === "contact");
    expect(contact?.to).toBe(`/clients/${A}?tab=contacts&new=contact`);
  });

  it("offers no contact for a chosen client the person cannot manage, nor for Unassigned", () => {
    expect(newMenuEntries(input({ selected: A, clients: [client(A, ["clients.read"])] })).some((entry) => entry.kind === "contact")).toBe(false);
    expect(newMenuEntries(input({ clients: [client(A, ALL, "unassigned")] })).some((entry) => entry.kind === "contact")).toBe(false);
  });

  // One code at a time (Task 4 review, fix round 1): each entry needs its own.
  it.each([
    ["ticket", "ticket.write"],
    ["device", "devices.read"],
    ["document", "docs.manage"],
  ] as const)("offers no %s without %s", (kind, code) => {
    const without = ALL.filter((held) => held !== code);
    const kinds = newMenuEntries(input({ permissions: without, everywhere: without })).map((entry) => entry.kind);
    expect(kinds).not.toContain(kind);
    // NON-VACUITY: the rest of the menu is still there.
    expect(kinds.length).toBeGreaterThan(0);
  });

  it("offers a change only where the change module is on", () => {
    expect(newMenuEntries(input({ modules: new Set() })).some((entry) => entry.kind === "change")).toBe(false);
  });
});
