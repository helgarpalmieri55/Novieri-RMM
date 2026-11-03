/**
 * The fixture proxy's three promises: a registered method answers with a
 * fresh copy, an unregistered one rejects with `FixtureMissing` and is
 * recorded on `window.__fixtureMisses`, and the registry finds every module's
 * `fixtures.ts` beside `core.ts`.
 */
import { afterEach, describe, expect, it } from "vitest";

import { FixtureMissing, fixtureClient, fixtureRegistry } from "./client";
import { FIXTURE_MODE } from "./mode";

afterEach(() => {
  delete window.__fixtureMisses;
});

describe("fixtureClient", () => {
  it("answers a registered method with a copy of the fixture's result", async () => {
    const shared = { id: "org-1", name: "Toeshee", slug: "toeshee", status: "active", settings: {} };
    const api = fixtureClient({ getCurrentOrg: () => shared });
    const first = await api.getCurrentOrg();
    const second = await api.getCurrentOrg();
    expect(first).toEqual(shared);
    expect(first).not.toBe(shared);
    expect(first).not.toBe(second);
  });

  it("passes the call's arguments to the fixture", async () => {
    const seen: string[] = [];
    const api = fixtureClient({
      getDevice: (id) => {
        seen.push(id);
        return { ...deviceStub, id };
      },
    });
    const device = await api.getDevice("dev-9");
    expect(seen).toEqual(["dev-9"]);
    expect(device.id).toBe("dev-9");
  });

  it("rejects an unregistered method with FixtureMissing and records the miss once", async () => {
    const api = fixtureClient({});
    await expect(api.listJobs()).rejects.toBeInstanceOf(FixtureMissing);
    await expect(api.listJobs()).rejects.toThrow(/ApiClient\.listJobs\(\)/);
    await expect(api.getDevice("dev-1")).rejects.toMatchObject({ method: "getDevice" });
    expect(window.__fixtureMisses).toEqual(["listJobs", "getDevice"]);
  });

  it("turns a fixture that throws synchronously into a rejection", async () => {
    const api = fixtureClient({
      getDevice: () => {
        throw new Error("no such device");
      },
    });
    await expect(api.getDevice("dev-404")).rejects.toThrow("no such device");
    expect(window.__fixtureMisses).toEqual([]);
  });

  it("is not a thenable", () => {
    const api = fixtureClient({}) as unknown as { then?: unknown };
    expect(api.then).toBeUndefined();
  });
});

describe("fixtureRegistry", () => {
  it("carries the shell's reads from core.ts", () => {
    const registry = fixtureRegistry();
    for (const name of [
      "me",
      "getCurrentOrg",
      "getHealthAlertSummary",
      "getPlatformStatus",
      "getDashboardLayout",
      "listDevices",
      "getDevice",
    ] as const) {
      expect(typeof registry[name], `core.ts has no ${name}`).toBe("function");
    }
  });

  it("signs the fixture session in with every permission the nav gates on", async () => {
    const { navItems } = await import("../shell/nav");
    const me = await fixtureClient().me();
    const held = new Set(me.permissions);
    for (const item of navItems()) {
      if (item.permission) expect(held, `me lacks ${item.permission}`).toContain(item.permission);
    }
    expect(held).toContain("ai.use");
    expect(held).toContain("platform.status.read");
  });

  it("signs the fixture session in on every client, listing the clients the settings fixtures list", async () => {
    const me = await fixtureClient().me();
    expect(me.permissions_everywhere).toEqual(me.permissions);
    expect((me.clients ?? []).map((client) => [client.id, client.kind])).toEqual([
      ["org-toeshee", "internal"],
      ["cli-logistics", "customer"],
      ["cli-unassigned", "unassigned"],
    ]);
  });
});

describe("FIXTURE_MODE", () => {
  it("is off in a plain test environment with no flag in the URL or the session", () => {
    expect(FIXTURE_MODE).toBe(false);
  });
});

const deviceStub = {
  id: "dev-1",
  hostname: "x",
  fqdn: null,
  os_family: null,
  os_version: "",
  architecture: "",
  device_group_id: null,
  status: "enrolled",
  agent_status: null,
  last_seen_at: null,
  cert_not_after: null,
  cert_state: null,
  cert_revoked: false,
  lifecycle: "in_service" as const,
  asset_type: "other" as const,
  source: "manual" as const,
  provenance: "operator" as const,
  description: null,
  primary_ip: null,
  owner: null,
  location: null,
};
