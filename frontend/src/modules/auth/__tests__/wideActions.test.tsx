/**
 * EVERY CARD IN THE SIGN-IN FRAME TAKES ITS ACTIONS ACROSS THE CARD (P3
 * adoption pass; coordinator's answer to the wave's Q3, "every card in the
 * frame gets the full-width primary").
 *
 * The two sign-in pages drew theirs across the card by hand (`AuthWideBtn`,
 * kit request K4); the other screens kept a primary at its own width at the
 * card's left edge, so the frame had two shapes for one kind of step. Now
 * every step action, primary or an alternative way through, is the kit's
 * `Btn wide` (`.btn.wide`), as the sign-in pages already drew them: one column
 * of full-width actions per card. "Copy the codes" is a tool on the list
 * above it, not a way through the step, and keeps its own width.
 *
 * And the address a person cannot change (the invitation, the recovery link)
 * is sunken by the kit's `.input-field[readonly]` (K5), not by a style the
 * page writes inline (`READ_ONLY`).
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { forgetCodes, handOverCodes } from "../flow";
import { TEN_CODES, authApi, calls, installDefaults, renderAuth } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));

const webauthn = vi.hoisted(() => ({ supported: true }));
vi.mock("@simplewebauthn/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@simplewebauthn/browser")>()),
  browserSupportsWebAuthn: () => webauthn.supported,
}));

const QUERY = "response_type=code&client_id=itms_x&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback&state=st4te";
const EVERY = { methods: ["passkey", "totp", "recovery"], rememberDays: 15 };

beforeEach(() => {
  installDefaults();
  forgetCodes();
  webauthn.supported = true;
});

afterEach(() => {
  forgetCodes();
  vi.restoreAllMocks();
});

/** The card's actions: every button and button-styled link inside the panel, by visible name. */
function actions(): HTMLElement[] {
  const card = document.querySelector<HTMLElement>(".panel");
  if (!card) throw new Error("no card on screen");
  return [...card.querySelectorAll<HTMLElement>("button, a.btn")];
}

/** Every step action is across the card, and so is the card's primary, which exists. */
async function acrossTheCard(headline: string, tools: readonly string[] = []) {
  await screen.findByRole("heading", { level: 1, name: headline });
  const all = actions();
  const steps = all.filter((action) => !tools.includes(action.textContent ?? ""));
  expect(steps.length, "the card draws at least one action").toBeGreaterThan(0);
  for (const action of steps) expect(action, action.textContent ?? "").toHaveClass("btn", "wide");
  for (const tool of all.filter((action) => tools.includes(action.textContent ?? ""))) {
    expect(tool).not.toHaveClass("wide");
  }
  return steps;
}

function primaryOf(steps: HTMLElement[]): HTMLElement {
  const primary = steps.filter((action) => action.classList.contains("primary"));
  expect(primary, "one primary").toHaveLength(1);
  return primary[0] as HTMLElement;
}

