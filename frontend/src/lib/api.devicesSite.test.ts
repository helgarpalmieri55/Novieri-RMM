/** One site's devices, and moving a device to another site of its client (design §4.3). */
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("a device's site", () => {
  it("asks for one site's devices, and moves a device with PATCH site_id", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementation(() => Promise.resolve(new Response(JSON.stringify({}), { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "t", vi.fn(), "/api/v1", () => null);
    await client.listDevices({ siteId: "s-north", limit: 50 });
    await client.changeDeviceSite("d1", "s-north");
    const sent = (fetchMock.mock.calls as [string, RequestInit][]).map(([url, init]) => [
      init.method ?? "GET",
      url,
      init.body === undefined ? undefined : JSON.parse(String(init.body)),
    ]);
    expect(sent).toEqual([
      ["GET", "/api/v1/devices?site_id=s-north&limit=50", undefined],
      ["PATCH", "/api/v1/devices/d1", { site_id: "s-north" }],
    ]);
  });
});
