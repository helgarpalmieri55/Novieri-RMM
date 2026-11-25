/**
 * The audit log on the kit (redesign plan, O3 3.11): the two views on the same
 * `?tab=`, the search and the action filter in the URL, an empty list that
 * says why, and no Client filter (the log is every client's: invariant 5a).
 */
import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry, type FixtureMap } from "../../fixtures/client";
import { renderPage } from "../../test/renderPage";
import { AuditPage } from "./AuditPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

describe("the audit log on the kit", () => {
  it("keeps its search in the URL, so a reload finds the same rows", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/audit", <AuditPage />, "/audit");
    const search = await screen.findByRole("textbox", { name: "Search the loaded page by action, actor or target" });
    await userEvent.type(search, "retry");
    expect(router.state.location.search).toBe("?q=retry");
    await act(() => router.navigate("/audit?q=retry"));
    expect(screen.getByRole("textbox", { name: "Search the loaded page by action, actor or target" })).toHaveValue("retry");
  });

  it("keeps the server's action filter in the URL, and sends it", async () => {
    const registry = fixtureRegistry();
    const listAuditEvents = vi.fn(registry.listAuditEvents as NonNullable<FixtureMap["listAuditEvents"]>);
    mockUseApi.mockReturnValue(fixtureClient({ ...registry, listAuditEvents }));
    renderPage("/audit?action=job.retry", <AuditPage />, "/audit");
    expect(await screen.findByRole("textbox", { name: "Filter by exact action, server-side" })).toHaveValue("job.retry");
    expect(listAuditEvents).toHaveBeenCalledWith(expect.objectContaining({ action: "job.retry" }));
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/audit?q=nothing-like-this", <AuditPage />, "/audit");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("still chooses events or integrity with ?tab=, and an unknown tab opens the events", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/audit?tab=integrity", <AuditPage />, "/audit");
    expect(await screen.findByRole("tab", { name: "Chain integrity" })).toHaveAttribute("aria-selected", "true");
    await userEvent.click(screen.getByRole("tab", { name: "Event search" }));
    expect(router.state.location.search).toBe("");
    await act(() => router.navigate("/audit?tab=nope"));
    expect(await screen.findByRole("tab", { name: "Event search" })).toHaveAttribute("aria-selected", "true");
  });

  it("draws no Client filter: the log is every client's (invariant 5a)", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/audit", <AuditPage />, "/audit");
    await screen.findByRole("table", { name: "Event search" });
    expect(screen.queryByRole("combobox", { name: "Client" })).toBeNull();
  });
});
