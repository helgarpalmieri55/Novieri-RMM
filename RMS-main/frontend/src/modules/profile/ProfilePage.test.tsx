/**
 * Profile tab — `GET/PATCH /api/v1/me`, plus the three in-flow credential
 * steps.
 *
 * THE SPLIT IS THE DESIGN (spec §7.1). ITMS owns the presentation preferences
 * — display name, phone, timezone, locale. Keycloak owns the credentials —
 * password, MFA device, passkeys, and the email address. So this tab has no
 * password field and never will.
 *
 * WHAT CHANGED, AND WHY IT IS NOT COSMETIC. These three actions used to be
 * ANCHORS into Keycloak's account console — a second application, with the
 * vendor's name on its chrome, opening in a second tab. That is the seam the
 * operator hit in production, and it is the one thing a buyer cannot be asked
 * to understand. They are now BUTTONS that start an application-initiated
 * action: one step, on the same themed sign-in pages, then back into ITMS.
 * The credential still never touches this platform — that part is unchanged
 * and is the reason a form here was never an option.
 *
 * `PATCH /api/v1/me` is being written by a concurrent workstream. A 404
 * renders the form disabled and says why, exactly as the Organization tab
 * does — never an editable form that posts into the void.
 */

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import {
  ALL_PERMISSIONS,
  ME_FIXTURE,
  apiClient,
  calls,
  installDefaults,
  renderProfilePage,
} from "../settings/__tests__/harness";

const { startAccountAction } = vi.hoisted(() => ({ startAccountAction: vi.fn() }));

vi.mock("../../lib/auth", () => ({
  accessToken: () => "test-token",
  startAccountAction,
}));

