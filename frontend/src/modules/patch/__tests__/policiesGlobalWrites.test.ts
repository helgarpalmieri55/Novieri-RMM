/**
 * The ORGANISATION DEFAULT patch policy (`device_group_id: null`) is EVERY
 * CLIENT'S: a `patch_policies` row with no group has no client (an O table,
 * `backend/app/core/client_scope_catalog.py`), and the server writes it only
 * under the every-client scope (`patch/api.py` `upsert_policy`, 403). The
 * policy editor defaults to it, so with a client chosen in the top bar its
 * default save was refused (phase 3 last fix round, item 2; operator decision
 * of 2026-11-04). It goes without `X-Client-Id`; a group's policy, its
 * group's client's, is narrowed like any other write.
 *
 * Global BY ITS BODY, which `globalRoutes.json` cannot see
 * (`RequestRoute.global`). The server still requires `patch.policy.manage` on
 * every client for the default.
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

const POLICY = {
  ring_name: "broad",
  ring_order: 3,
  maintenance_window: null,
  auto_approve_security: false,
  reboot: "if_required",
  reboot_deferral_hours: 24,
  scan_interval_hours: 24,
};

/** Each patch policy write, once for the organisation default and once for a group's. */
const WRITES: readonly {
  name: string;
  global: (api: ApiClient) => Promise<unknown>;
  client: (api: ApiClient) => Promise<unknown>;
}[] = [
  {
    name: "upsertPatchPolicy",
    global: (api) => api.upsertPatchPolicy({ ...POLICY, device_group_id: null }),
    client: (api) => api.upsertPatchPolicy({ ...POLICY, device_group_id: GROUP }),
  },
];

/** The patch policy methods that only read, named so a new one is classified, not missed. */
const READS = ["listPatchPolicies"];

describe("X-Client-Id on a patch policy write, with a client chosen in the top bar", () => {
  it.each(WRITES)("$name: not sent for the organisation default, sent for a group's", async ({ global, client }) => {
    const fetchMock = stubFetch();
    const api = narrowed();
    await global(api);
    await client(api);
    expect(sent(fetchMock, 0).has("X-Client-Id")).toBe(false);
    expect(sent(fetchMock, 1).get("X-Client-Id")).toBe(ACME);
  });

  it("covers every ApiClient method on patch policies", () => {
    // NON-VACUITY: a new policy write that is not in the table above is one
    // nobody has decided the header of.
    const methods = Object.getOwnPropertyNames(ApiClient.prototype).filter((name) => /patchpolic/i.test(name));
    expect(methods.sort()).toEqual([...WRITES.map((row) => row.name), ...READS].sort());
  });
});
