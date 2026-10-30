/**
 * Docs, migrated onto the design (UI port spec §6), mirroring
 * `modules/devices/DevicesPage.test.tsx`.
 *
 * The prototype's documentation screen is always loaded, always populated,
 * and never fails. This module previously had no test at all for the page
 * component itself (only its markdown/search-group helpers), so before this
 * file existed a broken articles endpoint and a genuinely empty knowledge
 * base rendered identically — an operator staring at an empty sidebar could
 * not tell "nothing has been documented yet" from "the request failed".
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";

// The selected article lives in the query string now (cycle 7's linkability
// finding), and `MemoryRouter` deliberately does not touch `window.location`.
// This is how a test reads the URL the page actually navigated to.
function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location-search">{location.search}</span>;
}
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import type { ApiClient, DocArticle, DocVersion, Me } from "../../lib/api";
import { DocsPage } from "./DocsPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["docs.read"],
  // ASIA/TOKYO, and this runner is UTC. Every timestamp assertion below can
  // therefore tell "read the operator's profile" from "read the machine the
  // browser happens to be on" — which is the whole of the cycle-5 defect.
  // +9 all year, so there is no DST caveat hiding in these numbers.
  timezone: "Asia/Tokyo",
};

/** The tile whose label reads exactly `label`. */
function tile(label: string): HTMLElement {
  const found = [...document.querySelectorAll<HTMLElement>(".stat-card")].find(
    (card) => card.querySelector(".stat-label")?.textContent === label,
  );
  if (!found) throw new Error(`no stat tile labelled "${label}"`);
  return found;
}

function renderPage(
  listArticles: () => Promise<unknown>,
  listDocRecords: () => Promise<unknown> = () => Promise.resolve({ items: [], total: 0 }),
  // The Documents section (spec-attachments.md §11.2) queries independently of
  // Articles, which is §0.9 working: with the object store down the metadata
  // list still answers, and a failing ARTICLE query must not blank the
  // documents list either. Defaulted to an empty page so the tests above,
  // which are about articles, keep testing only articles.
  listDocuments: () => Promise<unknown> = () => Promise.resolve({ items: [], total: 0 }),
) {
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue(ME),
    listArticles,
    listDocRecords,
    listDocuments,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/docs"]}>
        <DocsPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(ui);
}

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("DocsPage", () => {
  it("says it is loading rather than showing an empty library", async () => {
    renderPage(() => new Promise<never>(() => {}));
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty library", async () => {
    // The distinction this state machine exists for: a dead backend and a
    // knowledge base nobody has written into yet must not look identical.
    renderPage(() => Promise.reject(new Error("boom")));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No articles yet/i)).not.toBeInTheDocument();
  });

  it("says the library is empty when it is genuinely empty", async () => {
    renderPage(() => Promise.resolve({ items: [], total: 0 }));
    expect(await screen.findByText("No articles yet.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lists articles with the design's structure and counts from the data", async () => {
    renderPage(
      () =>
        Promise.resolve({
          items: [
            { id: "a1", title: "Windows workstation build standard", category: "Procedure", archived: false, updated_at: "2026-08-01T00:00:00.000Z", link_count: 2 },
            { id: "a2", title: "Finance share recovery runbook", category: "Runbook", archived: false, updated_at: "2026-08-05T00:00:00.000Z", link_count: 0 },
          ],
          total: 2,
        }),
      () => Promise.resolve({ items: [], total: 0 }),
      // A THIRD, DIFFERENT NUMBER on the fourth tile, so "the tiles read their
      // own queries" is distinguishable from "the tiles all read Articles" --
      // which is exactly the defect that made Categories worth replacing.
      () => Promise.resolve({ items: [], total: 3 }),
    );
    expect(await screen.findByText("Windows workstation build standard")).toBeInTheDocument();
    expect(screen.getByText("Finance share recovery runbook")).toBeInTheDocument();
    // The page head, from the design, verbatim.
    expect(screen.getByRole("heading", { name: "IT documentation" })).toBeInTheDocument();
    // No eyebrow between the crumb and the h1: the shell's breadcrumb names
    // the section, and the design draws crumb, h1, then the one-line sub.
    expect(screen.queryByText("Governance")).toBeNull();

    // Stats come from the data, not the prototype's mock 412/318/14/2 figures.
    const stats = document.querySelectorAll(".stat-card");
    const values = [...stats].map((card) => card.querySelector(".stat-value")?.textContent);
    expect(values).toEqual(["2", "0", "2", "3"]);
    expect(values).not.toContain("412");
    // ...and each one is LABELLED, in the element the tile now leads with.
    // The label matters more than the numeral and must not be the smallest
    // text in the card (shell/components.tsx).
    // D8: Categories is gone and Documents took its place, keeping the row at
    // four. Categories linked to the same list Articles links to and
    // decomposed into headings rather than rows -- a number an operator could
    // not act on. A fifth tile was the alternative and it breaks
    // `columns={4}`.
    expect([...stats].map((card) => card.querySelector(".stat-label")?.textContent)).toEqual([
      "Articles",
      "Records",
      "Linked references",
      "Documents",
    ]);
    expect([...stats].map((card) => card.querySelector(".stat-label")?.textContent)).not.toContain(
      "Categories",
    );
  });
});

/**
 * The form kernel, adopted here (UX remediation cycle 3).
 *
 * WHAT THESE PREVENT. Two auditors walked this route and filed four HIGHs
 * against the same two forms: an empty Save that was a silent no-op, an
 * invalid "Fields (JSON)" payload that produced no error at all, a Title whose
 * 300-character limit could only be found by failing, and a server refusal
 * that stayed on screen after the user had already fixed the input. Every one
 * of them is a *disabled* or *absent* explanation, so every test below asserts
 * on the explanation the user gets — `role="alert"` — and not on the button.
 */

const MANAGER: Me = { ...ME, permissions: ["docs.read", "docs.manage"] };

function renderManager(overrides: Partial<Record<string, unknown>> = {}) {
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue(MANAGER),
    listArticles: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listDocRecords: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listDocuments: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    createArticle: vi.fn().mockResolvedValue({ id: "a1" }),
    createDocRecord: vi.fn().mockResolvedValue({ id: "r1" }),
    ...overrides,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/docs"]}>
        <DocsPage />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(ui);
}

async function openArticleEditor(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "New article" }));
  return screen.getByLabelText("Title");
}

