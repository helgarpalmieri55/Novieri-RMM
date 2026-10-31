/**
 * The Site field (phase 3 design §3.2): the client's live sites; the only one
 * already chosen; nothing listed before the client is known; and a site of
 * another client never kept.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient, ClientSiteOut } from "../lib/api";
import { SiteField } from "./SiteField";

const mockUseApi = vi.fn();
vi.mock("../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";

function site(id: string, name: string, archived: string | null = null): ClientSiteOut {
  return {
    id,
    client_id: ACME,
    name,
    address: "",
    description: "",
    egress_networks: [],
    archived_at: archived,
    device_count: 0,
  };
}

function renderSite(clientId: string | null, sites: ClientSiteOut[], value = "") {
  const listClientSites = vi.fn().mockResolvedValue(sites);
  mockUseApi.mockReturnValue({ listClientSites } as unknown as ApiClient);
  const onChange = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SiteField clientId={clientId} value={value} onChange={onChange} />
    </QueryClientProvider>,
  );
  return { listClientSites, onChange };
}

describe("SiteField", () => {
  it("asks for the client first, and reads nothing without one", () => {
    const { listClientSites } = renderSite(null, [site("s1", "Bogotá")]);
    expect(screen.getByLabelText("Site")).toBeDisabled();
    expect(screen.getByText("Choose the client first.")).toBeInTheDocument();
    expect(listClientSites).not.toHaveBeenCalled();
  });

  it("offers the client's live sites, never an archived one", async () => {
    const { listClientSites } = renderSite(ACME, [
      site("s1", "Bogotá"),
      site("s2", "Medellín"),
      site("s3", "Cali", "2026-09-01T00:00:00Z"),
    ]);
    const field = screen.getByLabelText("Site");
    await within(field).findByRole("option", { name: "Medellín" });
    expect(within(field).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Choose a site",
      "Bogotá",
      "Medellín",
    ]);
    expect(listClientSites).toHaveBeenCalledWith(ACME);
  });

  it("chooses the only site by itself", async () => {
    const { onChange } = renderSite(ACME, [site("s1", "Bogotá")]);
    await waitFor(() => expect(onChange).toHaveBeenCalledWith("s1"));
  });

  it("drops a site that is not the client's", async () => {
    const { onChange } = renderSite(ACME, [site("s1", "Bogotá"), site("s2", "Medellín")], "s-other");
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(""));
  });

  it("says when the client has no site", async () => {
    renderSite(ACME, []);
    expect(
      await screen.findByText("This client has no sites. Add one on the Clients page."),
    ).toBeInTheDocument();
  });
});
