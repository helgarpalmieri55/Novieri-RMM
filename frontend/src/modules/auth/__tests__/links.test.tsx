/**
 * The two emailed links: `/reset-password#<token>` (own-auth design §2.8) and
 * `/invite#<token>` (§7).
 *
 * THE TOKEN IS IN THE FRAGMENT, which a browser never sends: it reaches no
 * access log and no `Referer`. The page reads it, takes it off the address,
 * and sends it in a request body -- never in a URL of its own.
 *
 * A REJECTED PASSWORD STATES ITS REASON (NIST SP 800-63B-4 §3.1.1.2: the
 * verifier "SHALL provide the reason for rejection", [S1]): the server's
 * `password_rejected` carries the sentence in `detail`, shown as sent.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { authApi, calls, installDefaults, renderAuth, where } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));

const GOOD = "a long phrase I can remember";

beforeEach(() => {
  installDefaults();
});

async function choose(password: string, again = password) {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("New password"), password);
  await user.type(screen.getByLabelText("The same password again"), again);
  return user;
}

describe("resetting the password from the emailed link", () => {
  it("sends the token from the fragment with the new password, and says the second factor still follows", async () => {
    renderAuth("/reset-password#tok-reset-1");
    const user = await choose(GOOD);
    await user.click(screen.getByRole("button", { name: "Change password" }));

    expect(calls.resetPassword).toHaveBeenCalledWith({ token: "tok-reset-1", password: GOOD });
    expect(await screen.findByRole("status")).toHaveTextContent(/your second factor is still asked for/);
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/sign-in");
  });

  it.each([
    ["short", "eleven char", "Use at least 12 characters."],
    ["long", "x".repeat(129), "Use at most 128 characters."],
  ])("refuses a %s password before asking, counting characters as a person does", async (_, password, sentence) => {
    renderAuth("/reset-password#tok");
    const user = await choose(password);
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(sentence);
    expect(calls.resetPassword).not.toHaveBeenCalled();
  });

  it("counts each character once, emoji included: eleven are too few, twelve are enough", async () => {
    // A key emoji is two UTF-16 units: `.length` would call eleven of them 22.
    renderAuth("/reset-password#tok");
    const eleven = "🔑".repeat(11);
    const user = await choose(eleven);
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Use at least 12 characters.");
    expect(calls.resetPassword).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText("New password"), "🔑");
    await user.type(screen.getByLabelText("The same password again"), "🔑");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    const twelve = "🔑".repeat(12);
    await waitFor(() => expect(calls.resetPassword).toHaveBeenCalledWith({ token: "tok", password: twelve }));
  });

  it("refuses two passwords that differ", async () => {
    renderAuth("/reset-password#tok");
    const user = await choose(GOOD, `${GOOD}!`);
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The two passwords are not the same.");
    expect(calls.resetPassword).not.toHaveBeenCalled();
  });

  it.each([400, 422])("shows the server's sentence for a refused password (%i password_rejected)", async (status) => {
    const sentence = "That password is on the list of passwords already exposed in breaches.";
    calls.resetPassword.mockRejectedValue(
      new ApiError(status, JSON.stringify({ code: "password_rejected", detail: sentence })),
    );
    renderAuth("/reset-password#tok");
    const user = await choose(GOOD);
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(sentence);
    // A refused password is not a dead link: the form stays.
    expect(screen.getByLabelText("New password")).toBeInTheDocument();
  });

  it("takes the token off the address once it has read it", async () => {
    renderAuth("/reset-password#tok-reset-1");
    await waitFor(() => expect(where.location?.hash).toBe(""));
    expect(where.location?.pathname).toBe("/reset-password");
    expect(where.location?.search).toBe("");
    expect(where.location?.state ?? null).toBeNull();
    const user = await choose(GOOD);
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(calls.resetPassword).toHaveBeenCalledWith({ token: "tok-reset-1", password: GOOD });
  });

  it("says a link the server no longer honours is dead, and offers a new one", async () => {
    calls.resetPassword.mockRejectedValue(new ApiError(410, '{"code":"invalid_link"}'));
    renderAuth("/reset-password#tok");
    const user = await choose(GOOD);
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This link has expired or was already used.");
    expect(screen.getByRole("link", { name: "Ask for a new link" })).toHaveAttribute("href", "/forgot-password");
  });
});

describe("accepting an invitation", () => {
  it("names who it is for, and goes on to the first factor once a password is chosen", async () => {
    renderAuth("/invite#tok-invite-1");
    expect(await screen.findByRole("heading", { name: "Welcome, Dana Okafor" })).toBeInTheDocument();
    expect(calls.getInvite).toHaveBeenCalledWith("tok-invite-1");
    expect(screen.getByText("dana@example.test", { selector: "b" })).toBeInTheDocument();
    expect(screen.getByLabelText("Email address")).toHaveValue("dana@example.test");

    const user = await choose(GOOD);
    await user.click(screen.getByRole("button", { name: "Set password and continue" }));

    expect(calls.acceptInvite).toHaveBeenCalledWith({ token: "tok-invite-1", password: GOOD });
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/enroll"));
  });

  it("says an expired, used or cancelled invitation for what it is", async () => {
    calls.getInvite.mockRejectedValue(new ApiError(410, '{"code":"invalid_link"}'));
    renderAuth("/invite#tok-gone");
    expect(await screen.findByRole("alert")).toHaveTextContent(/This invitation has expired, was already used, or was cancelled/);
    expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
  });

  it("says the server's reason when the password is refused, and stays", async () => {
    const sentence = "That password contains your email address.";
    calls.acceptInvite.mockRejectedValue(new ApiError(400, JSON.stringify({ code: "password_rejected", detail: sentence })));
    renderAuth("/invite#tok");
    const user = await choose(GOOD);
    await user.click(screen.getByRole("button", { name: "Set password and continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(sentence);
    expect(where.location?.pathname).toBe("/invite");
  });

  it("asks with an empty token when the link lost its fragment, and the server's 410 decides", async () => {
    calls.getInvite.mockRejectedValue(new ApiError(410, '{"code":"invalid_link"}'));
    renderAuth("/invite");
    expect(await screen.findByRole("alert")).toHaveTextContent(/This invitation has expired/);
    expect(calls.getInvite).toHaveBeenCalledWith("");
  });
});
