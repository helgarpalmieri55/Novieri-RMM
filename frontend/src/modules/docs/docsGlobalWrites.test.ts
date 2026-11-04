/**
 * A write to a documentation row that is EVERY client's -- an article, record
 * or document with no client (phase 3 final review B, Critical 1; C,
 * Important 2) -- never carries `X-Client-Id`. With the header the request's
 * scope is one client, and the server refuses any insert, update or delete of
 * a row with no client under such a scope (403, `core/global_rows.py`), which
 * read as "only a technician with access to every client can do this" to an
 * admin who has every client and simply had one chosen in the top bar.
 *
 * Global BY ITS BODY OR ITS ROW, which `globalRoutes.json` cannot see: the same
 * path is one client's for a row that has one, and is narrowed like any other
 * (`RequestRoute.global`, as `revokeMcpConnection` does). The server still
 * requires the code on every client for the global ones.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../../lib/api";

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

const ARTICLE = { title: "VPN", body: "", category: "General" };
const RECORD = { record_type: "service", name: "VPN gateway", fields: {} };
const DOCUMENT = { title: "Contract", description: "", category: "General" };
const FILE = new File(["%PDF"], "c.pdf");

/** Each documentation write, once for a global row and once for a client's. */
const WRITES: readonly {
  name: string;
  global: (api: ApiClient) => Promise<unknown>;
  client: (api: ApiClient) => Promise<unknown>;
}[] = [
  {
    name: "createArticle",
    global: (api) => api.createArticle(ARTICLE),
    client: (api) => api.createArticle({ ...ARTICLE, client_id: ACME }),
  },
  {
    name: "updateArticle",
    global: (api) => api.updateArticle("a1", ARTICLE, { global: true }),
    client: (api) => api.updateArticle("a1", ARTICLE, { global: false }),
  },
  {
    name: "archiveArticle",
    global: (api) => api.archiveArticle("a1", { global: true }),
    client: (api) => api.archiveArticle("a1", { global: false }),
  },
  {
    name: "setArticlePortalVisibility",
    global: (api) => api.setArticlePortalVisibility("a1", true, { global: true }),
    client: (api) => api.setArticlePortalVisibility("a1", true, { global: false }),
  },
  {
    name: "createDocRecord",
    global: (api) => api.createDocRecord(RECORD),
    client: (api) => api.createDocRecord({ ...RECORD, client_id: ACME }),
  },
  {
    name: "createDocument",
    global: (api) => api.createDocument(DOCUMENT),
    client: (api) => api.createDocument({ ...DOCUMENT, client_id: ACME }),
  },
  {
    name: "updateDocument",
    global: (api) => api.updateDocument("d1", DOCUMENT, { global: true }),
    client: (api) => api.updateDocument("d1", DOCUMENT, { global: false }),
  },
  {
    name: "archiveDocument",
    global: (api) => api.archiveDocument("d1", { global: true }),
    client: (api) => api.archiveDocument("d1", { global: false }),
  },
  {
    name: "uploadDocumentVersion",
    global: (api) => api.uploadDocumentVersion("d1", FILE, { global: true }),
    client: (api) => api.uploadDocumentVersion("d1", FILE, { global: false }),
  },
  {
    name: "purgeDocumentVersion",
    global: (api) => api.purgeDocumentVersion("d1", 2, { global: true }),
    client: (api) => api.purgeDocumentVersion("d1", 2, { global: false }),
  },
  {
    name: "addDocumentLink",
    global: (api) => api.addDocumentLink("d1", { target_type: "device", target_id: "x" }, { global: true }),
    client: (api) => api.addDocumentLink("d1", { target_type: "device", target_id: "x" }, { global: false }),
  },
  {
    name: "removeDocumentLink",
    global: (api) => api.removeDocumentLink("d1", "l1", { global: true }),
    client: (api) => api.removeDocumentLink("d1", "l1", { global: false }),
  },
];

describe("X-Client-Id on a documentation write, with a client chosen in the top bar", () => {
  it.each(WRITES)("$name: not sent for every client's row, sent for a client's", async ({ global, client }) => {
    const fetchMock = stubFetch();
    const api = narrowed();
    await global(api);
    await client(api);
    expect(sent(fetchMock, 0).has("X-Client-Id")).toBe(false);
    expect(sent(fetchMock, 1).get("X-Client-Id")).toBe(ACME);
  });
});
