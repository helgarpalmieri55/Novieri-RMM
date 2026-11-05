/**
 * Your profile — `GET/PATCH /api/v1/me`, and the way to Sign-in and security.
 *
 * THE SPLIT (own-auth design §1.1, §2). This page owns the presentation
 * preferences — display name, phone, timezone, locale — and has no password
 * field. Credentials are the platform's own since own-auth phase 1, and they
 * are changed on their own page, `/profile/security`, where every change asks
 * for a recent second factor first.
 *
 * RE-POINTED (own-auth Task 10). Three tests held this page to three buttons
 * that each started one step at Keycloak (`startAccountAction`): that each
 * asked for its own action, that a step that could not start said so, and
 * that no link to Keycloak's account console survived. The page starts no
 * step anywhere now; it links to its own sign-in and security page, and the
 * tests hold it to that, and still to no second product.
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

vi.mock("../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

beforeEach(() => {
  installDefaults();
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

  it("sends sign-in and security to their own page, in this console", async () => {
    // RE-POINTED: was "starts the credential step in place rather than
    // opening another app" and "wires each of the three steps to its own
    // action". The one control left is a link, and it stays on this origin.
    await openProfile();
    const link = await screen.findByRole("link", { name: "Manage sign-in and security" });
    expect(link).toHaveAttribute("href", "/profile/security");
    const links = screen.queryAllByRole("link");
    expect(links.filter((a) => /^https?:|account|realms/.test(a.getAttribute("href") ?? ""))).toEqual([]);
  });

  it("starts no credential step itself, so it has no failure of one to report", async () => {
    // RE-POINTED: was "says so when a credential step cannot even be started"
    // (a redirect to Keycloak that fetched its discovery document first). No
    // button here leaves the page, so nothing can fail before it goes.
    await openProfile();
    await screen.findByRole("link", { name: "Manage sign-in and security" });
    expect(screen.queryByRole("button", { name: /change password|mfa|passkey/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("never tells the reader there is a second product to go and understand", async () => {
    // Task 3's guard sweeps every source file; this one is the assertion at
    // the place the word actually leaked — "Keycloak asks for your current
    // password first" was rendered copy on this very panel.
    await openProfile();
    await screen.findByRole("link", { name: "Manage sign-in and security" });
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
