/**
 * Adding a passkey after the recent confirmation (Task 12 review, Important 2).
 *
 * A BROWSER CREATES A PASSKEY ONLY FROM A CLICK. WebKit, "Meet Face ID and
 * Touch ID for the Web" (https://webkit.org/blog/11312/meet-face-id-and-touch-id-for-the-web/):
 * "Face ID and Touch ID for the web require user gestures to function", and
 * "Normally, the user gesture will not be propagated to any async executors
 * within the context" -- only XHR and the Fetch API carry it, for 10 seconds.
 * When "Add a passkey" first needs the second factor confirmed, the click has
 * been spent on the confirmation dialog -- a passkey there waits on Touch ID,
 * which is no fetch -- so a `navigator.credentials.create()` sent from the
 * confirmation's continuation would be refused, and the page would say the
 * person closed a prompt they never saw.
 *
 * WHAT IS PINNED. After a confirmation that needed the dialog, `createPasskey`
 * is NOT called; the page asks for one more click ("Create the passkey"), and
 * `createPasskey` runs once, from that click, on options that click fetched.
 * Without a confirmation the one click still does everything. A refusal after
 * the confirmation is said as what it is: not created, try again.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import {
  CREATION_OPTIONS_FIXTURE,
  apiClient,
  calls,
  installDefaults,
  renderSecurityPage,
  stepUpRequired,
} from "../settings/__tests__/harness";

const { createPasskey, signWithPasskey } = vi.hoisted(() => ({
  createPasskey: vi.fn(),
  signWithPasskey: vi.fn(),
}));
// The real module but for the two ceremonies, spied: `describePasskeyError`
// stays Task 11's own.
vi.mock("../auth/passkey", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../auth/passkey")>()),
  createPasskey,
  signWithPasskey,
}));
vi.mock("../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

const ATTESTATION = {
  id: "bmV3",
  rawId: "bmV3",
  response: { clientDataJSON: "e30", attestationObject: "YXR0" },
  clientExtensionResults: {},
  type: "public-key",
};
const ASSERTION = {
  id: "Y3JlZA",
  rawId: "Y3JlZA",
  response: { clientDataJSON: "e30", authenticatorData: "YXV0aA", signature: "c2ln" },
  clientExtensionResults: {},
  type: "public-key",
};
/** The options the confirmation's re-send fetched, and the ones the new click fetched: told apart by their challenge. */
const AFTER_CONFIRMATION = { ...CREATION_OPTIONS_FIXTURE, challenge: "YWZ0ZXItY29uZmlybWF0aW9u" };
const IN_THE_CLICK = { ...CREATION_OPTIONS_FIXTURE, challenge: "aW4tdGhlLWNsaWNr" };

beforeEach(() => {
  installDefaults();
  setActiveLanguage("en");
  createPasskey.mockReset();
  signWithPasskey.mockReset();
  createPasskey.mockResolvedValue(ATTESTATION);
  signWithPasskey.mockResolvedValue(ASSERTION);
});

async function passkeysPanel(): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { name: "Passkeys" });
  return heading.closest("section") as HTMLElement;
}

async function askToAdd(user: ReturnType<typeof userEvent.setup>, panel: HTMLElement) {
  await user.type(within(panel).getByLabelText("Name for the new passkey"), "Desk key");
  await user.click(within(panel).getByRole("button", { name: "Add a passkey" }));
}

