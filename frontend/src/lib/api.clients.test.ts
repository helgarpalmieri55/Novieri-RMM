/**
 * The Clients page's routes (MSP clients phase 3, design §4.1), as
 * `backend/app/api/v1/clients.py` declares them: each method's verb, path and
 * body. An id is one path segment, encoded.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "./api";

function recorder(body: unknown = {}) {
  const fetchMock = vi
    .fn()
    .mockImplementation(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 })));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function sent(fetchMock: ReturnType<typeof vi.fn>): [string, string, unknown][] {
  return (fetchMock.mock.calls as [string, RequestInit][]).map(([url, init]) => [
    init.method ?? "GET",
    url,
    init.body === undefined ? undefined : JSON.parse(String(init.body)),
  ]);
}

afterEach(() => vi.unstubAllGlobals());

describe("the Clients page's routes", () => {
  it("names each route and sends each body as the API declares it", async () => {
    const fetchMock = recorder();
    const client = new ApiClient(() => "t", vi.fn(), "/api/v1", () => null);
    await client.getClientRecord("c 1");
    await client.createClient({
      name: "Andes",
      email_domains: ["andes.example"],
      phone: "",
      address: "",
      website: "",
      notes: "",
      enabled_modules: ["change"],
    });
    await client.updateClient("c1", { enabled_modules: ["budget"] });
    await client.archiveClient("c1");
    await client.listClientSites("c1", { include_archived: true });
    await client.createClientSite("c1", { name: "Norte", address: "", description: "" });
    await client.updateSite("s1", { name: "Sur" });
    await client.archiveSite("s1");
    await client.listContacts("c1");
    await client.createContact("c1", { email: "ana@andes.example", display_name: "Ana", phone: null });
    expect(sent(fetchMock)).toEqual([
      ["GET", "/api/v1/clients/c%201", undefined],
      [
        "POST",
        "/api/v1/clients",
        {
          name: "Andes",
          email_domains: ["andes.example"],
          phone: "",
          address: "",
          website: "",
          notes: "",
          enabled_modules: ["change"],
        },
      ],
      ["PATCH", "/api/v1/clients/c1", { enabled_modules: ["budget"] }],
      ["POST", "/api/v1/clients/c1/archive", undefined],
      ["GET", "/api/v1/clients/c1/sites?include_archived=true", undefined],
      ["POST", "/api/v1/clients/c1/sites", { name: "Norte", address: "", description: "" }],
      ["PATCH", "/api/v1/sites/s1", { name: "Sur" }],
      ["DELETE", "/api/v1/sites/s1", undefined],
      ["GET", "/api/v1/clients/c1/contacts", undefined],
      ["POST", "/api/v1/clients/c1/contacts", { email: "ana@andes.example", display_name: "Ana", phone: null }],
    ]);
  });

  it("keeps the one-argument sites read that the site pickers use", async () => {
    const fetchMock = recorder([]);
    await new ApiClient(() => "t", vi.fn(), "/api/v1", () => null).listClientSites("c1");
    expect(sent(fetchMock)).toEqual([["GET", "/api/v1/clients/c1/sites", undefined]]);
  });
});
