/**
 * "These might help" on the portal's new-ticket form: published help
 * articles matching the subject as it is typed, readable in place, without
 * getting in the way of sending the ticket.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, Me } from "../../../lib/api";
import { HELP_SUGGEST_DEBOUNCE_MS } from "../api";
import { NewTicketPage } from "../NewTicketPage";
import type { PortalCategoryOut, PortalHelpArticle, PortalTicketDetail } from "../types";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ME: Me = {
  user_id: "req-1",
  org_id: "o1",
  email: "requester@example.com",
  display_name: "Requester",
  permissions: ["portal.ticket.read", "portal.ticket.write"],
};

const CATEGORIES: PortalCategoryOut[] = [
  { id: "cat1", key: "network", name: "Network", type: "incident" },
];

const ARTICLE: PortalHelpArticle = {
  id: "a1",
  title: "Connect to the office wifi",
  body: "Pick the **Staff** network and sign in with your usual password.",
};

const CREATED = { id: "t9" } as PortalTicketDetail;

let api: Partial<ApiClient>;

function renderPage() {
  mockUseApi.mockReturnValue(api as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/portal/new"]}>
        <NewTicketPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  api = {
    me: vi.fn().mockResolvedValue(ME),
    listPortalCategories: vi.fn().mockResolvedValue(CATEGORIES),
    createPortalTicket: vi.fn().mockResolvedValue(CREATED),
    suggestPortalHelp: vi.fn().mockResolvedValue([{ id: ARTICLE.id, title: ARTICLE.title }]),
    getPortalHelpArticle: vi.fn().mockResolvedValue(ARTICLE),
  };
});

afterEach(() => {
  vi.useRealTimers();
});

async function typeSubject(text: string) {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  await screen.findByRole("option", { name: "Network" });
  await user.type(screen.getByLabelText("Subject"), text);
  return user;
}

describe("help suggestions on the new-ticket form", () => {
  it("asks once, after typing pauses, and lists what might help", async () => {
    renderPage();
    await typeSubject("wifi keeps dropping");

    expect(api.suggestPortalHelp).not.toHaveBeenCalled();
    await act(() => vi.advanceTimersByTimeAsync(HELP_SUGGEST_DEBOUNCE_MS));

    const region = await screen.findByRole("region", { name: "These might help" });
    expect(within(region).getByRole("button", { name: ARTICLE.title })).toBeInTheDocument();
    expect(api.suggestPortalHelp).toHaveBeenCalledTimes(1);
    expect(api.suggestPortalHelp).toHaveBeenCalledWith("wifi keeps dropping");
  });

  it("does not ask for a subject too short to match anything", async () => {
    renderPage();
    await typeSubject("hi");
    await act(() => vi.advanceTimersByTimeAsync(HELP_SUGGEST_DEBOUNCE_MS * 2));

    expect(api.suggestPortalHelp).not.toHaveBeenCalled();
    expect(screen.queryByRole("region", { name: "These might help" })).toBeNull();
  });

  it("shows nothing when there is no match", async () => {
    api.suggestPortalHelp = vi.fn().mockResolvedValue([]);
    renderPage();
    await typeSubject("printer on fire");
    await act(() => vi.advanceTimersByTimeAsync(HELP_SUGGEST_DEBOUNCE_MS));

    await waitFor(() => expect(api.suggestPortalHelp).toHaveBeenCalled());
    expect(screen.queryByRole("region", { name: "These might help" })).toBeNull();
  });

  it("opens an article in the portal and keeps what was typed", async () => {
    renderPage();
    const user = await typeSubject("wifi keeps dropping");
    await act(() => vi.advanceTimersByTimeAsync(HELP_SUGGEST_DEBOUNCE_MS));

    await user.click(await screen.findByRole("button", { name: ARTICLE.title }));
    const dialog = await screen.findByRole("dialog", { name: ARTICLE.title });
    expect(await within(dialog).findByText("Staff")).toBeInTheDocument();
    expect(api.getPortalHelpArticle).toHaveBeenCalledWith("a1");

    await user.click(within(dialog).getByRole("button", { name: "Back to my ticket" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByLabelText("Subject")).toHaveValue("wifi keeps dropping");
    expect(api.createPortalTicket).not.toHaveBeenCalled();
  });

  it("says so when an article cannot be opened", async () => {
    api.getPortalHelpArticle = vi.fn().mockRejectedValue(new Error("gone"));
    renderPage();
    const user = await typeSubject("wifi keeps dropping");
    await act(() => vi.advanceTimersByTimeAsync(HELP_SUGGEST_DEBOUNCE_MS));

    await user.click(await screen.findByRole("button", { name: ARTICLE.title }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "This article could not be opened.",
    );
  });

  it("still sends the ticket with suggestions on screen", async () => {
    renderPage();
    const user = await typeSubject("wifi keeps dropping");
    await act(() => vi.advanceTimersByTimeAsync(HELP_SUGGEST_DEBOUNCE_MS));
    await screen.findByRole("region", { name: "These might help" });

    await user.selectOptions(screen.getByLabelText("Category"), "cat1");
    await user.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() =>
      expect(api.createPortalTicket).toHaveBeenCalledWith(
        expect.objectContaining({ category_id: "cat1", subject: "wifi keeps dropping" }),
      ),
    );
  });

  it("stays quiet when suggestions fail, so the form still works", async () => {
    api.suggestPortalHelp = vi.fn().mockRejectedValue(new Error("down"));
    renderPage();
    await typeSubject("wifi keeps dropping");
    await act(() => vi.advanceTimersByTimeAsync(HELP_SUGGEST_DEBOUNCE_MS));

    await waitFor(() => expect(api.suggestPortalHelp).toHaveBeenCalled());
    expect(screen.queryByRole("region", { name: "These might help" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
