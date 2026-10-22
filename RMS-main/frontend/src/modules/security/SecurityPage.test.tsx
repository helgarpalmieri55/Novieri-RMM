/**
 * Security logs, migrated onto the design's "Event search" tab
 * (renderSecurityLogs in docs/design/itms-prototype.html). Mocks the
 * auth-backed API client, mirroring modules/devices/DevicesPage.test.tsx.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, SecurityEvent } from "../../lib/api";
import { SecurityPage } from "./SecurityPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

function event(overrides: Partial<SecurityEvent> = {}): SecurityEvent {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    device_id: "d1",
    hostname: "DC-01",
    ts: "2026-08-16T14:32:08Z",
    event_type: "logon_failure",
    username: "jrivera",
    source: "10.20.16.87",
    outcome: "failure",
    ...overrides,
  };
}

function renderPage(overrides: Partial<ApiClient> = {}) {
  const api: Partial<ApiClient> = {
    listSecurityEvents: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    getSecurityEvent: vi.fn(),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/security"]}>
        <SecurityPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(ui);
}

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("SecurityPage", () => {
  it("says it is loading rather than showing an empty log", async () => {
    renderPage({ listSecurityEvents: () => new Promise<never>(() => {}) });
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty log", async () => {
    renderPage({ listSecurityEvents: () => Promise.reject(new Error("boom")) });
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No events in this window/i)).not.toBeInTheDocument();
  });

  it("says the window is empty when it is genuinely empty", async () => {
    renderPage({ listSecurityEvents: () => Promise.resolve({ items: [], total: 0 }) });
    expect(await screen.findByText("No events in this window")).toBeInTheDocument();
  });

  it("lists events with the design's structure and counts from data", async () => {
    renderPage({
      listSecurityEvents: () =>
        Promise.resolve({
          items: [event(), event({ id: "2", hostname: "MACBOOK-14", event_type: "logon_success" })],
          total: 2,
        }),
    });
    expect(await screen.findByText("DC-01")).toBeInTheDocument();
    expect(screen.getByText("MACBOOK-14")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Security logs" })).toBeInTheDocument();

    const stats = document.querySelectorAll(".stat-card");
    const values = [...stats].map((card) => card.querySelector(".stat-value")?.textContent);
    // Events in window: total (2). Failed logins (loaded): 1. Distinct
    // principals (loaded): 1 (both events are jrivera).
    expect(values).toEqual(["2", "1", "1"]);
  });

  it("shows no fabricated Reporting endpoints or Saved searches card", async () => {
    // The design's Security logs screen has four stat cards; two have no
    // backing endpoint (see the comment atop SecurityPage.tsx). Omitted, not
    // faked, mirroring DevicesPage.test.tsx's "no fabricated column" test.
    renderPage({ listSecurityEvents: () => Promise.resolve({ items: [event()], total: 1 }) });
    await screen.findByText("DC-01");
    expect(screen.queryByText("Reporting endpoints")).not.toBeInTheDocument();
    expect(screen.queryByText("Saved searches")).not.toBeInTheDocument();
  });

  it("selecting a row fetches and shows the raw event", async () => {
    const user = userEvent.setup();
    const getSecurityEvent = vi.fn().mockResolvedValue({ ...event(), raw: { foo: "bar" } });
    renderPage({
      listSecurityEvents: () => Promise.resolve({ items: [event()], total: 1 }),
      getSecurityEvent,
    });
    const row = (await screen.findByText("DC-01")).closest("tr") as HTMLElement;
    await user.click(row);
    expect(getSecurityEvent).toHaveBeenCalledWith(event().id);
    // Labelled, not dumped -- see SecurityPagePrimitives.test.tsx's raw-event
    // block for the assertion that the JSON is behind a disclosure.
    expect(await screen.findByText("Foo")).toBeInTheDocument();
  });
});
