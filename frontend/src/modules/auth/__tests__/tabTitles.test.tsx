/**
 * Every screen before a session names itself in the browser tab: "<its
 * headline> · Novieri RMM" (redesign design §5, plan Task 13; WCAG 2.4.2).
 * Until the redesign every one of them was called what `index.html` is
 * called, so a person with a sign-in, a reset link and an invitation open in
 * three tabs could not tell them apart.
 *
 * The headline is the one the page already passes to `AuthCard`; the frame
 * writes the tab from it, so a page cannot name the tab one thing and the
 * card another.
 */
import { screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { forgetCodes, handOverCodes } from "../flow";
import { TEN_CODES, authApi, installDefaults, renderAuth } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));

const QUERY = "response_type=code&client_id=itms_x&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback&state=st4te";

beforeEach(() => {
  installDefaults();
  forgetCodes();
});

afterEach(() => {
  forgetCodes();
  vi.restoreAllMocks();
});

async function namesTheTab(headline: string) {
  expect(await screen.findByRole("heading", { level: 1, name: headline })).toBeInTheDocument();
  // Written in an effect, after the headline renders: waited for, not read at once.
  await waitFor(() => expect(document.title).toBe(`${headline} · Novieri RMM`));
}

describe("each screen before a session names itself in the tab", () => {
  it("names the screen in the tab: sign in (8.1)", async () => {
    renderAuth("/sign-in");
    await namesTheTab("Sign in");
  });

  it("names the screen in the tab: the second factor (8.2)", async () => {
    renderAuth("/sign-in/factor");
    await namesTheTab("Confirm it is you");
  });

  it("names the screen in the tab: the first second factor (8.3)", async () => {
    renderAuth("/sign-in/enroll");
    await namesTheTab("Add a second factor");
  });

  it("names the screen in the tab: the recovery codes (8.4)", async () => {
    handOverCodes(TEN_CODES);
    renderAuth("/sign-in/recovery-codes");
    await namesTheTab("Save your recovery codes");
  });

  it("names the screen in the tab: Microsoft's return (8.5)", async () => {
    renderAuth("/sign-in/microsoft");
    await namesTheTab("Signing in with Microsoft");
  });

  it("names the screen in the tab: forgotten password (8.6)", async () => {
    renderAuth("/forgot-password");
    await namesTheTab("Reset your password");
  });

  it("names the screen in the tab: the reset link (8.7)", async () => {
    renderAuth("/reset-password#t0ken");
    await namesTheTab("Choose a new password");
  });

  it("names the screen in the tab: the invitation (8.8)", async () => {
    renderAuth("/invite#t0ken");
    await namesTheTab("Welcome, Dana Okafor");
  });

  it("names the screen in the tab: account recovery (8.9)", async () => {
    renderAuth("/recover#t0ken");
    await namesTheTab("Recover your account");
  });

  it("names the screen in the tab: the portal's sign-in (8.10)", async () => {
    renderAuth("/portal/sign-in");
    await namesTheTab("Sign in to the portal");
  });

  it("names the screen in the tab: the AI app's request (8.11)", async () => {
    renderAuth(`/oauth/authorize?${QUERY}`);
    await namesTheTab("Connect the app at claude.ai");
  });

  it("names the token-bearing screens without the token", async () => {
    // The tab title lands in the browser's history: the link's secret must
    // never be in it (CLAUDE.md invariant 3's spirit; `./flow.ts`).
    renderAuth("/reset-password#s3cret-t0ken");
    await namesTheTab("Choose a new password");
    expect(document.title).not.toContain("s3cret-t0ken");
  });
});
