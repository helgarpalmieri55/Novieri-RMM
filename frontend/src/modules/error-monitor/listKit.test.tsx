/**
 * The error monitor on the list kit (redesign plan, O3 3.8): the filters as
 * chips in the URL, an empty list that says why, the open group from
 * `?group=`, and the resolve confirmation unchanged. Over the fixtures.
 */
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../fixtures/client";
import { renderPage } from "../../test/renderPage";
import { ErrorMonitorPage } from "./ErrorMonitorPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

describe("the error monitor on the list kit", () => {
  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/error-monitor?state=open", <ErrorMonitorPage />, "/error-monitor");
    const state = await screen.findByRole("group", { name: "Filter by triage state" });
    expect(within(state).getByRole("button", { name: "Open" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(state).getByRole("button", { name: "Resolved" }));
    expect(router.state.location.search).toBe("?state=resolved");
    await userEvent.click(within(screen.getByRole("group", { name: "Filter by last seen" })).getByRole("button", { name: "7 days" }));
    expect(router.state.location.search).toBe("?state=resolved&since=7");
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/error-monitor?q=nothing-like-this", <ErrorMonitorPage />, "/error-monitor");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("says nothing has failed when nothing has", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listErrorGroups: () => ({ groups: [], dropped: 0 }) }));
    renderPage("/error-monitor", <ErrorMonitorPage />, "/error-monitor");
    expect(await screen.findByText("No error groups. Nothing has failed.")).toBeInTheDocument();
  });

  it("opens a group from ?group=", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/error-monitor?group=eg-1", <ErrorMonitorPage />, "/error-monitor");
    expect(await screen.findByRole("heading", { name: "Recent events of PatchValidationError" })).toBeInTheDocument();
    expect(await screen.findByText("job-2")).toBeInTheDocument();
    // Opening another group is a link too.
    const row = screen.getByText("ConnectionResetError").closest("tr") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: /Events/ }));
    expect(router.state.location.search).toBe("?group=eg-2");
  });

  it("still asks before resolving", async () => {
    const resolveErrorGroup = vi.fn();
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), resolveErrorGroup }));
    renderPage("/error-monitor", <ErrorMonitorPage />, "/error-monitor");
    const row = (await screen.findByText("PatchValidationError")).closest("tr") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Resolve…" }));
    expect(await screen.findByRole("dialog", { name: "Resolve error group" })).toBeInTheDocument();
    expect(resolveErrorGroup).not.toHaveBeenCalled();
  });
});
