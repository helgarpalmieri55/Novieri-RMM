/**
 * NO PAGE A CLIENT'S CONTACT REACHES MOUNTS A SECOND `["me"]` OBSERVER (final
 * review F, item 6). The console's `GET /me` refuses a contact (403
 * `portal_only`); `RequireAuth`'s own `useSession` asks it once and keeps the
 * answer, and that is the one observer it may have. A second -- a page, a
 * panel or a dialog that reads `["me"]` for a permission or a time zone --
 * asks again on every mount and focus, and its refusal is drawn as an error
 * on a page that works (fix round 1 found two; `usePortalMe` is the portal's
 * own question).
 *
 * A CENSUS, NOT A SPOT CHECK: the whole application -- `App`'s own router,
 * query client and providers, the real `ApiClient` over a stubbed `fetch` --
 * at each of the four portal routes, and with the confirmation dialog open on
 * `/portal/security` from "Add password" and from "Link a Microsoft account".
 * A component added to any of them that mounts a `["me"]` observer turns
 * this red.
 */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setSelectedClient } from "./lib/clientChoice";
import { navigation } from "./lib/session";
import type { MySecurity } from "./modules/profile/types";

const { default: App, queryClient } = await import("./App");

const PORTAL_ONLY = { detail: "this account can only use the portal", code: "portal_only" };
const STEP_UP = { detail: "confirm it is you first", code: "step_up_required" };
const PORTAL_ME = {
  user_id: "contact-1",
  kind: "contact",
  email: "ana@acme.test",
  display_name: "Ana Ruiz",
  timezone: null,
  permissions: ["portal.ticket.read", "portal.ticket.write"],
};
const CATEGORIES = [{ id: "cat1", key: "hardware", name: "Hardware", type: "incident" }];
const TICKET = {
  id: "t1",
  number: "INC-1",
  type: "incident",
  status: "in_progress",
  priority: "normal",
  subject: "The VPN does not connect",
  description: "Since yesterday.",
  created_at: "2026-11-10T09:00:00Z",
  updated_at: "2026-11-10T10:00:00Z",
  comments: [],
  status_history: [{ from_status: null, to_status: "new", created_at: "2026-11-10T09:00:00Z" }],
  attachments: [],
  approval: null,
};
/** A contact with a password and no Microsoft account: "Link a Microsoft account" is offered. */
const WITH_PASSWORD: MySecurity = {
  kind: "contact",
  has_password: true,
  microsoft: null,
  has_totp: false,
  passkeys: [],
  recovery_codes_left: 0,
  trusted_devices: [],
  sessions: [],
};
/** A contact who signed up with a passkey: "Add password" is offered. */
const PASSKEY_ONLY: MySecurity = {
  ...WITH_PASSWORD,
  has_password: false,
  passkeys: [
    { id: "pk-1", name: "Phone", created_at: "2026-11-01T09:00:00Z", last_used_at: null, backup_state: true },
  ],
};
let security: MySecurity = WITH_PASSWORD;
const GOOD = "Ultramarine-Kettle-7319-Harbor";

function answer(method: string, path: string): { status: number; body: unknown } {
  if (path === "/api/v1/setup/status") return { status: 200, body: { needed: false } };
  if (path === "/api/v1/me") return { status: 403, body: PORTAL_ONLY };
  if (path === "/api/v1/portal/me") return { status: 200, body: PORTAL_ME };
  if (path === "/api/v1/portal/tickets") return { status: 200, body: [] };
  if (path === "/api/v1/portal/categories") return { status: 200, body: CATEGORIES };
  if (path === "/api/v1/portal/tickets/t1") return { status: 200, body: TICKET };
  if (path === "/api/v1/me/security") return { status: 200, body: security };
  // The two acts below need the second factor confirmed: the dialog opens.
  if (path === "/api/v1/me/password" && method === "POST") return { status: 403, body: STEP_UP };
  if (path === "/api/v1/auth/microsoft/start") return { status: 403, body: STEP_UP };
  return { status: 404, body: { detail: "not here" } };
}

function stubServer(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), window.location.origin);
      const { status, body } = answer((init?.method ?? "GET").toUpperCase(), url.pathname);
      return new Response(JSON.stringify(body), { status });
    }),
  );
}

/** How many components are subscribed to `["me"]` right now. */
function meObservers(): number {
  return queryClient.getQueryCache().find({ queryKey: ["me"], exact: true })?.getObserversCount() ?? 0;
}

/** The whole application at `path`, as the browser would open it. */
function openApp(path: string): void {
  window.history.replaceState(null, "", path);
  render(<App />);
}

/** Long enough for a late effect or a lazily enabled query to subscribe. */
async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 100));
  });
}

beforeEach(() => {
  // The application's client is module-level: one test's cache would answer the next.
  queryClient.clear();
  security = WITH_PASSWORD;
  vi.spyOn(navigation, "assign").mockImplementation(() => {});
  vi.spyOn(navigation, "replace").mockImplementation(() => {});
  stubServer();
});

afterEach(() => {
  queryClient.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setSelectedClient(null);
  window.localStorage.clear();
  window.history.replaceState(null, "", "/");
});

describe("a client's contact on every portal route", () => {
  it.each([
    ["/portal", "My tickets"],
    ["/portal/new", "New ticket"],
    ["/portal/tickets/t1", "INC-1: The VPN does not connect"],
    ["/portal/security", "Your security"],
  ])("%s has exactly one observer of the console's /me: the guard's", async (path, heading) => {
    openApp(path);
    expect(await screen.findByTestId("portal-shell")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: heading })).toBeInTheDocument();
    await settle();
    expect(meObservers()).toBe(1);
  });
});

describe("a client's contact with the confirmation open on /portal/security", () => {
  async function securityPage() {
    openApp("/portal/security");
    const heading = await screen.findByRole("heading", { name: "Password" });
    const panel = heading.closest("section");
    if (!panel) throw new Error("no Password panel");
    return panel;
  }

  /** The dialog, once it has read the person's methods (its own `/me/security` query). */
  async function confirmation(): Promise<HTMLElement> {
    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await waitFor(() => expect(within(dialog).queryByText("Loading your second factors…")).not.toBeInTheDocument());
    return dialog;
  }

  it("from Add password, still has exactly one", async () => {
    security = PASSKEY_ONLY;
    const user = userEvent.setup();
    const panel = await securityPage();
    await user.type(within(panel).getByLabelText("New password"), GOOD);
    await user.type(within(panel).getByLabelText("New password again"), GOOD);
    await user.click(within(panel).getByRole("button", { name: "Add password" }));
    await confirmation();
    await settle();
    expect(meObservers()).toBe(1);
  });

  it("from Link a Microsoft account, still has exactly one", async () => {
    const user = userEvent.setup();
    await securityPage();
    await user.click(screen.getByRole("button", { name: "Link a Microsoft account" }));
    const dialog = await confirmation();
    expect(within(dialog).getByLabelText("Your password")).toBeInTheDocument();
    await settle();
    expect(meObservers()).toBe(1);
    expect(navigation.assign).not.toHaveBeenCalled();
  });
});
