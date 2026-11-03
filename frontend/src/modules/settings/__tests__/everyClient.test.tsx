/**
 * Settings that every client shares (phase 3 design §2): organization, email,
 * roles, users, the assistant, notifications, service levels and health
 * settings are written through routes in `GLOBAL_WRITES`. A tab is offered
 * with its code; its writes, with the code on every client -- and a person
 * holding the code on some clients only is told that, not that they lack it.
 */

import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ALL_PERMISSIONS, ME_FIXTURE, apiClient, calls, installDefaults, renderSettings } from "./harness";

vi.mock("../../../lib/auth", () => ({
  accessToken: () => "test-token",
  oidcAuthority: "https://id.example.test/realms/it-suite",
}));

// Referenced lazily, inside the closure -- see `organization.test.tsx`.
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

/** Every code the harness knows, held on some clients; on every client, only `everywhere`. */
function holdOnSomeClients(everywhere: readonly string[] = []): void {
  installDefaults();
  calls.me.mockResolvedValue({
    ...ME_FIXTURE,
    permissions: [...ALL_PERMISSIONS],
    permissions_everywhere: [...everywhere],
  });
}

describe("Settings: configuration every client shares (phase 3 design §2)", () => {
  it("shows the organization read-only to orgs.manage on some clients, saying it needs every client", async () => {
    holdOnSomeClients();
    renderSettings();
    await screen.findByLabelText("Default timezone");
    expect(screen.getByLabelText("Organization name")).toBeDisabled();
    expect(
      await screen.findByText(/requires the orgs\.manage permission on every client/),
    ).toBeInTheDocument();
  });

  it("offers the organization's form to orgs.manage on every client", async () => {
    holdOnSomeClients(ALL_PERMISSIONS);
    renderSettings();
    await screen.findByLabelText("Default timezone");
    expect(screen.getByLabelText("Organization name")).toBeEnabled();
  });

  it("lists the users but disables every write for users.manage on some clients", async () => {
    holdOnSomeClients();
    renderSettings("/settings?tab=users");
    expect(await screen.findByRole("row", { name: /casey@example\.com/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create user" })).toBeDisabled();
    // "Create user" is also disabled while the email is empty, for anyone: the
    // field itself is what only `users.manage` on every client unlocks.
    expect(screen.getByLabelText("Email")).toBeDisabled();
  });

  it("still offers and reads the Email tab for orgs.manage on some clients, read-only", async () => {
    holdOnSomeClients();
    renderSettings("/settings?tab=email");
    expect(await screen.findByRole("button", { name: "Send a test message" })).toBeDisabled();
    expect(calls.getEmailSettings).toHaveBeenCalled();
  });
});
