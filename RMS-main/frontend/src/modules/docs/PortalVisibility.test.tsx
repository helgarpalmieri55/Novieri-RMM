/**
 * The "Visible in the portal" switch on an article. Articles are internal
 * until someone who can edit them turns this on; the server gates the change
 * on docs.manage regardless of what the page shows.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, DocArticle, Me } from "../../lib/api";
import { DocsPage } from "./DocsPage";
import { PortalVisibility } from "./PortalVisibility";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ARTICLE: DocArticle = {
  id: "a1",
  title: "Connect to the office wifi",
  body: "Pick the Staff network.",
  category: "Help",
  archived: false,
  portal_visible: false,
  current_version: 1,
  updated_at: "2026-09-01T00:00:00.000Z",
  links: [],
};

let api: Partial<ApiClient>;

function wrap(ui: React.ReactElement) {
  mockUseApi.mockReturnValue(api as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/docs?article=a1"]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  api = {
    setArticlePortalVisibility: vi.fn().mockResolvedValue({ portal_visible: true }),
  };
});

describe("PortalVisibility", () => {
  it("is off by default and turns on for someone who can edit articles", async () => {
    const user = userEvent.setup();
    wrap(<PortalVisibility article={ARTICLE} canManage />);

    const toggle = screen.getByRole("switch", { name: "Visible in the portal" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText(/People who raise tickets can find and read it/)).toBeInTheDocument();

    await user.click(toggle);
    await waitFor(() => expect(api.setArticlePortalVisibility).toHaveBeenCalledWith("a1", true));
  });

  it("turns it off again", async () => {
    const user = userEvent.setup();
    wrap(<PortalVisibility article={{ ...ARTICLE, portal_visible: true }} canManage />);

    await user.click(screen.getByRole("switch", { name: "Visible in the portal" }));
    await waitFor(() => expect(api.setArticlePortalVisibility).toHaveBeenCalledWith("a1", false));
  });

  it("says when the change was not saved", async () => {
    const user = userEvent.setup();
    api.setArticlePortalVisibility = vi.fn().mockRejectedValue(new Error("down"));
    wrap(<PortalVisibility article={ARTICLE} canManage />);

    await user.click(screen.getByRole("switch", { name: "Visible in the portal" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Not saved.");
  });

  it("gives a reader no control, only a note when the article is in the portal", () => {
    const { unmount } = wrap(<PortalVisibility article={ARTICLE} canManage={false} />);
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByText(/portal/)).toBeNull();
    unmount();

    wrap(<PortalVisibility article={{ ...ARTICLE, portal_visible: true }} canManage={false} />);
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.getByText("Visible in the portal.")).toBeInTheDocument();
  });

  it("is not offered on an archived article, which the portal never shows", () => {
    wrap(<PortalVisibility article={{ ...ARTICLE, archived: true }} canManage />);
    expect(screen.queryByRole("switch")).toBeNull();
  });
});

describe("DocsPage", () => {
  function pageApi(permissions: string[]): Partial<ApiClient> {
    const me: Me = { user_id: "u1", org_id: "o1", email: "u@x", display_name: "U", permissions };
    return {
      ...api,
      me: vi.fn().mockResolvedValue(me),
      listArticles: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      listDocRecords: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      listDocuments: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      getArticle: vi.fn().mockResolvedValue(ARTICLE),
    };
  }

  it("shows the switch on the article to someone who can edit articles", async () => {
    api = pageApi(["docs.read", "docs.manage"]);
    wrap(<DocsPage />);
    expect(await screen.findByRole("switch", { name: "Visible in the portal" })).toBeInTheDocument();
  });

  it("does not show it to someone who can only read", async () => {
    api = pageApi(["docs.read"]);
    wrap(<DocsPage />);
    await screen.findByRole("heading", { name: ARTICLE.title });
    expect(screen.queryByRole("switch", { name: "Visible in the portal" })).toBeNull();
  });
});
