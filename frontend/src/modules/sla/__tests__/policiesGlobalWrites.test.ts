/**
 * An SLA policy for EVERY client (no client, a global row) is created, edited
 * and deleted without `X-Client-Id` (phase 3 final review C, Important 2).
 * With the header the request's scope is one client, and the server refuses a
 * write to a row with no client under it (403, `core/global_rows.py`). Global
 * BY ITS BODY OR ITS ROW, which `globalRoutes.json` cannot see
 * (`RequestRoute.global`); a client's own policy is narrowed like any other
 * write. The server still requires `sla.admin` on every client for a global one.
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

const POLICY = { calendar_id: "cal1", response_target_minutes: 60, resolution_target_minutes: 480 };

const WRITES: readonly {
  name: string;
  global: (api: ApiClient) => Promise<unknown>;
  client: (api: ApiClient) => Promise<unknown>;
}[] = [
  {
    name: "createSlaPolicy",
    global: (api) => api.createSlaPolicy(POLICY),
    client: (api) => api.createSlaPolicy({ ...POLICY, client_id: ACME }),
  },
  {
    name: "updateSlaPolicy",
    global: (api) => api.updateSlaPolicy("sp1", { active: false }, { global: true }),
    client: (api) => api.updateSlaPolicy("sp1", { active: false }, { global: false }),
  },
  {
    name: "deleteSlaPolicy",
    global: (api) => api.deleteSlaPolicy("sp1", { global: true }),
    client: (api) => api.deleteSlaPolicy("sp1", { global: false }),
  },
];

describe("X-Client-Id on an SLA policy write, with a client chosen in the top bar", () => {
  it.each(WRITES)("$name: not sent for every client's policy, sent for a client's", async ({ global, client }) => {
    const fetchMock = stubFetch();
    const api = narrowed();
    await global(api);
    await client(api);
    expect(sent(fetchMock, 0).has("X-Client-Id")).toBe(false);
    expect(sent(fetchMock, 1).get("X-Client-Id")).toBe(ACME);
  });
});
