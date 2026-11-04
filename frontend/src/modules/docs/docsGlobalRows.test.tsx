/**
 * EVERY CLIENT'S DOCUMENTATION, WRITTEN WITH A CLIENT CHOSEN IN THE TOP BAR
 * (phase 3 final review B, Critical 1; C, Important 2).
 *
 * An article, record or document with no client is every client's: a global
 * row. With a client chosen, every request carries `X-Client-Id`, and the
 * server refuses a write to a global row under that narrowed scope (403,
 * `core/global_rows.py`) -- so "For every client", and every edit, archive,
 * version, link or purge of a global row on the list, failed for an admin who
 * holds every client. The console now says which writes are global and sends
 * them without the header (`RequestRoute.global`); the server still requires
 * the code on every client.
 *
 * The first half drives the real `ApiClient` against a stubbed `fetch` and
 * reads the headers that left. The second half pins that each write of an
 * existing row tells the client whether that row is global.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../../lib/api";
import type { DocArticle, DocDocument, DocDocumentVersion, Me, MeClient } from "../../lib/api";
import { DocsPage } from "./DocsPage";
import { DocumentView } from "./documents";
import { PortalVisibility } from "./PortalVisibility";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));
vi.mock("../../lib/download", () => ({ saveBlob: vi.fn() }));

const ACME_ID = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const ACME: MeClient = {
  id: ACME_ID,
  name: "Acme",
  kind: "customer",
  enabled_modules: [],
  permissions: ["docs.read", "docs.manage"],
};

/** An admin of every client, with Acme chosen in the top bar. */
const ADMIN: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "admin@example.com",
  display_name: "Admin",
  permissions: ["docs.read", "docs.manage", "docs.document.purge"],
  permissions_everywhere: ["docs.read", "docs.manage", "docs.document.purge"],
  clients: [ACME],
};

beforeEach(() => {
  mockClientScope.mockReturnValue({ selected: ACME_ID, select: vi.fn(), clients: [ACME], ready: true });
  URL.createObjectURL = vi.fn(() => "blob:mock/1") as unknown as typeof URL.createObjectURL;
  URL.revokeObjectURL = vi.fn() as unknown as typeof URL.revokeObjectURL;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const ARTICLE: DocArticle = {
  id: "a1",
  client_id: null,
  title: "VPN",
  body: "",
  category: "General",
  archived: false,
  current_version: 1,
  updated_at: "2026-10-01T00:00:00.000Z",
  links: [],
};

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

/** The real `ApiClient`, Acme chosen, over a `fetch` that answers the docs page. */
function realApiOverFetch(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    const path = url.replace(/^\/api\/v1/, "").split("?")[0];
    const method = init?.method ?? "GET";
    if (path === "/me") return Promise.resolve(json(ADMIN));
    if (method === "POST" && path === "/docs/articles") return Promise.resolve(json(ARTICLE));
    if (path === "/docs/articles/a1") return Promise.resolve(json(ARTICLE));
    return Promise.resolve(json({ items: [], total: 0 }));
  });
  vi.stubGlobal("fetch", fetchMock);
  mockUseApi.mockReturnValue(new ApiClient(() => "t", vi.fn(), "/api/v1", () => ACME_ID));
  return fetchMock;
}

/** The headers of the one request sent as `method path`. */
function headersOf(fetchMock: ReturnType<typeof vi.fn>, method: string, path: string): Headers {
  const call = fetchMock.mock.calls.find(
    ([url, init]) => (init as RequestInit | undefined)?.method === method && String(url) === `/api/v1${path}`,
  ) as [string, RequestInit] | undefined;
  if (!call) throw new Error(`no ${method} ${path} was sent`);
  return new Headers(call[1].headers);
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/docs"]}>
        <DocsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("'For every client', with a client chosen in the top bar", () => {
  it("files the article for every client with no X-Client-Id", async () => {
    const fetchMock = realApiOverFetch();
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New article" }));
    await user.type(screen.getByLabelText("Title"), "VPN");
    await user.click(await screen.findByRole("checkbox", { name: /For every client/ }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(headersOf(fetchMock, "POST", "/docs/articles")).toBeTruthy());

    const sent = headersOf(fetchMock, "POST", "/docs/articles");
    expect(sent.has("X-Client-Id")).toBe(false);
    const body = JSON.parse(
      String((fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "POST")?.[1] as RequestInit).body),
    ) as Record<string, unknown>;
    expect(body).not.toHaveProperty("client_id");
  });

  it("sends the header for the client's own article, unticked", async () => {
    const fetchMock = realApiOverFetch();
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New article" }));
    await user.type(screen.getByLabelText("Title"), "VPN");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(headersOf(fetchMock, "POST", "/docs/articles")).toBeTruthy());

    expect(headersOf(fetchMock, "POST", "/docs/articles").get("X-Client-Id")).toBe(ACME_ID);
  });
});

