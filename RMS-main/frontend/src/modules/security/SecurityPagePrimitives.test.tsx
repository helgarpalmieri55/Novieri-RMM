/**
 * Security logs against the two shared primitives (cycles 4 and 5).
 *
 * WHY THE ZONE IS NOT A COSMETIC QUESTION ON THIS PAGE. These rows are
 * governance evidence: a lockout is read here, quoted into a ticket, and
 * compared against a domain controller's own log, and the three have to agree
 * about which hour they mean. `new Date(event.ts).toLocaleString()` answered
 * with whatever zone the browser happened to be in, so the same event read
 * differently from two machines the same technician works from — auditor A's
 * "the same device reports two different last-contact times", on the page
 * where being wrong is most expensive.
 *
 * Every test here was falsified against the pre-cycle implementation.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient, type Me, type SecurityEvent } from "../../lib/api";
import { SecurityPage } from "./SecurityPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "a@b.c",
  display_name: "Alex Morgan",
  permissions: ["security.read"],
};

// 02:32 UTC. In Tokyo that is 11:32 on the 16th; in New York it is 22:32 on
// the 15th — a different HOUR and a different DAY for one instant.
const EVENT: SecurityEvent = {
  id: "11111111-1111-1111-1111-111111111111",
  device_id: "d1",
  hostname: "DC-01",
  ts: "2026-08-16T02:32:08Z",
  event_type: "logon_failure",
  username: "jrivera",
  source: "10.20.16.87",
  outcome: "failure",
};

function renderPage(overrides: Partial<ApiClient> = {}, timezone: string | null = null) {
  const api: Partial<ApiClient> = {
    me: vi.fn().mockResolvedValue({ ...ME, timezone }),
    listSecurityEvents: vi.fn().mockResolvedValue({ items: [EVENT], total: 1 }),
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

describe("SecurityPage timestamps", () => {
  it("renders an event in the SIGNED-IN USER'S profile zone", async () => {
    renderPage({}, "Asia/Tokyo");
    expect(await screen.findByText("16 Aug 2026, 11:32")).toBeInTheDocument();
  });

  it("renders the SAME instant differently for a user in another zone", async () => {
    // The pair is the assertion: a page that read the browser's zone instead of
    // the profile's would answer identically twice, and one of these two would
    // fail whatever zone the machine running the suite is set to.
    renderPage({}, "America/New_York");
    expect(await screen.findByText("15 Aug 2026, 22:32")).toBeInTheDocument();
    expect(screen.queryByText("16 Aug 2026, 11:32")).not.toBeInTheDocument();
  });

  it("renders an unparseable timestamp as an em dash, never 'Invalid Date'", async () => {
    renderPage({ listSecurityEvents: vi.fn().mockResolvedValue({ items: [{ ...EVENT, ts: "" }], total: 1 }) },
      "Asia/Tokyo");
    await screen.findByText("DC-01");
    expect(screen.queryByText(/invalid date/i)).not.toBeInTheDocument();
    // The When cell is the row's LAST cell on the v3 kit (the event leads,
    // as the design draws it), so it is located by position from the end.
    const cells = (await screen.findByText("DC-01")).closest("tr")?.querySelectorAll("td");
    const when = cells?.[cells.length - 1];
    expect(when?.textContent).toBe("—");
  });
});

describe("SecurityPage's raw-event panel", () => {
  it("announces a refused detail fetch instead of falling back to 'Select an event.'", async () => {
    // `detail.error` was read nowhere: a 403 or a 503 on
    // `GET /security-events/{id}` put the panel back into the state it is in
    // BEFORE anything is selected, so the operator's click looked like it had
    // never registered.
    renderPage({ getSecurityEvent: vi.fn().mockRejectedValue(new ApiError(403, "")) }, "Asia/Tokyo");
    const row = (await screen.findByText("DC-01")).closest("tr") as HTMLElement;
    await userEvent.click(row);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent?.trim()).not.toBe("");
    expect(alert).toHaveTextContent(/permission/i);
    expect(screen.queryByRole("heading", { name: "No event selected" })).not.toBeInTheDocument();
  });

  it("still invites a selection when nothing has been selected and nothing has failed", async () => {
    // NOW THE KIT'S `EmptyState`, NOT A BARE SENTENCE. Asserted as a HEADING
    // rather than as text: the finding was that this resting state had no
    // glyph and no emphasis while every sibling had both, and `getByText`
    // would go on passing if it reverted to a `<p>` with the same words.
    // `level: 3` is the rung under this panel's own title.
    renderPage({}, "Asia/Tokyo");
    await screen.findByText("DC-01");
    expect(screen.getByRole("heading", { name: "No event selected", level: 3 })).toBeInTheDocument();
    expect(screen.getByText(/Choose a row in the event list/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
