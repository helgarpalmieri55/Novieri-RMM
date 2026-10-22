/**
 * Waiting for review (install-link spec 2026-09-28 §1.5).
 *
 * A machine that joined through the install link is visible and held back. The
 * list says so, says how and from where it joined, and -- for a holder of
 * `devices.review` -- offers Approve, and Reject behind a confirmation that
 * says what Reject does. A Reject the agent cannot carry out itself tells the
 * technician to remove the agent by hand.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import type { ApiClient, Device, DeviceListParams, DevicePage, Me } from "../../lib/api";
import { DevicesPage } from "./DevicesPage";
import { joinedThroughLink, manualRemovalStatus, recordedByLabel } from "./reviewLabels";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

function device(overrides: Partial<Device>): Device {
  return {
    id: "n1", hostname: "lis-ws-041", fqdn: null, os_family: "windows", os_version: "11 24H2",
    architecture: "amd64", device_group_id: null, status: "active", agent_status: "online",
    last_seen_at: "2026-09-28T09:00:00Z", cert_not_after: null, cert_state: "ok", cert_revoked: false,
    lifecycle: "in_service", asset_type: "computer", source: "agent", provenance: "agent",
    description: null, primary_ip: null, owner: null, location: null,
    review_state: "approved", link_joined_from: null, link_joined_at: null,
    ...overrides,
  };
}

const PENDING = device({
  id: "p1", hostname: "DESKTOP-7Q2M4KD", review_state: "pending",
  link_joined_from: "10.50.0.9", link_joined_at: new Date(Date.now() - 35 * 60_000).toISOString(),
});
const APPROVED = device({ id: "n1" });

function listFrom(estate: Device[]) {
  return vi.fn(async (params: DeviceListParams = {}): Promise<DevicePage> => {
    const pending = estate.filter((d) => d.review_state === "pending");
    const items = params.state === "review" ? pending : estate;
    return {
      items, total: items.length,
      state_counts: { all: estate.length, online: estate.length, offline: 0, assets: 0, certs: 0, review: pending.length },
      fleet: { total: estate.length, managed: estate.length, online: estate.length, cert_attention: 0 },
    };
  });
}

function renderDevices(api: Partial<ApiClient>, permissions: string[], path = "/devices?state=review") {
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue({ permissions } as Me),
    ...api,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/devices" element={<DevicesPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => mockUseApi.mockReset());

describe("Waiting for review", () => {
  it("has its own tab, and a row says how, from where and that it waits", async () => {
    renderDevices({ listDevices: listFrom([PENDING, APPROVED]) }, ["devices.read", "devices.review"]);
    // The row first: the strip renders before the list answers, with every
    // count at 0, so the tab's count is only the server's once a row is in.
    const row = (await screen.findByText("DESKTOP-7Q2M4KD")).closest("tr")!;
    const tab = screen.getByRole("tab", { name: /Waiting for review/ });
    expect(tab).toHaveAttribute("aria-selected", "true");
    expect(tab).toHaveTextContent("1");
    expect(row).toHaveTextContent("Joined through the install link from 10.50.0.9");
    expect(row).toHaveTextContent("Install link");
    expect(within(row).getByText("Waiting for review")).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /Run job/ })).not.toBeInTheDocument();
  });

  it("approve asks the server and refreshes the list", async () => {
    const listDevices = listFrom([PENDING]);
    const approveDevice = vi.fn().mockResolvedValue({ ...PENDING, review_state: "approved" });
    renderDevices({ listDevices, approveDevice }, ["devices.read", "devices.review"]);
    await userEvent.click(await screen.findByRole("button", { name: "Approve DESKTOP-7Q2M4KD" }));
    expect(approveDevice).toHaveBeenCalledWith("p1");
    await vi.waitFor(() => expect(listDevices.mock.calls.length).toBeGreaterThan(1));
  });

  it("reject asks first, says what it does, and passes on a manual-removal notice", async () => {
    const rejectDevice = vi.fn().mockResolvedValue({
      device_id: "p1", uninstall: "manual", job_id: null,
      message: "This computer's agent is too old to remove itself. Its certificate has been revoked, so it can no longer connect; remove the agent from the computer by hand.",
    });
    renderDevices({ listDevices: listFrom([PENDING]), rejectDevice }, ["devices.read", "devices.review"]);
    await userEvent.click(await screen.findByRole("button", { name: "Reject DESKTOP-7Q2M4KD" }));
    let dialog = screen.getByRole("dialog", { name: "Reject this computer" });
    expect(dialog).toHaveTextContent("DESKTOP-7Q2M4KD");
    expect(dialog).toHaveTextContent("uninstall itself");
    // Codex review on #89: the certificate is NOT revoked at Reject for an
    // agent that can uninstall itself -- only once it confirms, or after 24
    // hours. The dialog must not tell the operator it is already cut off.
    expect(dialog).not.toHaveTextContent(/is revoked, so it can no longer connect/);
    expect(dialog).toHaveTextContent("24 hours");
    expect(dialog).toHaveTextContent("nothing except the uninstall");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(rejectDevice).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Reject DESKTOP-7Q2M4KD" }));
    dialog = screen.getByRole("dialog", { name: "Reject this computer" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Reject and uninstall" }));
    expect(rejectDevice).toHaveBeenCalledWith("p1");
    expect(await screen.findByRole("status")).toHaveTextContent("remove the agent from the computer by hand");
  });

  it("without the review permission the row waits and offers nothing", async () => {
    renderDevices({ listDevices: listFrom([PENDING]) }, ["devices.read"]);
    const row = (await screen.findByText("DESKTOP-7Q2M4KD")).closest("tr")!;
    expect(within(row).queryByRole("button", { name: /Approve|Reject/ })).not.toBeInTheDocument();
    expect(row).toHaveTextContent("Waiting for someone who can approve or reject new computers");
  });
});

const DID_NOT_CONFIRM =
  "Rejected — the agent didn't confirm it removed itself. Uninstall it on the computer by hand.";
const TOO_OLD =
  "Rejected — this agent is too old to remove itself. Uninstall it on the computer by hand.";

function leftBehind(outcome: NonNullable<Device["rejection_outcome"]>): Device {
  return device({
    id: "r1", hostname: "DESKTOP-J3K9P2L", review_state: "rejected", rejection_outcome: outcome,
    status: "decommissioned", agent_status: "offline", cert_revoked: true,
    link_joined_from: "10.50.0.12", link_joined_at: new Date(Date.now() - 26 * 3_600_000).toISOString(),
  });
}

/** The server's review tab: pending rows AND rejections that left the agent behind. */
function reviewFrom(estate: Device[]) {
  return vi.fn(async (params: DeviceListParams = {}): Promise<DevicePage> => {
    const review = estate.filter((d) => d.review_state === "pending" || d.review_state === "rejected");
    const shown = estate.filter((d) => d.review_state !== "rejected");
    const items = params.state === "review" ? review : shown;
    return {
      items, total: items.length,
      state_counts: { all: shown.length, online: shown.length, offline: 0, assets: 0, certs: 0, review: review.length },
      fleet: { total: shown.length, managed: shown.length, online: shown.length, cert_attention: 0 },
    };
  });
}

