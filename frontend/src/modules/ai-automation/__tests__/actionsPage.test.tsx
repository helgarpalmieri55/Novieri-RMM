// AiActionsPage's three states (UI port spec §6, §8 test 5), which the page
// had none of before this migration: the list only ever distinguished
// "error" from "nothing rendered", and a slow request and a genuinely empty
// queue both showed a blank table.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, Me } from "../../../lib/api";
import { AiActionsPage } from "../ActionsPage";
import type { AiActionOut } from "../types";
import { renderPage as renderUnderRouter } from "../../../test/renderPage";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "a@b.c",
  display_name: "Alex Morgan",
  permissions: ["ai.act.propose", "ai.act.confirm", "ai.admin"],
};

function action(overrides: Partial<AiActionOut> = {}): AiActionOut {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    session_id: "s1",
    persona: "admin",
    approved_action_key: "disk.cleanup.workstation",
    job_type: "deploy.script",
    target_device_id: "d1",
    risk_class: "medium",
    destructive: false,
    effective_autonomy: "require_approval",
    tainted: false,
    state: "previewed",
    expires_at: new Date(Date.now() + 600_000).toISOString(),
    confirmed_by: null,
    rejected_reason: null,
    job_id: null,
    created_at: "2026-08-16T09:00:00Z",
    ...overrides,
  };
}

function renderPage(listAiActions: () => Promise<unknown>) {
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue(ME),
    listAiActions,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AiActionsPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(ui);
}

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("AiActionsPage", () => {
  it("says it is loading rather than showing an empty queue", async () => {
    renderPage(() => new Promise<never>(() => {}));
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty queue", async () => {
    renderPage(() => Promise.reject(new Error("boom")));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/Nothing is awaiting a decision/i)).not.toBeInTheDocument();
  });

  it("says the queue is empty when it is genuinely empty", async () => {
    renderPage(() => Promise.resolve([]));
    expect(await screen.findByText("Nothing is awaiting a decision.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lists prepared actions with a link to the confirmation screen", async () => {
    renderPage(() => Promise.resolve([action(), action({ id: "2", approved_action_key: "backup.retry" })]));
    const link = await screen.findByRole("link", { name: "disk.cleanup.workstation" });
    expect(link).toHaveAttribute("href", "/assistant/actions/11111111-1111-1111-1111-111111111111");
    expect(screen.getByRole("link", { name: "backup.retry" })).toBeInTheDocument();
    const head = within(document.querySelector("thead") as HTMLElement);
    expect(head.getByText("Action")).toBeInTheDocument();
    expect(head.getByText("What runs")).toBeInTheDocument();
    expect(head.getByText("Asked by")).toBeInTheDocument();
  });

  it("marks a destructive and a tainted action, without inventing either flag for the rest", async () => {
    renderPage(() =>
      Promise.resolve([
        action({ destructive: true }),
        action({ id: "2", approved_action_key: "backup.retry", tainted: true }),
        action({ id: "3", approved_action_key: "agent.restart" }),
      ]),
    );
    await screen.findByRole("link", { name: "disk.cleanup.workstation" });
    expect(screen.getByText("Destructive")).toBeInTheDocument();
    expect(screen.getByText("Prompted by untrusted content")).toBeInTheDocument();
  });
});

// The list pattern (redesign plan, Task 12, 7.6; Receta L): the state chips
// live in the URL, the list says how many it shows, and an empty list says why.
describe("AiActionsPage on the list pattern", () => {
  const LIST = [
    action(),
    action({ id: "2", approved_action_key: "backup.retry", state: "dispatched" }),
    action({ id: "3", approved_action_key: "agent.restart", state: "rejected" }),
  ];

  function underRouter(path: string, list: AiActionOut[] = LIST) {
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME),
      listAiActions: vi.fn().mockResolvedValue(list),
    } as unknown as ApiClient);
    return renderUnderRouter(path, <AiActionsPage />);
  }

  // A SUB-PAGE WITH NO RAIL ENTRY TAKES ITS OWN NAME (coordinator ruling 1b;
  // O7 review): the shell titles the tab "Assistant", the entry it lights,
  // and the page puts its h1 in front of that.
  it("is named by its h1, and the browser tab leads with it", async () => {
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME),
      listAiActions: vi.fn().mockResolvedValue(LIST),
    } as unknown as ApiClient);
    renderUnderRouter("/assistant/actions", <AiActionsPage />, "/assistant/actions", {
      page: "shell.nav.item.assistant",
    });
    expect(await screen.findByRole("heading", { level: 1, name: "Assistant actions" })).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("Assistant actions · Assistant · Novieri RMM"));
  });

  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    const { router } = underRouter("/assistant/actions?state=rejected");
    const chips = await screen.findByRole("group", { name: "State" });
    expect(within(chips).getByRole("button", { name: /Declined/ })).toHaveAttribute("aria-pressed", "true");
    expect(await screen.findByRole("link", { name: "agent.restart" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "disk.cleanup.workstation" })).toBeNull();
    await userEvent.click(within(chips).getByRole("button", { name: /Awaiting approval/ }));
    expect(router.state.location.search).toContain("state=previewed");
    expect(await screen.findByRole("link", { name: "disk.cleanup.workstation" })).toBeInTheDocument();
  });

  it("counts a dispatched and a confirmed action as Ran", async () => {
    underRouter("/assistant/actions?state=ran", [...LIST, action({ id: "4", approved_action_key: "kb.apply", state: "confirmed" })]);
    expect(await screen.findByRole("link", { name: "backup.retry" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "kb.apply" })).toBeInTheDocument();
    expect(screen.getByText("Showing 2 of 4")).toBeInTheDocument();
  });

  it("says why the list is empty", async () => {
    underRouter("/assistant/actions?state=expired");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });
});
