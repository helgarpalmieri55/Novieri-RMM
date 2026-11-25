/**
 * Software (inventory), migrated onto the shell components. Mocks the
 * auth-backed API client, mirroring modules/devices/DevicesPage.test.tsx.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../lib/api";
import type { ApiClient, SoftwareAggregate, SoftwareDevice } from "../../lib/api";
import { renderPage as renderAt } from "../../test/renderPage";
import { SoftwarePage } from "./SoftwarePage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function aggregate(overrides: Partial<SoftwareAggregate> = {}): SoftwareAggregate {
  return { name: "Google Chrome", version: "140.0", publisher: "Google LLC", device_count: 214, ...overrides };
}

function renderPage(overrides: Partial<ApiClient> = {}) {
  const api: Partial<ApiClient> = {
    searchSoftware: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    softwareDevices: vi.fn().mockResolvedValue([]),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <SoftwarePage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(ui);
}

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("SoftwarePage", () => {
  it("says it is loading rather than showing an empty catalog", async () => {
    renderPage({ searchSoftware: () => new Promise<never>(() => {}) });
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty catalog", async () => {
    renderPage({ searchSoftware: () => Promise.reject(new Error("boom")) });
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No software discovered/i)).not.toBeInTheDocument();
  });

  it("says nothing is discovered yet when it is genuinely empty", async () => {
    renderPage({ searchSoftware: () => Promise.resolve({ items: [], total: 0 }) });
    expect(await screen.findByText("No software discovered yet.")).toBeInTheDocument();
  });

  it("lists discovered software and counts from data, not a constant", async () => {
    renderPage({
      searchSoftware: () =>
        Promise.resolve({ items: [aggregate(), aggregate({ name: "Adobe Acrobat Pro" })], total: 2 }),
    });
    expect(await screen.findByText("Google Chrome")).toBeInTheDocument();
    expect(screen.getByText("Adobe Acrobat Pro")).toBeInTheDocument();
    // THE COUNT IS NO LONGER A STAT CARD, and the reason is the tile contract:
    // `StatCard` requires a destination now, and this number's only possible
    // destination was the table directly beneath it. It moved to the filter
    // bar, next to the box that changes it. The assertion that mattered is
    // unchanged -- the figure comes from the response, not a constant.
    // The kit's count (redesign Receta L, 8): "Showing x of y", y the server's total.
    expect(screen.getByText("Showing 2 of 2")).toBeInTheDocument();
    expect(document.querySelectorAll(".stat-card")).toHaveLength(0);
  });

  it("counts one title in the singular", async () => {
    renderPage({ searchSoftware: () => Promise.resolve({ items: [aggregate()], total: 1 }) });
    // The count renders nothing until the query resolves; asserting before
    // that would pass against a page that never counted anything. The kit's
    // sentence (redesign Receta L, 8) carries no noun to agree with the number.
    await waitFor(() => expect(screen.getByText("Showing 1 of 1")).toBeInTheDocument());
  });

  it("shows devices for a selected title, and a prompt before any selection", async () => {
    const user = userEvent.setup();
    const softwareDevices = vi
      .fn()
      .mockResolvedValue([{ device_id: "d1", hostname: "ws-alpha", version: "140.0" } as SoftwareDevice]);
    renderPage({
      searchSoftware: () => Promise.resolve({ items: [aggregate()], total: 1 }),
      softwareDevices,
    });

    // NO PROMPT BEFORE A SELECTION (redesign Task 6, 1.3): the chosen title's
    // computers open in a panel under the list, so with nothing chosen there
    // is no second pane to explain -- the list is the whole page, and nothing
    // asks for the devices of a title nobody chose.
    await screen.findByText("Google Chrome");
    expect(screen.queryByRole("heading", { level: 2, name: "Google Chrome" })).toBeNull();
    expect(softwareDevices).not.toHaveBeenCalled();

    await user.click(await screen.findByText("Google Chrome"));
    expect(softwareDevices).toHaveBeenCalledWith("Google Chrome");
    expect(await screen.findByText("ws-alpha")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Google Chrome" })).toBeInTheDocument();
  });

  /* --- getting the catalogue out of the console ------------------------- */

  it("exports through the API client, which is the only caller that is signed in", async () => {
    // THE SAME DEFECT AS DEVICES, IN A SECOND PLACE. This was
    // `<a className="btn" href="/api/v1/export/software.csv" download>`, and a
    // browser following a link of its own sends no `Authorization` header --
    // the route answers 401 and the browser saves the refusal body. Both
    // anchors were written the same way, so both had never worked; only one
    // of them was reported.
    const exportInventory = vi.fn().mockResolvedValue(new Blob(["hostname\n"]));
    Object.assign(URL, {
      createObjectURL: vi.fn().mockReturnValue("blob:software"),
      revokeObjectURL: vi.fn(),
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    renderPage({ exportInventory });

    expect(screen.queryByRole("link", { name: /export/i })).toBeNull();
    await userEvent.click(await screen.findByRole("button", { name: "Export software" }));
    expect(exportInventory).toHaveBeenCalledWith("software");
  });

  it("says the export failed rather than saving the failure", async () => {
    const exportInventory = vi.fn().mockRejectedValue(new ApiError(403, ""));
    renderPage({ exportInventory });
    await userEvent.click(await screen.findByRole("button", { name: "Export software" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /You do not have permission to do that/i,
    );
  });
});

/* --- the list kit (redesign plan Task 6, 1.3; Receta L, a list that only searches) --- */

describe("Software on the list kit", () => {
  function under(path: string, overrides: Partial<ApiClient> = {}) {
    mockUseApi.mockReturnValue({
      searchSoftware: vi.fn(async (name: string) =>
        name === "" || "google chrome".includes(name.toLowerCase())
          ? { items: [aggregate()], total: 1 }
          : { items: [], total: 0 },
      ),
      softwareDevices: vi.fn().mockResolvedValue([{ device_id: "d1", hostname: "ws-alpha", version: "140.0" } as SoftwareDevice]),
      ...overrides,
    } as unknown as ApiClient);
    return renderAt(path, <SoftwarePage />);
  }

  it("keeps its search in the URL, so a reload finds the same rows", async () => {
    const { router } = under("/software");
    await userEvent.type(await screen.findByRole("textbox", { name: "Search software" }), "chrome");
    expect(router.state.location.search).toBe("?q=chrome");
    await act(() => router.navigate("/software?q=chrome"));
    expect(screen.getByRole("textbox", { name: "Search software" })).toHaveValue("chrome");
    expect(await screen.findByRole("link", { name: "Google Chrome" })).toBeInTheDocument();
  });

  it("says why the list is empty", async () => {
    under("/software?q=nothing-is-called-this");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("opens a title's computers from ?title= and Quitar takes it away", async () => {
    const { router } = under("/software?q=chrome&title=Google%20Chrome");
    const panel = (await screen.findByRole("heading", { level: 2, name: "Google Chrome" })).closest("section") as HTMLElement;
    expect(await within(panel).findByRole("link", { name: "ws-alpha" })).toHaveAttribute("href", "/devices/d1");
    await userEvent.click(within(panel).getByRole("link", { name: /^Close/ }));
    expect(router.state.location.search).toBe("?q=chrome");
    expect(screen.queryByRole("heading", { level: 2, name: "Google Chrome" })).toBeNull();
  });

  it("counts what it shows against the server's total", async () => {
    under("/software");
    expect(await screen.findByText("Showing 1 of 1")).toBeInTheDocument();
  });
});
