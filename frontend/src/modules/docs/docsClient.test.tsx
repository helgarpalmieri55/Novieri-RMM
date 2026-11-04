/**
 * Whose a new article, record or document is (MSP clients plan 2026-10-26
 * Task 14; phase 3 design §3.1, §3.5). The console files what a technician
 * documents under the client chosen at the top or, with every client chosen,
 * the one picked in the form's Client field -- and files it for EVERY client
 * only when the person asks, with the "For every client" switch, by sending no
 * client at all. An edit never sends one: a row's client is set at creation.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import type { ApiClient, DocArticle, Me, MeClient } from "../../lib/api";
import { DocsPage } from "./DocsPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

/** The internal client every test here works in unless it chooses otherwise. */
const SCOPE_INTERNAL: MeClient = {
  id: "o1",
  name: "Novieri",
  kind: "internal",
  enabled_modules: [],
  permissions: [],
};

/** The selector (Task 7): `selected` is the client chosen at the top, `null` for every client. */
function selectClient(selected: string | null, clients: MeClient[] = [SCOPE_INTERNAL]): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients, ready: true });
}

beforeEach(() => selectClient(SCOPE_INTERNAL.id));

const INTERNAL_CLIENT: MeClient = { ...SCOPE_INTERNAL, permissions: ["docs.read", "docs.manage"] };
const ACME_CLIENT: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: ["docs.read", "docs.manage"] };
// Phase 3 final review D, Important 2: Beta's documentation may be READ here,
// not written; and the Unassigned client is never offered.
const BETA_READER: MeClient = { id: "c-beta", name: "Beta", kind: "customer", enabled_modules: [], permissions: ["docs.read"] };
const UNASSIGNED_CLIENT: MeClient = {
  id: "c-unassigned", name: "Unassigned", kind: "unassigned", enabled_modules: [], permissions: ["docs.read", "docs.manage"],
};
const WITH_A_READER = [INTERNAL_CLIENT, ACME_CLIENT, BETA_READER, UNASSIGNED_CLIENT];

function offered(field: HTMLElement): (string | null)[] {
  return within(field).getAllByRole("option").map((option) => option.textContent);
}

const MANAGER: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["docs.read", "docs.manage"],
  clients: [INTERNAL_CLIENT, ACME_CLIENT],
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

// Phase 3 final review D, Important 3. In documentation a row with no client
// is EVERY client's (`backend/app/modules/documentation/api.py`), so a body
// sent with the Client field still empty would file a global article, record
// or document for a person who only forgot to pick one. With every client
// chosen and the field empty, Save names the field -- "Client is required" --
// and nothing is sent.

// Phase 3 final review D, Important 1: `MANAGER.org_id` is "o1", where
// phases 1-2 filed every new row whoever was chosen, so a test that chooses
// "o1" at the top cannot tell that rule from this one. These choose "c-acme".

