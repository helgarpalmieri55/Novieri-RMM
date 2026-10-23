import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { PushRunPage } from "../PushRunPage";
import { PushRunsPage } from "../PushRunsPage";
import type { PushRunOut, PushRunState, PushRunTargetOut } from "../types";

// The run page is where an operator watches a credential being presented to up
// to 1,024 machines. It must say where the run stands, what happened to each
// machine and why, and offer exactly the controls the run's state allows.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function run(state: PushRunState = "running", over: Partial<PushRunOut> = {}): PushRunOut {
  return {
    id: "run-1",
    prefix: "10.20.4.0/24",
    credential_id: "c1",
    credential_name: "Push install · rack B4",
    created_by: "u1",
    created_at: "2026-09-30T09:00:00Z",
    state,
    target_count: 4,
    concurrency: 50,
    list_digest: "ab".repeat(32),
    finished_at: null,
    counts: { waiting: 1, in_flight: 1, installed: 1, failed: 0, refused: 1, cancelled: 0 },
    stopped_at: state === "stopped_credential" ? "2026-09-30T09:05:00Z" : null,
    auth_failures: state === "stopped_credential" ? 3 : 0,
    auth_failure_limit: 3,
    signed_in: state !== "stopped_credential",
    ...over,
  };
}

const TARGETS: PushRunTargetOut[] = [
  { position: 0, device_id: "d1", address: "10.20.4.12", state: "installed", job_id: "j1", outcome: "installed", detail: "", reasons: [], updated_at: "2026-09-30T09:01:00Z" },
  { position: 1, device_id: "d2", address: "10.20.4.13", state: "refused", job_id: null, outcome: "", detail: "", reasons: ["already_enrolled"], updated_at: "2026-09-30T09:01:00Z" },
  { position: 2, device_id: "d3", address: "10.20.4.14", state: "in_flight", job_id: "j3", outcome: "", detail: "", reasons: [], updated_at: "2026-09-30T09:01:00Z" },
  { position: 3, device_id: "d4", address: "10.20.4.15", state: "waiting", job_id: null, outcome: "", detail: "", reasons: [], updated_at: "2026-09-30T09:01:00Z" },
];

function stubApi(state: PushRunState = "running") {
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions: ["agent.push.run"] }),
    listPushRuns: vi.fn().mockResolvedValue([run(state)]),
    getPushRun: vi.fn().mockResolvedValue(run(state)),
    listPushRunTargets: vi.fn().mockResolvedValue({ total: TARGETS.length, items: TARGETS, reason_help: {} }),
    pausePushRun: vi.fn().mockResolvedValue(run("paused")),
    resumePushRun: vi.fn().mockResolvedValue(run("running")),
    cancelPushRun: vi.fn().mockResolvedValue(run("cancelled")),
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderAt(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/devices/push-runs" element={<PushRunsPage />} />
          <Route path="/devices/push-runs/:runId" element={<PushRunPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => mockUseApi.mockReset());

describe("Push runs", () => {
  it("lists each run with its subnet, its state and where it stands", async () => {
    stubApi();
    renderAt("/devices/push-runs");
    const table = await screen.findByRole("table", { name: "Push runs" });
    expect(within(table).getByRole("link", { name: "10.20.4.0/24" }).getAttribute("href")).toBe("/devices/push-runs/run-1");
    expect(within(table).getByText("Running")).toBeTruthy();
    expect(within(table).getByText("1 installed · 0 failed · 1 refused · 2 to go")).toBeTruthy();
  });

  it("shows progress, every machine on the page, and why a machine was refused", async () => {
    stubApi();
    renderAt("/devices/push-runs/run-1");
    expect(await screen.findByRole("heading", { name: "Push run on 10.20.4.0/24" })).toBeTruthy();
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("50");
    const table = await screen.findByRole("table", { name: "Machines in this run" });
    for (const t of TARGETS) expect(within(table).getByText(t.address)).toBeTruthy();
    expect(within(table).getByText("It is running the agent now.")).toBeTruthy();
    expect(within(table).queryByText("already_enrolled")).toBeNull();
  });

  it("offers Pause and Cancel while running, and Cancel asks first", async () => {
    const user = userEvent.setup();
    const api = stubApi("running");
    renderAt("/devices/push-runs/run-1");
    await user.click(await screen.findByRole("button", { name: "Pause" }));
    expect(api.pausePushRun).toHaveBeenCalledWith("run-1");
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Cancel run" }));
    expect(api.cancelPushRun).not.toHaveBeenCalled();
    await user.click(await screen.findByRole("button", { name: "Cancel the run" }));
    expect(api.cancelPushRun).toHaveBeenCalledWith("run-1");
  });

  it("offers Resume while paused", async () => {
    const user = userEvent.setup();
    const api = stubApi("paused");
    renderAt("/devices/push-runs/run-1");
    await user.click(await screen.findByRole("button", { name: "Resume" }));
    expect(api.resumePushRun).toHaveBeenCalledWith("run-1");
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
  });

  it("shows a run the lockout guard stopped, why, and offers Resume and Cancel but not Pause", async () => {
    const user = userEvent.setup();
    const api = stubApi("stopped_credential");
    renderAt("/devices/push-runs/run-1");
    expect(await screen.findByText(/refused by 3 machines/)).toBeTruthy();
    expect(screen.getByText(/starts again with one machine/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
    expect(screen.getByRole("button", { name: "Cancel run" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Resume" }));
    expect(api.resumePushRun).toHaveBeenCalledWith("run-1");
  });

  it("says a run is going one machine at a time until a sign-in succeeds", async () => {
    const api = stubApi("running");
    api.getPushRun.mockResolvedValue(run("running", { signed_in: false }));
    renderAt("/devices/push-runs/run-1");
    expect(await screen.findByText(/One machine at a time until a sign-in succeeds/)).toBeTruthy();
  });

  it("offers nothing once cancelled, and says a cancelled run is final", async () => {
    stubApi("cancelled");
    renderAt("/devices/push-runs/run-1");
    expect(await screen.findByText(/A cancelled run cannot be resumed/)).toBeTruthy();
    for (const name of ["Pause", "Resume", "Cancel run"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
  });
});
