/**
 * Credentials has no screen in the approved design (UI port spec §7) and is
 * built from the design's existing components -- see CredentialsPage.tsx.
 *
 * The tests below cover the three states the design components exist to
 * distinguish (mirroring DevicesPage.test.tsx), AND the one property this
 * page may never lose: security invariant #3 (CLAUDE.md) -- a stored secret
 * is decrypted only at point of use and never otherwise reaches this
 * console. The list/loading/error/empty paths must never render a value from
 * a `secret` field (there is no such field on `Credential` -- see
 * `lib/api.ts` -- so any regression that added one would show up here as a
 * new, unexpected string on the page).
 */

import { existsSync, readFileSync } from "node:fs";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient, type Credential, type Me } from "../../lib/api";
import { NO_DATE } from "../../lib/datetime";
import { CredentialsPage } from "./CredentialsPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function credential(overrides: Partial<Credential> = {}): Credential {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    name: "Domain support admin",
    type: "windows_admin",
    device_group_id: null,
    created_at: "2026-08-01T00:00:00Z",
    last_used_at: null,
    has_secret: true,
    ...overrides,
  };
}

const ME: Me = { permissions: ["credentials.manage"] } as Me;

/**
 * THE ROUTE TABLE, READ FROM `App.tsx` ITSELF.
 *
 * "I don't want dead tiles everywhere" only stays kept if the destinations are
 * checked against the routes that exist, not against a copy of them in this
 * file that would rot beside them. The catch-all is excluded: a tile matching
 * only `*` is precisely the dead tile — it redirects to the device list and
 * calls that a destination.
 */
function appRoutes(): RegExp[] {
  // From the working directory, not `import.meta.url`: under jsdom that URL is
  // not a file: URL.
  const path = existsSync("src/App.tsx") ? "src/App.tsx" : "frontend/src/App.tsx";
  return [...readFileSync(path, "utf8").matchAll(/path="([^"]+)"/g)]
    .map((match) => match[1]!)
    .filter((route) => route !== "*")
    .map((route) => new RegExp(`^${route.replace(/:[^/]+/g, "[^/]+")}$`));
}

function expectTileLinks(expected: readonly string[]) {
  const tiles = [...document.querySelectorAll<HTMLAnchorElement>("a.stat-card")];
  expect(tiles.map((tile) => tile.getAttribute("href"))).toEqual([...expected]);
  const routes = appRoutes();
  for (const href of expected) {
    const path = href.split("?")[0]!;
    expect(routes.some((route) => route.test(path)), `${path} is not a route`).toBe(true);
  }
}

