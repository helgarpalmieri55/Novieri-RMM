/**
 * A credential with no client is EVERY CLIENT'S (a global row: `credentials`
 * is an O table, `backend/app/core/client_scope_catalog.py`), and the server
 * refuses an insert, update or delete of a global row under any scope but
 * "all" (`app/core/global_rows.py`, 403) -- which a chosen client's
 * `X-Client-Id` narrows away. So the `ApiClient` methods that write a
 * credential say, from the body or the row, when the write is global, and
 * such a request carries no `X-Client-Id` (phase 3 final review B, Critical
 * 1; operator decision of 2026-11-04). The server still requires the code on
 * every client: dropping the header never widens what the person holds.
 *
 * A checkout is a write too: it stamps `last_used_at` on the row.
 *
 * The same shape as `lib/api.clientScope.test.ts`'s table for
 * `revokeMcpConnection`.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../../lib/api";

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const CREDENTIAL = "11111111-1111-1111-1111-111111111111";

function stubFetch(): ReturnType<typeof vi.fn> {
  const fetchMock = vi
    .fn()
    .mockImplementation(() => Promise.resolve(new Response("{}", { status: 200 })));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function sent(fetchMock: ReturnType<typeof vi.fn>, call = 0): Headers {
  return new Headers((fetchMock.mock.calls[call] as [string, RequestInit])[1].headers);
}

/** A console with Acme chosen in the top bar. */
function narrowedToAcme(): ApiClient {
  return new ApiClient(vi.fn(), "/api/v1", () => ACME);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const CASES: readonly {
  method: string;
  global: (api: ApiClient) => Promise<unknown>;
  clients: (api: ApiClient) => Promise<unknown>;
}[] = [
  {
    method: "createCredential",
    global: (api) => api.createCredential({ name: "Domain admin", type: "windows_admin", secret: "s" }),
    clients: (api) =>
      api.createCredential({ name: "Domain admin", type: "windows_admin", secret: "s", client_id: ACME }),
  },
  {
    method: "checkoutCredential",
    global: (api) => api.checkoutCredential(CREDENTIAL, "ticket 4821", null),
    clients: (api) => api.checkoutCredential(CREDENTIAL, "ticket 4821", ACME),
  },
  {
    method: "deleteCredential",
    global: (api) => api.deleteCredential(CREDENTIAL, null),
    clients: (api) => api.deleteCredential(CREDENTIAL, ACME),
  },
];

describe("a write of a credential with no client, with a client chosen at the top", () => {
  it.each(CASES)("$method sends no X-Client-Id for every client's, and the chosen one for a client's", async (row) => {
    const fetchMock = stubFetch();
    const api = narrowedToAcme();

    await row.global(api);
    await row.clients(api);

    expect(sent(fetchMock, 0).has("X-Client-Id")).toBe(false);
    expect(sent(fetchMock, 1).get("X-Client-Id")).toBe(ACME);
  });

  it("covers every ApiClient method that writes a credential", () => {
    // NON-VACUITY: a new credential write that is not in the table above is
    // one nobody has decided the header of.
    const writes = Object.getOwnPropertyNames(ApiClient.prototype).filter((name) =>
      /^(create|update|delete|rotate|checkout)Credential$/.test(name),
    );
    expect(writes.sort()).toEqual(CASES.map((row) => row.method).sort());
  });
});
