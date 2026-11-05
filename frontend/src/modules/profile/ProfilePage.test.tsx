/**
 * Your profile -- `GET/PATCH /api/v1/me`, and the way to your security.
 *
 * THE SPLIT IS THE DESIGN. This page owns how the console shows you --
 * display name, phone, timezone, locale. How you sign in -- password,
 * passkeys, the authenticator app, recovery codes, remembered devices and
 * sessions -- is one page further, at `/profile/security`.
 *
 * WHAT CHANGED, AND WHY (own authentication design §1.1, §2). These used to be
 * three buttons that started one step each at the identity provider's own
 * pages, and the tests here pinned which step each asked for and what happened
 * when one could not be started. The platform is its own identity provider
 * now: there is no second application to step into, the panel is a link
 * inside this console, and `SecurityPage.test.tsx` pins that each change calls
 * its own route. (Own-auth Task 10 first made the panel a link, "Manage
 * sign-in and security"; Task 12 names it for the page it opens, "Open your
 * security", and the tests that pinned the old name are re-pointed to it.)
 *
 * A 404 on `PATCH /me` renders the form disabled and says why, exactly as the
 * Organization tab does -- never an editable form that posts into the void.
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
  // A ROUTE, NOT A TAB, so there is no tab to click: this used to render the
  // whole of Settings and click through to "Profile".
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

  it("offers your security page, a link inside this console", async () => {
    // RE-POINTED (own authentication design §2) from "says so when a
    // credential step cannot even be started": that test was about a step at a
    // second application that could fail to begin. There is no second
    // application; the panel is one link. (Task 10's "sends sign-in and
    // security to their own page, in this console" pinned the same link under
    // its old name, "Manage sign-in and security".)
    await openProfile();
    const link = await screen.findByRole("link", { name: "Open your security" });
    expect(link).toHaveAttribute("href", "/profile/security");
  });

  it("leaves for no other application", async () => {
    // RE-POINTED from "starts the credential step in place rather than
    // opening another app": THE SEAM THE BUYER SEES was a link to an account
    // console. No anchor out of this console survives on the page.
    await openProfile();
    await screen.findByRole("link", { name: "Open your security" });
    const outward = screen.queryAllByRole("link").filter((anchor) => {
      const href = anchor.getAttribute("href") ?? "";
      return href.includes("account") || href.includes("/realms/") || /^https?:/.test(href);
    });
    expect(outward).toEqual([]);
  });

  it("starts no credential step itself, so it has no failure of one to report", async () => {
    // KEPT FROM TASK 10, RE-POINTED to the link's new name ("Manage sign-in
    // and security" -> "Open your security"): was "says so when a credential
    // step cannot even be started" (a redirect to Keycloak that fetched its
    // discovery document first). No button here leaves the page, so nothing
    // can fail before it goes.
    await openProfile();
    await screen.findByRole("link", { name: "Open your security" });
    expect(screen.queryByRole("button", { name: /change password|mfa|passkey/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("never tells the reader there is a second product to go and understand", async () => {
    // Task 3's guard sweeps every source file; this one is the assertion at
    // the place the word once leaked, and at the promise this page used to
    // make that is no longer true ("never handled by this platform").
    await openProfile();
    await screen.findByRole("link", { name: "Open your security" });
    expect(document.body.textContent ?? "").not.toMatch(/keycloak/i);
    expect(document.body.textContent ?? "").not.toMatch(/account console/i);
    expect(document.body.textContent ?? "").not.toMatch(/never handled by this platform/i);
  });

  it("lets the four fields fold to one column instead of holding two at any width", async () => {
    // At 390px "Display name" / "Phone" and "Timezone" / "Locale" stayed
    // two-up at about 148px each. `auto-fit` also caps the pair at one
    // field's width in a wide panel, which no breakpoint expresses.
    await openProfile();
    const grid = (await screen.findByLabelText("Display name")).closest(".form-grid");
    expect(grid).not.toBeNull();
    const columns = (grid as HTMLElement).style.gridTemplateColumns;
    expect(columns).toContain("auto-fit");
    // A fixed two-column track is exactly what could not fold.
    expect(columns).not.toContain("repeat(2");
  });

  it("has no password field and no field for the email address", async () => {
    // RE-POINTED reason, same assertion: the password is changed on
    // `/profile/security`, behind the recent confirmation (design §2.5), not
    // in the profile's form; the address is not changed by this console.
    await openProfile();
    await screen.findByLabelText("Display name");
    expect(document.querySelectorAll('input[type="password"]')).toHaveLength(0);
    expect(screen.queryByLabelText(/^Email/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/current password/i)).not.toBeInTheDocument();
  });
});