function renderPage(overrides: Partial<ApiClient> = {}, entry = "/credentials") {
  const api: Partial<ApiClient> = {
    me: vi.fn().mockResolvedValue(ME),
    listCredentials: vi.fn().mockResolvedValue([]),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // A ROUTER IS NOW PART OF THE HARNESS. Every stat tile renders as a link
  // (`StatCard` requires `to`), and the tab strip keeps its state in the query
  // string, so this page cannot mount outside a router at all — which is the
  // point: a tile with nowhere to go no longer type-checks.
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <CredentialsPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return { ...render(ui), api };
}

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("CredentialsPage", () => {
  it("says it is loading rather than showing an empty vault", async () => {
    renderPage({ listCredentials: () => new Promise<never>(() => {}) });
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty vault", async () => {
    renderPage({ listCredentials: () => Promise.reject(new Error("boom")) });
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No credentials stored/i)).not.toBeInTheDocument();
  });

  it("says the vault is empty when it is genuinely empty", async () => {
    renderPage({ listCredentials: () => Promise.resolve([]) });
    expect(await screen.findByText("No credentials stored.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lists credential metadata with the design's structure, and counts from data", async () => {
    renderPage({
      listCredentials: () =>
        Promise.resolve([
          credential(),
          credential({ id: "2", name: "Linux support key", type: "ssh_key", last_used_at: "2026-08-15T09:00:00Z" }),
        ]),
    });
    expect(await screen.findByText("Domain support admin")).toBeInTheDocument();
    expect(screen.getByText("Linux support key")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Credentials" })).toBeInTheDocument();

    const stats = document.querySelectorAll(".stat-card");
    const values = [...stats].map((card) => card.querySelector(".stat-value")?.textContent);
    // Stored credentials: 2. Never checked out: 1 (only the first has no
    // last_used_at). Credential types: 2 distinct (windows_admin, ssh_key).
    expect(values).toEqual(["2", "1", "2"]);
  });

  // "All tiles and all numbers or KPIs need to be clickable and direct the user
  // to the specific menus, i dont want dead tiles everywhere" — the operator,
  // 2026-08-21. The second claim is the one that rots: a `?state=` the page has
  // stopped reading still renders a page that looks perfectly fine, so the
  // destination is opened and the rows behind it are asserted.
  it("gives every tile a destination App.tsx actually routes", async () => {
    renderPage({ listCredentials: () => Promise.resolve([credential()]) });
    await screen.findByText("Domain support admin");

    expectTileLinks(["/credentials", "/credentials?state=never-used", "/credentials"]);
  });

  it("opens the never-checked-out rows at the destination that tile names", async () => {
    renderPage(
      {
        listCredentials: () =>
          Promise.resolve([
            credential(),
            credential({ id: "2", name: "Linux support key", last_used_at: "2026-08-15T09:00:00Z" }),
          ]),
      },
      "/credentials?state=never-used",
    );

    // The one that has never been checked out, and only it. BREAKS AGAINST a
    // `?state=` this page renders and ignores.
    expect(await screen.findByText("Domain support admin")).toBeInTheDocument();
    expect(screen.queryByText("Linux support key")).not.toBeInTheDocument();
    // The tiles still count the whole vault: a number that changed when it was
    // clicked was lying before or is lying now.
    const values = [...document.querySelectorAll(".stat-card .stat-value")].map(
      (value) => value.textContent,
    );
    expect(values[0]).toBe("2");
  });

  it("never renders a secret value on the list, loading, or empty paths", async () => {
    const { container } = renderPage({
      listCredentials: () => Promise.resolve([credential({ name: "Domain support admin" })]),
    });
    await screen.findByText("Domain support admin");
    // The fixture has no `secret` field at all -- `Credential` (lib/api.ts)
    // carries only `has_secret: boolean`. Assert no plausible secret-shaped
    // string leaked in from elsewhere.
    expect(container.textContent).not.toMatch(/secret-value|password123/i);
    expect(document.querySelector("code")).not.toBeInTheDocument();
  });

  it("reveals a secret only after an audited checkout, and only inside the one-time dialog", async () => {
    const user = userEvent.setup();
    const checkoutCredential = vi.fn().mockResolvedValue({ secret: "sup3r-s3cr3t-value" });
    renderPage({
      listCredentials: () => Promise.resolve([credential()]),
      checkoutCredential,
    });
    await screen.findByText("Domain support admin");

    // Not present before checkout.
    expect(screen.queryByText("sup3r-s3cr3t-value")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /check out/i }));
    await user.type(screen.getByPlaceholderText(/helpdesk ticket/i), "helpdesk ticket 4821");
    await user.click(screen.getByRole("button", { name: "Reveal" }));

    expect(checkoutCredential).toHaveBeenCalledExactlyOnceWith(
      credential().id,
      "helpdesk ticket 4821",
    );
    // Now, and only now, the secret is on the page -- inside the dialog that
    // says it is shown once.
    expect(await screen.findByText("sup3r-s3cr3t-value")).toBeInTheDocument();
    expect(screen.getByText(/shown once/i)).toBeInTheDocument();

    // Closing the dialog clears it from local state -- it must not linger.
    await user.click(screen.getByRole("button", { name: /^done$/i }));
    expect(screen.queryByText("sup3r-s3cr3t-value")).not.toBeInTheDocument();
  });

  it("does not offer delete to an operator without credentials.manage", async () => {
    renderPage({
      me: vi.fn().mockResolvedValue({ permissions: [] } as unknown as Me),
      listCredentials: () => Promise.resolve([credential()]),
    });
    await screen.findByText("Domain support admin");
    expect(screen.queryByRole("button", { name: /delete|more actions/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /new credential/i })).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// UX AUDIT [HIGH], /credentials → New credential → Store: "Store with every
// field empty is a silent no-op".
//
// Both forms on this route carried the product-wide expression
// `<Button disabled={mutation.isPending || !field}>` with no <form>, no
// rendered reason and no live region, so the click moved nothing and named
// nothing. Every test below fails against that version.
// ---------------------------------------------------------------------------

describe("CredentialsPage opened from Push install", () => {
  it("opens New credential with Push install already chosen", async () => {
    // The push install screens link to /credentials?new=push_install when no
    // such credential exists; landing on a blank list would lose the thread.
    renderPage({ listCredentials: () => Promise.resolve([]) }, "/credentials?new=push_install");
    const dialog = await screen.findByRole("dialog", { name: "New credential" });
    expect(within(dialog).getByLabelText("Type")).toHaveValue("push_install");
  });

  it("ignores a type it does not know", async () => {
    renderPage({ listCredentials: () => Promise.resolve([]) }, "/credentials?new=root_password");
    await screen.findByRole("button", { name: /new credential/i });
    expect(screen.queryByRole("dialog", { name: "New credential" })).toBeNull();
  });
});

describe("CredentialsPage's New credential form", () => {
  async function openCreateDialog(overrides: Partial<ApiClient> = {}) {
    const user = userEvent.setup();
    const createCredential = vi.fn().mockResolvedValue(credential());
    const rendered = renderPage({
      listCredentials: () => Promise.resolve([]),
      createCredential,
      ...overrides,
    });
    await user.click(await screen.findByRole("button", { name: /new credential/i }));
    return { user, createCredential, ...rendered };
  }

  it("keeps Store enabled on an empty form and names every missing field", async () => {
    // THE RULE: a disabled button that never says why IS the silent no-op.
    // Missing input keeps the control live; the click produces the reason, and
    // it names BOTH fields, because a person who pressed Store needs the whole
    // list to plan what to type next.
    const { user, createCredential } = await openCreateDialog();

    const store = screen.getByRole("button", { name: "Store" });
    expect(store).toBeEnabled();
    await user.click(store);

    expect(createCredential).not.toHaveBeenCalled();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Name is required");
    expect(alert).toHaveTextContent("Secret is required");
  });

  it("treats a whitespace-only Name as empty", async () => {
    // `" "` is truthy, which is exactly how a blank name got past a `!name`
    // guard and became a vault profile nobody could identify later.
    const { user, createCredential } = await openCreateDialog();

    await user.type(screen.getByLabelText("Name"), "   ");
    await user.type(screen.getByLabelText("Secret"), "hunter2");
    await user.click(screen.getByRole("button", { name: "Store" }));

    expect(createCredential).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent("Name is required");
  });

  it("submits on Enter from the Name field", async () => {
    // The dialog was not a `<form>` at all, so Enter did nothing anywhere on
    // this route.
    const { user, createCredential } = await openCreateDialog();

    await user.type(screen.getByLabelText("Secret"), "hunter2");
    await user.type(screen.getByLabelText("Name"), "Domain support admin{Enter}");

    expect(createCredential).toHaveBeenCalledTimes(1);
  });

  it("stores the secret byte-for-byte while trimming the name", async () => {
    // Trailing whitespace in a vault secret is a CHARACTER OF THE CREDENTIAL.
    // `requiredProblems` trims to decide emptiness and nothing else; trimming
    // the value on the way out would store something that cannot authenticate
    // and surface weeks later as an unreproducible remote-control failure.
    const { user, createCredential } = await openCreateDialog();

    await user.type(screen.getByLabelText("Name"), " Domain support admin ");
    await user.type(screen.getByLabelText("Secret"), " hunter2 ");
    await user.click(screen.getByRole("button", { name: "Store" }));

    expect(createCredential).toHaveBeenCalledExactlyOnceWith({
      name: "Domain support admin",
      type: "windows_admin",
      secret: " hunter2 ",
    });
  });

  it("keeps the dialog open and explains a bodyless 503", async () => {
    // `String(create.error)` rendered "Error: " noise at best, and for the
    // bodyless 503 that `describeSubmitError` exists for it rendered an empty
    // string — a failed store looked exactly like a successful one.
    const { user } = await openCreateDialog({
      createCredential: vi.fn().mockRejectedValue(new ApiError(503, "")),
    });

    await user.type(screen.getByLabelText("Name"), "Domain support admin");
    await user.type(screen.getByLabelText("Secret"), "hunter2");
    await user.click(screen.getByRole("button", { name: "Store" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent?.trim()).not.toBe("");
    expect(alert).toHaveTextContent(/unavailable/i);
    expect(screen.getByRole("dialog", { name: "New credential" })).toBeInTheDocument();
  });

  it("does not store when Cancel is pressed", async () => {
    // `ui.tsx`'s Button sets no `type`, and an untyped button inside a form
    // submits it — wrapping these controls in a `<form>` would otherwise have
    // turned Cancel into a second Store.
    const { user, createCredential } = await openCreateDialog();

    await user.type(screen.getByLabelText("Name"), "Domain support admin");
    await user.type(screen.getByLabelText("Secret"), "hunter2");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(createCredential).not.toHaveBeenCalled();
  });
});

describe("CredentialsPage's checkout form", () => {
  it("keeps Reveal enabled with no reason and names the field", async () => {
    // The same defect, one dialog over. A checkout reason is written verbatim
    // into the hash-chained audit log (invariant #4), so the console must
    // demand one out loud rather than by refusing to respond.
    const user = userEvent.setup();
    const checkoutCredential = vi.fn();
    renderPage({
      listCredentials: () => Promise.resolve([credential()]),
      checkoutCredential,
    });
    await screen.findByText("Domain support admin");
    await user.click(screen.getByRole("button", { name: /check out/i }));

    const reveal = screen.getByRole("button", { name: "Reveal" });
    expect(reveal).toBeEnabled();
    await user.click(reveal);

    expect(checkoutCredential).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent("Reason is required");
    // And no secret was invented to fill the gap.
    expect(screen.queryByRole("dialog", { name: /Domain support admin/ })).toBeInTheDocument();
  });

  it("treats a whitespace-only reason as no reason", async () => {
    const user = userEvent.setup();
    const checkoutCredential = vi.fn();
    renderPage({
      listCredentials: () => Promise.resolve([credential()]),
      checkoutCredential,
    });
    await screen.findByText("Domain support admin");
    await user.click(screen.getByRole("button", { name: /check out/i }));
    await user.type(screen.getByPlaceholderText(/helpdesk ticket/i), "   ");
    await user.click(screen.getByRole("button", { name: "Reveal" }));

    expect(checkoutCredential).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent("Reason is required");
  });

  it("says why a refused checkout failed instead of printing the raw error", async () => {
    const user = userEvent.setup();
    renderPage({
      listCredentials: () => Promise.resolve([credential()]),
      checkoutCredential: vi.fn().mockRejectedValue(new ApiError(403, "")),
    });
    await screen.findByText("Domain support admin");
    await user.click(screen.getByRole("button", { name: /check out/i }));
    await user.type(screen.getByPlaceholderText(/helpdesk ticket/i), "ticket 4821");
    await user.click(screen.getByRole("button", { name: "Reveal" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/do not have permission/i);
    expect(alert.textContent).not.toMatch(/^Error:/);
  });
});

// ---------------------------------------------------------------------
// Datetime (cycle 5) and failure text (cycle 4) on this page.
//
// EVERY TEST BELOW WAS FALSIFIED: the implementation was put back to its
// pre-cycle shape, the test was confirmed RED, then the implementation was
// restored and the test confirmed green.
// ---------------------------------------------------------------------

// Not the runner's zone (UTC), and not a fixed-offset zone: New York is -4 in
// August and -5 in January, so a cached-offset helper is still caught here.
const ME_ZONED: Me = { permissions: ["credentials.manage"], timezone: "America/New_York" } as Me;

describe("CredentialsPage — last used", () => {
  // BREAKS AGAINST: `new Date(x).toLocaleString()`, one of the ~70 ad-hoc
  // call sites behind "the same record reports two different times in two
  // different formats". It renders the BROWSER's zone, which is UTC here.
  it("renders last-used in the operator's profile zone, not the browser's", async () => {
    renderPage({
      me: vi.fn().mockResolvedValue(ME_ZONED),
      listCredentials: () =>
        Promise.resolve([credential({ last_used_at: "2026-08-15T09:00:00Z" })]),
    });

    await screen.findByText("Domain support admin");
    // 09:00Z is 05:00 in New York on 15 August (EDT, -4).
    // Relative on screen; the absolute time on hover is in the profile zone
    // (the org has none set here).
    await waitFor(() =>
      expect(document.querySelector("time")?.getAttribute("title")).toBe("15 Aug 2026, 05:00"),
    );
  });

  // BREAKS AGAINST: `new Date(null).toLocaleString()` -- "Invalid Date" --
  // and against the bespoke word "never" this column used, which no other
  // absent date in the product uses.
  it("renders a credential never checked out as NO_DATE, never Invalid Date", async () => {
    renderPage({
      me: vi.fn().mockResolvedValue(ME_ZONED),
      listCredentials: () => Promise.resolve([credential({ last_used_at: null })]),
    });

    await screen.findByText("Domain support admin");
    expect(screen.getByText(NO_DATE)).toBeInTheDocument();
    expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument();
  });
});

describe("CredentialsPage — a refused delete", () => {
  // BREAKS AGAINST: the pre-cycle `remove` mutation, which had NO `onError`
  // and whose error was rendered NOWHERE. A refused delete left the row
  // exactly where it was -- indistinguishable from a table that simply had
  // not refetched. This is the swallowed destructive call Auditor A filed as
  // a BLOCKER, on the one route where the object being deleted is a
  // privileged credential.
  it("announces a delete the server refused, rather than leaving the row to imply staleness", async () => {
    const user = userEvent.setup();
    const deleteCredential = vi.fn().mockRejectedValue(new ApiError(503, ""));
    renderPage({
      listCredentials: () => Promise.resolve([credential()]),
      deleteCredential,
    });
    await screen.findByText("Domain support admin");

    await deleteCredentialNamed(user, "Domain support admin");

    await waitFor(() => expect(deleteCredential).toHaveBeenCalled());
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/That credential was not deleted — it is still stored/);
    expect(alert).toHaveTextContent(/That service is unavailable right now/);
  });

  // A 403 is the likelier refusal here (RBAC on a privileged endpoint), and
  // it must read as a permission problem rather than as a transient one the
  // operator should retry.
  it("names a permission refusal as a permission refusal", async () => {
    const user = userEvent.setup();
    renderPage({
      listCredentials: () => Promise.resolve([credential()]),
      deleteCredential: vi.fn().mockRejectedValue(new ApiError(403, "")),
    });
    await screen.findByText("Domain support admin");

    await deleteCredentialNamed(user, "Domain support admin");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /You do not have permission to do that/,
    );
  });
});


/**
 * Delete, through the confirmation that now stands in front of it.
 *
 * The row's Delete opens the dialog; the dialog's "Delete credential" is what
 * sends the request.
 */
async function deleteCredentialNamed(
  user: ReturnType<typeof userEvent.setup>,
  name: string,
): Promise<void> {
  await user.click(screen.getByRole("button", { name: /^More actions for / }));
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));
  const confirm = await screen.findByRole("dialog", { name: "Delete stored credential" });
  expect(within(confirm).getByText(name)).toBeInTheDocument();
  await user.click(within(confirm).getByRole("button", { name: "Delete credential" }));
}

// ---------------------------------------------------------------------------
// CYCLE 8 on this route. Delete fired on a single click, with no confirmation
// and no undo, from a control sitting a few pixels from "Check out" in the
// same row. The vaulted secret is not recoverable and this console holds no
// second copy of it -- there is nothing to restore from and nothing to
// re-read.
//
// EVERY TEST BELOW WAS FALSIFIED: `onClick={() => remove.mutate(credential.id)}`
// was restored, each test confirmed RED, then the confirmation was put back
// and each confirmed green.
// ---------------------------------------------------------------------------

describe("CredentialsPage — deleting a stored credential", () => {
  it("does not delete on the first click — it asks, naming the credential", async () => {
    const user = userEvent.setup();
    const deleteCredential = vi.fn();
    renderPage({
      listCredentials: () => Promise.resolve([credential()]),
      deleteCredential,
    });
    await screen.findByText("Domain support admin");

    await user.click(screen.getByRole("button", { name: /^More actions for / }));
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));

    expect(deleteCredential).not.toHaveBeenCalled();
    const confirm = await screen.findByRole("dialog", { name: "Delete stored credential" });
    // The SUBJECT is what lets somebody notice they have the wrong row.
    expect(within(confirm).getByText("Domain support admin")).toBeInTheDocument();
    // The CONSEQUENCE, not "are you sure?".
    expect(confirm).toHaveTextContent(/cannot be recovered/i);
    expect(confirm).toHaveTextContent(/will fail until it is stored again/i);
  });

  it("sends nothing when the confirmation is cancelled, and the row stays", async () => {
    const user = userEvent.setup();
    const deleteCredential = vi.fn();
    renderPage({
      listCredentials: () => Promise.resolve([credential()]),
      deleteCredential,
    });
    await screen.findByText("Domain support admin");

    await user.click(screen.getByRole("button", { name: /^More actions for / }));
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));
    const confirm = await screen.findByRole("dialog", { name: "Delete stored credential" });
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));

    expect(deleteCredential).not.toHaveBeenCalled();
    expect(screen.getByText("Domain support admin")).toBeInTheDocument();
  });

  it("deletes exactly the confirmed credential once confirmed", async () => {
    const user = userEvent.setup();
    const deleteCredential = vi.fn().mockResolvedValue({ deleted: true });
    renderPage({
      listCredentials: () =>
        Promise.resolve([credential({ id: "keep-me", name: "Linux support key", type: "ssh_key" })]),
      deleteCredential,
    });
    await screen.findByText("Linux support key");

    await deleteCredentialNamed(user, "Linux support key");

    expect(deleteCredential).toHaveBeenCalledExactlyOnceWith("keep-me");
  });

  // No `requireTyping` here, deliberately: the blast radius is one credential
  // that can be stored again, and teaching people to copy a word without
  // reading it is what makes the confirmations that DO need typing worthless.
  it("does not demand the name be typed for a single credential", async () => {
    const user = userEvent.setup();
    renderPage({
      listCredentials: () => Promise.resolve([credential()]),
      deleteCredential: vi.fn(),
    });
    await screen.findByText("Domain support admin");

    await user.click(screen.getByRole("button", { name: /^More actions for / }));
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));
    const confirm = await screen.findByRole("dialog", { name: "Delete stored credential" });

    expect(within(confirm).queryByText(/to confirm/i)).not.toBeInTheDocument();
    expect(within(confirm).getByRole("button", { name: "Delete credential" })).toBeEnabled();
  });
});