describe("adding a passkey after the recent confirmation", () => {
  it("after a passkey confirmation, creates nothing until the new button, then once, from that click", async () => {
    calls.enrollPasskeyOptions
      .mockRejectedValueOnce(stepUpRequired())
      .mockResolvedValueOnce(AFTER_CONFIRMATION)
      .mockResolvedValueOnce(IN_THE_CLICK);
    const user = userEvent.setup();
    renderSecurityPage();
    const panel = await passkeysPanel();
    await askToAdd(user, panel);

    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.click(await within(dialog).findByRole("button", { name: "Use a passkey" }));
    await waitFor(() => expect(calls.stepUpPasskey).toHaveBeenCalledTimes(1));

    const createNow = await within(panel).findByRole("button", { name: "Create the passkey" });
    expect(within(panel).getByText("Your second factor is confirmed. Press “Create the passkey” to make it.")).toBeInTheDocument();
    // The confirmation's continuation made no passkey and sent none.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(createPasskey).not.toHaveBeenCalled();
    expect(calls.enrollPasskey).not.toHaveBeenCalled();

    await user.click(createNow);
    await waitFor(() => expect(createPasskey).toHaveBeenCalledTimes(1));
    // On the options THIS click fetched: the click is what the browser counts.
    expect(createPasskey).toHaveBeenCalledWith(IN_THE_CLICK);
    expect(calls.enrollPasskeyOptions).toHaveBeenCalledTimes(3);
    await waitFor(() => expect(calls.enrollPasskey).toHaveBeenCalledExactlyOnceWith(ATTESTATION, "Desk key", { signedIn: true }));
    await waitFor(() => expect(within(panel).queryByRole("button", { name: "Create the passkey" })).not.toBeInTheDocument());
    expect(createPasskey).toHaveBeenCalledTimes(1);
  });

  it("after a code from the app, the same: one more click, and the passkey from it", async () => {
    calls.enrollPasskeyOptions
      .mockRejectedValueOnce(stepUpRequired())
      .mockResolvedValueOnce(AFTER_CONFIRMATION)
      .mockResolvedValueOnce(IN_THE_CLICK);
    const user = userEvent.setup();
    renderSecurityPage();
    const panel = await passkeysPanel();
    await askToAdd(user, panel);

    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(await within(dialog).findByLabelText("Code from your authenticator app"), "123456");
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));

    const createNow = await within(panel).findByRole("button", { name: "Create the passkey" });
    expect(createPasskey).not.toHaveBeenCalled();
    await user.click(createNow);
    await waitFor(() => expect(createPasskey).toHaveBeenCalledExactlyOnceWith(IN_THE_CLICK));
  });

  it("without a confirmation, the one click still makes the passkey", async () => {
    const user = userEvent.setup();
    renderSecurityPage();
    const panel = await passkeysPanel();
    await askToAdd(user, panel);

    await waitFor(() => expect(createPasskey).toHaveBeenCalledExactlyOnceWith(CREATION_OPTIONS_FIXTURE));
    await waitFor(() => expect(calls.enrollPasskey).toHaveBeenCalledExactlyOnceWith(ATTESTATION, "Desk key", { signedIn: true }));
    expect(calls.enrollPasskeyOptions).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "Create the passkey" })).not.toBeInTheDocument();
  });

  it("says a refusal after the confirmation as not created, to try again -- not as a prompt the person closed", async () => {
    calls.enrollPasskeyOptions
      .mockRejectedValueOnce(stepUpRequired())
      .mockResolvedValueOnce(AFTER_CONFIRMATION)
      .mockResolvedValueOnce(IN_THE_CLICK);
    createPasskey.mockRejectedValueOnce(
      Object.assign(new Error("The operation either timed out or was not allowed."), { name: "NotAllowedError" }),
    );
    const user = userEvent.setup();
    renderSecurityPage();
    const panel = await passkeysPanel();
    await askToAdd(user, panel);
    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.click(await within(dialog).findByRole("button", { name: "Use a passkey" }));
    await user.click(await within(panel).findByRole("button", { name: "Create the passkey" }));

    const alert = await within(panel).findByRole("alert");
    expect(alert).toHaveTextContent("The passkey was not created. Press “Create the passkey” to try again.");
    expect(alert).not.toHaveTextContent(/closed or timed out/);
    // Still offered: the next click is the one that can work.
    expect(within(panel).getByRole("button", { name: "Create the passkey" })).toBeEnabled();
    expect(calls.enrollPasskey).not.toHaveBeenCalled();
  });

  it("words the extra click in Spanish", async () => {
    setActiveLanguage("es");
    calls.enrollPasskeyOptions
      .mockRejectedValueOnce(stepUpRequired())
      .mockResolvedValueOnce(AFTER_CONFIRMATION)
      .mockResolvedValueOnce(IN_THE_CLICK);
    createPasskey.mockRejectedValueOnce(
      Object.assign(new Error("The operation either timed out or was not allowed."), { name: "NotAllowedError" }),
    );
    const user = userEvent.setup();
    renderSecurityPage();
    const heading = await screen.findByRole("heading", { name: "Claves de acceso" });
    const panel = heading.closest("section") as HTMLElement;
    await user.type(within(panel).getByLabelText("Nombre de la clave de acceso nueva"), "Desk key");
    await user.click(within(panel).getByRole("button", { name: "Añadir una clave de acceso" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirma que eres tú" });
    await user.click(await within(dialog).findByRole("button", { name: "Usar una clave de acceso" }));

    const createNow = await within(panel).findByRole("button", { name: "Crear la clave de acceso" });
    expect(
      within(panel).getByText("Tu segundo factor está confirmado. Pulsa «Crear la clave de acceso» para crearla."),
    ).toBeInTheDocument();
    await user.click(createNow);
    expect(await within(panel).findByRole("alert")).toHaveTextContent(
      "No se creó la clave de acceso. Pulsa «Crear la clave de acceso» para intentarlo de nuevo.",
    );
  });
});
