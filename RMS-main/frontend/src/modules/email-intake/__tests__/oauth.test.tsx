/**
 * Email intake: Connect with Microsoft.
 *
 * The callback is a CONSOLE route: the console reads `code` and `state`, strips
 * them from the address bar, and completes the sign-in with the operator's own
 * token. A revoked sign-in is a state with an action (Reconnect), not an error.
 * Design: docs/superpowers/specs/2026-09-11-mail-oauth-design.md §4.3-§4.5.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, type ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient, type Me } from "../../../lib/api";
import { browser } from "../browser";
import { EmailIntakeAdminPage } from "../EmailIntakeAdminPage";
import { OAuthCallback } from "../OAuthCallback";
import type { MailboxOut, OAuthAppOut } from "../types";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

afterEach(() => {
  vi.restoreAllMocks();
});

const ADMIN: Me = {
  user_id: "admin-1",
  org_id: "o1",
  email: "admin@example.com",
  display_name: "Admin",
  permissions: ["email_intake.read", "email_intake.admin"],
};

const REDIRECT = "https://itms.example/settings/email-intake/oauth/callback";

const APP: OAuthAppOut = {
  tenant_id: "contoso.onmicrosoft.com",
  client_id: "11111111-2222-3333-4444-555555555555",
  has_secret: true,
  redirect_uri: REDIRECT,
};

const PLANTED_SECRET = "planted-client-secret";

function mailbox(overrides: Partial<MailboxOut> = {}): MailboxOut {
  return {
    id: "m1",
    address: "support@toeshee.com",
    host: "outlook.office365.com",
    port: 993,
    use_ssl: true,
    folder: "INBOX",
    username: "support@toeshee.com",
    has_credential: false,
    auth_kind: "microsoft_oauth",
    credential_state: "ok",
    default_category_id: "cat-1",
    reopen_window_days: 14,
    active: false,
    last_polled_at: null,
    last_error: null,
    created_at: "2026-09-01T00:00:00.000Z",
    ...overrides,
  };
}

function api(overrides: Partial<ApiClient> = {}): Partial<ApiClient> {
  return {
    me: vi.fn().mockResolvedValue(ADMIN),
    listEmailMailboxes: vi.fn().mockResolvedValue([mailbox()]),
    listTicketCategories: vi.fn().mockResolvedValue([]),
    listEmailIntakeMessages: vi
      .fn()
      .mockResolvedValue({ messages: [], total: 0, quarantined_count: 0 }),
    // A regressed server that returned the secret must still not reach the DOM.
    getEmailOAuthApp: vi.fn().mockResolvedValue({ ...APP, client_secret: PLANTED_SECRET }),
    saveEmailOAuthApp: vi.fn().mockResolvedValue(APP),
    startMailboxOAuth: vi
      .fn()
      .mockResolvedValue({ authorize_url: "https://login.microsoftonline.com/x/oauth2/v2.0/authorize?state=s" }),
    completeMailboxOAuth: vi.fn().mockResolvedValue(mailbox({ has_credential: true })),
    abandonMailboxOAuth: vi.fn().mockResolvedValue(undefined),
    disconnectMailboxOAuth: vi.fn().mockResolvedValue(mailbox()),
    ...overrides,
  };
}

function wrap(children: ReactNode, entry: string, strict = false) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const tree = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>{children}</MemoryRouter>
    </QueryClientProvider>
  );
  return render(strict ? <StrictMode>{tree}</StrictMode> : tree);
}

function renderPage(client: Partial<ApiClient>) {
  mockUseApi.mockReturnValue(client as ApiClient);
  return wrap(<EmailIntakeAdminPage />, "/email-intake");
}

function Where() {
  const location = useLocation();
  return <output data-testid="where">{location.pathname + location.search}</output>;
}

function renderCallback(client: Partial<ApiClient>, query: string, strict = false) {
  mockUseApi.mockReturnValue(client as ApiClient);
  return wrap(
    <Routes>
      <Route
        path="/settings/email-intake/oauth/callback"
        element={
          <>
            <OAuthCallback />
            <Where />
          </>
        }
      />
      <Route path="/email-intake" element={<Where />} />
    </Routes>,
    `/settings/email-intake/oauth/callback${query}`,
    strict,
  );
}

describe("Connect with Microsoft", () => {
  it("sends the browser to the address the server returned", async () => {
    const assign = vi.spyOn(browser, "assign").mockImplementation(() => undefined);
    const client = api();
    renderPage(client);

    await userEvent.click(await screen.findByRole("button", { name: /Connect with Microsoft/ }));

    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith(expect.stringContaining("login.microsoftonline.com")),
    );
    expect(client.startMailboxOAuth).toHaveBeenCalledWith("m1");
  });

  it("shows an unconnected mailbox as Not connected", async () => {
    renderPage(api());

    const row = await screen.findByRole("row", { name: /support@toeshee\.com/ });
    expect(within(row).getByText("Not connected")).toBeInTheDocument();
  });

  it("offers Reconnect, not an error, when the sign-in has expired", async () => {
    renderPage(
      api({
        listEmailMailboxes: vi.fn().mockResolvedValue([
          mailbox({
            has_credential: true,
            credential_state: "needs_reconsent",
            last_error: "Microsoft sign-in is no longer valid — reconnect this mailbox",
          }),
        ]),
      }),
    );

    const row = await screen.findByRole("row", { name: /support@toeshee\.com/ });
    expect(within(row).getByText("Microsoft sign-in expired")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: /Reconnect/ })).toBeInTheDocument();
    expect(within(row).queryByRole("alert")).toBeNull();
  });

  it("asks before disconnecting, then disconnects", async () => {
    const client = api({
      listEmailMailboxes: vi
        .fn()
        .mockResolvedValue([mailbox({ has_credential: true, active: true })]),
    });
    renderPage(client);

    const row = await screen.findByRole("row", { name: /support@toeshee\.com/ });
    expect(within(row).getByText("Connected")).toBeInTheDocument();
    await userEvent.click(within(row).getByRole("button", { name: /Disconnect/ }));
    expect(client.disconnectMailboxOAuth).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: /Disconnect/ }));

    await waitFor(() => expect(client.disconnectMailboxOAuth).toHaveBeenCalledWith("m1"));
  });
});

describe("Microsoft 365 connection panel", () => {
  it("shows the return address to register and never the client secret", async () => {
    renderPage(api());

    expect(await screen.findByText(REDIRECT)).toBeInTheDocument();
    expect(screen.getByLabelText("Client secret")).toHaveValue("");
    expect(document.body.innerHTML).not.toContain(PLANTED_SECRET);
  });

  it("sends the client secret only when one was typed", async () => {
    const client = api();
    renderPage(client);

    const tenant = await screen.findByLabelText("Directory (tenant) ID");
    await waitFor(() => expect(tenant).toHaveValue(APP.tenant_id));
    await userEvent.click(screen.getByRole("button", { name: "Save Microsoft 365 connection" }));

    await waitFor(() => expect(client.saveEmailOAuthApp).toHaveBeenCalled());
    const body = (client.saveEmailOAuthApp as ReturnType<typeof vi.fn>).mock.calls[0]?.[0] as Record<
      string,
      unknown
    >;
    expect(body).toEqual({ tenant_id: APP.tenant_id, client_id: APP.client_id });
  });

  it("says plainly when the server cannot offer Microsoft sign-in", async () => {
    renderPage(
      api({ getEmailOAuthApp: vi.fn().mockResolvedValue({ ...APP, redirect_uri: null }) }),
    );

    expect(await screen.findByText(/switched off on this server/i)).toBeInTheDocument();
  });
});

describe("Add mailbox — sign-in", () => {
  it("does not offer a password box for a Microsoft mailbox, and sends none", async () => {
    const create = vi.fn().mockResolvedValue(mailbox());
    const client = api({
      createEmailMailbox: create,
      listTicketCategories: vi.fn().mockResolvedValue([
        {
          id: "cat-1",
          key: "hw",
          name: "Hardware",
          type: "incident",
          requires_approval: false,
          approver_role_id: null,
          default_queue_id: "q1",
          active: true,
          sort_order: 0,
          created_by: "a",
          created_at: "2026-01-01T00:00:00.000Z",
        },
      ]),
    });
    renderPage(client);

    await userEvent.click(await screen.findByRole("button", { name: "Add mailbox" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Microsoft 365" }));
    expect(within(dialog).queryByLabelText("Password")).toBeNull();
    // Microsoft's own server is filled in for the operator.
    expect(within(dialog).getByLabelText("IMAP host")).toHaveValue("outlook.office365.com");

    await userEvent.type(within(dialog).getByLabelText("Address"), "desk@toeshee.com");
    await userEvent.type(within(dialog).getByLabelText("Username"), "desk@toeshee.com");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(create).toHaveBeenCalled());
    const body = create.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(body.auth_kind).toBe("microsoft_oauth");
    expect("password" in body).toBe(false);
  });
});

describe("The Microsoft return page", () => {
  it("completes the sign-in and strips the code from the address bar", async () => {
    const client = api();
    renderCallback(client, "?code=abc&state=xyz");

    expect(await screen.findByText(/Connected/)).toBeInTheDocument();
    expect(client.completeMailboxOAuth).toHaveBeenCalledWith({ state: "xyz", code: "abc" });
    expect(screen.getByTestId("where").textContent).not.toContain("abc");
    expect(screen.getByTestId("where").textContent).not.toContain("xyz");
  });

  it("abandons rather than completing when the person cancelled at Microsoft", async () => {
    const client = api();
    renderCallback(client, "?error=access_denied&state=xyz");

    expect(await screen.findByText(/was not completed/i)).toBeInTheDocument();
    expect(client.abandonMailboxOAuth).toHaveBeenCalledWith({ state: "xyz" });
    expect(client.completeMailboxOAuth).not.toHaveBeenCalled();
  });

  it("completes exactly once under a double mount", async () => {
    const client = api();
    renderCallback(client, "?code=abc&state=xyz", true);

    expect(await screen.findByText(/Connected/)).toBeInTheDocument();
    expect(client.completeMailboxOAuth).toHaveBeenCalledTimes(1);
  });

  it("says what the server refused", async () => {
    const client = api({
      completeMailboxOAuth: vi.fn().mockRejectedValue(
        new ApiError(
          400,
          JSON.stringify({ detail: "Microsoft refused that sign-in; start it again" }),
        ),
      ),
    });
    renderCallback(client, "?code=abc&state=xyz");

    expect(await screen.findByRole("alert")).toHaveTextContent(/start it again/);
  });
});