// ---------------------------------------------------------------------------
// AUDIT [HIGH], /credentials → New credential → Secret: "The Secret input is
// `type="password"` with `autocomplete: null` ... password managers may
// autofill the operator's own password into the vault's Secret box, and offer
// to SAVE the privileged credential into the operator's personal password
// store on submit."
//
// Auditor A sharpened it from /email-intake, whose IMAP password already sets
// `autocomplete="new-password"`: the least sensitive of the product's three
// secrets was protected and the most sensitive was not.
// ---------------------------------------------------------------------------

describe("CredentialsPage — the vault form is not a login form", () => {
  it("tells password managers to keep out of the Secret field", async () => {
    const user = userEvent.setup();
    renderPage({ listCredentials: () => Promise.resolve([]), createCredential: vi.fn() });
    await user.click(await screen.findByRole("button", { name: /new credential/i }));

    const secret = screen.getByLabelText("Secret");
    expect(secret).toHaveAttribute("type", "password");
    // `new-password` is the value that stops both the autofill and the
    // offer-to-save; `off` is not honoured for password fields by Chrome.
    expect(secret).toHaveAttribute("autocomplete", "new-password");
    expect(secret).toHaveAttribute("data-1p-ignore");
    expect(secret).toHaveAttribute("data-lpignore", "true");
  });

  it("keeps the Name beside it out of the same heuristics", async () => {
    // A username-shaped field next to a password field is half of what makes
    // a manager treat the pair as a login.
    const user = userEvent.setup();
    renderPage({ listCredentials: () => Promise.resolve([]), createCredential: vi.fn() });
    await user.click(await screen.findByRole("button", { name: /new credential/i }));

    expect(screen.getByLabelText("Name")).toHaveAttribute("autocomplete", "off");
  });
});

