/**
 * Your security -- `/profile/security` (own authentication design §2.1-§2.6).
 *
 * WHAT THIS FILE HOLDS THE PAGE TO. Each change goes to its own route and only
 * there; the second factor is asked again when -- and only when -- the server
 * says the last confirmation is stale; the console's own checks on a new
 * password say so before the round trip and never stand in for the server's
 * refusal; and the values shown once (the authenticator key, new recovery
 * codes) are on screen only until the person says they have them. The page
 * renders named fields of `GET /me/security`, never the response, so a field a
 * server should not have sent is not drawn.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import {
  CREATION_OPTIONS_FIXTURE,
  MY_SECURITY_FIXTURE,
  OTPAUTH_URI_FIXTURE,
  PLANTED_SECRET,
  RECOVERY_CODES_FIXTURE,
  apiClient,
  calls,
  currentLocation,
  installDefaults,
  mutationCacheText,
  renderSecurityPage,
  stepUpRequired,
} from "../settings/__tests__/harness";
import { setupKeyOf } from "../auth/flow";

const { startRegistration, startAuthentication } = vi.hoisted(() => ({
  startRegistration: vi.fn(),
  startAuthentication: vi.fn(),
}));
// The real module but for the two ceremonies: the page goes through Task 11's
// `modules/auth/passkey.ts`, which also reads `WebAuthnError` from it.
vi.mock("@simplewebauthn/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@simplewebauthn/browser")>()),
  startRegistration,
  startAuthentication,
}));
vi.mock("../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

const { WebAuthnError } = await import("@simplewebauthn/browser");

/** What `startRegistration` hands back: the browser's answer, as W3C JSON. */
const ATTESTATION = {
  id: "bmV3",
  rawId: "bmV3",
  response: { clientDataJSON: "e30", attestationObject: "YXR0" },
  clientExtensionResults: {},
  type: "public-key",
};

const PASSWORD = { current: "an old phrase of mine", new: "correct horse battery staple" };

beforeEach(() => {
  installDefaults();
  startRegistration.mockReset();
  startAuthentication.mockReset();
  startRegistration.mockResolvedValue(ATTESTATION);
});

/** One panel of the page, found by its title. */
async function panel(title: string): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { name: title });
  const section = heading.closest("section");
  if (!section) throw new Error(`no panel titled ${title}`);
  return section;
}

async function typePassword(user: ReturnType<typeof userEvent.setup>, box: HTMLElement, next = PASSWORD.new) {
  await user.type(within(box).getByLabelText("Current password"), PASSWORD.current);
  await user.type(within(box).getByLabelText("New password"), next);
  await user.type(within(box).getByLabelText("New password again"), next);
  await user.click(within(box).getByRole("button", { name: "Change password" }));
}

