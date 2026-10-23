// Service levels tab — `GET /sla/settings`, `PUT /sla/settings`.

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { PLANTED_SECRET, apiClient, calls, installDefaults, renderSettings } from "./harness";

// The OIDC client is not under test and constructs a UserManager (with a
// silent-renew timer) at import time. `../api.ts`'s org request only needs the
// bearer token, so the token source is stubbed.
vi.mock("../../../lib/auth", () => ({
  accessToken: () => "test-token",
  // The Profile tab deep-links into Keycloak's account console, so the
  // authority is now part of what `SettingsPage` needs from this module.
  // A factory mock is a strict namespace: a missing export throws.
  oidcAuthority: "https://id.example.test/realms/it-suite",
}));

// `apiClient` is referenced LAZILY — inside the returned closure, never while
// the factory runs. Importing the harness from within the factory instead
// deadlocks: the harness imports `SettingsPage`, which imports the very module
// this factory is mocking, so the import waits on a factory that is waiting on
// the import.
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

beforeEach(() => {
  installDefaults();
});

async function openTab(): Promise<void> {
  const user = userEvent.setup();
  // A `RegExp`, not the exact string "Service levels": the nav now appends
  // "Set up" / "Not set up" to this tab's accessible name (Task 7), and
  // `getByRole`'s `name` matches a string by whole-string equality
  // regardless of `exact` (text-query-only) -- so an exact string here would
  // miss the tab as soon as its own settings query answers `configured`.
  await user.click(await screen.findByRole("tab", { name: /^Service levels/ }));
}

const FIELD = "Auto-close resolved tickets after";

describe("Settings — Service levels tab", () => {
  // Glossary, `docs/design/settings-language.md`: "organization-wide,
  // org-wide scope" -> "everywhere", never varied for rhythm. This panel's
  // own subtitle was still carrying the internal word.
  it("says the auto-close window applies everywhere, not 'org-wide'", async () => {
    renderSettings();
    await openTab();
    expect(screen.getByText(/auto-close window that applies everywhere/i)).toBeInTheDocument();
    expect(screen.queryByText(/org-wide/i)).not.toBeInTheDocument();
  });

  it("puts the unit in the description, not in the label", async () => {
    renderSettings();
    await openTab();
    const field = await screen.findByRole("spinbutton", { name: FIELD });
    expect(field).toHaveAccessibleDescription("Days");
  });

  it("loads the current value and the two misconfiguration counts", async () => {
    calls.getSlaSettings.mockResolvedValue({
      auto_close_after_days: 14,
      configured: true,
      tickets_without_sla: 3,
      stale_ticket_sla: 1,
    });
    renderSettings();
    await openTab();
    expect(await screen.findByLabelText<HTMLInputElement>(FIELD)).toHaveValue(14);
    expect(screen.getByText("Tickets with no SLA:")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
  });

  it("PUTs the edited value, shows success, and refetches", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    const field = await screen.findByLabelText<HTMLInputElement>(FIELD);
    await user.clear(field);
    await user.type(field, "21");
    calls.getSlaSettings.mockResolvedValue({
      auto_close_after_days: 21,
      configured: true,
      tickets_without_sla: 0,
      stale_ticket_sla: 0,
    });

    await user.click(screen.getByRole("button", { name: "Save service levels" }));

    await waitFor(() => expect(calls.updateSlaSettings).toHaveBeenCalledTimes(1));
    expect(calls.updateSlaSettings).toHaveBeenCalledWith({ auto_close_after_days: 21 });
    expect(await screen.findByRole("status")).toHaveTextContent(/Saved\./);
    await waitFor(() => expect(calls.getSlaSettings.mock.calls.length).toBeGreaterThan(1));
  });

  it("a failed save is unmistakable: the server's error is shown and success is not claimed", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    const field = await screen.findByLabelText<HTMLInputElement>(FIELD);
    await user.clear(field);
    await user.type(field, "21");
    calls.updateSlaSettings.mockRejectedValue(
      new ApiError(400, JSON.stringify({ detail: "auto_close_after_days is out of bounds" })),
    );

    await user.click(screen.getByRole("button", { name: "Save service levels" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("auto_close_after_days is out of bounds");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("renders no secret and offers no field that could carry one", async () => {
    renderSettings();
    await openTab();
    await screen.findByLabelText(FIELD);
    expect(document.body.textContent ?? "").not.toContain(PLANTED_SECRET);
    expect(document.querySelectorAll('input[type="password"]')).toHaveLength(0);
  });

  it("disables the form, with the reason, for an sla.read holder without sla.admin", async () => {
    installDefaults(["sla.read"]);
    renderSettings();
    await openTab();
    expect(await screen.findByLabelText(FIELD)).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save service levels" })).toBeDisabled();
    expect(screen.getByText(/requires the sla\.admin permission/i)).toBeInTheDocument();
  });

  it("neither renders nor fetches the tab without sla.read", async () => {
    installDefaults(["budget.read"]);
    renderSettings();
    await screen.findByRole("tab", { name: "Organization" });
    expect(screen.queryByRole("tab", { name: "Service levels" })).not.toBeInTheDocument();
    expect(calls.getSlaSettings).not.toHaveBeenCalled();
  });
});
