/**
 * A client's contact's own security, in the portal (own-auth phase 2, Task
 * 11, decisions 3-4; operator decision C5; design §2.6): the minimum of 15,
 * the first password added, the app optional, recovery codes only with the
 * app and offered as soon as it is on, never the last way in, and the
 * confirmation with the password -- only for a contact without the app.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import { MY_SECURITY_FIXTURE, apiClient, calls, installDefaults } from "../settings/__tests__/harness";
import { PortalSecurityPage } from "./SecurityPage";

vi.mock("../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

const CONTACT = {
  ...MY_SECURITY_FIXTURE,
  kind: "contact",
  has_password: true,
  microsoft: null,
  has_totp: false,
  passkeys: [],
  recovery_codes_left: 0,
};
const STEP_UP = new ApiError(403, JSON.stringify({ detail: { code: "step_up_required" } }));
const GOOD = "Ultramarine-Kettle-7319-Harbor";

function renderPortalSecurity() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/portal/security"]}>
        <PortalSecurityPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function panel(title: string): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { name: title });
  const section = heading.closest("section");
  if (!section) throw new Error(`no panel titled ${title}`);
  return section;
}

beforeEach(() => {
  installDefaults();
  calls.getMySecurity.mockResolvedValue(CONTACT);
});

describe("a contact's security", () => {
  it("reads the time zone from the portal, never from the console", async () => {
    renderPortalSecurity();
    await panel("Password");
    expect(calls.portalMe).toHaveBeenCalled();
    expect(calls.getCurrentOrg).not.toHaveBeenCalled();
    expect(calls.me).not.toHaveBeenCalled();
  });

  it("holds a new password to 15 characters", async () => {
    const user = userEvent.setup();
    renderPortalSecurity();
    const box = await panel("Password");
    await user.type(within(box).getByLabelText("Current password"), GOOD);
    await user.type(within(box).getByLabelText("New password"), "fourteen chars");
    await user.type(within(box).getByLabelText("New password again"), "fourteen chars");
    await user.click(within(box).getByRole("button", { name: "Change password" }));
    expect(await within(box).findByRole("alert")).toHaveTextContent("Use at least 15 characters.");
    expect(calls.changePassword).not.toHaveBeenCalled();
  });

  it("adds a first password for a contact who signed in with a passkey", async () => {
    calls.getMySecurity.mockResolvedValue({
      ...CONTACT,
      has_password: false,
      passkeys: [MY_SECURITY_FIXTURE.passkeys[0]],
    });
    const user = userEvent.setup();
    renderPortalSecurity();
    const box = await panel("Password");
    expect(within(box).queryByLabelText("Current password")).not.toBeInTheDocument();
    await user.type(within(box).getByLabelText("New password"), GOOD);
    await user.type(within(box).getByLabelText("New password again"), GOOD);
    await user.click(within(box).getByRole("button", { name: "Add password" }));
    await waitFor(() => expect(calls.addPassword).toHaveBeenCalledExactlyOnceWith(GOOD));
  });

  it("still says the password was added once the page offers to change it (fix round 1)", async () => {
    calls.getMySecurity
      .mockResolvedValueOnce({ ...CONTACT, has_password: false, passkeys: [MY_SECURITY_FIXTURE.passkeys[0]] })
      .mockResolvedValue({ ...CONTACT, has_password: true, passkeys: [MY_SECURITY_FIXTURE.passkeys[0]] });
    const user = userEvent.setup();
    renderPortalSecurity();
    const box = await panel("Password");
    await user.type(within(box).getByLabelText("New password"), GOOD);
    await user.type(within(box).getByLabelText("New password again"), GOOD);
    await user.click(within(box).getByRole("button", { name: "Add password" }));
    // The server's answer re-reads the page: the change form replaces the add form.
    const changed = await panel("Password");
    expect(await within(changed).findByLabelText("Current password")).toBeInTheDocument();
    expect(within(changed).getByText("Your password was added.")).toBeInTheDocument();
  });

  it("never offers to remove the last way in", async () => {
    calls.getMySecurity.mockResolvedValue({
      ...CONTACT,
      has_password: false,
      passkeys: [MY_SECURITY_FIXTURE.passkeys[0]],
    });
    renderPortalSecurity();
    const box = await panel("Passkeys");
    expect(within(box).getByRole("button", { name: /Remove/ })).toBeDisabled();
    expect(within(box).getByText("This passkey is your only way to sign in. Add a password before you remove it.")).toBeInTheDocument();
  });

  it("lets the authenticator app go, and shows recovery codes only with a second factor", async () => {
    calls.getMySecurity.mockResolvedValue({ ...CONTACT, has_totp: true });
    renderPortalSecurity();
    const app = await panel("Authenticator app");
    expect(within(app).getByRole("button", { name: /Remove/ })).toBeEnabled();
    expect(screen.getByRole("heading", { name: "Recovery codes" })).toBeInTheDocument();
  });

  it("hides recovery codes from a contact without a second factor", async () => {
    renderPortalSecurity();
    await panel("Password");
    expect(screen.queryByRole("heading", { name: "Recovery codes" })).not.toBeInTheDocument();
  });

  it("counts a contact's passkey as a way in, not a second factor (C5)", async () => {
    calls.getMySecurity.mockResolvedValue({ ...CONTACT, passkeys: [MY_SECURITY_FIXTURE.passkeys[0]] });
    renderPortalSecurity();
    await panel("Passkeys");
    expect(screen.queryByRole("heading", { name: "Recovery codes" })).not.toBeInTheDocument();
  });

  it("offers recovery codes as soon as the app is on and none exist (A2-41)", async () => {
    calls.getMySecurity.mockResolvedValue({ ...CONTACT, has_totp: true, recovery_codes_left: 0 });
    renderPortalSecurity();
    const codes = await panel("Recovery codes");
    expect(
      within(codes).getByText(
        "You have no recovery codes. Make new ones now: they get you in if you cannot use your second factor.",
      ),
    ).toBeInTheDocument();
    expect(within(codes).getByRole("button", { name: "Make new codes" })).toBeEnabled();
  });

  it("asks a contact with the app for its code, never the password alone (design §2.6, A2-16)", async () => {
    calls.getMySecurity.mockResolvedValue({ ...CONTACT, has_totp: true, recovery_codes_left: 8 });
    calls.changePassword.mockRejectedValueOnce(STEP_UP).mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPortalSecurity();
    const box = await panel("Password");
    await user.type(within(box).getByLabelText("Current password"), GOOD);
    await user.type(within(box).getByLabelText("New password"), `${GOOD}-2`);
    await user.type(within(box).getByLabelText("New password again"), `${GOOD}-2`);
    await user.click(within(box).getByRole("button", { name: "Change password" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    expect(within(dialog).getByLabelText("Code from your authenticator app")).toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Your password")).not.toBeInTheDocument();
  });

  it("confirms with the password when the server asks", async () => {
    calls.changePassword.mockRejectedValueOnce(STEP_UP).mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPortalSecurity();
    const box = await panel("Password");
    await user.type(within(box).getByLabelText("Current password"), GOOD);
    await user.type(within(box).getByLabelText("New password"), `${GOOD}-2`);
    await user.type(within(box).getByLabelText("New password again"), `${GOOD}-2`);
    await user.click(within(box).getByRole("button", { name: "Change password" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(dialog).getByLabelText("Your password"), GOOD);
    await user.click(within(dialog).getByRole("button", { name: "Confirm with your password" }));
    await waitFor(() => expect(calls.changePassword).toHaveBeenCalledTimes(2));
    expect(calls.stepUpPassword).toHaveBeenCalledExactlyOnceWith(GOOD);
  });
});
