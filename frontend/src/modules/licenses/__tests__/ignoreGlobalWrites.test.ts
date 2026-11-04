/**
 * Ignoring a software title for EVERY client (no client, a global
 * `software_ignore` row) is sent without `X-Client-Id`; one client's ignore is
 * narrowed like any other write (phase 3 final review B, Critical 1). With the
 * header the server refuses a global row (403, `core/global_rows.py`). Global
 * BY ITS BODY, which `globalRoutes.json` cannot see (`RequestRoute.global`).
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../../../lib/api";

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

/** Acme chosen in the top bar, as `ApiContext.tsx` wires it. */
const narrowed = (): ApiClient => new ApiClient(() => "t", vi.fn(), "/api/v1", () => ACME);

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("X-Client-Id on Ignore, with a client chosen in the top bar", () => {
  it("is not sent for every client's ignore, and is for a client's", async () => {
    const fetchMock = stubFetch();
    const api = narrowed();
    await api.ignoreSoftware({ name: "Toolbar" });
    await api.ignoreSoftware({ name: "Toolbar", client_id: ACME });
    expect(sent(fetchMock, 0).has("X-Client-Id")).toBe(false);
    expect(sent(fetchMock, 1).get("X-Client-Id")).toBe(ACME);
  });
});
