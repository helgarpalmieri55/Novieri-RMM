/**
 * `/recover#<token>` — the link the server's recovery command prints (own-auth
 * design §7, Q5; coordinator decision for Task 11).
 *
 * Opening the link changes nothing: the page names the account and waits for
 * one press. The press removes the account's second factors (server side) and
 * goes on to a new first factor. The token comes from the fragment, leaves the
 * address, and travels in bodies only.
 *
 * A PASSWORD AN EMAILED LINK SET (operator decision 2026-11-05, «Pedir
 * contraseña nueva si hay marca»): then the server asks for a new one before
 * the recovery finishes -- said by the details' `password_required`, or by a
 * 409 `recovery_needs_password` when a mail reset landed after the page
 * opened. The page asks as the invitation does, and sends it.
 *
 * AND ON EVERY RECOVERY (operator decision 2026-11-05, «Ofrecerla siempre,
 * aplicar»): without the mark the same fields are offered, optional. Left
 * empty, only the token travels and the password stays; filled in, it is
 * checked as the invitation checks it and sent, and the server applies it.
 */
import type { UserEvent } from "@testing-library/user-event";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { authApi, calls, installDefaults, mutationCacheText, renderAuth, where } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));

const GOOD = "a long phrase I can remember";
/** The server's sentence, as `flows._needs_password` words it. */
const NEEDS_PASSWORD =
  "Your password was last set with an emailed link. Choose a new password to finish recovering your account.";

beforeEach(() => {
  installDefaults();
});

async function choose(user: UserEvent, password: string, again = password) {
  await user.type(await screen.findByLabelText("New password"), password);
  await user.type(screen.getByLabelText("The same password again"), again);
}

function mailSetPassword() {
  calls.getRecovery.mockResolvedValue({ email: "owner@example.test", password_required: true });
}