describe("Your security", () => {
  it("says it is loading rather than drawing empty lists", async () => {
    calls.getMySecurity.mockReturnValue(new Promise(() => {}));
    renderSecurityPage();
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Passkeys" })).not.toBeInTheDocument();
  });

  it("says why it did not load", async () => {
    calls.getMySecurity.mockRejectedValue(new ApiError(503, ""));
    renderSecurityPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable/i);
  });

  it("shows the factors, the codes left, the remembered devices and the sessions it was given, and nothing else", async () => {
    renderSecurityPage();
    const passkeys = await panel("Passkeys");
    expect(within(passkeys).getByText("Work laptop")).toBeInTheDocument();
    const phone = within(passkeys).getByText("Phone").closest("li") as HTMLElement;
    expect(within(phone).getByText("Synced")).toBeInTheDocument();
    expect(within(await panel("Authenticator app")).getByText("Set up")).toBeInTheDocument();
    expect(within(await panel("Recovery codes")).getByText("8 of 10 left")).toBeInTheDocument();

    const devices = await panel("Remembered devices");
    const here = within(devices).getByText("Firefox on Windows").closest("li") as HTMLElement;
    expect(within(here).getByText("This device")).toBeInTheDocument();
    expect(within(devices).getByRole("button", { name: "Forget Safari on macOS" })).toBeInTheDocument();

    const sessions = await panel("Sessions");
    const current = within(sessions).getByText(/Firefox\/140\.0/).closest("li") as HTMLElement;
    expect(within(current).getByText("This session")).toBeInTheDocument();
    // This session ends by signing out, never from the list.
    expect(within(current).queryByRole("button")).not.toBeInTheDocument();
    expect(within(sessions).getByRole("button", { name: "Sign out the session from 10.0.0.9" })).toBeInTheDocument();

    expect(document.body.textContent).not.toContain(PLANTED_SECRET);
  });

  it("changes the password through its own route, and empties every box once the server answered", async () => {
    const user = userEvent.setup();
    renderSecurityPage();
    const password = await panel("Password");
    await typePassword(user, password);

    await waitFor(() => expect(calls.changePassword).toHaveBeenCalledTimes(1));
    expect(calls.changePassword).toHaveBeenCalledWith(PASSWORD);
    expect(await within(password).findByRole("status")).toHaveTextContent("Your password was changed.");
    for (const label of ["Current password", "New password", "New password again"]) {
      expect(within(password).getByLabelText<HTMLInputElement>(label)).toHaveValue("");
    }
  });

  it("refuses a short new password, and two that differ, before the round trip", async () => {
    // Task 11's rule and sentences (`checkNewPassword`), the ones the sign-in
    // pages say: one check of a new password in the console, not two.
    const user = userEvent.setup();
    renderSecurityPage();
    const password = await panel("Password");
    await typePassword(user, password, "too short");
    expect(await within(password).findByRole("alert")).toHaveTextContent("Use at least 12 characters.");

    await user.clear(within(password).getByLabelText("New password"));
    await user.type(within(password).getByLabelText("New password"), PASSWORD.new);
    await user.click(within(password).getByRole("button", { name: "Change password" }));
    expect(await within(password).findByRole("alert")).toHaveTextContent("The two passwords are not the same.");
    expect(calls.changePassword).not.toHaveBeenCalled();
  });

  it("counts each character once, the way the server does, and takes a pasted password", async () => {
    // Six keys are twelve UTF-16 units and six characters: `.length` would let
    // this through; the server would refuse it.
    const user = userEvent.setup();
    renderSecurityPage();
    const password = await panel("Password");
    await user.type(within(password).getByLabelText("Current password"), PASSWORD.current);
    for (const label of ["New password", "New password again"]) {
      await user.click(within(password).getByLabelText(label));
      await user.paste("🔑🔑🔑🔑🔑🔑");
    }
    await user.click(within(password).getByRole("button", { name: "Change password" }));
    expect(await within(password).findByRole("alert")).toHaveTextContent("Use at least 12 characters.");
    expect(within(password).getByText("6 characters")).toBeInTheDocument();
    expect(calls.changePassword).not.toHaveBeenCalled();
  });

  it("shows the server's own reason when it refuses the new password", async () => {
    calls.changePassword.mockRejectedValue(
      new ApiError(422, JSON.stringify({ detail: "That password is on the list attackers try first. Choose another." })),
    );
    const user = userEvent.setup();
    renderSecurityPage();
    const password = await panel("Password");
    await typePassword(user, password);
    expect(await within(password).findByRole("alert")).toHaveTextContent(
      "That password is on the list attackers try first. Choose another.",
    );
    expect(within(password).queryByRole("status")).not.toBeInTheDocument();
  });

  it("asks for the second factor again when the server says the last confirmation is stale, then changes the password", async () => {
    calls.changePassword.mockRejectedValueOnce(stepUpRequired()).mockResolvedValueOnce(undefined);
    const user = userEvent.setup();
    renderSecurityPage();
    const password = await panel("Password");
    await typePassword(user, password);

    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(await within(dialog).findByLabelText("Code from your authenticator app"), "123456");
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));

    expect(await within(password).findByRole("status")).toHaveTextContent("Your password was changed.");
    expect(calls.stepUpTotp).toHaveBeenCalledExactlyOnceWith("123456");
    expect(calls.changePassword).toHaveBeenCalledTimes(2);
    expect(calls.changePassword.mock.calls[1]?.[0]).toEqual(PASSWORD);
  });

  it("adds a passkey: the server's options to the browser, the browser's answer back, under the name given", async () => {
    const user = userEvent.setup();
    renderSecurityPage();
    const passkeys = await panel("Passkeys");
    await user.type(within(passkeys).getByLabelText("Name for the new passkey"), "Desk key");
    await user.click(within(passkeys).getByRole("button", { name: "Add a passkey" }));

    await waitFor(() => expect(calls.enrollPasskey).toHaveBeenCalledTimes(1));
    expect(startRegistration).toHaveBeenCalledExactlyOnceWith({ optionsJSON: CREATION_OPTIONS_FIXTURE });
    // With the full console session (Task 11's `EnrollFrom`): a 401 here is the
    // session ending, and must reach `onUnauthorized`.
    expect(calls.enrollPasskeyOptions).toHaveBeenCalledExactlyOnceWith({ signedIn: true });
    expect(calls.enrollPasskey).toHaveBeenCalledWith(ATTESTATION, "Desk key", { signedIn: true });
    // What the page shows next is what the server holds after the write.
    await waitFor(() => expect(calls.getMySecurity).toHaveBeenCalledTimes(2));
    expect(within(passkeys).getByLabelText<HTMLInputElement>("Name for the new passkey")).toHaveValue("");
  });

  it("changes nothing when the person cancels the browser's passkey prompt, and says so", async () => {
    startRegistration.mockRejectedValue(
      Object.assign(new Error("The operation either timed out or was not allowed."), { name: "NotAllowedError" }),
    );
    const user = userEvent.setup();
    renderSecurityPage();
    const passkeys = await panel("Passkeys");
    await user.type(within(passkeys).getByLabelText("Name for the new passkey"), "Desk key");
    await user.click(within(passkeys).getByRole("button", { name: "Add a passkey" }));

    // Task 11's sentence for a closed prompt (`describePasskeyError`).
    expect(await within(passkeys).findByRole("alert")).toHaveTextContent(
      "The passkey prompt was closed or timed out, so nothing was used.",
    );
    expect(calls.enrollPasskey).not.toHaveBeenCalled();
  });

  it("says a passkey already on the account is already there", async () => {
    // The library's own error, as Task 11's tests raise it: `describePasskeyError`
    // reads its `code`.
    startRegistration.mockRejectedValue(
      new WebAuthnError({
        message: "The authenticator was previously registered",
        code: "ERROR_AUTHENTICATOR_PREVIOUSLY_REGISTERED",
        cause: Object.assign(new Error("invalid state"), { name: "InvalidStateError" }),
      }),
    );
    const user = userEvent.setup();
    renderSecurityPage();
    const passkeys = await panel("Passkeys");
    await user.type(within(passkeys).getByLabelText("Name for the new passkey"), "Desk key");
    await user.click(within(passkeys).getByRole("button", { name: "Add a passkey" }));
    expect(await within(passkeys).findByRole("alert")).toHaveTextContent(
      "This device already holds a passkey for your account.",
    );
    expect(calls.enrollPasskey).not.toHaveBeenCalled();
  });

  it("removes a passkey once the confirmation names it", async () => {
    const user = userEvent.setup();
    renderSecurityPage();
    await user.click(await screen.findByRole("button", { name: "Remove the passkey Work laptop" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove passkey" });
    expect(within(dialog).getByText("Work laptop")).toBeInTheDocument();
    expect(calls.removePasskey).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "Remove passkey" }));
    await waitFor(() => expect(calls.removePasskey).toHaveBeenCalledExactlyOnceWith("pk-1"));
    await waitFor(() => expect(calls.getMySecurity).toHaveBeenCalledTimes(2));
  });

  it("does not offer to remove the only second factor, and says why", async () => {
    calls.getMySecurity.mockResolvedValue({ ...MY_SECURITY_FIXTURE, has_totp: false, passkeys: [MY_SECURITY_FIXTURE.passkeys[0]] });
    renderSecurityPage();
    const passkeys = await panel("Passkeys");
    expect(within(passkeys).getByRole("button", { name: "Remove the passkey Work laptop" })).toBeDisabled();
    expect(within(passkeys).getByText("This is your only second factor. Add another before you remove it.")).toBeInTheDocument();
  });

  it("sets up the app: the key and the link shown, the code confirmed, then the key gone", async () => {
    calls.getMySecurity.mockResolvedValue({ ...MY_SECURITY_FIXTURE, has_totp: false });
    const user = userEvent.setup();
    renderSecurityPage();
    const app = await panel("Authenticator app");
    expect(within(app).getByText("Not set up")).toBeInTheDocument();

    await user.click(within(app).getByRole("button", { name: "Set up the app" }));
    expect(await within(app).findByText("JBSW Y3DP EHPK 3PXP")).toBeInTheDocument();
    expect(calls.enrollTotpStart).toHaveBeenCalledExactlyOnceWith({ signedIn: true });
    expect(within(app).getByRole("link", { name: "Open in an authenticator app" })).toHaveAttribute("href", OTPAUTH_URI_FIXTURE);

    await user.type(within(app).getByLabelText("Code the app shows now"), "123 456");
    await user.click(within(app).getByRole("button", { name: "Turn it on" }));
    await waitFor(() =>
      expect(calls.enrollTotpConfirm).toHaveBeenCalledExactlyOnceWith("123456", { signedIn: true }),
    );
    await waitFor(() => expect(within(app).queryByText("JBSW Y3DP EHPK 3PXP")).not.toBeInTheDocument());
    expect(document.body.innerHTML).not.toContain("JBSWY3DPEHPK3PXP");
  });

  it("forgets the key when the set-up is cancelled", async () => {
    calls.getMySecurity.mockResolvedValue({ ...MY_SECURITY_FIXTURE, has_totp: false });
    const user = userEvent.setup();
    renderSecurityPage();
    const app = await panel("Authenticator app");
    await user.click(within(app).getByRole("button", { name: "Set up the app" }));
    await within(app).findByText("JBSW Y3DP EHPK 3PXP");

    await user.click(within(app).getByRole("button", { name: "Cancel" }));
    expect(document.body.innerHTML).not.toContain("JBSWY3DPEHPK3PXP");
    expect(calls.enrollTotpConfirm).not.toHaveBeenCalled();
  });

  it("removes the app after confirming", async () => {
    const user = userEvent.setup();
    renderSecurityPage();
    const app = await panel("Authenticator app");
    await user.click(within(app).getByRole("button", { name: "Remove the app" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove the authenticator app" });
    await user.click(within(dialog).getByRole("button", { name: "Remove the app" }));
    await waitFor(() => expect(calls.removeTotp).toHaveBeenCalledTimes(1));
  });

  it("makes new recovery codes, shows them once, and forgets them when the person says they have them", async () => {
    const user = userEvent.setup();
    renderSecurityPage();
    const codes = await panel("Recovery codes");
    await user.click(within(codes).getByRole("button", { name: "Make new codes" }));
    const dialog = await screen.findByRole("dialog", { name: "Make new recovery codes" });
    expect(calls.regenerateRecoveryCodes).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Make new codes" }));

    for (const code of RECOVERY_CODES_FIXTURE) expect(await within(codes).findByText(code)).toBeInTheDocument();
    expect(within(codes).getByText(/These codes are shown only now\./)).toBeInTheDocument();

    await user.click(within(codes).getByRole("button", { name: "I have saved them" }));
    for (const code of RECOVERY_CODES_FIXTURE) expect(document.body.textContent).not.toContain(code);
  });

  it("forgets a remembered device", async () => {
    const user = userEvent.setup();
    renderSecurityPage();
    await user.click(await screen.findByRole("button", { name: "Forget Safari on macOS" }));
    const dialog = await screen.findByRole("dialog", { name: "Forget device" });
    expect(within(dialog).getByText("Safari on macOS")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Forget device" }));
    await waitFor(() => expect(calls.revokeTrustedDevice).toHaveBeenCalledExactlyOnceWith("td-2"));
  });

  it("signs out another session", async () => {
    const user = userEvent.setup();
    renderSecurityPage();
    await user.click(await screen.findByRole("button", { name: "Sign out the session from 10.0.0.9" }));
    const dialog = await screen.findByRole("dialog", { name: "Sign out a session" });
    await user.click(within(dialog).getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(calls.endMySession).toHaveBeenCalledExactlyOnceWith("s-2"));
  });

  it("changes nothing when the recent confirmation is closed, and says so where the change was asked", async () => {
    calls.removePasskey.mockRejectedValue(stepUpRequired());
    const user = userEvent.setup();
    renderSecurityPage();
    const passkeys = await panel("Passkeys");
    await user.click(within(passkeys).getByRole("button", { name: "Remove the passkey Work laptop" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Remove passkey" })).getByRole("button", { name: "Remove passkey" }));
    const stepUp = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.click(within(stepUp).getByRole("button", { name: "Cancel" }));

    expect(await within(passkeys).findByRole("alert")).toHaveTextContent("Nothing was changed: the confirmation was closed.");
    expect(calls.removePasskey).toHaveBeenCalledTimes(1);
  });
});

/**
 * FIX ROUND (Task 12 review, Important 1). `reset()` only detaches the
 * mutation from the page: TanStack keeps the detached mutation -- its answer
 * and the body it was sent -- in the mutation cache until its `gcTime`, five
 * minutes by default, and a mutation never reset keeps them for as long as
 * the panel lives. So a value shown once is checked where it would linger,
 * the mutation cache, once the person has it.
 */
describe("what the mutation cache keeps of a value shown once", () => {
  it("keeps no authenticator key and no code once the app is on", async () => {
    calls.getMySecurity.mockResolvedValue({ ...MY_SECURITY_FIXTURE, has_totp: false });
    const user = userEvent.setup();
    const { queryClient } = renderSecurityPage();
    const app = await panel("Authenticator app");
    await user.click(within(app).getByRole("button", { name: "Set up the app" }));
    await within(app).findByText("JBSW Y3DP EHPK 3PXP");
    await user.type(within(app).getByLabelText("Code the app shows now"), "123 456");
    await user.click(within(app).getByRole("button", { name: "Turn it on" }));
    await waitFor(() => expect(within(app).queryByText("JBSW Y3DP EHPK 3PXP")).not.toBeInTheDocument());

    await waitFor(() => {
      const cached = mutationCacheText(queryClient);
      expect(cached).not.toContain("otpauth://");
      expect(cached).not.toContain("JBSWY3DPEHPK3PXP");
      expect(cached).not.toContain("123456");
    });
  });

  it("keeps no authenticator key once the set-up is cancelled", async () => {
    calls.getMySecurity.mockResolvedValue({ ...MY_SECURITY_FIXTURE, has_totp: false });
    const user = userEvent.setup();
    const { queryClient } = renderSecurityPage();
    const app = await panel("Authenticator app");
    await user.click(within(app).getByRole("button", { name: "Set up the app" }));
    await within(app).findByText("JBSW Y3DP EHPK 3PXP");
    await user.click(within(app).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(mutationCacheText(queryClient)).not.toContain("otpauth://"));
  });

  it("keeps no recovery code once the person says they have them", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderSecurityPage();
    const codes = await panel("Recovery codes");
    await user.click(within(codes).getByRole("button", { name: "Make new codes" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Make new recovery codes" })).getByRole("button", {
        name: "Make new codes",
      }),
    );
    await within(codes).findByText(RECOVERY_CODES_FIXTURE[0] as string);
    await user.click(within(codes).getByRole("button", { name: "I have saved them" }));

    await waitFor(() => {
      const cached = mutationCacheText(queryClient);
      for (const code of RECOVERY_CODES_FIXTURE) expect(cached).not.toContain(code);
    });
  });

  it("keeps neither password once the server changed it", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderSecurityPage();
    const password = await panel("Password");
    await typePassword(user, password);
    expect(await within(password).findByRole("status")).toHaveTextContent("Your password was changed.");

    await waitFor(() => {
      const cached = mutationCacheText(queryClient);
      expect(cached).not.toContain(PASSWORD.current);
      expect(cached).not.toContain(PASSWORD.new);
    });
    // The status line is the page's own record of the answer, not the mutation's.
    expect(within(password).getByRole("status")).toHaveTextContent("Your password was changed.");
  });

  it("keeps neither password once the server refused the change, and still says why", async () => {
    calls.changePassword.mockRejectedValue(
      new ApiError(403, JSON.stringify({ detail: "the current password is not right", code: "invalid_credentials" })),
    );
    const user = userEvent.setup();
    const { queryClient } = renderSecurityPage();
    const password = await panel("Password");
    await typePassword(user, password);
    expect(await within(password).findByRole("alert")).toHaveTextContent("the current password is not right");

    await waitFor(() => {
      const cached = mutationCacheText(queryClient);
      expect(cached).not.toContain(PASSWORD.current);
      expect(cached).not.toContain(PASSWORD.new);
    });
    expect(within(password).getByRole("alert")).toHaveTextContent("the current password is not right");
  });
});

/**
 * FIX ROUND (Task 12 review, Minor 3). A password change KEEPS THIS SESSION
 * (own-auth Task 7): the server ends the person's other sessions and forgets
 * their remembered devices, and this one stays signed in. So a 204 re-reads
 * the page and stays on it; nothing goes to sign-in.
 */
describe("after a password change", () => {
  it("re-reads your security, stays on this page, and says what the server ended", async () => {
    const user = userEvent.setup();
    renderSecurityPage();
    const password = await panel("Password");
    expect(calls.getMySecurity).toHaveBeenCalledTimes(1);
    await typePassword(user, password);

    expect(await within(password).findByRole("status")).toHaveTextContent(
      "Your password was changed. Your other sessions were signed out, and devices you had remembered ask for your second factor again.",
    );
    await waitFor(() => expect(calls.getMySecurity).toHaveBeenCalledTimes(2));
    expect(currentLocation.value?.pathname).toBe("/profile/security");
    expect(screen.getByRole("heading", { name: "Your security" })).toBeInTheDocument();
  });
});

describe("the key this page shows is Task 11's setupKeyOf", () => {
  // RE-POINTED from the page's own copy of this reading, now gone: the page
  // and the first-factor page read the key the same way. Same fixture, same
  // groups of four; a string that is no address is `null`, not "".
  it("reads the key out of the otpauth address, in groups of four", () => {
    expect(setupKeyOf(OTPAUTH_URI_FIXTURE)).toBe("JBSW Y3DP EHPK 3PXP");
    expect(setupKeyOf("not an address")).toBeNull();
  });
});