vi.mock("../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

beforeEach(() => {
  installDefaults();
  // `installDefaults` only knows about the API surface, so this one is reset
  // by hand. Left un-reset, a call from the previous test satisfies the next
  // assertion and the click is never actually exercised.
  startAccountAction.mockReset();
  startAccountAction.mockResolvedValue(undefined);
});

async function openProfile() {
  const user = userEvent.setup();
  // A ROUTE NOW, NOT A TAB, so there is no tab to click: this used to render
  // the whole of Settings and click through to "Profile". See the page's own
  // header for why the tab could not survive -- its credential steps return to
  // a URL, and the active tab was never in one.
  renderProfilePage();
  return user;
}

describe("Your profile", () => {
  it("says it is loading rather than rendering an empty form", async () => {
    calls.me.mockReturnValue(new Promise(() => {}));
    renderProfilePage();
    // The page needs no permission — every session has a profile — so it
    // renders before `/me` answers, and says so rather than showing an empty
    // form somebody might start typing into.
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("seeds the form from the server's own values", async () => {
    await openProfile();
    expect(await screen.findByLabelText<HTMLInputElement>("Display name")).toHaveValue("Operator");
    expect(screen.getByLabelText<HTMLInputElement>("Phone")).toHaveValue(ME_FIXTURE.phone);
    expect(screen.getByLabelText<HTMLInputElement>("Timezone")).toHaveValue(ME_FIXTURE.timezone);
    expect(screen.getByLabelText<HTMLSelectElement>("Language")).toHaveValue("en");
    expect(screen.getByText(/operator@example\.com/)).toBeInTheDocument();
  });

  it("PATCHes only the four fields this route owns, and claims nothing before the server answers", async () => {
    // The re-read after the write returns what the server STORED. Leaving the
    // original fixture in place would let the form claim "Saved." over a value
    // the server never confirmed — the same reason the org tab re-seeds from
    // the PATCH response rather than from what was typed.
    const stored = { ...ME_FIXTURE, phone: "+57 601 555 0199", permissions: [...ALL_PERMISSIONS] };
    calls.me
      .mockResolvedValueOnce({ ...ME_FIXTURE, permissions: [...ALL_PERMISSIONS] })
      .mockResolvedValue(stored);
    calls.updateMe.mockResolvedValue(stored);
    const user = await openProfile();
    const phone = await screen.findByLabelText("Phone");
    await user.clear(phone);
    await user.type(phone, "+57 601 555 0199");
    expect(calls.updateMe).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(calls.updateMe).toHaveBeenCalledTimes(1));
    expect(calls.updateMe).toHaveBeenCalledWith({
      display_name: "Operator",
      phone: "+57 601 555 0199",
      timezone: "America/Bogota",
      locale: "en-GB",
    });
    expect(await screen.findByRole("status")).toHaveTextContent(/Saved/);
    expect(screen.getByLabelText<HTMLInputElement>("Phone")).toHaveValue("+57 601 555 0199");
  });

  it("refuses a clean save out loud instead of presenting a dead button", async () => {
    // ONE CONTROL, ONE BEHAVIOUR, ACROSS THE GROUP. The sibling route
    // /settings draws this same `SaveBar` with `requireDirty={false}`, so its
    // "Save changes" is a live accent button that says why it will not save.
    // Here the default left a pale disabled chip -- same size, radius, icon
    // and shape as the enabled "Change password" below it, and no on-screen
    // reason it could not be pressed.
    const user = await openProfile();
    const save = await screen.findByRole("button", { name: "Save changes" });
    expect(save).toBeEnabled();

    await user.click(save);
    expect(await screen.findByRole("alert")).toHaveTextContent(/no unsaved changes/i);
    // Refused, not written: nothing reached the server.
    expect(calls.updateMe).not.toHaveBeenCalled();

    // And the refusal is stale the moment there IS something to save.
    await user.type(screen.getByLabelText("Phone"), "9");
    expect(screen.queryByText(/no unsaved changes/i)).not.toBeInTheDocument();
  });

  it("a 404 disables the form and says why, rather than accepting typing that goes nowhere", async () => {
    calls.updateMe.mockRejectedValue(new ApiError(404, "Not Found"));
    const user = await openProfile();
    const phone = await screen.findByLabelText("Phone");
    await user.clear(phone);
    await user.type(phone, "+57 601 555 0199");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    const alert = await screen.findByRole("alert");
    // THE SERVER'S OWN WORDS, not the status code (cycle 4). This asserted
    // `/404/` while the save bar printed `error.message`; the kernel's
    // `describeSubmitError` renders the body a server sent and falls back to a
    // sentence when it sent none. The property that matters is unchanged and
    // is asserted on the next line: nothing claims a save.
    expect(alert).toHaveTextContent(/Not Found/);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("says why the profile did not load, rather than one sentence for every cause", async () => {
    // "That did not load. Retry, or check the platform status page." was the
    // page's answer to a 403, to a 404 on an API older than `GET /me`, and to
    // a proxy 503 alike -- three different things for the operator to do.
    calls.me.mockRejectedValue(new ApiError(403, ""));
    renderProfilePage();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/do not have permission/i);
    expect(screen.queryByLabelText("Display name")).not.toBeInTheDocument();
  });

  it("says so when a credential step cannot even be started", async () => {
    // A FAILURE SWALLOWED WHOLE: `void startAccountAction(...)` discarded the
    // rejection. `signinRedirect` fetches the provider's discovery document
    // before it navigates, so an identity provider that is down produced a
    // button that did nothing at all -- which is exactly the report this page
    // was rewritten to fix ("when I click it just refreshes"), arriving
    // through a second door.
    startAccountAction.mockRejectedValue(new Error("Failed to fetch the OIDC metadata"));
    const user = await openProfile();
    await user.click(await screen.findByRole("button", { name: "Change password" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/could not be started/i);
    expect(alert).toHaveTextContent(/nothing about your sign-in was changed/i);
    expect(alert).toHaveTextContent(/Failed to fetch the OIDC metadata/);
  });

  it("starts the credential step in place rather than opening another app", async () => {
    /**
     * THE SEAM THE BUYER SEES. A link to the account console is a visibly
     * different product: different chrome, different navigation, and the
     * vendor's name on it. This asserts the button starts an in-flow action
     * and that no anchor to an account console survives anywhere on the page.
     */
    const user = await openProfile();

    const button = await screen.findByRole("button", { name: /change password/i });
    await user.click(button);
    expect(startAccountAction).toHaveBeenCalledWith("UPDATE_PASSWORD");

    const links = screen.queryAllByRole("link");
    expect(links.filter((a) => a.getAttribute("href")?.includes("account"))).toEqual([]);
  });

  it("wires each of the three steps to its own action, not all three to one", async () => {
    // Three buttons that all change the password is exactly the bug a single
    // shared handler produces, and it looks correct until somebody clicks the
    // second one.
    const user = await openProfile();
    for (const [label, action] of [
      ["Change password", "UPDATE_PASSWORD"],
      ["Manage MFA device", "CONFIGURE_TOTP"],
      ["Add a passkey", "webauthn-register-passwordless"],
    ] as const) {
      startAccountAction.mockClear();
      await user.click(await screen.findByRole("button", { name: new RegExp(label, "i") }));
      expect(startAccountAction).toHaveBeenCalledWith(action);
    }
  });

  it("never tells the reader there is a second product to go and understand", async () => {
    // Task 3's guard sweeps every source file; this one is the assertion at
    // the place the word actually leaked — "Keycloak asks for your current
    // password first" was rendered copy on this very panel.
    await openProfile();
    await screen.findByRole("button", { name: /change password/i });
    expect(document.body.textContent ?? "").not.toMatch(/keycloak/i);
    expect(document.body.textContent ?? "").not.toMatch(/account console/i);
  });

  it("lets the four fields fold to one column instead of holding two at any width", async () => {
    // At 390px "Display name" / "Phone" and "Timezone" / "Locale" stayed
    // two-up at about 148px each — the only thing on the page still in two
    // columns, with both panels and the sign-in buttons folding around them.
    // The kit's own 640px fold has since landed, so this grid would reflow
    // without the inline value; it keeps counting its own columns because
    // `auto-fit` also caps the pair at one field's width in a wide panel,
    // which no breakpoint expresses.
    await openProfile();
    const grid = (await screen.findByLabelText("Display name")).closest(".form-grid");
    expect(grid).not.toBeNull();
    const columns = (grid as HTMLElement).style.gridTemplateColumns;
    expect(columns).toContain("auto-fit");
    // A fixed two-column track is exactly what could not fold.
    expect(columns).not.toContain("repeat(2");
  });

  it("has no password field, and no field for the email address (invariant 3, spec §7.1)", async () => {
    await openProfile();
    await screen.findByLabelText("Display name");
    expect(document.querySelectorAll('input[type="password"]')).toHaveLength(0);
    expect(screen.queryByLabelText(/^Email/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/current password/i)).not.toBeInTheDocument();
  });
});
