/**
 * A contact's invitation (own-auth phase 2, Task 11, decision 2; design §3):
 * a password of 15 or more, or a passkey, and straight to the portal. A
 * technician's invitation is unchanged.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { navigation } from "../../../lib/session";
import { checkNewPassword } from "../errors";
import { authApi, calls, installDefaults, renderAuth, where } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));

const webauthn = vi.hoisted(() => ({ supported: true, startRegistration: vi.fn() }));
vi.mock("@simplewebauthn/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@simplewebauthn/browser")>()),
  browserSupportsWebAuthn: () => webauthn.supported,
  startRegistration: (options: unknown) => webauthn.startRegistration(options),
}));

const CONTACT_INVITE = {
  email: "ana@acme.test",
  display_name: "Ana Ruiz",
  kind: "contact",
  password_minimum: 15,
  passkey: true,
  microsoft: false,
};
const ATTESTATION = { id: "bmV3", rawId: "bmV3", type: "public-key", response: {}, clientExtensionResults: {} };
const GOOD = "Ultramarine-Kettle-7319-Harbor";
let replace: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  installDefaults();
  calls.getInvite.mockResolvedValue(CONTACT_INVITE);
  calls.acceptInvite.mockResolvedValue({ state: "signed_in" });
  webauthn.supported = true;
  webauthn.startRegistration.mockReset().mockResolvedValue(ATTESTATION);
  replace = vi.spyOn(navigation, "replace").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the new-password check", () => {
  it("takes the minimum of the person's kind", () => {
    expect(checkNewPassword("fourteen chars", "fourteen chars", 15)).toBe("Use at least 15 characters.");
    expect(checkNewPassword("fourteen chars", "fourteen chars")).toBeNull();
  });
});

describe("a contact's invitation", () => {
  it("asks for 15 characters, then lands on the portal", async () => {
    const user = userEvent.setup();
    renderAuth("/invite#tok-contact-1");
    expect(await screen.findByText("At least 15 characters, any you like, spaces included. A phrase you can remember works well.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("New password"), "fourteen chars");
    await user.type(screen.getByLabelText("The same password again"), "fourteen chars");
    await user.click(screen.getByRole("button", { name: "Set password and continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Use at least 15 characters.");
    expect(calls.acceptInvite).not.toHaveBeenCalled();
    await user.clear(screen.getByLabelText("New password"));
    await user.clear(screen.getByLabelText("The same password again"));
    await user.type(screen.getByLabelText("New password"), GOOD);
    await user.type(screen.getByLabelText("The same password again"), GOOD);
    await user.click(screen.getByRole("button", { name: "Set password and continue" }));
    expect(calls.acceptInvite).toHaveBeenCalledWith({ token: "tok-contact-1", password: GOOD });
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/portal"));
  });

  it("accepts with a passkey instead, named by the person", async () => {
    calls.invitePasskeyOptions.mockResolvedValue({ challenge: "cmVnaXN0ZXI", rp: { name: "ITMS" } });
    calls.invitePasskey.mockResolvedValue({ state: "signed_in" });
    const user = userEvent.setup();
    renderAuth("/invite#tok-contact-1");
    await user.click(await screen.findByRole("button", { name: "Use a passkey instead" }));
    expect(calls.invitePasskeyOptions).toHaveBeenCalledWith("tok-contact-1");
    await waitFor(() =>
      expect(calls.invitePasskey).toHaveBeenCalledWith({
        token: "tok-contact-1",
        credential: ATTESTATION,
        name: "My passkey",
      }),
    );
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/portal"));
  });

  it("does not offer a passkey on a browser that cannot make one", async () => {
    webauthn.supported = false;
    renderAuth("/invite#tok-contact-1");
    await screen.findByLabelText("New password");
    expect(screen.queryByRole("button", { name: "Use a passkey instead" })).not.toBeInTheDocument();
  });
});

describe("a technician's invitation", () => {
  it("still goes on to the second factor", async () => {
    calls.getInvite.mockResolvedValue({ ...CONTACT_INVITE, kind: "staff", password_minimum: 12, passkey: false });
    calls.acceptInvite.mockResolvedValue({ state: "enroll_required" });
    const user = userEvent.setup();
    renderAuth("/invite#tok-staff-1");
    await user.type(await screen.findByLabelText("New password"), GOOD);
    await user.type(screen.getByLabelText("The same password again"), GOOD);
    await user.click(screen.getByRole("button", { name: "Set password and continue" }));
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/enroll"));
    expect(screen.queryByRole("button", { name: "Use a passkey instead" })).not.toBeInTheDocument();
  });
});
