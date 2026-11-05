/**
 * The recent confirmation of the second factor (own authentication design
 * §2.5), as `useStepUp` answers the server's 403 `step_up_required`.
 *
 * THE SERVER DECIDES; THIS ONLY ANSWERS. Nothing here checks a clock or skips
 * a request: `run` sends it, and only that one refusal opens the dialog. What
 * is pinned is what a regression would turn into a security defect IN THE
 * CONSOLE: the request is sent again only after the server ACCEPTED the
 * factor, at most once, and never when the person closed the dialog; and
 * nothing typed survives the dialog.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../lib/api";
import { StepUpCancelled, isStepUpRequired, useStepUp } from "./stepUp";

const api = vi.hoisted(() => ({
  getMySecurity: vi.fn(),
  stepUpTotp: vi.fn(),
  stepUpPasskeyOptions: vi.fn(),
  stepUpPasskey: vi.fn(),
  stepUpRecovery: vi.fn(),
}));
vi.mock("../lib/ApiContext", () => ({ useApi: () => api }));

const { startAuthentication, webauthn } = vi.hoisted(() => ({
  startAuthentication: vi.fn(),
  webauthn: { supported: true },
}));
// The real module but for the ceremony and the browser's support: Task 11's
// `modules/auth/passkey.ts`, which this hook signs through, also reads
// `WebAuthnError` from it. jsdom has no WebAuthn, so support is the test's to
// say (final review D1, Minor 6: the dialog offers a passkey only where the
// browser can use one).
vi.mock("@simplewebauthn/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@simplewebauthn/browser")>()),
  browserSupportsWebAuthn: () => webauthn.supported,
  startAuthentication,
  startRegistration: vi.fn(),
}));

/** The server's answer to a change whose second factor is not fresh (Task 6). */
const REFUSED = () => new ApiError(403, JSON.stringify({ detail: { code: "step_up_required" } }));
const WRONG_CODE = () =>
  new ApiError(400, JSON.stringify({ detail: "That code is not right, or it was already used." }));
const OPTIONS = { challenge: "Y2hhbGxlbmdl", rpId: "itms.example.test", allowCredentials: [], userVerification: "required" };
const ASSERTION = {
  id: "Y3JlZA",
  rawId: "Y3JlZA",
  response: { clientDataJSON: "e30", authenticatorData: "YXV0aA", signature: "c2ln" },
  clientExtensionResults: {},
  type: "public-key",
};
/** A person with all three: a passkey, the app and three recovery codes. */
const EVERYTHING = {
  has_totp: true,
  passkeys: [{ id: "pk-1", name: "Laptop", created_at: "2026-10-01T00:00:00Z", last_used_at: null, backup_state: false }],
  recovery_codes_left: 3,
  trusted_devices: [],
  sessions: [],
};

/** A page that sends `action` through the hook and prints what came of it. */
function Probe({ action }: { action: () => Promise<string> }) {
  const stepUp = useStepUp();
  const [outcome, setOutcome] = useState("");
  return (
    <>
      <button
        type="button"
        onClick={() =>
          void stepUp.run(action).then(
            (value) => setOutcome(`done: ${value}`),
            (error: unknown) =>
              setOutcome(
                error instanceof StepUpCancelled
                  ? "cancelled"
                  : `failed: ${error instanceof ApiError ? error.status : "?"}`,
              ),
          )
        }
      >
        Go
      </button>
      <output>{outcome}</output>
      {stepUp.dialog}
    </>
  );
}

function renderProbe(action: () => Promise<string>) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <Probe action={action} />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

async function openConfirmation(user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> {
  await user.click(screen.getByRole("button", { name: "Go" }));
  return screen.findByRole("dialog", { name: "Confirm it's you" });
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  webauthn.supported = true;
  startAuthentication.mockReset();
  api.getMySecurity.mockResolvedValue(EVERYTHING);
  api.stepUpTotp.mockResolvedValue(undefined);
  api.stepUpPasskeyOptions.mockResolvedValue(OPTIONS);
  api.stepUpPasskey.mockResolvedValue(undefined);
  api.stepUpRecovery.mockResolvedValue(undefined);
  startAuthentication.mockResolvedValue(ASSERTION);
});

describe("isStepUpRequired", () => {
  it("is the server's 403 step_up_required, in either body shape", () => {
    expect(isStepUpRequired(REFUSED())).toBe(true);
    expect(isStepUpRequired(new ApiError(403, JSON.stringify({ code: "step_up_required" })))).toBe(true);
  });

  it("is no other refusal", () => {
    expect(isStepUpRequired(new ApiError(403, JSON.stringify({ detail: "You do not have permission." })))).toBe(false);
    expect(isStepUpRequired(new ApiError(403, JSON.stringify({ code: "csrf" })))).toBe(false);
    expect(isStepUpRequired(new ApiError(401, JSON.stringify({ code: "step_up_required" })))).toBe(false);
    expect(isStepUpRequired(new ApiError(403, "<html>Forbidden</html>"))).toBe(false);
    expect(isStepUpRequired(new Error("step_up_required"))).toBe(false);
  });
});