describe("every card in the sign-in frame draws its actions across the card", () => {
  it("sign in (8.1): Continue and Microsoft", async () => {
    renderAuth("/sign-in");
    const steps = await acrossTheCard("Sign in");
    expect(primaryOf(steps)).toHaveTextContent("Continue");
    expect(steps.map((step) => step.textContent)).toEqual(["Continue", "Sign in with Microsoft"]);
  });

  it("the portal's sign-in (8.10): the passkey, Continue and Microsoft", async () => {
    renderAuth("/portal/sign-in");
    const steps = await acrossTheCard("Sign in to the portal");
    expect(steps).toHaveLength(3);
    primaryOf(steps);
  });

  it("the second factor (8.2): the passkey, the code, and the recovery code", async () => {
    renderAuth("/sign-in/factor?use=recovery", EVERY);
    const steps = await acrossTheCard("Confirm it is you");
    expect(steps).toHaveLength(3);
    primaryOf(steps);
  });

  it("the first second factor (8.3): a passkey and the app, then the app's code", async () => {
    const user = userEvent.setup();
    renderAuth("/sign-in/enroll");
    const steps = await acrossTheCard("Add a second factor");
    expect(steps).toHaveLength(2);
    primaryOf(steps);
    await user.click(screen.getByRole("button", { name: "Set up an authenticator app" }));
    await screen.findByRole("button", { name: "Confirm code" });
    await acrossTheCard("Add a second factor");
  });

  it("the recovery codes (8.4): Continue across, Copy at its own width", async () => {
    handOverCodes(TEN_CODES);
    renderAuth("/sign-in/recovery-codes");
    const steps = await acrossTheCard("Save your recovery codes", ["Copy the codes"]);
    expect(primaryOf(steps)).toHaveTextContent("Continue");
  });

  it("the recovery codes with none to show (8.4)", async () => {
    renderAuth("/sign-in/recovery-codes");
    primaryOf(await acrossTheCard("Save your recovery codes"));
  });

  it("forgotten password (8.6)", async () => {
    renderAuth("/forgot-password");
    primaryOf(await acrossTheCard("Reset your password"));
  });

  it("the reset link (8.7), and the sign-in link once it is done", async () => {
    const user = userEvent.setup();
    renderAuth("/reset-password#t0ken");
    primaryOf(await acrossTheCard("Choose a new password"));
    const [password, again] = document.querySelectorAll<HTMLInputElement>('input[type="password"]');
    await user.type(password as HTMLInputElement, "Ultramarine-Kettle-7319");
    await user.type(again as HTMLInputElement, "Ultramarine-Kettle-7319");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    await waitFor(() => expect(calls.resetPassword).toHaveBeenCalled());
    const done = await screen.findByRole("link", { name: "Sign in" });
    expect(done).toHaveClass("btn", "primary", "wide");
  });

  it("a technician's invitation (8.8)", async () => {
    renderAuth("/invite#t0ken");
    primaryOf(await acrossTheCard("Welcome, Dana Okafor"));
  });

  it("a contact's invitation (8.8): the passkey, Microsoft and the password", async () => {
    calls.getInvite.mockResolvedValue({
      email: "ana@acme.test",
      display_name: "Ana Ruiz",
      kind: "contact",
      password_minimum: 15,
      passkey: true,
      microsoft: true,
    });
    renderAuth("/invite#t0ken");
    const steps = await acrossTheCard("Welcome, Ana Ruiz");
    expect(steps).toHaveLength(3);
    primaryOf(steps);
  });

  it("account recovery (8.9)", async () => {
    renderAuth("/recover#t0ken");
    primaryOf(await acrossTheCard("Recover your account"));
  });

  it("the AI app's request (8.11): Approve and Decline, stacked", async () => {
    renderAuth(`/oauth/authorize?${QUERY}`);
    const steps = await acrossTheCard("Connect the app at claude.ai");
    expect(steps.map((step) => step.textContent)).toEqual(["Approve", "Decline"]);
    expect(primaryOf(steps)).toHaveTextContent("Approve");
  });

  it("the AI app's request, after a decline: the way back", async () => {
    const user = userEvent.setup();
    calls.oauthAuthorizeDecision.mockResolvedValue({
      outcome: "back",
      reason: "declined",
      detail: "You declined. The app will be told.",
      redirect_host: "claude.ai",
      redirect_to: "https://claude.ai/api/mcp/auth_callback?error=access_denied&state=st4te",
    });
    renderAuth(`/oauth/authorize?${QUERY}`);
    await user.click(await screen.findByRole("button", { name: "Decline" }));
    await acrossTheCard("You did not connect this app");
  });

  it("the AI app's request that cannot be checked now: Try again", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({
      outcome: "refused",
      error: "temporarily_unavailable",
      detail: "This AI app's description could not be read just now.",
    });
    renderAuth(`/oauth/authorize?${QUERY}`);
    await acrossTheCard("This request cannot be checked right now");
  });
});

describe("an address the person cannot change", () => {
  it.each([
    ["the invitation", "/invite#t0ken", "Welcome, Dana Okafor"],
    ["the recovery link", "/recover#t0ken", "Recover your account"],
  ])("is sunken by the kit's read-only field on %s, with no style of the page's own", async (_, path, headline) => {
    renderAuth(path);
    await screen.findByRole("heading", { level: 1, name: headline });
    const address = document.querySelector<HTMLInputElement>('input[type="email"]') as HTMLInputElement;
    expect(address).toHaveAttribute("readonly");
    expect(address).toHaveClass("input-field");
    expect(address.getAttribute("style")).toBeNull();
  });
});
