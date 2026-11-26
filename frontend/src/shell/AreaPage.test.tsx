import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, Me } from "../lib/api";
import { AreaPage } from "./AreaPage";

const mockUseApi = vi.fn();
vi.mock("../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderArea(to: string, permissions: string[], at: string = to) {
  const api: Partial<ApiClient> = {
    me: vi.fn().mockResolvedValue({ permissions } as unknown as Me),
  };
  mockUseApi.mockReturnValue(api as ApiClient);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[at]}>
        <AreaPage to={to} />
        <Where />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Where the router is, so a test can read the query the page wrote. */
function Where() {
  const location = useLocation();
  return <output data-testid="where">{`${location.pathname}${location.search}`}</output>;
}

/** A section's card on the Admin index, found by its heading (a `Panel` is an unnamed <section>). */
async function card(name: string): Promise<HTMLElement> {
  return (await screen.findByRole("heading", { level: 2, name })).closest("section") as HTMLElement;
}

beforeEach(() => mockUseApi.mockReset());

describe("an area's landing page", () => {
  // RE-POINTED (redesign design §1.1, decision D10): each section was a table
  // and is a card now, so the cases find a section by its heading. What they
  // assert is unchanged.
  it("lists the area's pages this operator may open, grouped, with what each is for", async () => {
    renderArea("/admin", ["notification.read", "jobs.read", "audit.read"]);
    expect(await screen.findByRole("heading", { name: "Admin", level: 1 })).toBeInTheDocument();
    expect(within(await card("Service desk")).getByRole("link", { name: "Notifications" })).toHaveAttribute(
      "href",
      "/notifications",
    );
    expect(within(await card("Automation")).getByRole("link", { name: "Job history" })).toBeInTheDocument();
    expect(screen.getByText("Who did what, and when.")).toBeInTheDocument();
  });

  it("offers no page the rail would hide", async () => {
    renderArea("/admin", ["notification.read"]);
    await screen.findByRole("link", { name: "Notifications" });
    expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
    expect(screen.queryByRole("heading", { level: 2, name: "Platform" })).toBeNull();
  });

  it("says so when nothing in the area is open to the account", async () => {
    renderArea("/admin", ["docs.read"]);
    expect(
      await screen.findByText(/Nothing in this area is open to your account/),
    ).toBeInTheDocument();
  });

  it("keeps its search in the URL, as every filter is (Task 4 review, fix round 1)", async () => {
    renderArea("/admin", ["audit.read", "orgs.manage"], "/admin?q=audit");
    expect(await screen.findByRole("searchbox", { name: "Search Admin" })).toHaveValue("audit");
    expect(screen.getByRole("link", { name: "Audit log" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
    await userEvent.clear(screen.getByRole("searchbox", { name: "Search Admin" }));
    await userEvent.type(screen.getByRole("searchbox", { name: "Search Admin" }), "sett");
    expect(screen.getByTestId("where")).toHaveTextContent("/admin?q=sett");
  });

  it("draws the search on its own above the cards, not in a panel of its own (critique answer Q1)", async () => {
    renderArea("/admin", ["audit.read"]);
    const field = await screen.findByRole("searchbox", { name: "Search Admin" });
    expect(field.closest(".panel")).toBeNull();
  });

  it("finds an Admin page by part of its name", async () => {
    renderArea("/admin", ["audit.read", "orgs.manage"]);
    await userEvent.type(await screen.findByRole("searchbox", { name: "Search Admin" }), "audit");
    expect(screen.getByRole("link", { name: "Audit log" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
  });
});
