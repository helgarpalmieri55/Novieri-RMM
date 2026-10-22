import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../../lib/api";
import { SearchPage } from "../SearchPage";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function mockApi(overrides: Partial<Record<string, unknown>> = {}) {
  const api = {
    searchIpam: vi.fn().mockResolvedValue({
      kind: "address",
      subnets: [],
      addresses: [],
      devices: [],
      observations: 0,
    }),
    ...overrides,
  } as unknown as ApiClient;
  mockUseApi.mockReturnValue(api);
  return api;
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/ipam/search"]}>
        <SearchPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/**
 * THE RESTING STATE (design review I3).
 *
 * As shipped this route drew a search box and then nothing: bare page
 * background from under the input to the bottom of the viewport, at both
 * reviewed widths. Every sibling surface in the console answers "there is
 * nothing here yet" with the kit's `EmptyState` -- /software, /bcp/report,
 * /remote/:id -- and the finding is a CONSISTENCY finding, so these assertions
 * are about the component, not about the sentence: the glyph inside
 * `.empty-icon` (a bare `<svg>` takes Tailwind preflight's `display:block` and
 * sits flush left beside centred text) and the heading rung `EmptyState`
 * renders where a hand-rolled empty draws a `<b>`.
 */
describe("SearchPage resting state", () => {
  it("draws the kit's empty state before anything has been searched", async () => {
    mockApi();
    renderPage();

    const heading = await screen.findByRole("heading", { name: "Nothing searched yet" });
    const empty = heading.closest(".empty") as HTMLElement | null;
    expect(empty).not.toBeNull();
    // The glyph is WRAPPED. This is the half that a hand-rolled `<div
    // class="empty">` keeps getting wrong.
    expect(empty?.querySelector(".empty-icon svg")).not.toBeNull();
  });

  it("asks the server nothing while it rests", async () => {
    const api = mockApi();
    renderPage();

    await screen.findByRole("heading", { name: "Nothing searched yet" });
    // The resting state is drawn from no answer at all, which is what keeps
    // headless fixture rendering of this route from needing one.
    expect(api.searchIpam).not.toHaveBeenCalled();
  });

  it("stands down the moment a query is submitted, and does not come back with the results", async () => {
    const user = userEvent.setup();
    mockApi({
      searchIpam: vi.fn().mockResolvedValue({
        kind: "address",
        subnets: [],
        addresses: ["10.4.7.9"],
        devices: [],
        observations: 3,
      }),
    });
    renderPage();

    await screen.findByRole("heading", { name: "Nothing searched yet" });
    await user.type(
      screen.getByLabelText("Search addresses, subnets and devices"),
      "10.4.7.9",
    );
    await user.click(screen.getByRole("button", { name: /Search/ }));

    expect(await screen.findByText("10.4.7.9")).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.queryByRole("heading", { name: "Nothing searched yet" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("puts the resting title on the rung under the page heading, not two below it", async () => {
    // The panel here carries no title, so it renders no `h2`; the kit's
    // default `h3` would follow the page `h1` directly and skip a level. axe
    // reported `heading-order` on this route at BOTH widths -- and the harmony
    // gate passed it anyway, because that rule is `moderate` and the gate
    // fails on `serious`. So the guard has to live here.
    renderPage();
    expect(await screen.findByRole("heading", { name: "Nothing searched yet", level: 2 })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Nothing searched yet", level: 3 })).toBeNull();
  });
});