// ---------------------------------------------------------------------------
// AUDIT [MEDIUM], /credentials → Type: "The options are `windows_admin`,
// `ssh_key`, `domain_join` — unmapped database enum values with underscores
// ... an operator choosing between 'ssh_key' and 'domain_join' gets no
// explanation of what each type implies for how the secret is used or where it
// can be checked out."
// ---------------------------------------------------------------------------

describe("CredentialsPage — credential types as a person reads them", () => {
  it("labels the type on every row rather than printing the enum", async () => {
    renderPage({
      listCredentials: () =>
        Promise.resolve([
          credential(),
          credential({ id: "2", name: "Linux support key", type: "ssh_key" }),
        ]),
    });
    await screen.findByText("Domain support admin");

    expect(screen.getByText("Windows administrator")).toBeInTheDocument();
    expect(screen.getByText("SSH key")).toBeInTheDocument();
    expect(screen.queryByText("windows_admin")).not.toBeInTheDocument();
    expect(screen.queryByText("ssh_key")).not.toBeInTheDocument();
  });

  it("says what the selected type means for how its secret is used", async () => {
    const user = userEvent.setup();
    renderPage({ listCredentials: () => Promise.resolve([]), createCredential: vi.fn() });
    await user.click(await screen.findByRole("button", { name: /new credential/i }));

    expect(screen.getByText(/checked out for unattended remote-control sessions/i)).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Type"), "domain_join");

    expect(screen.getByText(/used by provisioning — not by interactive remote sessions/i)).toBeInTheDocument();
  });

  it("still submits the wire value, not the label", async () => {
    const user = userEvent.setup();
    const createCredential = vi.fn().mockResolvedValue(credential());
    renderPage({ listCredentials: () => Promise.resolve([]), createCredential });
    await user.click(await screen.findByRole("button", { name: /new credential/i }));

    await user.selectOptions(screen.getByLabelText("Type"), "ssh_key");
    await user.type(screen.getByLabelText("Name"), "Linux support key");
    await user.type(screen.getByLabelText("Secret"), "-----BEGIN KEY-----");
    await user.click(screen.getByRole("button", { name: "Store" }));

    expect(createCredential).toHaveBeenCalledExactlyOnceWith({
      name: "Linux support key",
      type: "ssh_key",
      secret: "-----BEGIN KEY-----",
    });
  });

  // AUDIT [LOW]: "'Credential types 0' is misleading given the Type dropdown
  // offers three; the card appears to count *used* types while reading as a
  // capability count."
  it("says the types card counts what is stored, and what the vault could hold", async () => {
    renderPage({ listCredentials: () => Promise.resolve([credential()]) });
    await screen.findByText("Domain support admin");

    const cards = [...document.querySelectorAll(".stat-card")].map((card) => card.textContent);
    expect(cards[2]).toMatch(/Types in use/);
    // FIVE SINCE `windows_domain` (WinRM push, design §2.5); four since
    // `push_install` (`CLAUDE.md` invariant 2a, cycle B1). The number is
    // deliberately read off `CREDENTIAL_TYPES` on the card and asserted as a
    // literal here: a card that said "4" while the dropdown offered five is
    // the exact misstatement this test was written for.
    expect(cards[2]).toMatch(/5 types available/);
    expect(cards[2]).not.toMatch(/Credential types/);
  });

  // v3 draws a supporting line under every tile. Two of these three carried
  // only a figure, so one tile in the row said what it was made of and the
  // others said nothing -- the anatomy was inconsistent inside a single grid.
  //
  // BREAKS AGAINST: either line removed, and against a line invented for a
  // figure the rows cannot support (an empty vault has nothing to break down).
  it("gives every tile in the row a supporting line", async () => {
    renderPage({
      listCredentials: () =>
        Promise.resolve([
          credential({ last_used_at: "2026-08-30T09:00:00Z" }),
          credential({ id: "cred-2", name: "Unused key", last_used_at: null }),
        ]),
    });
    await screen.findByText("Domain support admin");

    const cards = [...document.querySelectorAll(".stat-card")].map((card) => card.textContent);
    expect(cards[0]).toMatch(/1 checked out at least once/);
    expect(cards[1]).toMatch(/Stored, and never decrypted since/);
    expect(cards[2]).toMatch(/5 types available/);
  });

  it("invents no supporting line for an empty vault", async () => {
    renderPage({ listCredentials: () => Promise.resolve([]) });
    await screen.findByText("The vault is empty");

    const cards = [...document.querySelectorAll(".stat-card")].map((card) => card.textContent);
    expect(cards[0]).not.toMatch(/checked out at least once/);
    expect(cards[1]).not.toMatch(/never decrypted/);
  });
});

