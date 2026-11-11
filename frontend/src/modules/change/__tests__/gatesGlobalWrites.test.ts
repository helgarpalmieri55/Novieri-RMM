/**
 * The "Every device" change gate is EVERY CLIENT'S: a `change_gates` row with
 * no group has no client (an O table, `backend/app/core/client_scope_catalog.py`),
 * so it is the global gate, and the server sets or removes it only under the
 * every-client scope (`change_mgmt/gates.py` `refuse_unless_every_client`, and
 * the flush guard of `core/global_rows.py`, 403). With a client chosen in the
 * top bar `X-Client-Id` narrows the request to it, and the panel's DEFAULT
 * submit -- "Every device" -- was refused (phase 3 last fix round, item 1;
 * operator decision of 2026-11-04). So both writes go without the header for
 * the global gate, and with it for a group's, which is its group's client's.
 *
 * Global BY ITS BODY OR ITS ROW, which `globalRoutes.json` cannot see
 * (`RequestRoute.global`). The server still requires `change.gate.manage` on
 * every client for the global gate.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../../../lib/api";

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const GROUP = "22222222-2222-2222-2222-222222222222";

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
const narrowed = (): ApiClient => new ApiClient(vi.fn(), "/api/v1", () => ACME);

afterEach(() => {
  vi.unstubAllGlobals();
});

const FLAGS = { applies_to_patches: true, applies_to_deployments: true };

/** Each change gate write, once for the global gate and once for a group's. */
const WRITES: readonly {
  name: string;
  global: (api: ApiClient) => Promise<unknown>;
  client: (api: ApiClient) => Promise<unknown>;
}[] = [
  {
    name: "setChangeGate",
    global: (api) => api.setChangeGate({ device_group_id: null, ...FLAGS }),
    client: (api) => api.setChangeGate({ device_group_id: GROUP, ...FLAGS }),
  },
  {
    name: "removeChangeGate",
    global: (api) => api.removeChangeGate("g1", { global: true }),
    client: (api) => api.removeChangeGate("g1", { global: false }),
  },
];

/** The change gate methods that only read, named so a new one is classified, not missed. */
const READS = ["listChangeGates"];

describe("X-Client-Id on a change gate write, with a client chosen in the top bar", () => {
  it.each(WRITES)("$name: not sent for the every-device gate, sent for a group's", async ({ global, client }) => {
    const fetchMock = stubFetch();
    const api = narrowed();
    await global(api);
    await client(api);
    expect(sent(fetchMock, 0).has("X-Client-Id")).toBe(false);
    expect(sent(fetchMock, 1).get("X-Client-Id")).toBe(ACME);
  });

  it("covers every ApiClient method on change gates", () => {
    // NON-VACUITY: a new gate write that is not in the table above is one
    // nobody has decided the header of.
    const methods = Object.getOwnPropertyNames(ApiClient.prototype).filter((name) => /changegate/i.test(name));
    expect(methods.sort()).toEqual([...WRITES.map((row) => row.name), ...READS].sort());
  });
});
