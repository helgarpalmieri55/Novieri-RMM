/**
 * A patch approval for EVERY client (no client, a global row: "For every
 * client") is written without `X-Client-Id`, and so is the revoke of one
 * (phase 3 final review B, Critical 1; C, Important 2). With the header the
 * request's scope is one client, and the server refuses a global approval
 * under it (403, `refuse_unless_every_client`). Global BY ITS BODY OR ITS ROW,
 * which `globalRoutes.json` cannot see (`RequestRoute.global`). The server
 * still requires `patch.approve` on every client for the global ones.
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

const WRITES: readonly {
  name: string;
  global: (api: ApiClient) => Promise<unknown>;
  client: (api: ApiClient) => Promise<unknown>;
}[] = [
  {
    name: "bulkApprovePatches",
    global: (api) => api.bulkApprovePatches({ patch_ids: ["p1"], ring_name: "test" }),
    client: (api) => api.bulkApprovePatches({ patch_ids: ["p1"], ring_name: "test", client_id: ACME }),
  },
  // The single approval, which no screen calls (the bar sends the bulk one):
  // the same body rule, so the same flag (phase 3 last fix round, item 4).
  {
    name: "approvePatch",
    global: (api) => api.approvePatch({ patch_id: "p1", ring_name: "test" }),
    client: (api) => api.approvePatch({ patch_id: "p1", ring_name: "test", client_id: ACME }),
  },
  {
    name: "revokePatchApproval",
    global: (api) => api.revokePatchApproval("ap1", { global: true }),
    client: (api) => api.revokePatchApproval("ap1", { global: false }),
  },
];

/** The patch approval methods that only read, named so a new one is classified, not missed. */
const READS = ["listPatchApprovals"];

describe("X-Client-Id on a patch approval write, with a client chosen in the top bar", () => {
  it.each(WRITES)("$name: not sent for every client's approval, sent for a client's", async ({ global, client }) => {
    const fetchMock = stubFetch();
    const api = narrowed();
    await global(api);
    await client(api);
    expect(sent(fetchMock, 0).has("X-Client-Id")).toBe(false);
    expect(sent(fetchMock, 1).get("X-Client-Id")).toBe(ACME);
  });

  it("covers every ApiClient method on patch approvals", () => {
    // NON-VACUITY: a new approval write that is not in the table above is one
    // nobody has decided the header of (phase 3 last fix round).
    const methods = Object.getOwnPropertyNames(ApiClient.prototype).filter((name) =>
      /patch.*approv|approv.*patch/i.test(name),
    );
    expect(methods.sort()).toEqual([...WRITES.map((row) => row.name), ...READS].sort());
  });
});