// ---------------------------------------------------------------------------
// The checkout reason is written verbatim into the hash-chained audit log
// (invariant #4). Required alone buys "x" — the same bar /remote's start
// reason carries applies here.
// ---------------------------------------------------------------------------

describe("CredentialsPage — a checkout reason that says something", () => {
  it("refuses a one-character reason", async () => {
    const user = userEvent.setup();
    const checkoutCredential = vi.fn();
    renderPage({
      listCredentials: () => Promise.resolve([credential()]),
      checkoutCredential,
    });
    await screen.findByText("Domain support admin");
    await user.click(screen.getByRole("button", { name: /check out/i }));
    await user.type(screen.getByPlaceholderText(/helpdesk ticket/i), "x");
    await user.click(screen.getByRole("button", { name: "Reveal" }));

    expect(checkoutCredential).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent(/audit log/i);
  });
});

// ---------------------------------------------------------------------------
// AUDIT [LOW], /credentials → "Vault policy" / "Point-of-use boundary": "with
// zero credentials stored there is no checkout affordance anywhere ... The
// excellent policy copy is also disconnected from any action the user can
// take."
//
// EVERY TEST BELOW WAS FALSIFIED: the two sentences were removed from the
// panel, both confirmed RED, then restored.
// ---------------------------------------------------------------------------