/* ------------------------------------------- writes to an EXISTING row */

function renderArticle(article: DocArticle, overrides: Record<string, unknown> = {}) {
  const api = {
    me: vi.fn().mockResolvedValue(ADMIN),
    listArticles: vi.fn().mockResolvedValue({
      items: [{ ...article, link_count: 0 }],
      total: 1,
    }),
    listDocRecords: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listDocuments: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    getArticle: vi.fn().mockResolvedValue(article),
    updateArticle: vi.fn().mockResolvedValue(article),
    archiveArticle: vi.fn().mockResolvedValue({ archived: true }),
    setArticlePortalVisibility: vi.fn().mockResolvedValue({ portal_visible: true }),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api);
  renderPage();
  return api;
}

describe("an existing article says whether it is every client's", () => {
  it.each([
    { label: "every client's", clientId: null, global: true },
    { label: "a client's", clientId: ACME_ID, global: false },
  ])("edits and archives $label article with global: $global", async ({ clientId, global }) => {
    const user = userEvent.setup();
    const api = renderArticle({ ...ARTICLE, client_id: clientId });
    await user.click(await screen.findByRole("button", { name: /VPN/ }));
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.updateArticle).toHaveBeenCalledTimes(1));
    expect(api.updateArticle.mock.calls[0]?.[2]).toEqual({ global });

    await user.click(await screen.findByRole("button", { name: "Archive" }));
    await user.click(await screen.findByRole("button", { name: "Archive article" }));
    await waitFor(() => expect(api.archiveArticle).toHaveBeenCalledWith("a1", { global }));
  });

  it.each([
    { label: "every client's", clientId: null, global: true },
    { label: "a client's", clientId: ACME_ID, global: false },
  ])("turns portal visibility on for $label article with global: $global", async ({ clientId, global }) => {
    const user = userEvent.setup();
    const setArticlePortalVisibility = vi.fn().mockResolvedValue({ portal_visible: true });
    mockUseApi.mockReturnValue({ setArticlePortalVisibility });
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <PortalVisibility article={{ ...ARTICLE, client_id: clientId }} canManage />
      </QueryClientProvider>,
    );
    await user.click(screen.getByRole("switch", { name: "Visible in the portal" }));
    await waitFor(() => expect(setArticlePortalVisibility).toHaveBeenCalledWith("a1", true, { global }));
  });
});

const PDF: DocDocumentVersion = {
  version: 2,
  filename: "contract.pdf",
  byte_size: 2048,
  content_type: "application/pdf",
  viewer: "pdf",
  sha256: "b".repeat(64),
  created_at: "2026-08-20T01:00:00.000Z",
  uploaded_by: "u1",
  purged_at: null,
};

const DOC: DocDocument = {
  id: "d1",
  client_id: null,
  title: "Shared contract",
  description: "",
  category: "Contracts",
  archived: false,
  current_version: 2,
  updated_at: "2026-08-20T01:00:00.000Z",
  current: PDF,
  links: [{ id: "l1", target_type: "device", target_id: "11111111-1111-1111-1111-111111111111" }],
};

function renderDocument(doc: DocDocument) {
  const api = {
    getDocument: vi.fn().mockResolvedValue(doc),
    documentVersions: vi.fn().mockResolvedValue([PDF]),
    downloadDocumentVersion: vi.fn().mockResolvedValue(new Blob(["%PDF-1.7"])),
    updateDocument: vi.fn().mockResolvedValue(doc),
    archiveDocument: vi.fn().mockResolvedValue({ archived: true }),
    uploadDocumentVersion: vi.fn().mockResolvedValue(PDF),
    purgeDocumentVersion: vi.fn().mockResolvedValue({ purged: true }),
    addDocumentLink: vi.fn().mockResolvedValue({ id: "l2", target_type: "device", target_id: "x" }),
    removeDocumentLink: vi.fn().mockResolvedValue({ removed: true }),
  };
  mockUseApi.mockReturnValue(api);
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={["/docs?document=d1"]}>
        <DocumentView documentId="d1" canManage canPurge zone="UTC" onCleared={() => undefined} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return api;
}

