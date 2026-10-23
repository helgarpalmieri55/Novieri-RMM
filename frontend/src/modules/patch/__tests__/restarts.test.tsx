import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { RestartsPanel } from "../RestartsPanel";
import { describeMinutes, restartConfirmBody, restartLabel, restartSentence, restartWhen } from "../restartCopy";
import type { PatchRestartListItem } from "../types";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderWith(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const BOOKED: PatchRestartListItem = {
  id: "r1",
  device_id: "d1",
  hostname: "LAPTOP-7",
  state: "scheduled",
  origin: "apply",
  scheduled_for: "2026-09-26T16:00:00Z",
  user_present: true,
  reason: null,
  created_at: "2026-09-26T08:00:00Z",
  closed_at: null,
};

describe("restart copy", () => {
  it("says when a booked restart happens in the given zone", () => {
    expect(restartSentence(BOOKED, "Europe/London")).toBe(
      "The person signed in was warned; it restarts at 26 Sept 2026, 17:00.",
    );
    expect(restartSentence({ ...BOOKED, user_present: false }, "UTC")).toBe(
      "It restarts at 26 Sept 2026, 16:00.",
    );
  });

  it("names the window rather than a schedule expression", () => {
    const text = restartSentence({ ...BOOKED, state: "pending", scheduled_for: null }, null);
    expect(text).toContain("maintenance window");
    expect(text).not.toMatch(/\*|\d \d/);
  });

  it("says an operator's restart was asked for, not that updates need one", () => {
    const text = restartSentence({ ...BOOKED, state: "pending", origin: "operator", scheduled_for: null }, null);
    expect(text).toBe("A restart was asked for. It will be sent in the next maintenance window.");
  });

  it("tells whoever books a restart when it happens and what a signed-in user sees", () => {
    const closed = { open_now: false, opens_at: "2026-09-26T01:00:00Z", user_warning_minutes: 1440 };
    expect(restartConfirmBody(closed, "Europe/Lisbon")).toBe(
      "It restarts in its next maintenance window, which opens 26 Sept 2026, 02:00 (Europe/Lisbon). " +
        "If someone is signed in, they get 1 day of warning first and can restart sooner themselves. " +
        "If the computer is offline, it waits until it is back.",
    );
    expect(restartWhen({ ...closed, open_now: true }, "UTC")).toContain("open now");
    expect(restartWhen({ ...closed, opens_at: null }, "UTC")).toContain("does not open in the next week");
    expect(restartConfirmBody(null, null)).toContain("next maintenance window");
    expect(describeMinutes(15)).toBe("15 minutes");
    expect(describeMinutes(90)).toBe("90 minutes");
    expect(describeMinutes(120)).toBe("2 hours");
  });

  it("gives the server's reason for a failed restart", () => {
    expect(
      restartSentence(
        { ...BOOKED, state: "failed", reason: "the agent cannot restart this device; update the agent" },
        null,
      ),
    ).toBe("The agent cannot restart this device; update the agent.");
    expect(restartLabel("failed").kind).toBe("crit");
  });
});

describe("RestartsPanel", () => {
  it("lists the machines waiting to restart", async () => {
    mockUseApi.mockReturnValue({
      listPatchRestarts: vi.fn().mockResolvedValue({
        items: [BOOKED, { ...BOOKED, id: "r2", hostname: "SRV-1", state: "pending", scheduled_for: null }],
        total: 2,
      }),
    });
    renderWith(<RestartsPanel zone="UTC" />);
    expect(await screen.findByText("LAPTOP-7")).toBeTruthy();
    expect(screen.getByText("Restart booked")).toBeTruthy();
    expect(screen.getByText("Waiting for window")).toBeTruthy();
    expect(screen.getByText("2 waiting")).toBeTruthy();
  });

  it("says so when nothing is waiting", async () => {
    mockUseApi.mockReturnValue({
      listPatchRestarts: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    });
    renderWith(<RestartsPanel zone={null} />);
    expect(await screen.findByText("No machine is waiting to restart after updates.")).toBeTruthy();
  });
});
