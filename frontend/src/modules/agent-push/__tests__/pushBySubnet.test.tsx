import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { ApiError } from "../../../lib/api";
import { PushInstallChooser } from "../PushInstallChooser";
import type { PushRunPreviewOut } from "../types";

// **THE LAST SCREEN BEFORE A CREDENTIAL IS PRESENTED TO A WHOLE SUBNET.** Each
// assertion is a claim the screen makes to the operator: the credential comes
// first, because a run is made in its credential's client (MSP clients phase
// 2, Task 12); only that client's authorized subnets can be chosen; every
// listed machine is shown with why it is or is not ready, the count on the
// button is the ready count, and a list that changed is shown again rather
// than started.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function preview(over: Partial<PushRunPreviewOut> = {}): PushRunPreviewOut {
  return {
    prefix: "10.20.4.0/24",
    client_id: "o1",
    ambiguous_count: 0,
    credential_id: "c1",
    credential_name: "Push install · rack B4",
    list_digest: "ab".repeat(32),
    ready_count: 2,
    blocked_count: 1,
    unvouched_count: 3,
    awaiting_review_count: 1,
    run_max: 1024,
    concurrency: 50,
    rows: [
      { device_id: "d1", address: "10.20.4.12", hostname: "lis-srv-12", os_family: "linux", ready: true, reasons: [] },
      { device_id: "d2", address: "10.20.4.13", hostname: "lis-srv-13", os_family: "linux", ready: true, reasons: [] },
      { device_id: "d3", address: "10.20.4.40", hostname: "lis-srv-40", os_family: "linux", ready: false, reasons: ["host_key_unconfirmed"] },
    ],
    reason_help: {},
    ...over,
  };
}

