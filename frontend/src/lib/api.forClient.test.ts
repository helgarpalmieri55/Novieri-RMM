import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "./api";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57";

afterEach(() => vi.unstubAllGlobals());

function stubFetch() {
  const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response("[]", { status: 200 })));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const headersOf = (fetchMock: ReturnType<typeof vi.fn>, call: number) =>
  new Headers((fetchMock.mock.calls[call] as [string, RequestInit])[1].headers);

describe("ApiClient.forClient (decision D6)", () => {
  it("narrows every request to the one client it was made for, whatever the top bar says", async () => {
    const fetchMock = stubFetch();
    const console = new ApiClient(vi.fn(), "/api/v1", () => null);
    await console.forClient(ANDES).listLicenses();
    expect(headersOf(fetchMock, 0).get("X-Client-Id")).toBe(ANDES);
    await console.listLicenses();
    expect(headersOf(fetchMock, 1).get("X-Client-Id")).toBeNull();
  });

  it("keeps the session's own handling of a 401", async () => {
    const onUnauthorized = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 401 })));
    await expect(new ApiClient(onUnauthorized).forClient(ANDES).listLicenses()).rejects.toThrow();
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });
});
