/**
 * Review follow-ups to the sign-in pages (own-auth Task 11):
 *
 *   - a factor accepted but `enroll/finish` failing is retried for the codes
 *     alone (the server refuses the factor steps again on a pending session);
 *   - a refused passkey reads as a passkey, and a browser/WebAuthn error is
 *     always OUR sentence in the active language, never the library's text;
 *   - a passkey-only account on a browser without WebAuthn is told so;
 *   - `method_unavailable` and `sign_in_unavailable` have their sentences;
 *   - a link's token is gone from the address, the router state and the DOM.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../../i18n/core";
import { expectNoEnglish } from "../../../i18n/test-utils";
import { ApiError } from "../../../lib/api";
import { navigation } from "../../../lib/session";
import { forgetCodes } from "../flow";
import { TEN_CODES, authApi, calls, installDefaults, renderAuth, where } from "./harness";

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

beforeEach(() => {
  installDefaults();
  forgetCodes();
  setActiveLanguage("en");
  webauthn.supported = true;
  webauthn.startRegistration.mockReset().mockResolvedValue(ATTESTATION);
  webauthn.startAuthentication.mockReset();
  vi.spyOn(navigation, "replace").mockImplementation(() => {});
});

afterEach(() => {
  setActiveLanguage("en");
  vi.restoreAllMocks();
});

function libraryError(code: string, message = "LIBRARY ENGLISH TEXT") {
  return new WebAuthnError({ message, code: code as never, cause: new Error("x") });
}

describe("a factor accepted but the recovery codes not made", () => {
  async function totpThenFailFinish() {
    calls.enrollFinish.mockRejectedValueOnce(new ApiError(500, ""));
    const user = userEvent.setup();
    renderAuth("/sign-in/enroll");
    await user.click(screen.getByRole("button", { name: "Set up an authenticator app" }));
    await user.type(await screen.findByLabelText("Six-digit code the app now shows"), "123456");
    await user.click(screen.getByRole("button", { name: "Confirm code" }));
    return user;
  }

  it("offers to try again for the codes alone, never the factor steps", async () => {
    const user = await totpThenFailFinish();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByLabelText("Six-digit code the app now shows")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create a passkey" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/recovery-codes"));
    for (const code of TEN_CODES) expect(screen.getByText(code)).toBeInTheDocument();
    expect(calls.enrollTotpConfirm).toHaveBeenCalledTimes(1);
    expect(calls.enrollFinish).toHaveBeenCalledTimes(2);
  });

  it("does the same after a passkey", async () => {
    calls.enrollFinish.mockRejectedValueOnce(new ApiError(503, ""));
    const user = userEvent.setup();
    renderAuth("/sign-in/enroll");
    await user.click(screen.getByRole("button", { name: "Create a passkey" }));
    await user.click(await screen.findByRole("button", { name: "Try again" }));
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/recovery-codes"));
    expect(calls.enrollPasskey).toHaveBeenCalledTimes(1);
  });

  // FINAL REVIEW D1, MINOR 5: three refusals of `enroll/finish` never change
  // on a retry, and the card offered only "Try again".
  async function passkeyThenFinish(refusal: ApiError) {
    calls.enrollFinish.mockRejectedValue(refusal);
    const user = userEvent.setup();
    renderAuth("/sign-in/enroll?next=%2Fdevices");
    await user.click(screen.getByRole("button", { name: "Create a passkey" }));
    await screen.findByRole("alert");
    return user;
  }

  it("always offers a way back to the sign-in page", async () => {
    await passkeyThenFinish(new ApiError(503, ""));
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Start again" })).toHaveAttribute("href", "/sign-in?next=%2Fdevices");
  });

  it("409 already_signed_in: the first answer was lost after success, so it offers the console, not a retry", async () => {
    const user = await passkeyThenFinish(
      new ApiError(409, JSON.stringify({ code: "already_signed_in", detail: "you are already signed in" })),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "You are already signed in: the first answer was lost on its way. Your recovery codes were made but cannot be shown again; make new ones in Your security.",
    );
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Continue to the console" }));
    expect(navigation.replace).toHaveBeenCalledExactlyOnceWith("/devices");
    expect(calls.enrollFinish).toHaveBeenCalledTimes(1);
  });

  it("409 enroll_required: the time to finish ran out, so it offers only to start again", async () => {
    await passkeyThenFinish(
      new ApiError(409, JSON.stringify({ code: "enroll_required", detail: "set up a passkey or an authenticator app first" })),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/The time to finish this step has run out\. Start again from the sign-in page/);
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Start again" })).toHaveAttribute("href", "/sign-in?next=%2Fdevices");
  });

  it("401 auth_required: the sign-in timed out, so it offers only to start again", async () => {
    await passkeyThenFinish(new ApiError(401, JSON.stringify({ code: "auth_required", detail: "sign in to do this" })));
    expect(screen.getByRole("alert")).toHaveTextContent("This sign-in has timed out. Start again from the sign-in page.");
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Start again" })).toBeInTheDocument();
  });

  it("says the dead ends in Spanish", async () => {
    setActiveLanguage("es");
    calls.enrollFinish.mockRejectedValue(
      new ApiError(409, JSON.stringify({ code: "already_signed_in", detail: "ya iniciaste sesión" })),
    );
    const user = userEvent.setup();
    const { container } = renderAuth("/sign-in/enroll");
    await user.click(screen.getByRole("button", { name: "Crear una clave de acceso" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Ya iniciaste sesión/);
    expect(screen.getByRole("button", { name: "Continuar a la consola" })).toBeInTheDocument();
    expectNoEnglish(container);
  });
});

describe("a passkey the server refused", () => {
  it("reads as a passkey, not as a code", async () => {
    calls.enrollPasskey.mockRejectedValue(new ApiError(400, '{"code":"passkey_rejected"}'));
    const user = userEvent.setup();
    renderAuth("/sign-in/enroll");
    await user.click(screen.getByRole("button", { name: "Create a passkey" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("That passkey was not accepted.");
    expect(alert).not.toHaveTextContent("code");
  });
});

describe.each([
  ["en", "Use another passkey or an authenticator app.", "The passkey could not be used on this device.", "Another passkey request replaced this one.", "This device already holds a passkey for your account."],
  ["es", "Usa otra clave de acceso o una aplicación de autenticación.", "No se pudo usar la clave de acceso en este dispositivo.", "Otra solicitud de clave de acceso reemplazó a esta.", "Este dispositivo ya tiene una clave de acceso para tu cuenta."],
] as const)("WebAuthn errors in %s", (language, noVerification, generic, aborted, already) => {
  beforeEach(() => setActiveLanguage(language));

  const button = language === "en" ? "Create a passkey" : "Crear una clave de acceso";

  it.each([
    ["ERROR_AUTHENTICATOR_MISSING_USER_VERIFICATION_SUPPORT", () => noVerification],
    ["ERROR_CEREMONY_ABORTED", () => aborted],
    ["ERROR_AUTHENTICATOR_PREVIOUSLY_REGISTERED", () => already],
    ["ERROR_AUTHENTICATOR_GENERAL_ERROR", () => generic],
    ["ERROR_INVALID_RP_ID", () => generic],
  ])("%s says our sentence and never the library's", async (code, sentence) => {
    webauthn.startRegistration.mockRejectedValue(libraryError(code));
    const user = userEvent.setup();
    const { container } = renderAuth("/sign-in/enroll");
    await user.click(screen.getByRole("button", { name: button }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(sentence());
    expect(alert).not.toHaveTextContent("LIBRARY ENGLISH TEXT");
    if (language === "es") expectNoEnglish(container);
  });

  it("says the same on the second-factor page", async () => {
    webauthn.startAuthentication.mockRejectedValue(libraryError("ERROR_AUTHENTICATOR_GENERAL_ERROR"));
    const user = userEvent.setup();
    renderAuth("/sign-in/factor", { methods: ["passkey"], rememberDays: null });
    await user.click(screen.getByRole("button", { name: language === "en" ? "Use a passkey" : "Usar una clave de acceso" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(generic);
  });
});

describe("a passkey-only account on a browser without passkeys", () => {
  it("is told so instead of being left with only 'Start again'", () => {
    webauthn.supported = false;
    renderAuth("/sign-in/factor", { methods: ["passkey"], rememberDays: null });
    expect(screen.getByText("This browser cannot use passkeys. Use an authenticator app instead.")).toBeInTheDocument();
  });
});

describe("the server's other answers on the sign-in pages", () => {
  it("a method the account lacks (409 method_unavailable) shows the server's sentence", async () => {
    calls.factorTotp.mockRejectedValue(
      new ApiError(409, '{"code":"method_unavailable","detail":"This account has no authenticator app."}'),
    );
    const user = userEvent.setup();
    renderAuth("/sign-in/factor");
    await user.type(screen.getByLabelText("Six-digit code from your authenticator app"), "123456");
    await user.click(screen.getByRole("button", { name: "Verify code" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This account has no authenticator app.");
  });

  it("a server that cannot sign anyone in (503) says the service is unavailable", async () => {
    calls.login.mockRejectedValue(new ApiError(503, '{"code":"sign_in_unavailable"}'));
    const user = userEvent.setup();
    renderAuth("/sign-in");
    await user.type(screen.getByLabelText("Email address"), "dana@example.test");
    await user.type(screen.getByLabelText("Password"), "correct horse battery");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That service is unavailable right now.");
  });

  it("the same 503 on the second factor", async () => {
    calls.factorTotp.mockRejectedValue(new ApiError(503, '{"code":"sign_in_unavailable"}'));
    const user = userEvent.setup();
    renderAuth("/sign-in/factor");
    await user.type(screen.getByLabelText("Six-digit code from your authenticator app"), "123456");
    await user.click(screen.getByRole("button", { name: "Verify code" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That service is unavailable right now.");
  });
});

describe("one-time links", () => {
  it.each([
    ["/invite#tok-secret-1"],
    ["/reset-password#tok-secret-1"],
    ["/recover#tok-secret-1"],
  ])("%s: the token is gone from address, state and page", async (path) => {
    const { container } = renderAuth(path);
    await waitFor(() => expect(where.location?.hash).toBe(""));
    await screen.findAllByRole("heading");
    expect(where.location?.search).toBe("");
    expect(where.location?.state ?? null).toBeNull();
    expect(container.innerHTML).not.toContain("tok-secret-1");
    expect(document.body.textContent).not.toContain("tok-secret-1");
  });

  it("an invitation that died at ACCEPT shows the dead-link state", async () => {
    calls.acceptInvite.mockRejectedValue(new ApiError(410, '{"code":"invalid_link"}'));
    const user = userEvent.setup();
    renderAuth("/invite#tok");
    await user.type(await screen.findByLabelText("New password"), "correct horse battery");
    await user.type(screen.getByLabelText("The same password again"), "correct horse battery");
    await user.click(screen.getByRole("button", { name: "Set password and continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/This invitation has expired, was already used, or was cancelled/);
    expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
  });

  it("the reset form carries a read-only username field for password managers", () => {
    const { container } = renderAuth("/reset-password#tok");
    const username = container.querySelector('input[autocomplete="username"]') as HTMLInputElement;
    expect(username).not.toBeNull();
    expect(username.readOnly).toBe(true);
    expect(username.value).toBe("");
  });
});
