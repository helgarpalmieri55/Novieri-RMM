import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "./api";

describe("ApiClient", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("sends the bearer token on requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0 }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "tok-123", vi.fn());
    await client.listDevices();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/devices");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok-123");
  });

  it("maps 401 to the unauthorized handler and throws ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("{}", { status: 401 })),
    );
    const onUnauthorized = vi.fn();
    const client = new ApiClient(() => null, onUnauthorized);
    await expect(client.me()).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  // The client id is whatever an AI app registered, and it is one path
  // segment: a `/`, a space or a `?` in it must not become path or query.
  it("puts an MCP client id in the revoke path as one encoded segment, with the scope in the body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ revoked: true, keycloak_failures: 0 }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "t", vi.fn());
    const result = await client.revokeMcpConnection("my app/1?x", "everyone");
    expect(result).toEqual({ revoked: true, keycloak_failures: 0 });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/mcp/connections/my%20app%2F1%3Fx/revoke");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ scope: "everyone" });
  });

  it("reads the MCP connections", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ enabled: false, address: null, apps: [] }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "t", vi.fn());
    expect(await client.getMcpConnections()).toEqual({ enabled: false, address: null, apps: [] });
    expect((fetchMock.mock.calls[0] as [string])[0]).toBe("/api/v1/mcp/connections");
  });

  it("checkout returns the secret to the caller with the reason in the body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ secret: "hunter2" }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "t", vi.fn());
    const result = await client.checkoutCredential("cred-1", "ticket 9");
    expect(result.secret).toBe("hunter2");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/credentials/cred-1/checkout");
    expect(JSON.parse(init.body as string)).toEqual({ reason: "ticket 9" });
  });

  it("serializes job dispatch bodies", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "j1", status: "queued" }), { status: 201 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "t", vi.fn());
    await client.dispatchJob({ type: "hello", device_id: "d1", params: { message: "hi" } });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/jobs");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      type: "hello",
      device_id: "d1",
      params: { message: "hi" },
    });
  });
  // --------------------------------------------------------------- rec. 10
  // `listJobs()` sent NO query string, so the console silently took the
  // server's default `limit=100` while its own KPI tile showed the true
  // total -- and the closeout refused to add a client-side sort on top of
  // that, because sorting a silently truncated page by "oldest first" shows
  // the newest hundred re-sorted, which is worse than no sort. Paging and
  // ordering are one change, and this is the client half of it.

  it("asks the jobs route for the page and the order it wants", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0 }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "t", vi.fn());
    await client.listJobs({ limit: 25, offset: 50, order: "oldest" });
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("/api/v1/jobs?limit=25&offset=50&order=oldest");
  });

  it("sends no jobs query string when it is asking for the default page", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0 }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "t", vi.fn());
    await client.listJobs();
    expect((fetchMock.mock.calls[0] as [string])[0]).toBe("/api/v1/jobs");
  });

  it("reads the ticket list as a page with a total, not as a bare array", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [{ id: "t1" }], total: 412 }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "t", vi.fn());
    const page = await client.listTickets({ limit: 200 });
    expect(page.total).toBe(412);
    expect(page.items).toHaveLength(1);
    expect((fetchMock.mock.calls[0] as [string])[0]).toBe(
      "/api/v1/service-desk/tickets?limit=200",
    );
  });

  it("reads, saves and regenerates the install link on its own route", async () => {
    const fetchMock = vi.fn().mockImplementation(
      () => Promise.resolve(new Response(JSON.stringify({ enabled: false, url: null }), { status: 200 })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "t", vi.fn());
    await client.getInstallLink();
    await client.saveInstallLink({
      enabled: true, networks: ["10.20.0.0/16"], disable_at: null, hourly_limit: 50,
    });
    await client.regenerateInstallLink();
    const calls = fetchMock.mock.calls as [string, RequestInit][];
    expect(calls.map(([url, init]) => [url, init.method ?? "GET"])).toEqual([
      ["/api/v1/install-link", "GET"],
      ["/api/v1/install-link", "PUT"],
      ["/api/v1/install-link/regenerate", "POST"],
    ]);
    expect(JSON.parse(calls[1]![1].body as string)).toEqual({
      enabled: true, networks: ["10.20.0.0/16"], disable_at: null, hourly_limit: 50,
    });
  });

  it("approves and rejects a device on its review routes", async () => {
    const fetchMock = vi.fn().mockImplementation(
      () => Promise.resolve(new Response(JSON.stringify({}), { status: 200 })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "t", vi.fn());
    await client.approveDevice("d1");
    await client.rejectDevice("d1");
    await client.listDevices({ state: "review", limit: 1 });
    const calls = fetchMock.mock.calls as [string, RequestInit][];
    expect(calls.map(([url, init]) => [url, init.method ?? "GET"])).toEqual([
      ["/api/v1/devices/d1/review/approve", "POST"],
      ["/api/v1/devices/d1/review/reject", "POST"],
      ["/api/v1/devices?state=review&limit=1", "GET"],
    ]);
  });

  it("acknowledges a manual removal on its own route, and a 204 is success", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "t", vi.fn());
    await expect(client.acknowledgeDeviceRemoval("d1")).resolves.toBeUndefined();
    const calls = fetchMock.mock.calls as [string, RequestInit][];
    expect(calls.map(([url, init]) => [url, init.method ?? "GET"])).toEqual([
      ["/api/v1/devices/d1/review/acknowledge-removal", "POST"],
    ]);
  });

  it("sends the MSI's code in the body, never the URL, and reads the name and digest back", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("MSI", {
        status: 200,
        headers: {
          "Content-Disposition": 'attachment; filename="ITMS-Agent-itms.example.com-amd64.msi"',
          "X-Content-SHA256": "ab".repeat(32),
        },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "t", vi.fn());
    const code = "AbCdEfGhIjKlMnOpQrStUvWxYz012_-9";
    const msi = await client.downloadInstallLinkMsi({ code, arch: "amd64" });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/install-link/msi");
    expect(url).not.toContain(code);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ code, arch: "amd64" });
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer t");
    expect(msi.filename).toBe("ITMS-Agent-itms.example.com-amd64.msi");
    expect(msi.sha256).toBe("ab".repeat(32));
    expect(await msi.blob.text()).toBe("MSI");
  });

  it("posts Approve all's list and digest", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ bulk_id: "b", approved: 1, not_pending: 0, still_waiting: 0 }), {
        status: 200,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "t", vi.fn());
    await client.approveAllDevices({ device_ids: ["d1"], list_digest: "f".repeat(64) });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/devices/review/approve-all");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ device_ids: ["d1"], list_digest: "f".repeat(64) });
  });
});
