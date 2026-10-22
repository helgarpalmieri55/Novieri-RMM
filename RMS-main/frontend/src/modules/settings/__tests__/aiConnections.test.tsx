// AI connections tab — `GET /mcp/connections`, `POST /mcp/connections/{id}/revoke`,
// and the org's `mcp_enabled` switch over `PATCH /orgs/current` (MCP server
// design §8).

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { formatDateTime } from "../../../lib/datetime";
import { permissionReason } from "../settingsState";
import {
  MCP_APP_FIXTURE,
  MCP_CONNECTIONS_FIXTURE,
  ME_FIXTURE,
  ORG_FIXTURE,
  apiClient,
  calls,
  installDefaults,
  renderSettings,
} from "./harness";

vi.mock("../../../lib/auth", () => ({
  accessToken: () => "test-token",
  oidcAuthority: "https://id.example.test/realms/it-suite",
}));

// Lazily, for the reason `assistant.test.tsx` gives.
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

const ORG_MCP_ON = { ...ORG_FIXTURE, settings: { ...ORG_FIXTURE.settings, mcp_enabled: true } };
const SWITCH = "Allow AI apps to connect over MCP";

beforeEach(() => {
  installDefaults();
  calls.getCurrentOrg.mockResolvedValue(ORG_MCP_ON);
});

function openTab() {
  return renderSettings("/settings?tab=ai-connections");
}

