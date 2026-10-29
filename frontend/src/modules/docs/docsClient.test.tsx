/**
 * Whose a new article, record or document is (MSP clients plan 2026-10-26
 * Task 14). Until phase 3's selector the console files what a technician
 * documents under the organisation's INTERNAL client -- `/me`'s `org_id`
 * (`lib/internalClient.ts`) -- and files it for EVERY client only when the
 * person asks, with the "For every client" switch, by sending no client at all.
 * An edit never sends one: a row's client is set at creation.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient, DocArticle, Me } from "../../lib/api";
import { DocsPage } from "./DocsPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const MANAGER: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["docs.read", "docs.manage"],
};

const ARTICLE: DocArticle = {
  id: "a1",
  client_id: "o1",
  title: "VPN",
  body: "steps",
  category: "Runbook",
  archived: false,
  current_version: 1,
  updated_at: "2026-10-01T00:00:00.000Z",
  links: [],
};

function renderDocs(api: Partial<Record<keyof ApiClient, unknown>>, me: Me = MANAGER) {
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue(me),
    listArticles: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listDocRecords: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listDocuments: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    ...api,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/docs"]}>
        <DocsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("a new article", () => {
  it("is the internal client's", async () => {
    const user = userEvent.setup();
    const createArticle = vi.fn().mockResolvedValue(ARTICLE);
    renderDocs({ createArticle });
    await user.click(await screen.findByRole("button", { name: "New article" }));
    await user.type(screen.getByLabelText("Title"), "VPN");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(createArticle).toHaveBeenCalledTimes(1));
    expect(createArticle.mock.calls[0]?.[0]).toEqual({
      title: "VPN",
      body: "",
      category: "General",
      client_id: "o1",
    });
  });

  it("is every client's when asked, sending no client", async () => {
    const user = userEvent.setup();
    const createArticle = vi.fn().mockResolvedValue({ ...ARTICLE, client_id: null });
    renderDocs({ createArticle });
    await user.click(await screen.findByRole("button", { name: "New article" }));
    await user.type(screen.getByLabelText("Title"), "VPN");
    await user.click(await screen.findByRole("checkbox", { name: /For every client/ }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(createArticle).toHaveBeenCalledTimes(1));
    expect(createArticle.mock.calls[0]?.[0]).not.toHaveProperty("client_id");
  });

  it("is not sent before /me has answered", async () => {
    // A body without its client would be a global row the person never chose.
    const user = userEvent.setup();
    const createArticle = vi.fn();
    let answer: (value: Me) => void = () => undefined;
    const pendingMe = new Promise<Me>((resolve) => {
      answer = resolve;
    });
    mockUseApi.mockReturnValue({
      me: vi.fn().mockReturnValue(pendingMe),
      listArticles: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      listDocRecords: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      listDocuments: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      createArticle,
    } as unknown as ApiClient);
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/docs"]}>
          <DocsPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // Nothing to create from yet: the editor needs `docs.manage` from `/me`.
    expect(screen.queryByRole("button", { name: "New article" })).not.toBeInTheDocument();
    answer(MANAGER);
    await user.click(await screen.findByRole("button", { name: "New article" }));
    expect(createArticle).not.toHaveBeenCalled();
  });

  it("an edit sends no client and offers no switch", async () => {
    const user = userEvent.setup();
    const updateArticle = vi.fn().mockResolvedValue(ARTICLE);
    renderDocs({
      listArticles: vi.fn().mockResolvedValue({
        items: [{ ...ARTICLE, link_count: 0 }],
        total: 1,
      }),
      getArticle: vi.fn().mockResolvedValue(ARTICLE),
      updateArticle,
    });
    await user.click(await screen.findByRole("button", { name: /VPN/ }));
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    expect(screen.queryByRole("checkbox", { name: /For every client/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(updateArticle).toHaveBeenCalledTimes(1));
    expect(updateArticle.mock.calls[0]?.[1]).not.toHaveProperty("client_id");
  });
});

describe("a new record", () => {
  it("is the internal client's, or every client's when asked", async () => {
    const user = userEvent.setup();
    const createDocRecord = vi.fn().mockResolvedValue({ id: "r1" });
    renderDocs({ createDocRecord });
    await user.click(await screen.findByRole("button", { name: "New record" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Name"), "VPN gateway");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(createDocRecord).toHaveBeenCalledTimes(1));
    expect(createDocRecord.mock.calls[0]?.[0]).toMatchObject({ name: "VPN gateway", client_id: "o1" });

    await user.click(await screen.findByRole("button", { name: "New record" }));
    const again = await screen.findByRole("dialog");
    await user.clear(within(again).getByLabelText("Name"));
    await user.type(within(again).getByLabelText("Name"), "Shared vendor");
    await user.click(within(again).getByRole("checkbox", { name: /For every client/ }));
    await user.click(within(again).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(createDocRecord).toHaveBeenCalledTimes(2));
    expect(createDocRecord.mock.calls[1]?.[0]).toMatchObject({ name: "Shared vendor" });
    expect(createDocRecord.mock.calls[1]?.[0]).not.toHaveProperty("client_id");
  });
});

describe("a new document", () => {
  it("is the internal client's, or every client's when asked", async () => {
    const user = userEvent.setup();
    const createDocument = vi.fn().mockResolvedValue({ ...ARTICLE, id: "d1", current: null });
    const uploadDocumentVersion = vi.fn().mockResolvedValue({ version: 1 });
    renderDocs({ createDocument, uploadDocumentVersion });

    await user.click(await screen.findByRole("button", { name: "Upload document" }));
    let dialog = await screen.findByRole("dialog", { name: "Upload document" });
    await user.type(within(dialog).getByLabelText("Title"), "Contract");
    await user.upload(within(dialog).getByLabelText("File"), new File(["%PDF"], "c.pdf"));
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(createDocument).toHaveBeenCalledTimes(1));
    expect(createDocument.mock.calls[0]?.[0]).toMatchObject({ title: "Contract", client_id: "o1" });

    await user.click(await screen.findByRole("button", { name: "Upload document" }));
    dialog = await screen.findByRole("dialog", { name: "Upload document" });
    await user.type(within(dialog).getByLabelText("Title"), "Shared policy");
    await user.upload(within(dialog).getByLabelText("File"), new File(["%PDF"], "p.pdf"));
    await user.click(within(dialog).getByRole("checkbox", { name: /For every client/ }));
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(createDocument).toHaveBeenCalledTimes(2));
    expect(createDocument.mock.calls[1]?.[0]).not.toHaveProperty("client_id");
  });
});
