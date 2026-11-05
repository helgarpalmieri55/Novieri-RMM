/**
 * `/sign-in/enroll` and `/sign-in/recovery-codes` (own-auth design §2.2).
 *
 * THE RECOVERY CODES NEVER TOUCH THE URL OR THE ROUTER'S STATE. Router state
 * is `history.state`, which a browser keeps with the tab's history and may
 * write to disk; the codes are handed over in memory (`../flow.ts`). The test
 * that asserts it reads the location the codes page was reached with.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { navigation } from "../../../lib/session";
import { forgetCodes, handOverCodes } from "../flow";
import { OTPAUTH, TEN_CODES, authApi, calls, installDefaults, renderAuth, where } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));

const webauthn = vi.hoisted(() => ({
  supported: true,
  startAuthentication: vi.fn(),
  startRegistration: vi.fn(),
}));

vi.mock("@simplewebauthn/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@simplewebauthn/browser")>()),
  browserSupportsWebAuthn: () => webauthn.supported,
  startAuthentication: (options: unknown) => webauthn.startAuthentication(options),
  startRegistration: (options: unknown) => webauthn.startRegistration(options),
}));

const { WebAuthnError } = await import("@simplewebauthn/browser");

const ATTESTATION = { id: "bmV3", rawId: "bmV3", type: "public-key", response: {}, clientExtensionResults: {} };

let replace: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  installDefaults();
  forgetCodes();
  webauthn.supported = true;
  webauthn.startRegistration.mockReset().mockResolvedValue(ATTESTATION);
  replace = vi.spyOn(navigation, "replace").mockImplementation(() => {});
});

// A spy on a property that is already one is the same spy, calls and all:
// restore it, or one test's navigation answers the next test's assertion.
afterEach(() => {
  vi.restoreAllMocks();
});

describe("adding the first second factor", () => {
  it("makes a passkey from the server's options, names it, and shows the recovery codes", async () => {
    const options = { challenge: "cmVnaXN0ZXI", rp: { name: "ITMS" }, excludeCredentials: [] };
    calls.enrollPasskeyOptions.mockResolvedValue(options);
    const user = userEvent.setup();
    renderAuth("/sign-in/enroll?next=%2Fdevices");

    const name = screen.getByLabelText("Name for this passkey");
    expect(name).toHaveValue("My passkey");
    await user.clear(name);
    await user.type(name, "Work laptop");
    await user.click(screen.getByRole("button", { name: "Create a passkey" }));

    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/recovery-codes"));
    expect(webauthn.startRegistration).toHaveBeenCalledWith({ optionsJSON: options });
    expect(calls.enrollPasskey).toHaveBeenCalledWith(ATTESTATION, "Work laptop");
    expect(calls.enrollFinish).toHaveBeenCalledOnce();
    expect(where.location?.search).toBe("?next=%2Fdevices");
    // NOT in the URL, NOT in history.state.
    expect(where.location?.state ?? null).toBeNull();
    expect(JSON.stringify(where.location)).not.toContain(TEN_CODES[0]);
    for (const code of TEN_CODES) expect(screen.getByText(code)).toBeInTheDocument();
  });

  it("sets up an authenticator app from its setup key, and confirms it with a code", async () => {
    const user = userEvent.setup();
    renderAuth("/sign-in/enroll");
    await user.click(screen.getByRole("button", { name: "Set up an authenticator app" }));

    expect(await screen.findByText("JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open in an authenticator app" })).toHaveAttribute("href", OTPAUTH);
    await user.type(screen.getByLabelText("Six-digit code the app now shows"), "654 321");
    await user.click(screen.getByRole("button", { name: "Confirm code" }));

    await waitFor(() => expect(calls.enrollTotpConfirm).toHaveBeenCalledWith("654321"));
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/recovery-codes"));
  });

  it("says a refused code in one sentence, clears it, and keeps the key on screen", async () => {
    calls.enrollTotpConfirm.mockRejectedValue(new ApiError(400, '{"code":"factor_failed"}'));
    const user = userEvent.setup();
    renderAuth("/sign-in/enroll");
    await user.click(screen.getByRole("button", { name: "Set up an authenticator app" }));
    await user.type(await screen.findByLabelText("Six-digit code the app now shows"), "111111");
    await user.click(screen.getByRole("button", { name: "Confirm code" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/That code was not accepted/);
    expect(screen.getByLabelText("Six-digit code the app now shows")).toHaveValue("");
    expect(calls.enrollFinish).not.toHaveBeenCalled();
  });

  it("says when this device already holds a passkey for the account", async () => {
    webauthn.startRegistration.mockRejectedValue(
      new WebAuthnError({
        message: "The authenticator was previously registered",
        code: "ERROR_AUTHENTICATOR_PREVIOUSLY_REGISTERED",
        cause: Object.assign(new Error("invalid state"), { name: "InvalidStateError" }),
      }),
    );
    const user = userEvent.setup();
    renderAuth("/sign-in/enroll");
    await user.click(screen.getByRole("button", { name: "Create a passkey" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This device already holds a passkey for your account.");
    expect(calls.enrollPasskey).not.toHaveBeenCalled();
  });

  it("offers the app alone, and says why, where the browser cannot make a passkey", () => {
    webauthn.supported = false;
    renderAuth("/sign-in/enroll");
    expect(screen.queryByRole("button", { name: "Create a passkey" })).not.toBeInTheDocument();
    expect(screen.getByText(/This browser cannot use passkeys/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set up an authenticator app" })).toBeInTheDocument();
  });
});

describe("the recovery codes", () => {
  it("open the console only once the person says they saved them", async () => {
    handOverCodes(TEN_CODES);
    const user = userEvent.setup();
    renderAuth("/sign-in/recovery-codes?next=%2Fdevices");
    const list = screen.getByRole("list", { name: "Your recovery codes" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(10);

    const go = screen.getByRole("button", { name: "Continue to the console" });
    expect(go).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: "I have saved these codes somewhere safe" }));
    await user.click(go);
    expect(replace).toHaveBeenCalledWith("/devices");
  });

  it("copies them, and says so; and says so when the browser refuses", async () => {
    handOverCodes(TEN_CODES);
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    renderAuth("/sign-in/recovery-codes");
    await user.click(screen.getByRole("button", { name: "Copy the codes" }));
    expect(writeText).toHaveBeenCalledWith(TEN_CODES.join("\n"));
    expect(await screen.findByRole("status")).toHaveTextContent("The codes are on the clipboard.");

    writeText.mockRejectedValue(new Error("denied"));
    await user.click(screen.getByRole("button", { name: "Copy the codes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/would not copy them/);
  });

  it("are not there to show again on a page opened directly", () => {
    renderAuth("/sign-in/recovery-codes");
    expect(screen.getByText(/There are no codes to show/)).toBeInTheDocument();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });

  it("are dropped from memory once their page is gone", () => {
    handOverCodes(TEN_CODES);
    const { unmount } = renderAuth("/sign-in/recovery-codes");
    unmount();
    renderAuth("/sign-in/recovery-codes");
    expect(screen.getByText(/There are no codes to show/)).toBeInTheDocument();
  });
});
