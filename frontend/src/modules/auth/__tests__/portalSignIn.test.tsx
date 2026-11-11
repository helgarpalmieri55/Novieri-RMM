/**
 * `/portal/sign-in` (own-auth phase 2, Task 10; design §3): the same
 * `POST /auth/login` as the console's page, and a passkey alone (D28). Lands
 * on the portal unless `next` says a portal page; every refusal is one
 * sentence.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { navigation } from "../../../lib/session";
import { authApi, calls, installDefaults, renderAuth, where } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));

const webauthn = vi.hoisted(() => ({ supported: true, startAuthentication: vi.fn() }));
vi.mock("@simplewebauthn/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@simplewebauthn/browser")>()),
  browserSupportsWebAuthn: () => webauthn.supported,
  startAuthentication: (options: unknown) => webauthn.startAuthentication(options),
}));

const ASSERTION = { id: "Y3JlZA", rawId: "Y3JlZA", type: "public-key", response: {}, clientExtensionResults: {} };
let replace: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  installDefaults();
  webauthn.supported = true;
  webauthn.startAuthentication.mockReset().mockResolvedValue(ASSERTION);
  replace = vi.spyOn(navigation, "replace").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("signing in to the portal", () => {
  it("with a password and no second factor, lands on the portal", async () => {
    calls.login.mockResolvedValue({ state: "signed_in" });
    const user = userEvent.setup();
    renderAuth("/portal/sign-in");
    await user.type(screen.getByLabelText("Email address"), "ana@acme.test");
    await user.type(screen.getByLabelText("Password"), "Ultramarine-Kettle-7319-Harbor");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(calls.login).toHaveBeenCalledWith({ email: "ana@acme.test", password: "Ultramarine-Kettle-7319-Harbor" });
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/portal"));
  });

  it("with a password and a second factor, goes to the factor step carrying the portal", async () => {
    calls.login.mockResolvedValue({ state: "factor_required", methods: ["totp"], remember_device_days: 15 });
    const user = userEvent.setup();
    renderAuth("/portal/sign-in");
    await user.type(screen.getByLabelText("Email address"), "ana@acme.test");
    await user.type(screen.getByLabelText("Password"), "Ultramarine-Kettle-7319-Harbor");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/factor"));
    expect(where.location?.search).toBe("?next=%2Fportal");
  });

  it("with a passkey alone, sends the browser's assertion and lands on the portal", async () => {
    const user = userEvent.setup();
    renderAuth("/portal/sign-in");
    await user.click(screen.getByRole("button", { name: "Sign in with a passkey" }));
    expect(calls.passkeySignInOptions).toHaveBeenCalledOnce();
    await waitFor(() => expect(calls.passkeySignIn).toHaveBeenCalledWith(ASSERTION));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/portal"));
  });

  it("says one sentence when the passkey is refused", async () => {
    calls.passkeySignIn.mockRejectedValue(new ApiError(401, '{"code":"passkey_rejected"}'));
    const user = userEvent.setup();
    renderAuth("/portal/sign-in");
    await user.click(screen.getByRole("button", { name: "Sign in with a passkey" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That passkey was not accepted. Try again, or use your email address and password.",
    );
  });

  it("does not offer a passkey on a browser that cannot use one", () => {
    webauthn.supported = false;
    renderAuth("/portal/sign-in");
    expect(screen.queryByRole("button", { name: "Sign in with a passkey" })).not.toBeInTheDocument();
  });
});
