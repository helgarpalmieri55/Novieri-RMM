/**
 * The error monitor against the two shared primitives (cycles 4 and 5).
 *
 * THE ZONE IS THE WHOLE WORKFLOW HERE. This page captures no exception text by
 * design, so what an operator leaves with is a raise site and a pair of
 * timestamps that they then go and grep the logs for. A page that reports those
 * in the browser's zone while the logs are written in another sends them
 * looking in the wrong hour and finding nothing.
 *
 * Every test here was falsified against the pre-cycle implementation.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient, type Me } from "../../lib/api";
import { ErrorMonitorPage } from "./ErrorMonitorPage";
import type { ErrorGroup, ErrorGroupDetail, ErrorGroupList } from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "a@b.c",
  display_name: "Alex Morgan",
  // BOTH CODES. The triage controls are drawn only for a caller holding
  // `platform.errors.write` (cycle 13), so a read-only fixture here would
  // remove the button these tests click rather than test the failure path
  // behind it — the 403 below is a permission REVOKED between this cached
  // `/me` and the click, which is exactly the case a hidden button cannot
  // prevent.
  permissions: ["platform.errors.read", "platform.errors.write"],
};

// 02:30 UTC: 11:30 on the 16th in Tokyo, 22:30 on the 15th in New York.
const GROUP: ErrorGroup = {
  id: "11111111-1111-1111-1111-111111111111",
  fingerprint: "abc123def4567890",
  exc_type: "ValueError",
  module: "app.jobs.dispatch",
  lineno: 142,
  qualname: "dispatch_job",
  where: "worker",
  first_seen: "2026-08-10T12:00:00Z",
  last_seen: "2026-08-16T02:30:00Z",
  event_count: 7,
  resolved_at: null,
};

const LIST: ErrorGroupList = { groups: [GROUP], dropped: 0 };

const DETAIL: ErrorGroupDetail = {
  group: GROUP,
  events: [
    {
      id: "33333333-3333-3333-3333-333333333333",
      occurred_at: "2026-08-16T02:30:00Z",
      request_id: "req-abc-999",
      org_id: null,
      user_id: null,
      job_id: null,
      device_id: null,
    },
  ],
};

function renderPage(overrides: Partial<ApiClient> = {}, timezone: string | null = null) {
  const api: Partial<ApiClient> = {
    me: vi.fn().mockResolvedValue({ ...ME, timezone }),
    listErrorGroups: vi.fn().mockResolvedValue(LIST),
    getErrorGroup: vi.fn().mockResolvedValue(DETAIL),
    resolveErrorGroup: vi.fn().mockResolvedValue({ ...GROUP, resolved_at: "2026-08-16T03:00:00Z" }),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // The page reads its filters from the query string (cycle 12), so it needs
  // a router around it exactly as the shell gives it one.
  const ui: ReactElement = (
    <MemoryRouter initialEntries={["/error-monitor"]}>
      <QueryClientProvider client={queryClient}>
        <ErrorMonitorPage />
      </QueryClientProvider>
    </MemoryRouter>
  );
  return { ...render(ui), api };
}

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("ErrorMonitorPage timestamps", () => {
  it("renders last-seen in the SIGNED-IN USER'S profile zone", async () => {
    renderPage({}, "Asia/Tokyo");
    expect(await screen.findByText("16 Aug 2026, 11:30")).toBeInTheDocument();
  });

  it("renders the SAME instant differently for a user in another zone", async () => {
    renderPage({}, "America/New_York");
    expect(await screen.findByText("15 Aug 2026, 22:30")).toBeInTheDocument();
    expect(screen.queryByText("16 Aug 2026, 11:30")).not.toBeInTheDocument();
  });

  it("renders an event's occurred-at in the same zone as the group's", async () => {
    // Two tables, one zone. They used to share a helper that read the browser,
    // so they agreed by accident; they must now agree on purpose.
    renderPage({}, "Asia/Tokyo");
    await userEvent.click((await screen.findByText("ValueError")).closest("tr") as HTMLElement);
    const eventRow = (await screen.findByText("req-abc-999")).closest("tr") as HTMLElement;
    expect(within(eventRow).getByText("16 Aug 2026, 11:30")).toBeInTheDocument();
  });

  it("renders an unparseable timestamp as an em dash, never 'Invalid Date'", async () => {
    renderPage(
      { listErrorGroups: vi.fn().mockResolvedValue({ groups: [{ ...GROUP, last_seen: "" }], dropped: 0 }) },
      "Asia/Tokyo",
    );
    const row = (await screen.findByText("ValueError")).closest("tr") as HTMLElement;
    expect(screen.queryByText(/invalid date/i)).not.toBeInTheDocument();
    expect(within(row).getAllByText("—").length).toBeGreaterThan(0);
  });
});

describe("ErrorMonitorPage's resolve failure", () => {
  it("announces a bodyless 503 as a sentence instead of printing the error object", async () => {
    // `String(resolve.error)` printed "ApiError: " and then whatever the body
    // happened to be — the class name for the operator to read, and nothing at
    // all when the body was empty.
    renderPage({ resolveErrorGroup: vi.fn().mockRejectedValue(new ApiError(503, "")) }, "Asia/Tokyo");
    const row = (await screen.findByText("ValueError")).closest("tr") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: /resolve/i }));
    // Cycle 12: the write is behind a confirmation now.
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Resolve group" }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable/i);
    expect(alert.textContent).not.toContain("ApiError");
  });

  it("names a 403 as a permission problem rather than as a class name", async () => {
    // RBAC is server-side and it is what actually decides. The console hides
    // the control from a caller whose cached `/me` lacks the code, which
    // narrows this alert's audience to the case that matters and cannot be
    // designed away: the permission was withdrawn after the page loaded.
    renderPage({ resolveErrorGroup: vi.fn().mockRejectedValue(new ApiError(403, "")) }, "Asia/Tokyo");
    const row = (await screen.findByText("ValueError")).closest("tr") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: /resolve/i }));
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Resolve group" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(/permission/i);
  });
});