describe("CredentialsPage — the policy copy points at the action", () => {
  it("says where to check a credential out once there is one to check out", async () => {
    renderPage({ listCredentials: () => Promise.resolve([credential()]) });
    await screen.findByText("Domain support admin");

    expect(screen.getByText(/Check out any credential from its row above/i)).toBeInTheDocument();
  });

  it("says what would make that action exist, when the vault is empty", async () => {
    renderPage({ listCredentials: () => Promise.resolve([]) });
    await screen.findByText("No credentials stored.");

    expect(screen.getByText(/its checkout action appears on its row/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// The Windows domain account (WinRM push, design §2.3, §2.5).
//
// The vault stores ONE secret per credential, and a WinRM push needs an
// account, a password and, optionally, a KDC and the AD CS root for 5986. So
// this type composes the JSON blob the dispatch path parses
// (`{"username","secret","kdc","ca_bundle"}`) instead of asking the operator
// to hand-type JSON into a password box.
// ---------------------------------------------------------------------------

describe("CredentialsPage — a Windows domain account", () => {
  async function openWindowsDomain() {
    const user = userEvent.setup();
    const createCredential = vi.fn().mockResolvedValue(credential({ type: "windows_domain" }));
    renderPage(
      { listCredentials: () => Promise.resolve([]), createCredential },
      "/credentials?new=windows_domain",
    );
    const dialog = await screen.findByRole("dialog", { name: "New credential" });
    return { user, createCredential, dialog };
  }

  it("asks for the account, the password and an optional domain controller and CA", async () => {
    const { dialog } = await openWindowsDomain();

    expect(within(dialog).getByLabelText("Type")).toHaveValue("windows_domain");
    expect(within(dialog).getByLabelText(/^Account/)).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/^Password/)).toHaveAttribute("type", "password");
    expect(within(dialog).getByLabelText(/^Domain controller/)).toBeInTheDocument();
    // The CA bundle is public material: a textarea, never a password box.
    const ca = within(dialog).getByLabelText(/^CA certificate/);
    expect(ca.tagName).toBe("TEXTAREA");
    expect(ca).toHaveAttribute("autocomplete", "off");
    // The single free-form Secret box is gone for this type.
    expect(within(dialog).queryByLabelText("Secret")).toBeNull();
  });

  it("keeps the password out of password managers exactly as Secret does", async () => {
    const { dialog } = await openWindowsDomain();
    const password = within(dialog).getByLabelText(/^Password/);
    expect(password).toHaveAttribute("autocomplete", "new-password");
    expect(password).toHaveAttribute("data-1p-ignore");
    expect(password).toHaveAttribute("data-lpignore", "true");
  });

  it("carries a standing warning against a Domain Admin account", async () => {
    const { dialog } = await openWindowsDomain();
    const warning = within(dialog).getByRole("note");
    expect(warning).toHaveTextContent(/Domain Admin/);
    expect(warning).toHaveTextContent(/compromised domain/);
  });

  it("shows the realm a UPN names, and does not pass a NetBIOS name off as one", async () => {
    const { user, dialog } = await openWindowsDomain();
    const account = within(dialog).getByLabelText(/^Account/);

    await user.type(account, "svc-push@corp.example.com");
    expect(within(dialog).getByText(/Realm: CORP\.EXAMPLE\.COM/)).toBeInTheDocument();
    // Review I1: the realm is the ACCOUNT's, and a push only reaches machines
    // inside it -- said at the point of choice.
    expect(within(dialog).getByText(/only to machines whose DNS name is in this domain/i)).toBeInTheDocument();

    await user.clear(account);
    await user.type(account, "CORP\\svc-push");
    expect(within(dialog).queryByText(/Realm: CORP\b(?!\.)/)).toBeNull();
    // The old hint -- "each target's DNS domain" -- described the rule review
    // I1 removed. A NetBIOS account is now simply not accepted.
    expect(within(dialog).queryByText(/each target's DNS domain/i)).toBeNull();
    expect(within(dialog).getByText(/Enter the account as user@your\.domain/)).toBeInTheDocument();
  });

  it("stores the account and password as the JSON secret the push reads", async () => {
    const { user, createCredential, dialog } = await openWindowsDomain();

    await user.type(within(dialog).getByLabelText("Name"), "Workstation installer");
    await user.type(within(dialog).getByLabelText(/^Account/), "svc-push@corp.example.com");
    await user.type(within(dialog).getByLabelText(/^Password/), " s3cret ");
    await user.type(within(dialog).getByLabelText(/^Domain controller/), "dc1.corp.example.com");
    await user.click(within(dialog).getByRole("button", { name: "Store" }));

    expect(createCredential).toHaveBeenCalledTimes(1);
    const body = createCredential.mock.calls[0]![0] as { name: string; type: string; secret: string };
    expect(body.name).toBe("Workstation installer");
    expect(body.type).toBe("windows_domain");
    // The password byte-for-byte, as every other vault secret; an empty
    // optional field is left out rather than stored as "".
    expect(JSON.parse(body.secret)).toEqual({
      username: "svc-push@corp.example.com",
      secret: " s3cret ",
      kdc: "dc1.corp.example.com",
    });
  });

  it("names the missing account and password rather than storing half a credential", async () => {
    const { user, createCredential, dialog } = await openWindowsDomain();

    await user.type(within(dialog).getByLabelText("Name"), "Workstation installer");
    await user.click(within(dialog).getByRole("button", { name: "Store" }));

    expect(createCredential).not.toHaveBeenCalled();
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Account is required");
    expect(alert).toHaveTextContent("Password is required");
  });

  it.each(["svc-push", "CORP\\svc-push", "svc-push@corp"])(
    "refuses %s: the account must be user@domain, which names its realm",
    async (typed) => {
      const { user, createCredential, dialog } = await openWindowsDomain();

      await user.type(within(dialog).getByLabelText("Name"), "Workstation installer");
      await user.type(within(dialog).getByLabelText(/^Account/), typed);
      await user.type(within(dialog).getByLabelText(/^Password/), "s3cret");
      await user.click(within(dialog).getByRole("button", { name: "Store" }));

      expect(createCredential).not.toHaveBeenCalled();
      expect(await within(dialog).findByRole("alert")).toHaveTextContent(/Account must be user@domain/);
    },
  );
});
