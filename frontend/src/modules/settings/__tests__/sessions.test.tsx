/**
 * Settings -> Sessions -- `GET/PUT /api/v1/settings/sessions` (own
 * authentication design §2.4, §2.6, §3): how long a sign-in lasts, for the
 * whole organization.
 *
 * WHAT IS PINNED. The seven values the server holds are what the form shows; a
 * save sends all seven, as numbers; a value outside the table's CHECK bounds
 * is refused before the round trip, in words, with the range under its field;
 * the tab is offered with `orgs.manage` and written with it on every client.
 * And the trade "Remember this device" makes is said where it is switched on.
 */

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { ALL_PERMISSIONS, ME_FIXTURE, apiClient, calls, installDefaults, renderSettings } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

const REMEMBER = 'Offer "Remember this device" at sign-in';

beforeEach(() => {
  installDefaults();
});

async function openSessions() {
  const user = userEvent.setup();
  renderSettings("/settings?tab=sessions");
  expect(await screen.findByRole("tab", { name: "Sessions", selected: true })).toBeInTheDocument();
  return user;
}

describe("Settings — Sessions tab", () => {
  it("is not offered without orgs.manage, and then nothing is read", async () => {
    installDefaults(["notification.read"]);
    renderSettings("/settings?tab=sessions");
    await screen.findByRole("tab", { name: "Notifications" });
    expect(screen.queryByRole("tab", { name: "Sessions" })).not.toBeInTheDocument();
    expect(calls.getSessionSettings).not.toHaveBeenCalled();
  });

  it("shows the seven values the server holds, each with its range", async () => {
    await openSessions();
    expect(await screen.findByLabelText<HTMLInputElement>("Sign out after inactivity")).toHaveValue(30);
    expect(screen.getByLabelText("Longest session")).toHaveValue(12);
    expect(screen.getByRole("switch", { name: REMEMBER })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByLabelText("Remember a device for")).toHaveValue(15);
    expect(screen.getByLabelText("Longest session on a remembered device")).toHaveValue(168);
    expect(screen.getByLabelText("Sign a contact out after inactivity")).toHaveValue(60);
    expect(screen.getByLabelText("Longest contact session")).toHaveValue(7);
    expect(screen.getByLabelText("Sign out after inactivity")).toHaveAccessibleDescription("Between 5 and 480 minutes.");
    expect(screen.getByLabelText("Longest contact session")).toHaveAccessibleDescription("Between 1 and 30 days.");
  });

  it("saves all seven, as numbers", async () => {
    const user = await openSessions();
    const idle = await screen.findByLabelText("Sign out after inactivity");
    await user.clear(idle);
    await user.type(idle, "45");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(calls.putSessionSettings).toHaveBeenCalledTimes(1));
    expect(calls.putSessionSettings).toHaveBeenCalledWith({
      staff_idle_minutes: 45,
      staff_max_hours: 12,
      remember_device_enabled: true,
      remember_device_days: 15,
      remembered_max_hours: 168,
      contact_idle_minutes: 60,
      contact_max_days: 7,
    });
  });

  it("refuses a value outside its range before the round trip, and says so", async () => {
    const user = await openSessions();
    const idle = await screen.findByLabelText("Sign out after inactivity");
    await user.clear(idle);
    await user.type(idle, "3");
    expect(screen.getByText("Each value has to be within the range shown under it.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(calls.putSessionSettings).not.toHaveBeenCalled();
  });

  it("turning remembered devices off disables their two values and sends false", async () => {
    const user = await openSessions();
    await user.click(await screen.findByRole("switch", { name: REMEMBER }));
    expect(screen.getByLabelText("Remember a device for")).toBeDisabled();
    expect(screen.getByLabelText("Longest session on a remembered device")).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(calls.putSessionSettings).toHaveBeenCalledTimes(1));
    expect(calls.putSessionSettings.mock.calls[0]?.[0]).toMatchObject({
      remember_device_enabled: false,
      remember_device_days: 15,
    });
  });

  it("does not count a value typed out of range and then switched off: Save works, and sends what was saved (D2, Minor 6)", async () => {
    const user = await openSessions();
    const days = await screen.findByLabelText("Remember a device for");
    await user.clear(days);
    await user.type(days, "999");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();

    await user.click(screen.getByRole("switch", { name: REMEMBER }));

    expect(screen.queryByText("Each value has to be within the range shown under it.")).not.toBeInTheDocument();
    const save = screen.getByRole("button", { name: "Save changes" });
    expect(save).toBeEnabled();
    await user.click(save);
    await waitFor(() => expect(calls.putSessionSettings).toHaveBeenCalledTimes(1));
    // The value it holds, not the one typed and hidden: the server would refuse 999.
    expect(calls.putSessionSettings.mock.calls[0]?.[0]).toMatchObject({
      remember_device_enabled: false,
      remember_device_days: 15,
    });
  });

  it("says the trade a remembered device makes, where it is switched on", async () => {
    await openSessions();
    expect(await screen.findByText(/^On a remembered device the second factor is not asked/)).toBeInTheDocument();
  });

  it("is read-only, with the reason, for orgs.manage on some clients only", async () => {
    calls.me.mockResolvedValue({ ...ME_FIXTURE, permissions: [...ALL_PERMISSIONS], permissions_everywhere: [] });
    await openSessions();
    expect(await screen.findByLabelText("Sign out after inactivity")).toBeDisabled();
    expect(screen.getByRole("switch", { name: REMEMBER })).toBeDisabled();
    expect(screen.getByText(/requires the orgs\.manage permission on every client/)).toBeInTheDocument();
  });

  it("says the settings are not available when the server refuses them", async () => {
    calls.getSessionSettings.mockRejectedValue(new ApiError(403, ""));
    await openSessions();
    expect(
      await screen.findByText("Session settings are not available (insufficient permissions: orgs.manage)."),
    ).toBeInTheDocument();
  });
});