describe("useStepUp", () => {
  it("passes a success through and asks nothing", async () => {
    const user = renderProbe(vi.fn().mockResolvedValue("saved"));
    await user.click(screen.getByRole("button", { name: "Go" }));
    expect(await screen.findByText("done: saved")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.getMySecurity).not.toHaveBeenCalled();
  });

  it("passes any other refusal through as it came", async () => {
    const action = vi.fn().mockRejectedValue(new ApiError(403, JSON.stringify({ detail: "You do not have permission." })));
    const user = renderProbe(action);
    await user.click(screen.getByRole("button", { name: "Go" }));
    expect(await screen.findByText("failed: 403")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(action).toHaveBeenCalledTimes(1);
  });

  it("asks for the second factor without claiming when it was last confirmed", async () => {
    // FIX ROUND (Task 12 review, Minor 6): a session opened on a remembered
    // device never confirmed a second factor, so "you last confirmed it more
    // than 10 minutes ago" would be false exactly where the vault asks.
    const user = renderProbe(vi.fn().mockRejectedValueOnce(REFUSED()));
    const dialog = await openConfirmation(user);
    expect(within(dialog).getByText("Confirm your second factor to continue.")).toBeInTheDocument();
    expect(dialog).not.toHaveTextContent(/minutes ago/);
  });

  it("asks for a code, and sends the request again only after the server accepted it", async () => {
    const action = vi.fn().mockRejectedValueOnce(REFUSED()).mockResolvedValueOnce("saved");
    const user = renderProbe(action);
    const dialog = await openConfirmation(user);
    expect(action).toHaveBeenCalledTimes(1);

    await user.type(await within(dialog).findByLabelText("Code from your authenticator app"), "123 456");
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));

    expect(await screen.findByText("done: saved")).toBeInTheDocument();
    // Spaces are how a code is read aloud; they are not part of it.
    expect(api.stepUpTotp).toHaveBeenCalledExactlyOnceWith("123456");
    expect(action).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("says a code the server refused in the dialog, and does not send the request again", async () => {
    api.stepUpTotp.mockRejectedValue(WRONG_CODE());
    const action = vi.fn().mockRejectedValueOnce(REFUSED()).mockResolvedValueOnce("saved");
    const user = renderProbe(action);
    const dialog = await openConfirmation(user);

    await user.type(await within(dialog).findByLabelText("Code from your authenticator app"), "000000");
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("That code is not right, or it was already used.");
    expect(action).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("dialog", { name: "Confirm it's you" })).toBeInTheDocument();
  });

  it("confirms with a passkey: the server's challenge, the browser's answer, then the request again", async () => {
    const action = vi.fn().mockRejectedValueOnce(REFUSED()).mockResolvedValueOnce("saved");
    const user = renderProbe(action);
    const dialog = await openConfirmation(user);

    await user.click(await within(dialog).findByRole("button", { name: "Use a passkey" }));

    expect(await screen.findByText("done: saved")).toBeInTheDocument();
    expect(startAuthentication).toHaveBeenCalledExactlyOnceWith({ optionsJSON: OPTIONS });
    expect(api.stepUpPasskey).toHaveBeenCalledExactlyOnceWith(ASSERTION);
    expect(action).toHaveBeenCalledTimes(2);
  });

  it("sends nothing when the person cancels the passkey prompt, and says so", async () => {
    startAuthentication.mockRejectedValue(
      Object.assign(new Error("The operation either timed out or was not allowed."), { name: "NotAllowedError" }),
    );
    const action = vi.fn().mockRejectedValueOnce(REFUSED());
    const user = renderProbe(action);
    const dialog = await openConfirmation(user);

    await user.click(await within(dialog).findByRole("button", { name: "Use a passkey" }));

    // Task 11's sentence for a closed prompt (`describePasskeyError`), the one
    // the sign-in pages say too.
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "The passkey prompt was closed or timed out, so nothing was used.",
    );
    expect(api.stepUpPasskey).not.toHaveBeenCalled();
    expect(action).toHaveBeenCalledTimes(1);
  });

  it("confirms with a recovery code", async () => {
    const action = vi.fn().mockRejectedValueOnce(REFUSED()).mockResolvedValueOnce("saved");
    const user = renderProbe(action);
    const dialog = await openConfirmation(user);

    await user.click(await within(dialog).findByText("Use a recovery code instead"));
    await user.type(within(dialog).getByLabelText("Recovery code"), " 7KQ2M-X9D4P ");
    await user.click(within(dialog).getByRole("button", { name: "Use this code" }));

    expect(await screen.findByText("done: saved")).toBeInTheDocument();
    expect(api.stepUpRecovery).toHaveBeenCalledExactlyOnceWith("7KQ2M-X9D4P");
  });

  it("offers only the second factors the person has", async () => {
    api.getMySecurity.mockResolvedValue({ ...EVERYTHING, passkeys: [], recovery_codes_left: 0 });
    const user = renderProbe(vi.fn().mockRejectedValueOnce(REFUSED()));
    const dialog = await openConfirmation(user);

    expect(await within(dialog).findByLabelText("Code from your authenticator app")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Use a passkey" })).not.toBeInTheDocument();
    expect(within(dialog).queryByText("Use a recovery code instead")).not.toBeInTheDocument();
  });

  it("rejects with StepUpCancelled when it is closed, and sends nothing again", async () => {
    const action = vi.fn().mockRejectedValueOnce(REFUSED()).mockResolvedValueOnce("saved");
    const user = renderProbe(action);
    const dialog = await openConfirmation(user);

    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(await screen.findByText("cancelled")).toBeInTheDocument();
    expect(action).toHaveBeenCalledTimes(1);
    expect(api.stepUpTotp).not.toHaveBeenCalled();
  });

  it("returns a second refusal after a confirmation instead of asking again", async () => {
    const action = vi.fn().mockRejectedValue(REFUSED());
    const user = renderProbe(action);
    const dialog = await openConfirmation(user);

    await user.type(await within(dialog).findByLabelText("Code from your authenticator app"), "123456");
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));

    expect(await screen.findByText("failed: 403")).toBeInTheDocument();
    expect(action).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("keeps nothing it was given: the boxes and the refusal are gone the next time it opens", async () => {
    api.stepUpTotp.mockRejectedValue(WRONG_CODE());
    const user = renderProbe(vi.fn().mockRejectedValue(REFUSED()));
    let dialog = await openConfirmation(user);
    await user.type(await within(dialog).findByLabelText("Code from your authenticator app"), "000000");
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));
    await within(dialog).findByRole("alert");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    dialog = await openConfirmation(user);
    expect(await within(dialog).findByLabelText<HTMLInputElement>("Code from your authenticator app")).toHaveValue("");
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("useStepUp on a browser that cannot use passkeys (final review D1, Minor 6)", () => {
  beforeEach(() => {
    webauthn.supported = false;
  });

  it("offers no passkey button, and says why and what to use instead", async () => {
    const user = renderProbe(vi.fn().mockRejectedValueOnce(REFUSED()).mockResolvedValueOnce("saved"));
    const dialog = await openConfirmation(user);

    expect(
      await within(dialog).findByText(
        "This browser cannot use passkeys, so your passkey is not offered here. Use your authenticator app or one of your recovery codes.",
      ),
    ).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Use a passkey" })).not.toBeInTheDocument();
    // The app is the way now, and the primary button.
    await user.type(within(dialog).getByLabelText("Code from your authenticator app"), "123456");
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText("done: saved")).toBeInTheDocument();
    expect(startAuthentication).not.toHaveBeenCalled();
  });

  it("points a person with only a passkey and recovery codes at a code", async () => {
    api.getMySecurity.mockResolvedValue({ ...EVERYTHING, has_totp: false });
    const user = renderProbe(vi.fn().mockRejectedValue(REFUSED()));
    const dialog = await openConfirmation(user);
    expect(
      await within(dialog).findByText(
        "This browser cannot use passkeys, so your passkey is not offered here. Use one of your recovery codes, or a browser that supports passkeys.",
      ),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("Use a recovery code instead")).toBeInTheDocument();
  });

  it("tells a person with only a passkey to come back from a browser that supports one", async () => {
    api.getMySecurity.mockResolvedValue({ ...EVERYTHING, has_totp: false, recovery_codes_left: 0 });
    const user = renderProbe(vi.fn().mockRejectedValue(REFUSED()));
    const dialog = await openConfirmation(user);
    expect(
      await within(dialog).findByText(
        "This browser cannot use passkeys, and you have no other second factor to confirm with. Do this from a browser that supports passkeys.",
      ),
    ).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Use a passkey" })).not.toBeInTheDocument();
  });

  it("says nothing about passkeys to a person who has none", async () => {
    api.getMySecurity.mockResolvedValue({ ...EVERYTHING, passkeys: [] });
    const user = renderProbe(vi.fn().mockRejectedValue(REFUSED()));
    const dialog = await openConfirmation(user);
    await within(dialog).findByLabelText("Code from your authenticator app");
    expect(within(dialog).queryByText(/cannot use passkeys/)).not.toBeInTheDocument();
  });
});
