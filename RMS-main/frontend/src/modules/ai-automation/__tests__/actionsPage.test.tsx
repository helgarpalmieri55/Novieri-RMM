// AiActionsPage's three states (UI port spec §6, §8 test 5), which the page
// had none of before this migration: the list only ever distinguished
// "error" from "nothing rendered", and a slow request and a genuinely empty
// queue both showed a blank table.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, Me } from "../../../lib/api";
import { AiActionsPage } from "../ActionsPage";
import type { AiActionOut } from "../types";

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