describe("recovering an account from the server's link", () => {
  it("names whose account it is and changes nothing until the person presses", async () => {
    renderAuth("/recover#tok-recover-1");
    expect(await screen.findByRole("heading", { name: "Recover your account" })).toBeInTheDocument();
    expect(calls.getRecovery).toHaveBeenCalledWith("tok-recover-1");
    expect(screen.getByText("owner@example.test", { selector: "b" })).toBeInTheDocument();
    expect(screen.getByText(/removes this account's second factors and recovery codes/)).toBeInTheDocument();
    expect(calls.acceptRecovery).not.toHaveBeenCalled();
    await waitFor(() => expect(where.location?.hash).toBe(""));
    expect(where.location?.search).toBe("");
    expect(where.location?.state ?? null).toBeNull();
  });

  it("recovers on the press, with the token in the body, and goes on to a new first factor", async () => {
    const user = userEvent.setup();
    renderAuth("/recover#tok-recover-1");
    await user.click(await screen.findByRole("button", { name: "Recover my account" }));
    expect(calls.acceptRecovery).toHaveBeenCalledWith({ token: "tok-recover-1" });
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/enroll"));
  });

  it("says a used, expired or unknown link is dead, and who can make another", async () => {
    calls.getRecovery.mockRejectedValue(new ApiError(410, '{"code":"invalid_link"}'));
    renderAuth("/recover#tok-gone");
    expect(await screen.findByRole("alert")).toHaveTextContent(/This recovery link has expired or was already used/);
    expect(screen.queryByRole("button", { name: "Recover my account" })).not.toBeInTheDocument();
  });

  it("says so when the link died between opening it and pressing", async () => {
    calls.acceptRecovery.mockRejectedValue(new ApiError(410, '{"code":"invalid_link"}'));
    const user = userEvent.setup();
    renderAuth("/recover#tok-recover-1");
    await user.click(await screen.findByRole("button", { name: "Recover my account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/This recovery link has expired or was already used/);
    expect(where.location?.pathname).toBe("/recover");
  });

  it("says a wait for what it is, and stays", async () => {
    calls.acceptRecovery.mockRejectedValue(new ApiError(429, '{"code":"factor_wait","wait_s":30}'));
    const user = userEvent.setup();
    renderAuth("/recover#tok-recover-1");
    await user.click(await screen.findByRole("button", { name: "Recover my account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many attempts. Wait 30 seconds and try again.");
    expect(screen.getByRole("button", { name: "Recover my account" })).toBeInTheDocument();
  });
});

describe("a new password, offered on every recovery", () => {
  it("offers it, optional, when the details say none is needed", async () => {
    renderAuth("/recover#tok-recover-1");
    expect(await screen.findByRole("button", { name: "Recover my account" })).toBeInTheDocument();
    expect(screen.getByText(/Optional: set a new password now/)).toBeInTheDocument();
    const field = screen.getByLabelText("New password");
    expect(field).toHaveAttribute("type", "password");
    expect(field).not.toHaveAttribute("data-keep");
    expect(screen.getByLabelText("The same password again")).toHaveAttribute("type", "password");
    expect(screen.queryByText(/last set with an emailed link/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Recover my account" })).toBeEnabled();
  });

  it("left empty, sends only the token: the password stays", async () => {
    const user = userEvent.setup();
    renderAuth("/recover#tok-recover-1");
    expect(await screen.findByLabelText("New password")).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "Recover my account" }));
    expect(calls.acceptRecovery).toHaveBeenCalledExactlyOnceWith({ token: "tok-recover-1" });
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/enroll"));
  });

  it("filled in, sends it with the token", async () => {
    const user = userEvent.setup();
    renderAuth("/recover#tok-recover-1");
    await choose(user, GOOD);
    await user.click(screen.getByRole("button", { name: "Recover my account" }));
    expect(calls.acceptRecovery).toHaveBeenCalledExactlyOnceWith({ token: "tok-recover-1", password: GOOD });
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/enroll"));
  });

  it.each([
    ["too short", "eleven char", "eleven char", "Use at least 12 characters."],
    ["not the same twice", GOOD, "a different long phrase", "The two passwords are not the same."],
    ["typed only once", GOOD, "", "The two passwords are not the same."],
  ])("checks one that is %s before asking, as the invitation does", async (_, password, again, sentence) => {
    const user = userEvent.setup();
    renderAuth("/recover#tok-recover-1");
    await user.type(await screen.findByLabelText("New password"), password);
    if (again) await user.type(screen.getByLabelText("The same password again"), again);
    await user.click(screen.getByRole("button", { name: "Recover my account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(sentence);
    expect(calls.acceptRecovery).not.toHaveBeenCalled();
  });

  it("says the server's reason when the offered password is refused, and stays", async () => {
    const sentence = "That password contains your email address.";
    calls.acceptRecovery.mockRejectedValue(new ApiError(422, JSON.stringify({ code: "password_rejected", detail: sentence })));
    const user = userEvent.setup();
    renderAuth("/recover#tok-recover-1");
    await choose(user, GOOD);
    await user.click(screen.getByRole("button", { name: "Recover my account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(sentence);
    expect(where.location?.pathname).toBe("/recover");
  });
});

describe("recovering an account whose password an emailed link set", () => {
  it("does not call the field optional", async () => {
    mailSetPassword();
    renderAuth("/recover#tok-recover-1");
    expect(await screen.findByText(/Your password was last set with an emailed link/)).toBeInTheDocument();
    expect(screen.queryByText(/Optional: set a new password now/)).not.toBeInTheDocument();
  });

  it("asks for a new password, twice, when the details say so, and sends it with the token", async () => {
    mailSetPassword();
    const user = userEvent.setup();
    renderAuth("/recover#tok-recover-1");
    expect(await screen.findByText(/Your password was last set with an emailed link/)).toBeInTheDocument();
    const field = screen.getByLabelText("New password");
    // A secret: never shown, and never kept by the session-end "kept text" rule.
    expect(field).toHaveAttribute("type", "password");
    expect(field).not.toHaveAttribute("data-keep");
    expect(screen.getByLabelText("The same password again")).toHaveAttribute("type", "password");
    // For the password manager: the account the new password belongs to.
    expect(screen.getByLabelText("Email address")).toHaveValue("owner@example.test");

    await choose(user, GOOD);
    await user.click(screen.getByRole("button", { name: "Recover my account" }));

    expect(calls.acceptRecovery).toHaveBeenCalledExactlyOnceWith({ token: "tok-recover-1", password: GOOD });
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/enroll"));
  });

  it.each([
    ["too short", "eleven char", "eleven char", "Use at least 12 characters."],
    ["not the same twice", GOOD, "a different long phrase", "The two passwords are not the same."],
  ])("refuses a password that is %s before asking, as the invitation does", async (_, password, again, sentence) => {
    mailSetPassword();
    const user = userEvent.setup();
    renderAuth("/recover#tok-recover-1");
    await choose(user, password, again);
    await user.click(screen.getByRole("button", { name: "Recover my account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(sentence);
    expect(calls.acceptRecovery).not.toHaveBeenCalled();
  });

  it("asks for the password when the press meets a mail reset (409), says the server's sentence, and the retry sends it", async () => {
    calls.acceptRecovery
      .mockRejectedValueOnce(new ApiError(409, JSON.stringify({ detail: NEEDS_PASSWORD, code: "recovery_needs_password" })))
      .mockResolvedValueOnce(undefined);
    const user = userEvent.setup();
    renderAuth("/recover#tok-recover-1");
    await user.click(await screen.findByRole("button", { name: "Recover my account" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(NEEDS_PASSWORD);
    expect(where.location?.pathname).toBe("/recover");
    expect(screen.queryByText(/This recovery link has expired/)).not.toBeInTheDocument();
    // Required now: no longer offered as optional, and an empty press is not possible.
    expect(screen.queryByText(/Optional: set a new password now/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Recover my account" })).toBeDisabled();

    await choose(user, GOOD);
    await user.click(screen.getByRole("button", { name: "Recover my account" }));

    expect(calls.acceptRecovery).toHaveBeenNthCalledWith(1, { token: "tok-recover-1" });
    expect(calls.acceptRecovery).toHaveBeenNthCalledWith(2, { token: "tok-recover-1", password: GOOD });
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/enroll"));
  });

  it("says the server's reason when the new password is refused, and stays", async () => {
    const sentence = "That password contains your email address.";
    mailSetPassword();
    calls.acceptRecovery.mockRejectedValue(new ApiError(422, JSON.stringify({ code: "password_rejected", detail: sentence })));
    const user = userEvent.setup();
    renderAuth("/recover#tok-recover-1");
    await choose(user, GOOD);
    await user.click(screen.getByRole("button", { name: "Recover my account" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(sentence);
    expect(where.location?.pathname).toBe("/recover");
    expect(screen.getByLabelText("New password")).toBeInTheDocument();
  });

  it("leaves neither the password nor the token in the mutation cache", async () => {
    mailSetPassword();
    calls.acceptRecovery.mockRejectedValue(
      new ApiError(422, JSON.stringify({ code: "password_rejected", detail: "Use a longer password." })),
    );
    const user = userEvent.setup();
    const { queryClient } = renderAuth("/recover#tok-recover-1");
    await choose(user, GOOD);
    await user.click(screen.getByRole("button", { name: "Recover my account" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Use a longer password.");
    await waitFor(() => expect(mutationCacheText(queryClient)).not.toContain(GOOD));
    expect(mutationCacheText(queryClient)).not.toContain("tok-recover-1");
  });
});
