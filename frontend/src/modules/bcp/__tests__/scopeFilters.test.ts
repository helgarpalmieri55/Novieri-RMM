/**
 * The plan editor's group and article pickers ask the server for ONE client's
 * rows (phase 3 final review C, Important 5; the filters are review A6's):
 * `GET /device-groups?client_id=` and `GET /docs/articles?client_id=`. This
 * pins the query string the real `ApiClient` sends, which the editor's tests
 * (on a mocked client) cannot see. Without a client the URL is as before.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../../../lib/api";

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubFetch(body: unknown): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 })));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const api = (): ApiClient => new ApiClient(() => "t", vi.fn(), "/api/v1");

describe("one client's groups and articles", () => {
  it("asks for one client's device groups", async () => {
    const fetchMock = stubFetch({ items: [], total: 0, ungrouped_device_count: 0, evaluation: null });
    await api().listDeviceGroups({ client_id: "c-acme" });
    await api().listDeviceGroups();
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/device-groups?client_id=c-acme");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/v1/device-groups");
  });

  it("asks for one client's articles", async () => {
    const fetchMock = stubFetch({ items: [], total: 0 });
    await api().listArticles({ client_id: "c-acme" });
    await api().listArticles();
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/docs/articles?client_id=c-acme");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/v1/docs/articles");
  });
});