function stubApi(overrides: Partial<Record<string, ReturnType<typeof vi.fn>>> = {}) {
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions: ["agent.push.run"] }),
    listCredentials: vi.fn().mockResolvedValue([
      { id: "c1", name: "Push install · rack B4", type: "push_install", device_group_id: null, client_id: "o1", created_at: null, last_used_at: null, has_secret: true },
      { id: "c3", name: "Push install · Porto", type: "push_install", device_group_id: null, client_id: "client-porto", created_at: null, last_used_at: null, has_secret: true },
      { id: "c2", name: "Remote control", type: "remote_control", device_group_id: null, client_id: null, created_at: null, last_used_at: null, has_secret: true },
    ]),
    listPushRunPrefixes: vi.fn().mockImplementation((credentialId: string) =>
      Promise.resolve(
        credentialId === "c1"
          ? [{ prefix: "10.20.4.0/24", client_id: "o1", address_count: 256, expires_at: null }]
          : [{ prefix: "10.30.1.0/24", client_id: "client-porto", address_count: 256, expires_at: null }],
      ),
    ),
    previewPushRun: vi.fn().mockResolvedValue(preview()),
    createPushRun: vi.fn().mockResolvedValue({ id: "run-9" }),
    listPushHostKeys: vi.fn().mockResolvedValue([]),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderChooser(machines: never[] = []) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/devices"]}>
        <Routes>
          <Route path="/devices" element={<PushInstallChooser machines={machines} onRemove={() => {}} onClose={() => {}} />} />
          <Route path="/devices/push-runs/:runId" element={<p>run page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function choose(user: ReturnType<typeof userEvent.setup>) {
  // The credential first: its client's subnets arrive asynchronously, and are
  // chosen only once they are there.
  await waitFor(() => expect(screen.getByRole("option", { name: /rack B4/ })).toBeTruthy());
  await user.selectOptions(await screen.findByLabelText("Credential"), "c1");
  await waitFor(() => expect(screen.getByRole("option", { name: /10\.20\.4\.0\/24/ })).toBeTruthy());
  await user.selectOptions(screen.getByLabelText("Subnet"), "10.20.4.0/24");
}

beforeEach(() => mockUseApi.mockReset());

describe("Push install by subnet", () => {
  it("opens on By subnet when nothing is ticked, and offers only authorized subnets, never a text box", async () => {
    const user = userEvent.setup();
    const api = stubApi();
    renderChooser();
    expect((screen.getByRole("radio", { name: /By subnet/ }) as HTMLInputElement).checked).toBe(true);
    const subnet = await screen.findByLabelText("Subnet");
    await waitFor(() => expect(screen.getByRole("option", { name: /rack B4/ })).toBeTruthy());
    await user.selectOptions(screen.getByLabelText("Credential"), "c1");
    await waitFor(() => expect(within(subnet).getAllByRole("option")).toHaveLength(2));
    expect(within(subnet).getByRole("option", { name: /10\.20\.4\.0\/24/ })).toBeTruthy();
    expect(api.listPushRunPrefixes).toHaveBeenCalledWith("c1");
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("asks for the credential before any subnet, and offers that credential's client's subnets only", async () => {
    // A run is made in its credential's client (MSP clients phase 2, Task 12):
    // nothing is listed before a credential is chosen, and another credential
    // -- of another client -- offers its own client's subnets, the subnet
    // chosen for the first one forgotten.
    const user = userEvent.setup();
    const api = stubApi();
    renderChooser();
    const subnet = (await screen.findByLabelText("Subnet")) as HTMLSelectElement;
    expect(subnet.disabled).toBe(true);
    expect(within(subnet).getAllByRole("option").map((o) => o.textContent)).toEqual(["Choose the credential first"]);
    expect(api.listPushRunPrefixes).not.toHaveBeenCalled();

    await choose(user);
    await screen.findByRole("table", { name: "Machines in this subnet" });
    await user.selectOptions(screen.getByLabelText("Credential"), "c3");
    await waitFor(() => expect(within(subnet).getByRole("option", { name: /10\.30\.1\.0\/24/ })).toBeTruthy());
    expect(api.listPushRunPrefixes).toHaveBeenLastCalledWith("c3");
    expect(within(subnet).queryByRole("option", { name: /10\.20\.4\.0\/24/ })).toBeNull();
    expect(subnet.value).toBe("");
    expect(api.previewPushRun).toHaveBeenCalledTimes(1);
    expect(api.previewPushRun).toHaveBeenCalledWith("10.20.4.0/24", "c1");
  });

  it("says why no subnet is offered to someone who may not use a stored secret", async () => {
    stubApi({
      listPushRunPrefixes: vi.fn().mockRejectedValue(
        new ApiError(
          403,
          JSON.stringify({
            detail:
              "a subnet push requires the `credentials.checkout` permission as well as `agent.push.run`",
          }),
        ),
      ),
    });
    const user = userEvent.setup();
    renderChooser();
    await waitFor(() => expect(screen.getByRole("option", { name: /rack B4/ })).toBeTruthy());
    await user.selectOptions(screen.getByLabelText("Credential"), "c1");
    expect((await screen.findByRole("alert")).textContent).toMatch(/credentials\.checkout/);
    expect(within(screen.getByLabelText("Subnet")).getAllByRole("option")).toHaveLength(1);
  });

  it("offers only push credentials", async () => {
    stubApi();
    renderChooser();
    const credential = await screen.findByLabelText("Credential");
    await waitFor(() => expect(within(credential).getByRole("option", { name: /rack B4/ })).toBeTruthy());
    expect(within(credential).queryByRole("option", { name: /Remote control/ })).toBeNull();
  });

  it("shows every listed machine with its readiness, what is not listed and why, and the ready count on the button", async () => {
    const user = userEvent.setup();
    stubApi();
    renderChooser();
    await choose(user);
    const table = await screen.findByRole("table", { name: "Machines in this subnet" });
    for (const address of ["10.20.4.12", "10.20.4.13", "10.20.4.40"]) {
      expect(within(table).getByText(address)).toBeTruthy();
    }
    expect(within(table).getAllByText("Ready")).toHaveLength(2);
    expect(within(table).getByText("Not probed")).toBeTruthy();
    expect(screen.getByText(/3 machines in this subnet are not listed because nobody has vouched for them/)).toBeTruthy();
    expect(screen.getByText(/1 machine in this subnet is not listed because it is waiting for review/)).toBeTruthy();
    // Inside one client no address is ambiguous, and nothing is said of it.
    expect(screen.queryByText(/does not name one machine|do not name one machine/)).toBeNull();
    expect(screen.getByRole("button", { name: "Start push to 2 machines" })).toBeTruthy();
    expect(screen.queryByText(/and \d+ more/i)).toBeNull();
  });

  it("says how many addresses were not listed because they name no single machine", async () => {
    const user = userEvent.setup();
    stubApi({ previewPushRun: vi.fn().mockResolvedValue(preview({ ambiguous_count: 2 })) });
    renderChooser();
    await choose(user);
    expect(await screen.findByText("2 addresses in this subnet are not listed because they do not name one machine.")).toBeTruthy();
  });

  it("says nothing about unlisted machines when there are none", async () => {
    const user = userEvent.setup();
    stubApi({ previewPushRun: vi.fn().mockResolvedValue(preview({ unvouched_count: 0, awaiting_review_count: 0 })) });
    renderChooser();
    await choose(user);
    await screen.findByRole("table", { name: "Machines in this subnet" });
    expect(screen.queryByText(/not listed because/)).toBeNull();
  });

  it("confirms exactly the digest it was shown, then opens the run page", async () => {
    const user = userEvent.setup();
    const api = stubApi();
    renderChooser();
    await choose(user);
    await user.click(await screen.findByRole("button", { name: "Start push to 2 machines" }));
    expect(api.createPushRun).toHaveBeenCalledWith({
      prefix: "10.20.4.0/24",
      credential_id: "c1",
      list_digest: "ab".repeat(32),
    });
    expect(await screen.findByText("run page")).toBeTruthy();
  });

  it("shows a changed list again instead of starting it", async () => {
    const user = userEvent.setup();
    const api = stubApi({
      createPushRun: vi.fn().mockRejectedValue(
        new ApiError(409, JSON.stringify({ detail: { code: "list_changed", message: "changed" } })),
      ),
    });
    renderChooser();
    await choose(user);
    await user.click(await screen.findByRole("button", { name: "Start push to 2 machines" }));
    expect(await screen.findByText(/changed since the list was shown/)).toBeTruthy();
    await waitFor(() => expect(api.previewPushRun).toHaveBeenCalledTimes(2));
  });

  it("cannot start when nothing is ready", async () => {
    const user = userEvent.setup();
    stubApi({ previewPushRun: vi.fn().mockResolvedValue(preview({ ready_count: 0, rows: [] })) });
    renderChooser();
    await choose(user);
    expect(((await screen.findByRole("button", { name: "Start push to 0 machines" })) as HTMLButtonElement).disabled).toBe(true);
  });
});
