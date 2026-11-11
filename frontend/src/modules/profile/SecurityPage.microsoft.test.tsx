/**
 * The Microsoft account in Sign-in and security (own-auth phase 2, Task 12;
 * design §4: linked the first time from the invitation or from the profile):
 * link with a recent confirmation, unlink never the last way in.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import { navigation } from "../../lib/session";
import { takeMicrosoftReturn } from "../auth/microsoft";
import { MY_SECURITY_FIXTURE, apiClient, calls, installDefaults, renderSecurityPage } from "../settings/__tests__/harness";
import { PortalSecurityPage } from "./SecurityPage";

vi.mock("../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

const AUTHORIZE = "https://login.microsoftonline.com/9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d/oauth2/v2.0/authorize?x=1";
const LINK = { tenant_id: "9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d", created_at: "2026-11-01T09:00:00Z", last_used_at: null };
let assign: ReturnType<typeof vi.spyOn>;

async function panel(): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { name: "Microsoft account" });
  return heading.closest("section") as HTMLElement;
}

beforeEach(() => {
  installDefaults();
  window.sessionStorage.clear();
  assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the Microsoft account", () => {
  it("links after the server's recent confirmation, and comes back here", async () => {
    calls.microsoftStart
      .mockRejectedValueOnce(new ApiError(403, '{"code":"step_up_required"}'))
      .mockResolvedValue({ authorize_url: AUTHORIZE });
    const user = userEvent.setup();
    renderSecurityPage();
    await user.click(within(await panel()).getByRole("button", { name: "Link a Microsoft account" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(dialog).getByLabelText("Code from your authenticator app"), "123456");
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(AUTHORIZE));
    expect(calls.microsoftStart).toHaveBeenLastCalledWith({ intent: "link" }, { signedIn: true });
    expect(takeMicrosoftReturn()).toEqual({ intent: "link", back: "/profile/security" });
  });

  it("unlinks after a confirmation", async () => {
    calls.getMySecurity.mockResolvedValue({ ...MY_SECURITY_FIXTURE, microsoft: LINK });
    const user = userEvent.setup();
    renderSecurityPage();
    await user.click(within(await panel()).getByRole("button", { name: "Unlink" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Unlink your Microsoft account" })).getByRole("button", {
        name: "Unlink",
      }),
    );
    await waitFor(() => expect(calls.unlinkMicrosoft).toHaveBeenCalledOnce());
  });

  it("does not offer a contact to unlink their last way in", async () => {
    calls.getMySecurity.mockResolvedValue({
      ...MY_SECURITY_FIXTURE,
      kind: "contact",
      has_password: false,
      has_totp: false,
      passkeys: [],
      microsoft: LINK,
    });
    renderSecurityPage();
    expect(within(await panel()).getByRole("button", { name: "Unlink" })).toBeDisabled();
  });

  it("says the server's refusal of the last way in, in one sentence", async () => {
    calls.getMySecurity.mockResolvedValue({ ...MY_SECURITY_FIXTURE, microsoft: LINK });
    calls.unlinkMicrosoft.mockRejectedValue(new ApiError(409, '{"code":"last_method","detail":"x"}'));
    const user = userEvent.setup();
    renderSecurityPage();
    await user.click(within(await panel()).getByRole("button", { name: "Unlink" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Unlink your Microsoft account" })).getByRole("button", {
        name: "Unlink",
      }),
    );
    expect(await within(await panel()).findByRole("alert")).toHaveTextContent(
      "That is your last way to sign in. Add a password or a passkey first.",
    );
  });

  it("links a contact from the portal, back to the portal, and asks the console's /me nothing", async () => {
    calls.getMySecurity.mockResolvedValue({ ...MY_SECURITY_FIXTURE, kind: "contact", has_totp: false, passkeys: [] });
    calls.microsoftStart.mockResolvedValue({ authorize_url: AUTHORIZE });
    const user = userEvent.setup();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/portal/security"]}>
          <PortalSecurityPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await user.click(within(await panel()).getByRole("button", { name: "Link a Microsoft account" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(AUTHORIZE));
    expect(takeMicrosoftReturn()).toEqual({ intent: "link", back: "/portal/security" });
    // A contact's `/me` is the console's door's 403 (Task 10): nothing here asks it.
    expect(calls.me).not.toHaveBeenCalled();
  });
});