describe("an existing document says whether it is every client's", () => {
  it.each([
    { label: "every client's", clientId: null, global: true },
    { label: "a client's", clientId: ACME_ID, global: false },
  ])("edits, versions, links, purges and archives $label document with global: $global", async ({ clientId, global }) => {
    const user = userEvent.setup();
    const api = renderDocument({ ...DOC, client_id: clientId });
    await screen.findByRole("heading", { name: "Shared contract" });

    // Edit details.
    await user.click(screen.getByRole("button", { name: "Edit details" }));
    let dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.updateDocument).toHaveBeenCalledTimes(1));
    expect(api.updateDocument.mock.calls[0]?.[2]).toEqual({ global });

    // A new version.
    await user.click(screen.getByRole("button", { name: "Upload new version" }));
    dialog = await screen.findByRole("dialog");
    await user.upload(within(dialog).getByLabelText("File"), new File(["%PDF"], "v3.pdf"));
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(api.uploadDocumentVersion).toHaveBeenCalledTimes(1));
    expect(api.uploadDocumentVersion.mock.calls[0]?.[2]).toEqual({ global });

    // A link added and one removed.
    await user.click(screen.getByRole("button", { name: "Links" }));
    await user.type(screen.getByLabelText("Target id"), "22222222-2222-2222-2222-222222222222");
    await user.click(screen.getByRole("button", { name: "Link" }));
    await waitFor(() => expect(api.addDocumentLink).toHaveBeenCalledTimes(1));
    expect(api.addDocumentLink.mock.calls[0]?.[2]).toEqual({ global });
    await user.click(screen.getByRole("button", { name: /Unlink/ }));
    await waitFor(() => expect(api.removeDocumentLink).toHaveBeenCalledWith("d1", "l1", { global }));

    // The version on screen, purged: the dialog asks for its subject typed.
    await user.click(screen.getByRole("button", { name: "Purge v2" }));
    dialog = await screen.findByRole("dialog");
    const subject = dialog.querySelector(".confirm-subject")?.textContent ?? "";
    await user.type(within(dialog).getByRole("textbox"), subject);
    await user.click(within(dialog).getByRole("button", { name: "Destroy the bytes" }));
    await waitFor(() => expect(api.purgeDocumentVersion).toHaveBeenCalledWith("d1", 2, { global }));

    // And archived.
    await user.click(screen.getByRole("button", { name: "Archive" }));
    await user.click(await screen.findByRole("button", { name: "Archive document" }));
    await waitFor(() => expect(api.archiveDocument).toHaveBeenCalledWith("d1", { global }));
  });

  it("uploads the file of a new document for every client with global: true", async () => {
    const user = userEvent.setup();
    const createDocument = vi.fn().mockResolvedValue({ ...DOC, current_version: 0, current: null });
    const uploadDocumentVersion = vi.fn().mockResolvedValue(PDF);
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ADMIN),
      listArticles: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      listDocRecords: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      listDocuments: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      getDocument: vi.fn().mockResolvedValue(DOC),
      documentVersions: vi.fn().mockResolvedValue([PDF]),
      downloadDocumentVersion: vi.fn().mockResolvedValue(new Blob(["%PDF-1.7"])),
      createDocument,
      uploadDocumentVersion,
    });
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Upload document" }));
    const dialog = await screen.findByRole("dialog", { name: "Upload document" });
    await user.type(within(dialog).getByLabelText("Title"), "Shared contract");
    await user.upload(within(dialog).getByLabelText("File"), new File(["%PDF"], "c.pdf"));
    await user.click(within(dialog).getByRole("checkbox", { name: /For every client/ }));
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(uploadDocumentVersion).toHaveBeenCalledTimes(1));
    expect(createDocument.mock.calls[0]?.[0]).not.toHaveProperty("client_id");
    expect(uploadDocumentVersion.mock.calls[0]?.[2]).toEqual({ global: true });
  });
});