describe("A rejection that left the agent behind", () => {
  it.each([
    ["timeout", DID_NOT_CONFIRM],
    ["agent_did_not_confirm", DID_NOT_CONFIRM],
    ["agent_cannot_uninstall", TOO_OLD],
  ] as const)("after %s it says, in words, to uninstall it by hand", async (outcome, text) => {
    renderDevices({ listDevices: reviewFrom([leftBehind(outcome), PENDING]) }, ["devices.read", "devices.review"]);
    const row = (await screen.findByText("DESKTOP-J3K9P2L")).closest("tr")!;
    expect(row).toHaveTextContent(text);
    expect(row).not.toHaveTextContent(outcome);
    expect(within(row).queryByRole("button", { name: /Approve|Reject/ })).not.toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /Run job/ })).not.toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "I removed it from DESKTOP-J3K9P2L" })).toBeInTheDocument();
    // The device page would answer 404: the name is not a link to it.
    expect(within(row).queryByRole("link", { name: "DESKTOP-J3K9P2L" })).not.toBeInTheDocument();
    // It counts in the tab's badge beside the machine still waiting.
    expect(screen.getByRole("tab", { name: /Waiting for review/ })).toHaveTextContent("2");
  });

  it("\"I removed it\" calls the acknowledge route and the row leaves the list", async () => {
    const estate = [leftBehind("agent_did_not_confirm"), PENDING];
    const listDevices = reviewFrom(estate);
    const acknowledgeDeviceRemoval = vi.fn(async (id: string) => {
      estate.splice(estate.findIndex((d) => d.id === id), 1);
    });
    renderDevices({ listDevices, acknowledgeDeviceRemoval }, ["devices.read", "devices.review"]);
    await userEvent.click(await screen.findByRole("button", { name: "I removed it from DESKTOP-J3K9P2L" }));
    expect(acknowledgeDeviceRemoval).toHaveBeenCalledWith("r1");
    await vi.waitFor(() => expect(screen.queryByText("DESKTOP-J3K9P2L")).not.toBeInTheDocument());
    expect(screen.getByText("DESKTOP-7Q2M4KD")).toBeInTheDocument();
  });

  it("a refusal is said in the server's words and the row stays", async () => {
    const acknowledgeDeviceRemoval = vi.fn().mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "someone has already said they removed this agent" })),
    );
    renderDevices(
      { listDevices: reviewFrom([leftBehind("timeout")]), acknowledgeDeviceRemoval },
      ["devices.read", "devices.review"],
    );
    await userEvent.click(await screen.findByRole("button", { name: "I removed it from DESKTOP-J3K9P2L" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("someone has already said they removed this agent");
    expect(screen.getByText("DESKTOP-J3K9P2L")).toBeInTheDocument();
  });

  it("without the review permission it still says so, and offers nothing", async () => {
    renderDevices({ listDevices: reviewFrom([leftBehind("timeout")]) }, ["devices.read"]);
    const row = (await screen.findByText("DESKTOP-J3K9P2L")).closest("tr")!;
    expect(row).toHaveTextContent(DID_NOT_CONFIRM);
    expect(within(row).queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("reviewLabels", () => {
  it("says what is left to do after a rejection, and only when something is", () => {
    expect(manualRemovalStatus(leftBehind("timeout"))).toBe(DID_NOT_CONFIRM);
    expect(manualRemovalStatus(leftBehind("agent_did_not_confirm"))).toBe(DID_NOT_CONFIRM);
    expect(manualRemovalStatus(leftBehind("agent_cannot_uninstall"))).toBe(TOO_OLD);
    expect(manualRemovalStatus(leftBehind("agent_confirmed"))).toBeNull();
    expect(manualRemovalStatus(PENDING)).toBeNull();
    expect(manualRemovalStatus(APPROVED)).toBeNull();
  });

  it("names how a row joined in the operator's words", () => {
    expect(joinedThroughLink(PENDING)).toBe("Joined through the install link from 10.50.0.9");
    expect(joinedThroughLink({ ...PENDING, link_joined_from: null })).toBe("Joined through the install link");
    expect(joinedThroughLink(APPROVED)).toBeNull();
    expect(recordedByLabel(PENDING, "Agent enrolment")).toBe("Install link");
    expect(recordedByLabel(APPROVED, "Agent enrolment")).toBe("Agent enrolment");
  });
});
