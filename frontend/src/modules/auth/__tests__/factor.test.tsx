/**
 * `/sign-in/factor` (own-auth design §2.2, §2.3, §2.6).
 *
 * The passkey goes through `@simplewebauthn/browser`, mocked at its own
 * boundary so `../passkey.ts` runs for real: the options the server sent
 * reach `startAuthentication` untouched, and its answer reaches the server
 * untouched -- the console adds nothing to a ceremony the server verifies.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { navigation } from "../../../lib/session";
import { authApi, calls, installDefaults, renderAuth } from "./harness";

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

const ASSERTION = { id: "Y3JlZA", rawId: "Y3JlZA", type: "public-key", response: {}, clientExtensionResults: {} };
const EVERY = { methods: ["passkey", "totp", "recovery"], rememberDays: 15 };

let replace: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  installDefaults();
  webauthn.supported = true;
  webauthn.startAuthentication.mockReset().mockResolvedValue(ASSERTION);
  replace = vi.spyOn(navigation, "replace").mockImplementation(() => {});
});

// A spy on a property that is already one is the same spy, calls and all:
// restore it, or one test's navigation answers the next test's assertion.
afterEach(() => {
  vi.restoreAllMocks();
});

describe("the second factor", () => {
  it("offers the passkey first, then the app, with the recovery code folded away", () => {
    renderAuth("/sign-in/factor", EVERY);
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual(["Passkey", "Authenticator app"]);
    expect(screen.getByRole("button", { name: "Use a recovery code instead" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Recovery code")).not.toBeInTheDocument();
  });

  it("passes the server's options to the browser untouched, and the browser's answer back", async () => {
    const options = { challenge: "c2lnbi1pbg", userVerification: "required", allowCredentials: [] };
    calls.factorPasskeyOptions.mockResolvedValue(options);
    const user = userEvent.setup();
    renderAuth("/sign-in/factor?next=%2Fdevices", EVERY);

    await user.click(screen.getByRole("button", { name: "Use a passkey" }));

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/devices"));
    expect(webauthn.startAuthentication).toHaveBeenCalledWith({ optionsJSON: options });
    expect(calls.factorPasskey).toHaveBeenCalledWith({ credential: ASSERTION, remember_device: false });
  });

  it("sends the app's code without its spaces, and the remember choice when ticked", async () => {
    const user = userEvent.setup();
    renderAuth("/sign-in/factor", EVERY);
    await user.click(screen.getByRole("checkbox", { name: "Do not ask again on this computer for 15 days" }));
    await user.type(screen.getByLabelText("Six-digit code from your authenticator app"), "123 456");
    await user.click(screen.getByRole("button", { name: "Verify code" }));

    await waitFor(() => expect(calls.factorTotp).toHaveBeenCalledWith({ code: "123456", remember_device: true }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
  });

  it("offers no remember box when the sign-in step did not say how long, and never ticks it", () => {
    renderAuth("/sign-in/factor", { methods: ["totp"], rememberDays: null });
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    renderAuth("/sign-in/factor", EVERY);
    expect(screen.getByRole("checkbox")).not.toBeChecked();
  });

  it("puts a recovery code in the form it was shown, however it was typed", async () => {
    const user = userEvent.setup();
    renderAuth("/sign-in/factor", EVERY);
    await user.click(screen.getByRole("button", { name: "Use a recovery code instead" }));
    await user.type(screen.getByLabelText("Recovery code"), "7k3qd 9xm2p");
    await user.click(screen.getByRole("button", { name: "Use recovery code" }));
    await waitFor(() =>
      expect(calls.factorRecovery).toHaveBeenCalledWith({ code: "7K3QD-9XM2P", remember_device: false }),
    );
  });

  it("offers only what the account has, and no passkey where the browser cannot use one", () => {
    webauthn.supported = false;
    renderAuth("/sign-in/factor", EVERY);
    expect(screen.queryByRole("button", { name: "Use a passkey" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Six-digit code from your authenticator app")).toBeInTheDocument();
  });

  it("offers every factor on a page opened directly, which the server then decides", () => {
    renderAuth("/sign-in/factor");
    expect(screen.getByRole("button", { name: "Use a passkey" })).toBeInTheDocument();
    expect(screen.getByLabelText("Six-digit code from your authenticator app")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("says one sentence for a refused code, and clears it", async () => {
    calls.factorTotp.mockRejectedValue(new ApiError(401, '{"code":"factor_failed"}'));
    const user = userEvent.setup();
    renderAuth("/sign-in/factor", EVERY);
    await user.type(screen.getByLabelText("Six-digit code from your authenticator app"), "000000");
    await user.click(screen.getByRole("button", { name: "Verify code" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That was not accepted. Try again.");
    expect(screen.getByLabelText("Six-digit code from your authenticator app")).toHaveValue("");
    expect(replace).not.toHaveBeenCalled();
  });

  it("says the sign-in timed out when its session has gone, and offers to start again", async () => {
    calls.factorTotp.mockRejectedValue(new ApiError(401, '{"code":"auth_required"}'));
    const user = userEvent.setup();
    renderAuth("/sign-in/factor?next=%2Fdevices", EVERY);
    await user.type(screen.getByLabelText("Six-digit code from your authenticator app"), "123456");
    await user.click(screen.getByRole("button", { name: "Verify code" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This sign-in has timed out.");
    expect(screen.getByRole("link", { name: "Start again" })).toHaveAttribute("href", "/sign-in?next=%2Fdevices");
  });

  it("says a closed passkey prompt for what it is, and sends nothing", async () => {
    webauthn.startAuthentication.mockRejectedValue(
      new WebAuthnError({
        message: "The operation either timed out or was not allowed.",
        code: "ERROR_PASSTHROUGH_SEE_CAUSE_PROPERTY",
        cause: Object.assign(new Error("not allowed"), { name: "NotAllowedError" }),
      }),
    );
    const user = userEvent.setup();
    renderAuth("/sign-in/factor", EVERY);
    await user.click(screen.getByRole("button", { name: "Use a passkey" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The passkey prompt was closed or timed out");
    expect(calls.factorPasskey).not.toHaveBeenCalled();
  });
});
