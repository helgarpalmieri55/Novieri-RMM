/**
 * Software (inventory), migrated onto the shell components. Mocks the
 * auth-backed API client, mirroring modules/devices/DevicesPage.test.tsx.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../lib/api";
import type { ApiClient, SoftwareAggregate, SoftwareDevice } from "../../lib/api";
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
    expect(screen.getByTestId("software-count")).toHaveTextContent("2 titles");
    expect(document.querySelectorAll(".stat-card")).toHaveLength(0);
  });

  it("counts one title in the singular", async () => {
    renderPage({ searchSoftware: () => Promise.resolve({ items: [aggregate()], total: 1 }) });
    // The count renders "…" until the query resolves; asserting before that
    // would pass against a page that never counted anything.
    await waitFor(() =>
      expect(screen.getByTestId("software-count")).toHaveTextContent("1 title"),
    );
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

    // The prompt is the kit's `EmptyState`: a glyph in `.empty-icon`, a
    // heading naming what is missing, and the sentence saying why the panel is
    // empty. The hand-rolled `<div class="empty">` it replaced had a bare
    // `<svg>` (flush left under Tailwind preflight, beside centred text) and no
    // "why" line at all.
    const prompt = await screen.findByRole("heading", { name: "No software selected" });
    const empty = prompt.closest(".empty") as HTMLElement;
    expect(empty.querySelector(".empty-icon svg")).not.toBeNull();
    // AND THE SENTENCE NAMES NO DIRECTION (design review I6). It used to say
    // "in the list to the left", which is a fact about 1440 only: below the
    // kit's 640px fold the two panes stack and the list is above. The copy
    // must be true at every width, so it names the list rather than a side --
    // any of "left", "right", "above" or "below" in this body is the bug.
    const body = within(empty).getByText(/Choose a title from the software inventory/);
    expect(body).toBeInTheDocument();
    expect(body.textContent ?? "").not.toMatch(/\b(left|right|above|below|beside)\b/i);

    await user.click(await screen.findByText("Google Chrome"));
    expect(softwareDevices).toHaveBeenCalledWith("Google Chrome");
    expect(await screen.findByText("ws-alpha")).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "No software selected" }),
    ).not.toBeInTheDocument();
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