describe("Settings — AI connections tab", () => {
  it("lets an org-admin turn the switch off, read the address, and revoke an app for everyone after confirming", async () => {
    const user = userEvent.setup();
    openTab();

    const toggle = await screen.findByRole("switch", { name: SWITCH });
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));
    expect(toggle).toBeEnabled();
    expect(await screen.findByText(MCP_CONNECTIONS_FIXTURE.address)).toBeInTheDocument();

    // Turning it OFF closes a path, so it is not asked about.
    await user.click(toggle);
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(calls.updateCurrentOrg).toHaveBeenCalledTimes(1));
    // The switch and nothing else: no name, no other settings key rides along.
    expect(JSON.parse(JSON.stringify(calls.updateCurrentOrg.mock.calls[0]?.[0]))).toEqual({
      settings: { mcp_enabled: false },
    });

    // The table: app, people, last used, actions.
    const row = (await screen.findByText("claude")).closest("tr");
    expect(row).not.toBeNull();
    const cells = within(row as HTMLElement);
    expect(cells.getByText("3")).toBeInTheDocument();
    expect(cells.getByText(formatDateTime(MCP_APP_FIXTURE.last_used_at, ME_FIXTURE.timezone))).toBeInTheDocument();
    expect(cells.getByRole("button", { name: /Revoke for me: claude/ })).toBeInTheDocument();

    await user.click(cells.getByRole("button", { name: /Revoke for everyone: claude/ }));
    const dialog = await screen.findByRole("dialog", { name: "Revoke this AI app for everyone" });
    expect(within(dialog).getByText("claude")).toBeInTheDocument();
    // What it does, and only that: revoking is not a block-list.
    expect(
      within(dialog).getByText(
        "Every current sign-in of this app in this organization is ended; people must approve it again to reconnect.",
      ),
    ).toBeInTheDocument();
    // Nothing is sent until the dialog is confirmed.
    expect(calls.revokeMcpConnection).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Revoke for everyone" }));
    await waitFor(() => expect(calls.revokeMcpConnection).toHaveBeenCalledWith("claude", "everyone"));
    expect(await screen.findByText("claude is revoked for everyone in this organization.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows a person with mcp.use but not orgs.manage the switch disabled with the reason, and only Revoke for me", async () => {
    installDefaults(["mcp.use"]);
    calls.getCurrentOrg.mockResolvedValue(ORG_MCP_ON);
    const user = userEvent.setup();
    openTab();

    const toggle = await screen.findByRole("switch", { name: SWITCH });
    expect(toggle).toBeDisabled();
    expect(screen.getByText(permissionReason("orgs.manage"))).toBeInTheDocument();

    const row = (await screen.findByText("claude")).closest("tr") as HTMLElement;
    expect(within(row).queryByRole("button", { name: /for everyone/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /for everyone/ })).toBeNull();

    await user.click(within(row).getByRole("button", { name: /Revoke for me: claude/ }));
    await waitFor(() => expect(calls.revokeMcpConnection).toHaveBeenCalledWith("claude", "me"));
    expect(await screen.findByText("claude is revoked for you.")).toBeInTheDocument();
    expect(calls.updateCurrentOrg).not.toHaveBeenCalled();
  });

  it("does not offer the tab to a person with neither mcp.use nor orgs.manage", async () => {
    installDefaults(["roles.read"]);
    openTab();
    // The deep link is no way round the tab list.
    await screen.findByRole("tab", { name: "Roles" });
    expect(screen.queryByRole("tab", { name: "AI connections" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Organization" })).toHaveAttribute("aria-selected", "true");
    expect(calls.getMcpConnections).not.toHaveBeenCalled();
  });

  it("shows an org-admin without mcp.use the switch, and the rest read-only with the reason", async () => {
    installDefaults(["orgs.manage"]);
    calls.getCurrentOrg.mockResolvedValue(ORG_MCP_ON);
    openTab();

    expect(await screen.findByRole("tab", { name: "AI connections" })).toHaveAttribute("aria-selected", "true");
    const toggle = await screen.findByRole("switch", { name: SWITCH });
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(screen.getAllByText(/require the mcp\.use permission/).length).toBeGreaterThan(0);
    // The read is gated `mcp.use`: it is never sent just to draw a 403.
    expect(calls.getMcpConnections).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /Revoke/ })).toBeNull();
  });

  it("warns when sign-in for some people could not be withdrawn, and says the app may reconnect", async () => {
    calls.revokeMcpConnection.mockResolvedValue({ revoked: true, keycloak_failures: 2 });
    const user = userEvent.setup();
    openTab();

    await user.click(await screen.findByRole("button", { name: /Revoke for everyone: claude/ }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke for everyone" }));
    const warning = await screen.findByText(/may still reconnect without asking/);
    expect(warning.textContent).toContain("For 2 people");
    expect(warning.textContent).toContain("Try revoking again");
    expect(screen.queryByText("claude is revoked for everyone in this organization.")).toBeNull();
  });

  it("words a one-person failure in the singular", async () => {
    calls.revokeMcpConnection.mockResolvedValue({ revoked: true, keycloak_failures: 1 });
    const user = userEvent.setup();
    openTab();
    await user.click(await screen.findByRole("button", { name: /Revoke for everyone: claude/ }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke for everyone" }));
    expect((await screen.findByText(/may still reconnect without asking/)).textContent).toContain("For 1 person,");
  });

  it("words a failed revoke for me as being about you, not a count of people", async () => {
    calls.revokeMcpConnection.mockResolvedValue({ revoked: true, keycloak_failures: 1 });
    const user = userEvent.setup();
    openTab();
    await user.click(await screen.findByRole("button", { name: /Revoke for me: claude/ }));
    const warning = await screen.findByText(/may still reconnect for you without asking/);
    expect(warning.textContent).toContain("claude is revoked for you");
    expect(warning.textContent).not.toMatch(/person|people/);
  });

  it("says so when a revoke fails, and claims nothing", async () => {
    calls.revokeMcpConnection.mockRejectedValue(
      new ApiError(503, JSON.stringify({ detail: "The revocation store is unavailable." })),
    );
    const user = userEvent.setup();
    openTab();
    await user.click(await screen.findByRole("button", { name: /Revoke for me: claude/ }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("The revocation store is unavailable.");
    expect(screen.queryByText("claude is revoked for you.")).toBeNull();
  });

  it("says so when saving the switch fails", async () => {
    calls.updateCurrentOrg.mockRejectedValue(
      new ApiError(422, JSON.stringify({ detail: "settings.mcp_enabled must be a boolean" })),
    );
    const user = userEvent.setup();
    openTab();
    const toggle = await screen.findByRole("switch", { name: SWITCH });
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));
    await user.click(toggle);
    expect((await screen.findByRole("alert")).textContent).toContain("settings.mcp_enabled must be a boolean");
  });

  it("reads a 403 on the connections read as a permission answer, not an outage", async () => {
    calls.getMcpConnections.mockRejectedValue(new ApiError(403, JSON.stringify({ detail: "forbidden" })));
    openTab();
    expect(await screen.findByText(/require the mcp\.use permission/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("disables both revoke buttons while a revoke is in flight", async () => {
    calls.revokeMcpConnection.mockReturnValue(new Promise(() => undefined));
    const user = userEvent.setup();
    openTab();
    const forMe = await screen.findByRole("button", { name: /Revoke for me: claude/ });
    const forEveryone = screen.getByRole("button", { name: /Revoke for everyone: claude/ });
    await user.click(forMe);
    await waitFor(() => expect(forMe).toBeDisabled());
    expect(forEveryone).toBeDisabled();
  });

  it("asks before turning AI connections on, and sends nothing on cancel", async () => {
    const { mcp_enabled: _unused, ...withoutSwitch } = ORG_MCP_ON.settings;
    void _unused;
    calls.getCurrentOrg.mockResolvedValue({ ...ORG_MCP_ON, settings: withoutSwitch });
    const user = userEvent.setup();
    openTab();

    // Absent is OFF.
    const toggle = await screen.findByRole("switch", { name: SWITCH });
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).toHaveAttribute("aria-checked", "false");

    await user.click(toggle);
    const dialog = await screen.findByRole("dialog", { name: "Allow AI apps to connect" });
    expect(dialog.textContent).toMatch(/acts as the person who connected it/);
    expect(dialog.textContent).toMatch(/within their permissions/);
    expect(dialog.textContent).toMatch(/audit log/);
    expect(calls.updateCurrentOrg).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(calls.updateCurrentOrg).not.toHaveBeenCalled();

    await user.click(toggle);
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Allow AI apps" }),
    );
    await waitFor(() => expect(calls.updateCurrentOrg).toHaveBeenCalledTimes(1));
    expect(JSON.parse(JSON.stringify(calls.updateCurrentOrg.mock.calls[0]?.[0]))).toEqual({
      settings: { mcp_enabled: true },
    });
  });

  it("reads a stored \"true\" string as off, the way the server does", async () => {
    calls.getCurrentOrg.mockResolvedValue({ ...ORG_MCP_ON, settings: { ...ORG_MCP_ON.settings, mcp_enabled: "true" } });
    const user = userEvent.setup();
    openTab();
    const toggle = await screen.findByRole("switch", { name: SWITCH });
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).toHaveAttribute("aria-checked", "false");
    // So clicking it is turning it ON, which asks first.
    await user.click(toggle);
    expect(await screen.findByRole("dialog", { name: "Allow AI apps to connect" })).toBeInTheDocument();
  });

  it("says the address is not set when the deployment has no public console address", async () => {
    calls.getMcpConnections.mockResolvedValue({ ...MCP_CONNECTIONS_FIXTURE, address: null });
    openTab();
    expect(await screen.findByText(/ITS_PUBLIC_CONSOLE_URL is not set/)).toBeInTheDocument();
  });

  it("says so when no AI app has been used here", async () => {
    calls.getMcpConnections.mockResolvedValue({ ...MCP_CONNECTIONS_FIXTURE, apps: [] });
    openTab();
    expect(
      await screen.findByText("No AI app has been used in this organization in the last 90 days."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("guides the connection in three steps without naming one vendor as the only option", async () => {
    openTab();
    const guide = await screen.findByRole("list", { name: "Connect an AI app" });
    const steps = within(guide).getAllByRole("listitem");
    expect(steps).toHaveLength(3);
    expect(steps[0]?.textContent).toMatch(/custom connector/);
    expect(steps[1]?.textContent).toMatch(/sign in/);
    expect(steps[2]?.textContent).toMatch(/Always allow or Needs approval/);
  });

  it("says which apps were already revoked", async () => {
    calls.getMcpConnections.mockResolvedValue({
      ...MCP_CONNECTIONS_FIXTURE,
      apps: [{ ...MCP_APP_FIXTURE, revoked_for_everyone: true }],
    });
    openTab();
    expect(await screen.findByText("Revoked for everyone")).toBeInTheDocument();
  });
});
