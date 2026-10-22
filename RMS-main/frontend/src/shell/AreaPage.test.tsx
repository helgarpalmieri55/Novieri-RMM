import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, Me } from "../lib/api";
import { AreaPage } from "./AreaPage";

const mockUseApi = vi.fn();
vi.mock("../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderArea(to: string, permissions: string[]) {
  const api: Partial<ApiClient> = {
    me: vi.fn().mockResolvedValue({ permissions } as unknown as Me),
  };
  mockUseApi.mockReturnValue(api as ApiClient);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AreaPage to={to} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => mockUseApi.mockReset());

describe("an area's landing page", () => {
  it("lists the area's pages this operator may open, grouped, with what each is for", async () => {
    renderArea("/admin", ["notification.read", "jobs.read", "audit.read"]);
    expect(await screen.findByRole("heading", { name: "Admin", level: 1 })).toBeInTheDocument();
    const serviceDesk = await screen.findByRole("table", { name: "Service desk" });
    expect(within(serviceDesk).getByRole("link", { name: "Notifications" })).toHaveAttribute(
      "href",
      "/notifications",
    );
    expect(
      within(screen.getByRole("table", { name: "Automation" })).getByRole("link", {
        name: "Job history",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("Who did what, and when.")).toBeInTheDocument();
  });

  it("offers no page the rail would hide", async () => {
    renderArea("/admin", ["notification.read"]);
    await screen.findByRole("link", { name: "Notifications" });
    expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
    expect(screen.queryByRole("table", { name: "Platform" })).toBeNull();
  });

  it("says so when nothing in the area is open to the account", async () => {
    renderArea("/admin", ["docs.read"]);
    expect(
      await screen.findByText(/Nothing in this area is open to your account/),
    ).toBeInTheDocument();
  });

});
