/**
 * `/recover#<token>` — the link the server's recovery command prints (own-auth
 * design §7, Q5; coordinator decision for Task 11).
 *
 * Opening the link changes nothing: the page names the account and waits for
 * one press. The press removes the account's second factors (server side) and
 * goes on to a new first factor. The token comes from the fragment, leaves the
 * address, and travels in bodies only.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { authApi, calls, installDefaults, renderAuth, where } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));

beforeEach(() => {
  installDefaults();
});

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
    expect(calls.acceptRecovery).toHaveBeenCalledWith("tok-recover-1");
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