async function openRecordDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "New record" }));
}

describe("DocsPage article editor", () => {
  it("names the empty field on submit instead of doing nothing", async () => {
    // AUDIT (HIGH): "empty submit is silent". The old control read
    // `disabled={save.isPending || !draft.title}` — the click landed on a dead
    // button and the interface offered no theory about which field was at
    // fault. Save now stays enabled and answers.
    const user = userEvent.setup();
    const createArticle = vi.fn().mockResolvedValue({ id: "a1" });
    renderManager({ createArticle });
    await openArticleEditor(user);

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    await user.click(save);

    expect(await screen.findByRole("alert")).toHaveTextContent("Title is required");
    expect(createArticle).not.toHaveBeenCalled();
  });

  it("submits on Enter, because the controls are inside a real form", async () => {
    // There were six <form> elements in the entire product and none here, so
    // Enter in the Title field did nothing at all.
    const user = userEvent.setup();
    const createArticle = vi.fn().mockResolvedValue({ id: "a1" });
    renderManager({ createArticle });
    const title = await openArticleEditor(user);

    await user.type(title, "Firewall change runbook{Enter}");

    expect(createArticle).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Firewall change runbook" }),
    );
  });

  it("caps Title at the server's 300 characters rather than letting it fail there", async () => {
    // AUDIT (HIGH): "Title field has no maxlength, so the 300-character limit
    // is only discoverable by failing" — and failing meant the 422 body,
    // which is the other half of "over-long submit dumps JSON".
    const user = userEvent.setup();
    renderManager();
    const title = await openArticleEditor(user);

    expect(title).toHaveAttribute("maxlength", "300");
  });

  it("explains an over-long Title in a sentence instead of shipping it to the server", async () => {
    // `maxLength` stops typing but not a paste or an autofill, so the bound is
    // also a rule: the user reads the limit, never a Pydantic payload.
    const user = userEvent.setup();
    const createArticle = vi.fn().mockResolvedValue({ id: "a1" });
    renderManager({ createArticle });
    const title = await openArticleEditor(user);

    fireEvent.change(title, { target: { value: "x".repeat(301) } });
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Title must be 300 characters or fewer",
    );
    expect(createArticle).not.toHaveBeenCalled();
  });

  it("states a server refusal as a sentence, through the same channel as the field errors", async () => {
    // AUDIT (HIGH): "Error surfacing is inconsistent within the same form."
    // One `role="alert"`, one vocabulary — and `String(save.error)`, which
    // rendered "Error: " for a bodyless failure, is gone.
    const user = userEvent.setup();
    renderManager({ createArticle: vi.fn().mockRejectedValue(new ApiError(409, "")) });
    const title = await openArticleEditor(user);

    await user.type(title, "Firewall change runbook");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That conflicts with something that already exists",
    );
  });

  it("drops the server's complaint once the input it described has changed", async () => {
    // AUDIT (HIGH): "Server error text persists after the user corrects the
    // input and is never cleared." A refusal describes a request that was
    // sent; after an edit it describes a submission that no longer exists.
    const user = userEvent.setup();
    renderManager({ createArticle: vi.fn().mockRejectedValue(new ApiError(409, "")) });
    const title = await openArticleEditor(user);

    await user.type(title, "Firewall change runbook");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();

    await user.type(title, " v2");

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("DocsPage new record dialog", () => {
  it("says the JSON is invalid rather than failing somewhere the user cannot see", async () => {
    // AUDIT (HIGH): "Invalid JSON in the Fields (JSON) editor produces no
    // error at all." `JSON.parse` used to run inside the mutation function,
    // where a SyntaxError is not a message about the field it came from.
    const user = userEvent.setup();
    const createDocRecord = vi.fn().mockResolvedValue({ id: "r1" });
    renderManager({ createDocRecord });
    await openRecordDialog(user);

    await user.type(screen.getByLabelText("Name"), "Core VLAN");
    const fields = screen.getByLabelText("Fields (JSON)");
    await user.clear(fields);
    await user.type(fields, "{{vlan: 10");
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Fields (JSON) is not valid JSON",
    );
    expect(createDocRecord).not.toHaveBeenCalled();
  });

  it("rejects valid JSON that is not an object, which the server would 422", async () => {
    const user = userEvent.setup();
    const createDocRecord = vi.fn().mockResolvedValue({ id: "r1" });
    renderManager({ createDocRecord });
    await openRecordDialog(user);

    await user.type(screen.getByLabelText("Name"), "Core VLAN");
    const fields = screen.getByLabelText("Fields (JSON)");
    await user.clear(fields);
    // `fireEvent`, not `type`: user-event reads "[" as a keyboard descriptor.
    fireEvent.change(fields, { target: { value: "[1, 2]" } });
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("must be a JSON object");
    expect(createDocRecord).not.toHaveBeenCalled();
  });

  it("names the missing Name on submit instead of doing nothing", async () => {
    const user = userEvent.setup();
    const createDocRecord = vi.fn().mockResolvedValue({ id: "r1" });
    renderManager({ createDocRecord });
    await openRecordDialog(user);

    const create = screen.getByRole("button", { name: "Create" });
    expect(create).toBeEnabled();
    await user.click(create);

    expect(await screen.findByRole("alert")).toHaveTextContent("Name is required");
    expect(createDocRecord).not.toHaveBeenCalled();
  });

  it("creates the record with the parsed fields when the form is answerable", async () => {
    const user = userEvent.setup();
    const createDocRecord = vi.fn().mockResolvedValue({ id: "r1" });
    renderManager({ createDocRecord });
    await openRecordDialog(user);

    await user.type(screen.getByLabelText("Name"), "Core VLAN");
    const fields = screen.getByLabelText("Fields (JSON)");
    await user.clear(fields);
    await user.type(fields, '{{"vlan": 10}');
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(createDocRecord).toHaveBeenCalledWith({
        // Was "network" -- the value the backend refuses unconditionally
        // (`_SUPERSEDED_RECORD_TYPES`). The default is now one that can be
        // saved.
        record_type: "service",
        name: "Core VLAN",
        fields: { vlan: 10 },
        // The internal client -- `/me`'s `org_id` -- until the selector
        // exists (MSP clients plan 2026-10-26 Task 14).
        client_id: "o1",
      }),
    );
  });

  it("drops the server's complaint once the input it described has changed", async () => {
    const user = userEvent.setup();
    renderManager({ createDocRecord: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    await openRecordDialog(user);

    await user.type(screen.getByLabelText("Name"), "Core VLAN");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("unavailable right now");

    await user.type(screen.getByLabelText("Name"), " 2");

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});


/**
 * The article view: version history and archive (UX remediation cycles 4/5).
 *
 * TWO DEFECTS OF THE SAME FAMILY LIVED HERE, and both were invisible rather
 * than wrong. The history line rendered `new Date(ts).toLocaleString()`, so an
 * edit's time was reported in whatever zone the reading machine was set to —
 * on the one panel whose entire purpose is "when did this change". And
 * `archive` was a mutation with NO error channel at all: a refusal left the
 * article on screen with nothing said.
 */

const ARTICLE: DocArticle = {
  id: "a1",
  client_id: "o1",
  title: "Finance share recovery runbook",
  body: "# Runbook",
  category: "Runbook",
  archived: false,
  current_version: 3,
  updated_at: "2026-08-05T23:30:00.000Z",
  links: [],
};

const VERSIONS: DocVersion[] = [
  // 23:30Z on the 5th is 08:30 on the SIXTH in Tokyo. A wrong zone on a date
  // is off by a whole day, which is much harder to notice than five hours —
  // so the fixture is deliberately parked either side of midnight.
  { version: 3, snapshot: {}, changed_by: "u1", ts: "2026-08-05T23:30:00.000Z" },
];

function renderArticleView(overrides: Partial<Record<string, unknown>> = {}) {
  return renderManager({
    listArticles: vi.fn().mockResolvedValue({
      items: [
        {
          id: "a1",
          title: ARTICLE.title,
          category: ARTICLE.category,
          archived: false,
          updated_at: ARTICLE.updated_at,
          link_count: 0,
        },
      ],
      total: 1,
    }),
    getArticle: vi.fn().mockResolvedValue(ARTICLE),
    articleVersions: vi.fn().mockResolvedValue(VERSIONS),
    archiveArticle: vi.fn().mockResolvedValue({ archived: true }),
    ...overrides,
  });
}

// Archiving is confirmed now (cycle 8), so every test that used to click one
// button clicks two. The single-click path is asserted to be inert in its own
// test below rather than being assumed here.
async function archiveArticleThroughDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Archive" }));
  await user.click(await screen.findByRole("button", { name: "Archive article" }));
}

async function openArticle(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: /Finance share recovery runbook/ }));
  await screen.findByRole("heading", { name: ARTICLE.title });
}