describe("a new article", () => {
  it("is the client chosen at the top's, which is not the organisation's own", async () => {
    selectClient("c-acme", [INTERNAL_CLIENT, ACME_CLIENT]);
    const user = userEvent.setup();
    const createArticle = vi.fn().mockResolvedValue(ARTICLE);
    renderDocs({ createArticle });
    await user.click(await screen.findByRole("button", { name: "New article" }));
    expect(screen.queryByLabelText("Client")).toBeNull();
    await user.type(screen.getByLabelText("Title"), "VPN");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(createArticle).toHaveBeenCalledTimes(1));
    expect(createArticle.mock.calls[0]?.[0]).toEqual({
      title: "VPN",
      body: "",
      category: "General",
      client_id: "c-acme",
    });
  });

  it("is the client chosen at the top's", async () => {
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

  it("with every client chosen, asks for the client, and not when it is for every client", async () => {
    selectClient(null, [INTERNAL_CLIENT, ACME_CLIENT]);
    const user = userEvent.setup();
    const createArticle = vi.fn().mockResolvedValue(ARTICLE);
    renderDocs({ createArticle });
    await user.click(await screen.findByRole("button", { name: "New article" }));
    await user.type(screen.getByLabelText("Title"), "VPN");
    await user.selectOptions(screen.getByLabelText("Client"), "c-acme");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(createArticle).toHaveBeenCalledTimes(1));
    expect(createArticle.mock.calls[0]?.[0]).toMatchObject({ title: "VPN", client_id: "c-acme" });
  });

  it("with every client chosen, offers only the clients holding docs.manage, never Unassigned", async () => {
    selectClient(null, WITH_A_READER);
    const user = userEvent.setup();
    renderDocs({ createArticle: vi.fn() }, { ...MANAGER, clients: WITH_A_READER });
    await user.click(await screen.findByRole("button", { name: "New article" }));
    expect(offered(screen.getByLabelText("Client"))).toEqual(["Choose a client", "Novieri", "Acme"]);
  });

  it("with every client chosen and no client picked, Save sends nothing and says so", async () => {
    selectClient(null, [INTERNAL_CLIENT, ACME_CLIENT]);
    const user = userEvent.setup();
    const createArticle = vi.fn().mockResolvedValue(ARTICLE);
    renderDocs({ createArticle });
    await user.click(await screen.findByRole("button", { name: "New article" }));
    await user.type(screen.getByLabelText("Title"), "VPN");
    expect(screen.getByLabelText("Client")).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "Save" }));

    // The field is named before anything is built (phase 3 final review D2):
    // the form's own "Client is required", where it used to fall through to
    // `ClientNotChosen`. Nothing is sent either way.
    expect(await screen.findByRole("alert")).toHaveTextContent("Client is required");
    expect(createArticle).not.toHaveBeenCalled();
  });

  it("hides the Client field once the article is for every client", async () => {
    selectClient(null, [INTERNAL_CLIENT, ACME_CLIENT]);
    const user = userEvent.setup();
    renderDocs({ createArticle: vi.fn() });
    await user.click(await screen.findByRole("button", { name: "New article" }));
    expect(screen.getByLabelText("Client")).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /For every client/ }));
    expect(screen.queryByLabelText("Client")).toBeNull();
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
  it("is the client chosen at the top's, which is not the organisation's own", async () => {
    selectClient("c-acme", [INTERNAL_CLIENT, ACME_CLIENT]);
    const user = userEvent.setup();
    const createDocRecord = vi.fn().mockResolvedValue({ id: "r1" });
    renderDocs({ createDocRecord });
    await user.click(await screen.findByRole("button", { name: "New record" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByLabelText("Client")).toBeNull();
    await user.type(within(dialog).getByLabelText("Name"), "VPN gateway");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(createDocRecord).toHaveBeenCalledTimes(1));
    expect(createDocRecord.mock.calls[0]?.[0]).toMatchObject({ name: "VPN gateway", client_id: "c-acme" });
  });

  it("with every client chosen, asks for the client and files it there", async () => {
    selectClient(null, [INTERNAL_CLIENT, ACME_CLIENT]);
    const user = userEvent.setup();
    const createDocRecord = vi.fn().mockResolvedValue({ id: "r1" });
    renderDocs({ createDocRecord });
    await user.click(await screen.findByRole("button", { name: "New record" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Name"), "VPN gateway");
    await user.selectOptions(within(dialog).getByLabelText("Client"), "c-acme");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(createDocRecord).toHaveBeenCalledTimes(1));
    expect(createDocRecord.mock.calls[0]?.[0]).toMatchObject({ name: "VPN gateway", client_id: "c-acme" });
  });

  it("with every client chosen, offers only the clients holding docs.manage, never Unassigned", async () => {
    selectClient(null, WITH_A_READER);
    const user = userEvent.setup();
    renderDocs({ createDocRecord: vi.fn() }, { ...MANAGER, clients: WITH_A_READER });
    await user.click(await screen.findByRole("button", { name: "New record" }));
    const dialog = await screen.findByRole("dialog");
    expect(offered(within(dialog).getByLabelText("Client"))).toEqual(["Choose a client", "Novieri", "Acme"]);
  });

  it("with every client chosen and no client picked, Create sends nothing and says so", async () => {
    selectClient(null, [INTERNAL_CLIENT, ACME_CLIENT]);
    const user = userEvent.setup();
    const createDocRecord = vi.fn().mockResolvedValue({ id: "r1" });
    renderDocs({ createDocRecord });
    await user.click(await screen.findByRole("button", { name: "New record" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Name"), "VPN gateway");
    expect(within(dialog).getByLabelText("Client")).toHaveValue("");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    // Named by the form's own rule now (phase 3 final review D2); nothing sent.
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Client is required");
    expect(createDocRecord).not.toHaveBeenCalled();
  });

  it("is the client chosen at the top's, or every client's when asked", async () => {
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
  it("is the client chosen at the top's, which is not the organisation's own", async () => {
    selectClient("c-acme", [INTERNAL_CLIENT, ACME_CLIENT]);
    const user = userEvent.setup();
    const createDocument = vi.fn().mockResolvedValue({ ...ARTICLE, id: "d1", current: null });
    const uploadDocumentVersion = vi.fn().mockResolvedValue({ version: 1 });
    renderDocs({ createDocument, uploadDocumentVersion });

    await user.click(await screen.findByRole("button", { name: "Upload document" }));
    const dialog = await screen.findByRole("dialog", { name: "Upload document" });
    expect(within(dialog).queryByLabelText("Client")).toBeNull();
    await user.type(within(dialog).getByLabelText("Title"), "Contract");
    await user.upload(within(dialog).getByLabelText("File"), new File(["%PDF"], "c.pdf"));
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(createDocument).toHaveBeenCalledTimes(1));
    expect(createDocument.mock.calls[0]?.[0]).toMatchObject({ title: "Contract", client_id: "c-acme" });
  });

  it("with every client chosen, asks for the client and files it there", async () => {
    selectClient(null, [INTERNAL_CLIENT, ACME_CLIENT]);
    const user = userEvent.setup();
    const createDocument = vi.fn().mockResolvedValue({ ...ARTICLE, id: "d1", current: null });
    const uploadDocumentVersion = vi.fn().mockResolvedValue({ version: 1 });
    renderDocs({ createDocument, uploadDocumentVersion });

    await user.click(await screen.findByRole("button", { name: "Upload document" }));
    const dialog = await screen.findByRole("dialog", { name: "Upload document" });
    await user.type(within(dialog).getByLabelText("Title"), "Contract");
    await user.upload(within(dialog).getByLabelText("File"), new File(["%PDF"], "c.pdf"));
    await user.selectOptions(within(dialog).getByLabelText("Client"), "c-acme");
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(createDocument).toHaveBeenCalledTimes(1));
    expect(createDocument.mock.calls[0]?.[0]).toMatchObject({ title: "Contract", client_id: "c-acme" });
  });

  it("with every client chosen, offers only the clients holding docs.manage, never Unassigned", async () => {
    selectClient(null, WITH_A_READER);
    const user = userEvent.setup();
    renderDocs({ createDocument: vi.fn() }, { ...MANAGER, clients: WITH_A_READER });
    await user.click(await screen.findByRole("button", { name: "Upload document" }));
    const dialog = await screen.findByRole("dialog", { name: "Upload document" });
    expect(offered(within(dialog).getByLabelText("Client"))).toEqual(["Choose a client", "Novieri", "Acme"]);
  });

  it("with every client chosen and no client picked, Upload sends nothing and says so", async () => {
    selectClient(null, [INTERNAL_CLIENT, ACME_CLIENT]);
    const user = userEvent.setup();
    const createDocument = vi.fn().mockResolvedValue({ ...ARTICLE, id: "d1", current: null });
    const uploadDocumentVersion = vi.fn().mockResolvedValue({ version: 1 });
    renderDocs({ createDocument, uploadDocumentVersion });
    await user.click(await screen.findByRole("button", { name: "Upload document" }));
    const dialog = await screen.findByRole("dialog", { name: "Upload document" });
    await user.type(within(dialog).getByLabelText("Title"), "Contract");
    await user.upload(within(dialog).getByLabelText("File"), new File(["%PDF"], "c.pdf"));
    expect(within(dialog).getByLabelText("Client")).toHaveValue("");
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));

    // Named by the form's own rule now (phase 3 final review D2); nothing sent.
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Client is required");
    expect(createDocument).not.toHaveBeenCalled();
    expect(uploadDocumentVersion).not.toHaveBeenCalled();
  });

  it("is the client chosen at the top's, or every client's when asked", async () => {
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

// Phase 3 final review B, Important 4: with Unassigned chosen at the top every
// new row is filed under it and refused (422), and no Client field can help --
// the header confines the request to Unassigned. The forms say so instead of
// offering a Save that can only fail; "For every client" files nothing under
// Unassigned, so ticking it brings the Save back.
describe("with Unassigned chosen at the top", () => {
  const REFUSAL =
    "Unassigned is the client chosen at the top, and nothing new can be filed under it. Choose another client first.";
  const ADMIN: Me = { ...MANAGER, permissions_everywhere: ["docs.read", "docs.manage"] };

  beforeEach(() => selectClient(UNASSIGNED_CLIENT.id, [INTERNAL_CLIENT, UNASSIGNED_CLIENT]));

  it("a new article says so instead of offering Save, until it is for every client", async () => {
    const user = userEvent.setup();
    const createArticle = vi.fn().mockResolvedValue({ ...ARTICLE, client_id: null });
    renderDocs({ createArticle }, ADMIN);
    await user.click(await screen.findByRole("button", { name: "New article" }));
    await user.type(screen.getByLabelText("Title"), "VPN");
    expect(screen.getByText(REFUSAL)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    expect(screen.queryByLabelText("Client")).toBeNull();
    // Enter in the title submits the form: still nothing is sent.
    await user.type(screen.getByLabelText("Title"), "{Enter}");
    expect(createArticle).not.toHaveBeenCalled();

    await user.click(await screen.findByRole("checkbox", { name: /For every client/ }));
    expect(screen.queryByText(REFUSAL)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(createArticle).toHaveBeenCalledTimes(1));
    expect(createArticle.mock.calls[0]?.[0]).not.toHaveProperty("client_id");
  });

  it("a new record says so instead of offering Create", async () => {
    const user = userEvent.setup();
    const createDocRecord = vi.fn();
    renderDocs({ createDocRecord });
    await user.click(await screen.findByRole("button", { name: "New record" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(REFUSAL)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Create" })).toBeNull();
    expect(createDocRecord).not.toHaveBeenCalled();
  });

  it("a new document says so instead of offering Upload", async () => {
    const user = userEvent.setup();
    const createDocument = vi.fn();
    renderDocs({ createDocument });
    await user.click(await screen.findByRole("button", { name: "Upload document" }));
    const dialog = await screen.findByRole("dialog", { name: "Upload document" });
    expect(within(dialog).getByText(REFUSAL)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Upload" })).toBeNull();
    expect(createDocument).not.toHaveBeenCalled();
  });

  it("says it in Spanish", async () => {
    setActiveLanguage("es");
    const user = userEvent.setup();
    renderDocs({ createDocRecord: vi.fn() });
    await user.click(await screen.findByRole("button", { name: "Registro nuevo" }));
    expect(
      await screen.findByText(
        "El cliente elegido arriba es «Sin asignar», y en él no se puede crear nada nuevo. Elige antes otro cliente.",
      ),
    ).toBeInTheDocument();
  });
});
