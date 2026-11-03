/**
 * `X-Client-Id` (phase 3 design §1.2). The client chosen in the top bar
 * narrows every request this console sends -- the JSON ones through
 * `request()` and the raw file ones alike -- and a route of global
 * configuration never carries it: with it, the server answers 403
 * (`backend/tests/test_global_writes.py`). The list of those routes is
 * `globalRoutes.json`, generated from the backend's (Task 2), and matched by
 * method as well as path: `PATCH /orgs/current` is global, `GET /orgs/current`
 * is not.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient, requestHeaders } from "./api";
import { selectedClient, setSelectedClient } from "./clientChoice";

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";

function stubFetch(): ReturnType<typeof vi.fn> {
  const fetchMock = vi
    .fn()
    .mockImplementation(() => Promise.resolve(new Response("{}", { status: 200 })));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** The headers of the nth `fetch` call. */
function sent(fetchMock: ReturnType<typeof vi.fn>, call = 0): Headers {
  return new Headers((fetchMock.mock.calls[call] as [string, RequestInit])[1].headers);
}

function narrowedTo(client: string | null): ApiClient {
  return new ApiClient(() => "t", vi.fn(), "/api/v1", () => client);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("X-Client-Id", () => {
  it("names the client chosen in the top bar", async () => {
    const fetchMock = stubFetch();
    await narrowedTo(ACME).listDevices();
    expect(sent(fetchMock).get("X-Client-Id")).toBe(ACME);
  });

  it("is not sent with every client", async () => {
    const fetchMock = stubFetch();
    await narrowedTo(null).listDevices();
    expect(sent(fetchMock).has("X-Client-Id")).toBe(false);
  });

  it("is read on every request, not when the client was built", async () => {
    const fetchMock = stubFetch();
    let chosen: string | null = null;
    const api = new ApiClient(() => "t", vi.fn(), "/api/v1", () => chosen);
    await api.me();
    chosen = ACME;
    await api.me();
    expect(sent(fetchMock, 0).has("X-Client-Id")).toBe(false);
    expect(sent(fetchMock, 1).get("X-Client-Id")).toBe(ACME);
  });

  it("never travels on a route of global configuration, by method and path", async () => {
    const fetchMock = stubFetch();
    const api = narrowedTo(ACME);
    await api.listAuditEvents({ limit: 10 }); // GET /audit: GLOBAL_READS
    await api.updateCurrentOrg({ name: "Toeshee" }); // PATCH /orgs/current: GLOBAL_WRITES
    await api.getCurrentOrg(); // GET /orgs/current: not global
    expect(sent(fetchMock, 0).has("X-Client-Id")).toBe(false);
    expect(sent(fetchMock, 1).has("X-Client-Id")).toBe(false);
    expect(sent(fetchMock, 2).get("X-Client-Id")).toBe(ACME);
  });

  it("travels on the raw file routes too, and not on a global upload", async () => {
    const fetchMock = stubFetch();
    const api = narrowedTo(ACME);
    await api.exportPatchReport();
    await api.uploadPackageArtifact("p1", new File(["x"], "agent.msi"));
    expect(sent(fetchMock, 0).get("X-Client-Id")).toBe(ACME);
    // POST /deploy/packages/{package_id}/artifact is in GLOBAL_WRITES.
    expect(sent(fetchMock, 1).has("X-Client-Id")).toBe(false);
  });

  it("is decided by the builder alone, whatever a caller put in", () => {
    const own = { "X-Client-Id": "a-caller's-own" };
    expect(
      requestHeaders("t", own, { method: "GET", path: "/audit?limit=5", client: ACME }).has("X-Client-Id"),
    ).toBe(false);
    expect(
      requestHeaders("t", own, { method: "GET", path: "/devices", client: ACME }).get("X-Client-Id"),
    ).toBe(ACME);
    expect(requestHeaders("t", own).has("X-Client-Id")).toBe(false);
  });

  it("is decided on every request ApiClient builds headers for", () => {
    // A raw fetch path whose `requestHeaders(` call forgets the route sends no
    // header at all: the request runs with the person's whole scope, and the
    // page shows another client's rows under the one chosen.
    const source = readFileSync(resolve(import.meta.dirname, "api.ts"), "utf8");
    const calls = source
      .split("\n")
      .filter((line) => line.includes("requestHeaders(") && !line.includes("export function requestHeaders("));
    expect(calls.length).toBeGreaterThanOrEqual(18);
    expect(calls.filter((line) => !line.includes("this.route("))).toEqual([]);
  });
});

/**
 * A route that is global BY ITS BODY, not by its path: `POST
 * /mcp/connections/{client_id}/revoke` with `scope: "everyone"` withdraws an AI
 * app from the whole organization, so the server refuses it narrowed (403
 * ALL_CLIENTS_REQUIRED, `backend/tests/test_global_writes.py`), while the same
 * path with `scope: "me"` is the person's own and is narrowed like any other.
 * `globalRoutes.json` lists paths and methods, never bodies, so it cannot say
 * this; the request says it itself (`RequestRoute.global`).
 */
describe("X-Client-Id on a route global by its body", () => {
  it("is not sent on a revoke for everyone, and is on a revoke for oneself, with a client chosen", async () => {
    const fetchMock = stubFetch();
    setSelectedClient(ACME);
    // Wired as `ApiContext.tsx` wires it: the top bar's choice, read per request.
    const api = new ApiClient(() => "t", vi.fn(), "/api/v1", selectedClient);

    await api.revokeMcpConnection("some-ai-app", "everyone");
    await api.revokeMcpConnection("some-ai-app", "me");

    expect(sent(fetchMock, 0).has("X-Client-Id")).toBe(false);
    expect(sent(fetchMock, 1).get("X-Client-Id")).toBe(ACME);
  });

  it("is dropped by the builder for a route marked global, whatever its path", () => {
    const own = { "X-Client-Id": "a-caller's-own" };
    expect(
      requestHeaders("t", own, {
        method: "POST",
        path: "/mcp/connections/some-ai-app/revoke",
        client: ACME,
        global: true,
      }).has("X-Client-Id"),
    ).toBe(false);
    expect(
      requestHeaders("t", own, {
        method: "POST",
        path: "/mcp/connections/some-ai-app/revoke",
        client: ACME,
      }).get("X-Client-Id"),
    ).toBe(ACME);
  });
});