describe("DocsPage article view", () => {
  it("dates a version in the operator's profile zone, across the day boundary", async () => {
    // FALSIFIED by `new Date(version.ts).toLocaleString()`: under this runner
    // (UTC) that prints the 5th at 23:30, and this asserts the 6th at 08:30 —
    // a different DAY, which is the failure mode a reader would never question.
    const user = userEvent.setup();
    renderArticleView();
    await openArticle(user);
    await user.click(screen.getByRole("button", { name: "History" }));

    expect(await screen.findByText("v3 — 06 Aug 2026, 08:30")).toBeInTheDocument();
    expect(screen.queryByText(/05 Aug 2026/)).not.toBeInTheDocument();
  });

  it("says the history could not be loaded rather than showing an empty history", async () => {
    // `(versions.data ?? []).map` over a FAILED query renders nothing at all,
    // which on this panel reads as "this article has never been edited" — a
    // claim the page has no evidence for.
    const user = userEvent.setup();
    renderArticleView({
      articleVersions: vi.fn().mockRejectedValue(new ApiError(503, "")),
    });
    await openArticle(user);
    await user.click(screen.getByRole("button", { name: "History" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The version history could not be loaded");
    expect(alert).toHaveTextContent("That service is unavailable right now.");
    expect(screen.queryByText(/No earlier versions/)).not.toBeInTheDocument();
  });

  it("says an article has no earlier versions, distinctly from a failed history", async () => {
    // THE PAIR: without this, "renders the failure" is also satisfied by a
    // page that renders the failure for a genuinely empty history.
    const user = userEvent.setup();
    renderArticleView({ articleVersions: vi.fn().mockResolvedValue([]) });
    await openArticle(user);
    await user.click(screen.getByRole("button", { name: "History" }));

    expect(await screen.findByText("No earlier versions.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says so when an archive is REFUSED, instead of leaving the article there in silence", async () => {
    // The swallowed-failure class, on this route. `archive` had no `onError`
    // and its `error` was rendered nowhere: the operator clicked Archive, the
    // article stayed, and the interface offered no theory at all — which reads
    // as a broken button rather than a refusal.
    const user = userEvent.setup();
    const archiveArticle = vi.fn().mockRejectedValue(new ApiError(403, ""));
    renderArticleView({ archiveArticle });
    await openArticle(user);
    await archiveArticleThroughDialog(user);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This article was not archived.");
    expect(alert).toHaveTextContent("You do not have permission to do that.");
    // AND THE ARTICLE IS STILL THERE. A page that reported the refusal but had
    // already removed the article optimistically would be a worse lie than the
    // silence it replaced.
    expect(screen.getByRole("heading", { name: ARTICLE.title })).toBeInTheDocument();
  });

  it("keeps no refusal on screen once the operator moves to another article", async () => {
    // A refusal describes ONE request about ONE article. Carried onto the
    // next, it is a claim about a submission that never happened.
    const user = userEvent.setup();
    renderArticleView({
      listArticles: vi.fn().mockResolvedValue({
        items: [
          { id: "a1", title: ARTICLE.title, category: "Runbook", archived: false, updated_at: ARTICLE.updated_at, link_count: 0 },
          { id: "a2", title: "Windows build standard", category: "Procedure", archived: false, updated_at: ARTICLE.updated_at, link_count: 0 },
        ],
        total: 2,
      }),
      getArticle: vi.fn().mockImplementation((id: string) =>
        Promise.resolve(id === "a1" ? ARTICLE : { ...ARTICLE, id: "a2", title: "Windows build standard" }),
      ),
      archiveArticle: vi.fn().mockRejectedValue(new ApiError(403, "")),
    });
    await openArticle(user);
    await archiveArticleThroughDialog(user);
    expect(await screen.findByRole("alert")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Windows build standard/ }));
    await screen.findByRole("heading", { name: "Windows build standard" });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("archives the article when the server accepts it, so the tests above are not vacuous", async () => {
    const user = userEvent.setup();
    const archiveArticle = vi.fn().mockResolvedValue({ archived: true });
    renderArticleView({ archiveArticle });
    await openArticle(user);
    await archiveArticleThroughDialog(user);

    await waitFor(() => {
      expect(archiveArticle).toHaveBeenCalledWith("a1");
    });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

/**
 * UX remediation cycles 7/8 on /docs.
 *
 * The auditor's words for what these close:
 *   - BLOCKER: "The default value of the New record Type dropdown can never be
 *     saved, and the error tells the user to call the REST API by hand."
 *   - MEDIUM: "Archive sits beside Edit plan with no confirmation."
 *   - MEDIUM: "Search renders an empty Articles group header when a query
 *     matches only records."
 *   - MEDIUM: "Selecting an article does not change the URL — articles are not
 *     linkable."
 *   - MEDIUM: "Article editor is not a dialog: Escape does not close it and
 *     focus is never moved into it."
 *   - LOW: "Search field has no clear ('×') affordance."
 */
describe("DocsPage — cycles 7/8", () => {
  it("does not default the record Type to the one value the server always rejects", async () => {
    const user = userEvent.setup();
    renderManager();
    await openRecordDialog(user);

    const type = screen.getByLabelText("Type");
    expect(type).toHaveValue("service");
    // `network` stays listed, because existing network records remain
    // readable, editable and archivable — but it cannot be chosen for a new
    // one, and it says where it went.
    // Humanised, and the note is a parenthesis rather than an em dash: the
    // vocabulary is spelled for a reader in the picker as well as on the rows.
    const network = within(type).getByRole("option", {
      name: "Network (moved to IP address management)",
    });
    expect(network).toBeDisabled();
  });

  it("points a console user at a console, not at a REST endpoint", async () => {
    // "the remediation offered to a UI user is to issue raw HTTP calls — a
    // console user has no way to act on that."
    const user = userEvent.setup();
    renderManager();
    await openRecordDialog(user);

    const link = screen.getByRole("link", { name: /Create a subnet there/ });
    expect(link).toHaveAttribute("href", "/ipam");
    expect(screen.queryByText(/POST \/api\/v1\/ipam/)).toBeNull();
  });

  it("does NOT archive an article on the first click", async () => {
    // FALSIFIED BY the pre-cycle `onClick={() => archive.mutate()}`: the click
    // WAS the archive, which is the finding.
    const user = userEvent.setup();
    const archiveArticle = vi.fn().mockResolvedValue({ archived: true });
    renderArticleView({ archiveArticle });
    await openArticle(user);

    await user.click(screen.getByRole("button", { name: "Archive" }));
    expect(archiveArticle).not.toHaveBeenCalled();

    const dialog = await screen.findByRole("dialog", { name: "Archive article" });
    expect(within(dialog).getByText(ARTICLE.title)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(archiveArticle).not.toHaveBeenCalled();
  });

  it("styles Archive apart from Edit beside it", async () => {
    mockUseApi.mockReset();
    const user = userEvent.setup();
    renderArticleView();
    await openArticle(user);

    expect(screen.getByRole("button", { name: "Archive" })).toHaveClass("danger");
    expect(screen.getByRole("button", { name: "Edit" })).not.toHaveClass("danger");
  });

  it("puts the selected article in the URL so it can be linked", async () => {
    const user = userEvent.setup();
    renderArticleView();
    await openArticle(user);

    expect(screen.getByTestId("location-search")).toHaveTextContent("article=a1");
  });

  it("opens the article named in the URL on load", async () => {
    // The other half of linkability: a pasted link has to resolve.
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(MANAGER),
      listArticles: vi.fn().mockResolvedValue({
        items: [
          {
            id: "a1",
            title: ARTICLE.title,
            category: ARTICLE.category,
            archived: false,
            updated_at: ARTICLE.updated_at,
            link_count: 0,
          },
        ],
        total: 1,
      }),
      listDocRecords: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      listDocuments: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      getArticle: vi.fn().mockResolvedValue(ARTICLE),
    } as unknown as ApiClient);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/docs?article=a1"]}>
          <DocsPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: ARTICLE.title })).toBeInTheDocument();
  });

  it("moves focus into the editor and closes it on Escape", async () => {
    const user = userEvent.setup();
    renderManager();
    const title = await openArticleEditor(user);

    expect(title).toHaveFocus();

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByLabelText("Title")).toBeNull());
  });

  it("renders no Articles heading when only records match", async () => {
    // "An empty group heading reads as a loading state or a bug."
    const user = userEvent.setup();
    renderManager({
      searchDocs: vi
        .fn()
        .mockResolvedValue([{ id: "r1", kind: "record", title: "AuditB Net", category: "service" }]),
    });

    await user.type(await screen.findByLabelText("Search documentation"), "a");

    // Scoped to the results region: "Articles" and "Records" also name a stat
    // card and the sidebar section, so an unscoped query would answer a
    // different question than the finding asks.
    const results = await screen.findByRole("region", { name: "Search results" });
    expect(within(results).getByText("AuditB Net")).toBeInTheDocument();
    expect(within(results).getByText("Records")).toBeInTheDocument();
    expect(within(results).queryByText("Articles")).toBeNull();
  });

  it("clears the search without making the operator delete it by hand", async () => {
    const user = userEvent.setup();
    renderManager({ searchDocs: vi.fn().mockResolvedValue([]) });

    const box = await screen.findByLabelText("Search documentation");
    expect(box).toHaveAttribute("type", "search");
    await user.type(box, "zzz");

    await user.click(screen.getByRole("button", { name: "Clear search" }));
    expect(box).toHaveValue("");
    expect(box).toHaveFocus();
  });
});

/**
 * Cycle 12 — the editor's geometry, and the shape of a record chip.
 *
 * WHAT jsdom CANNOT DO, said out loud: it computes no layout, so none of these
 * can measure the 190px textarea or the label at y≈629 that the auditor
 * measured in a real browser. What they CAN pin is the mechanism that produced
 * those measurements — a textarea with no width declaration at all, and a
 * label whose only styling hook (`class="field"`) matches no rule in any
 * stylesheet this application ships. Both are asserted here as the classes
 * that replace them, which is a test that fails the moment the panes go back
 * to `<Field>`.
 */
describe("DocsPage — cycle 12", () => {
  it("gives the markdown textarea its column instead of the browser's 20 columns", async () => {
    // AUDIT (MEDIUM): "`getBoundingClientRect()` on the textarea returns
    // `width: 190` while the editor card is roughly 650px wide ... a markdown
    // table wraps after ~9 characters." 190px is `<textarea>`'s intrinsic
    // `cols="20"`: nothing ever gave it a width.
    const user = userEvent.setup();
    renderManager();
    await openArticleEditor(user);

    const textarea = screen.getByLabelText("Markdown");
    expect(textarea.tagName).toBe("TEXTAREA");
    expect(textarea.className).toContain("w-full");
  });

  it("puts both editor-pane labels above their panes, by one rule", async () => {
    // AUDIT (MEDIUM): "'Markdown' label sits below and to the left of its
    // textarea while 'Preview' sits above its pane ... two labels for two
    // side-by-side panes placed by two different rules."
    const user = userEvent.setup();
    renderManager();
    await openArticleEditor(user);

    const textarea = screen.getByLabelText("Markdown");
    const editorPane = textarea.parentElement as HTMLElement;
    const label = editorPane.querySelector("label") as HTMLElement;
    expect(label).not.toBeNull();
    expect(label.textContent).toBe("Markdown");
    // Above, not beside: a column, and the label first in it.
    expect(editorPane.className).toContain("flex-col");
    expect(
      label.compareDocumentPosition(textarea) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    // The other pane is placed by the SAME rule, which is the half of the
    // finding about consistency rather than position.
    const preview = screen.getByRole("region", { name: "Preview" });
    expect((preview.parentElement as HTMLElement).className).toContain("flex-col");
  });

  it("renders a runbook's structure with a visible hierarchy, not one flat stack", async () => {
    // AUDIT (HIGH), at the render site it was measured on: the article body.
    // `markdown.test.ts` pins the rule; this pins that the article view is
    // wired to it and no longer relies on the `.prose` classes, which name no
    // rule this application defines.
    const user = userEvent.setup();
    renderArticleView({
      getArticle: vi
        .fn()
        .mockResolvedValue({ ...ARTICLE, body: "# Heading\n\nbody\n\n- one\n- two" }),
    });
    await openArticle(user);

    const heading = document.querySelector("h1[style]") as HTMLElement;
    expect(heading.textContent).toBe("Heading");
    expect(heading.getAttribute("style")).toContain("font-size: 24px");
    const list = document.querySelector("ul[style]") as HTMLElement;
    expect(list.getAttribute("style")).toContain("list-style: disc");
    expect(document.querySelector(".prose")).toBeNull();
  });

  it("orders a record chip the same way in the search results and in the list", async () => {
    // AUDIT (LOW): "In search results a record renders as `AuditB Net`
    // followed immediately by a `service` pill; in the Records list below it
    // renders as a `service` pill followed by `AuditB Net`. Same two pieces of
    // information, opposite order in two lists 100px apart."
    const user = userEvent.setup();
    renderManager({
      listDocuments: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      listDocRecords: vi.fn().mockResolvedValue({
        items: [{ id: "r1", record_type: "service", name: "AuditB Net" }],
        total: 1,
      }),
      searchDocs: vi.fn().mockResolvedValue([
        { id: "r1", kind: "record", title: "AuditB Net", category: "service", rank: 0 },
      ]),
    });

    await user.type(await screen.findByLabelText("Search documentation"), "a");
    // Both lists are on screen at once — the search replaces the article list
    // above, not the Records card below — which is what made the two orders
    // visible together.
    await waitFor(() => expect(screen.getAllByText("AuditB Net")).toHaveLength(2));

    // THE WHOLE ROW, not the element the name happens to sit in: both lists
    // now draw the record as glyph + name + type sub-line (the article rows'
    // shape), so the assertion has to read the row to see the order at all.
    // Same claim as before -- one order in both lists -- against the row that
    // replaced the pill.
    const chips = screen
      .getAllByText("AuditB Net")
      .map((el) => (el.closest(".mini-row") as HTMLElement).textContent);
    expect(chips).toEqual(["AuditB NetService", "AuditB NetService"]);
  });
});

/**
 * THE STAT ROW: no dead tiles.
 *
 * "All tiles and all numbers or kpis need to be clickable and direct the user
 * to the specific menus, i dont want dead tiles everywhere." `StatCard`'s
 * required `to` makes the compiler insist a destination was NAMED; only a test
 * can check that the destination EXISTS. `/docs#articles` type-checks
 * perfectly whether or not anything on the page carries that id, and a
 * fragment nothing answers is as dead as no link at all.
 */
describe("DocsPage stat tiles", () => {
  const ARTICLES = {
    items: [
      { id: "a1", title: "Windows workstation build standard", category: "Procedure", archived: false, updated_at: "2026-08-01T00:00:00.000Z", link_count: 2 },
      { id: "a2", title: "Finance share recovery runbook", category: "Runbook", archived: false, updated_at: "2026-08-05T00:00:00.000Z", link_count: 0 },
    ],
    total: 2,
  };

  // Label -> destination, in DOM order. `/docs` is the module's only route in
  // App.tsx, and each number decomposes into a section of this page: the
  // library list (grouped by category, link counts rendered per row) and the
  // records list.
  const DESTINATIONS: ReadonlyArray<readonly [string, string]> = [
    ["Articles", "/docs#articles"],
    ["Records", "/docs#records"],
    ["Linked references", "/docs#articles"],
    // Not `#articles`: the Documents tile decomposes into its OWN section, so
    // it lands there. A tile pointing at a list that does not contain its rows
    // is the dead tile the operator asked us to remove, wearing an href.
    ["Documents", "/docs#documents"],
  ];

  it("renders every tile as a link with a real href", async () => {
    renderPage(() => Promise.resolve(ARTICLES));
    await screen.findByText("Windows workstation build standard");

    const cards = [...document.querySelectorAll<HTMLElement>(".stat-card")];
    expect(cards).toHaveLength(4);
    for (const card of cards) {
      // An <a>, not a div with onClick: keyboard reachable, middle-clickable
      // into a new tab, pasteable into a ticket.
      expect(card.tagName).toBe("A");
      const href = card.getAttribute("href");
      expect(href).toBeTruthy();
      expect(href).not.toBe("#");
      expect(href?.startsWith("/docs")).toBe(true);
    }
    expect(
      cards.map((card) => [card.querySelector(".stat-label")?.textContent, card.getAttribute("href")]),
    ).toEqual(DESTINATIONS.map(([label, to]) => [label, to]));
  });

  it("points every fragment at an element that is actually on the page", async () => {
    renderPage(() => Promise.resolve(ARTICLES));
    await screen.findByText("Windows workstation build standard");

    const fragments = [...document.querySelectorAll<HTMLElement>(".stat-card")]
      .map((card) => card.getAttribute("href")?.split("#")[1])
      .filter((fragment): fragment is string => Boolean(fragment));
    expect(fragments).toHaveLength(4);
    for (const fragment of fragments) {
      expect(document.getElementById(fragment)).not.toBeNull();
    }
  });

  it("keeps a tile click from destroying the page's other query-string state", async () => {
    // `?article=` is this page's selection (the linkability finding), and the
    // tiles navigate within the same route. A tile that replaced the whole
    // query string would silently close the article the reader was on.
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME),
      listArticles: () => Promise.resolve(ARTICLES),
      listDocRecords: () => Promise.resolve({ items: [], total: 0 }),
      listDocuments: () => Promise.resolve({ items: [], total: 0 }),
      getArticle: vi.fn().mockResolvedValue({
        id: "a1",
        title: "Windows workstation build standard",
        category: "Procedure",
        body: "# Build",
        current_version: 1,
        archived: false,
        updated_at: "2026-08-01T00:00:00.000Z",
        links: [],
      }),
    } as unknown as ApiClient);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/docs?article=a1"]}>
          <DocsPage />
          <LocationProbe />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await screen.findByRole("heading", { name: "Windows workstation build standard" });
    await user.click(tile("Records"));
    // Still on the same article: the tile moved the view, not the selection.
    expect(screen.getByTestId("location-search")).toHaveTextContent("?article=a1");
  });

  it("nothing on this page carries urgent weight", async () => {
    // A documentation library has no failing state. Spending the console's
    // only "look here" signal on an article count is how a row of six equally
    // loud tiles gets built.
    renderPage(() => Promise.resolve(ARTICLES));
    await screen.findByText("Windows workstation build standard");

    for (const card of document.querySelectorAll<HTMLElement>(".stat-card")) {
      expect(card.className).not.toContain("urgent");
    }
  });

  it("says WHICH zero each zero is", async () => {
    // "0 articles" on an empty library and "0 articles" on a page that has
    // measured nothing are the same pixels without these.
    renderPage(() => Promise.resolve({ items: [], total: 0 }));
    await screen.findByText("No articles yet.");

    expect(tile("Articles")).toHaveTextContent("nothing written yet");
    expect(tile("Records")).toHaveTextContent("no network, service, vendor or location records yet");
    expect(tile("Linked references")).toHaveTextContent("no article is linked to an object yet");
    expect(tile("Documents")).toHaveTextContent("no files uploaded yet");
  });

  it("gives every tile a sub-line that decomposes its own figure", async () => {
    // WHAT THIS PREVENTS: four tiles showing a caption and a number over an
    // empty band. The figure says how many; the sub-line says what of, which
    // is the fact an operator can act on.
    renderPage(
      () => Promise.resolve(ARTICLES),
      () =>
        Promise.resolve({
          items: [
            { id: "r1", record_type: "service", name: "Payroll", fields: {}, archived: false, current_version: 1 },
            { id: "r2", record_type: "service", name: "Billing", fields: {}, archived: false, current_version: 1 },
            { id: "r3", record_type: "vendor", name: "Northwind", fields: {}, archived: false, current_version: 1 },
          ],
          total: 3,
        }),
      () =>
        Promise.resolve({
          items: [
            { id: "d1", title: "MSA", description: "", category: "Contract", archived: false, current_version: 1, updated_at: "2026-08-01T00:00:00.000Z", link_count: 0 },
            { id: "d2", title: "Rack diagram", description: "", category: "Diagram", archived: false, current_version: 0, updated_at: "2026-08-02T00:00:00.000Z", link_count: 0 },
          ],
          total: 2,
        }),
    );
    await screen.findByText("Windows workstation build standard");

    // Two articles, two categories; one of the two carries a link.
    expect(tile("Articles")).toHaveTextContent("In 2 categories");
    expect(tile("Linked references")).toHaveTextContent("From 1 of 2 articles");
    // The types spelled as the rows below spell them, and pluralised.
    expect(tile("Records")).toHaveTextContent("2 services, 1 vendor");
    // `current_version: 0` is a document row with no bytes behind it yet.
    expect(tile("Documents")).toHaveTextContent("1 document with no file uploaded yet");
  });

  it("withholds the sub-line when the rows in hand are not the whole set", async () => {
    // A breakdown computed over a capped page would describe that page while
    // the figure beside it counts the library. Say nothing rather than that.
    renderPage(() => Promise.resolve({ items: ARTICLES.items, total: 9 }));
    await screen.findByText("Windows workstation build standard");

    expect(tile("Articles").querySelector(".stat-value")?.textContent).toBe("9");
    expect(tile("Articles")).not.toHaveTextContent("In 2 categories");
    expect(tile("Linked references")).not.toHaveTextContent("From 1 of 2 articles");
  });

  it("shows no number at all while the library has not answered", async () => {
    // The distinction the whole page's state machine exists for, carried up
    // into the stat row: a dead backend printed "0 articles" and looked
    // exactly like an empty knowledge base.
    renderPage(
      () => new Promise<never>(() => {}),
      () => new Promise<never>(() => {}),
      () => new Promise<never>(() => {}),
    );
    // TWO lists are pending now, so two say "Loading…". `findAllByText`
    // rather than `findByText`, which throws on more than one match.
    expect(await screen.findAllByText("Loading…")).not.toHaveLength(0);

    expect(tile("Articles").querySelector(".stat-value")?.textContent).toBe("—");
    expect(tile("Linked references").querySelector(".stat-value")?.textContent).toBe("—");
    // The Documents tile reads an em dash for the SAME reason and from its own
    // query: a library whose metadata request has not answered and one that is
    // genuinely empty must not print the same zero.
    expect(tile("Documents").querySelector(".stat-value")?.textContent).toBe("—");
    expect(tile("Articles")).not.toHaveTextContent("nothing written yet");
  });

  it("shows no number when the library request FAILED", async () => {
    renderPage(() => Promise.reject(new Error("boom")));
    await screen.findByRole("alert");

    expect(tile("Articles").querySelector(".stat-value")?.textContent).toBe("—");
    expect(tile("Articles")).not.toHaveTextContent("nothing written yet");
  });
});
