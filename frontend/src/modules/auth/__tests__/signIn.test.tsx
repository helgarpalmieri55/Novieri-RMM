/**
 * `/sign-in` and `/forgot-password` (own-auth design §2.1, §2.3, §2.8).
 *
 * What these hold the pages to:
 *   - the server's answer alone decides the next step, and `next` rides along;
 *   - a refusal is ONE sentence whatever the server's reason (§2.3: the page
 *     must not be more precise than the server), and the password is cleared;
 *   - a wait is said with its length, because a person can act on it;
 *   - "forgot" says the same thing whether or not the address has an account;
 *   - no request leaves twice for one double-click.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { navigation } from "../../../lib/session";
import { authApi, calls, installDefaults, renderAuth, where } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));

let replace: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  installDefaults();
  replace = vi.spyOn(navigation, "replace").mockImplementation(() => {});
});

// A spy on a property that is already one is the same spy, calls and all:
// restore it, or one test's navigation answers the next test's assertion.
afterEach(() => {
  vi.restoreAllMocks();
});

async function signIn(email = "dana@example.test", password = "correct horse battery") {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Email address"), email);
  await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: "Continue" }));
  return user;
}

describe("signing in", () => {
  it("sends the address and password, and goes to the second factor with what the server said", async () => {
    renderAuth("/sign-in?next=%2Fdevices");
    await signIn("  dana@example.test ");

    expect(calls.login).toHaveBeenCalledWith({ email: "dana@example.test", password: "correct horse battery" });
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/factor"));
    expect(where.location?.search).toBe("?next=%2Fdevices");
    expect(where.location?.state).toEqual({ methods: ["passkey", "totp", "recovery"], rememberDays: 15 });
  });

  it("goes to add a first factor when the account has none", async () => {
    calls.login.mockResolvedValue({ state: "enroll_required" });
    renderAuth("/sign-in");
    await signIn();
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/enroll"));
    expect(where.location?.search).toBe("");
  });

  it("enters the console at the page asked for on a remembered computer", async () => {
    calls.login.mockResolvedValue({ state: "signed_in" });
    renderAuth("/sign-in?next=%2Fsettings%3Ftab%3Dusers");
    await signIn();
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/settings?tab=users"));
  });

  it("never sends the person off-site, whatever next says", async () => {
    calls.login.mockResolvedValue({ state: "signed_in" });
    renderAuth("/sign-in?next=%2F%2Fevil.example");
    await signIn();
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
  });

  it("says one sentence for any refusal, and clears the password", async () => {
    calls.login.mockRejectedValue(new ApiError(401, '{"code":"invalid_credentials"}'));
    renderAuth("/sign-in");
    await signIn();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("That email address and password do not match an account here.");
    expect(screen.getByLabelText("Password")).toHaveValue("");
    expect(screen.getByLabelText("Email address")).toHaveValue("dana@example.test");
    expect(where.location?.pathname).toBe("/sign-in");
  });

  it("says how long to wait when the server asks for one", async () => {
    calls.login.mockRejectedValue(new ApiError(429, '{"code":"factor_wait","wait_s":7.2}'));
    renderAuth("/sign-in");
    await signIn();
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many attempts. Wait 8 seconds and try again.");
  });

  it("asks once for a double-click", async () => {
    let answer: (value: unknown) => void = () => {};
    calls.login.mockReturnValue(new Promise((resolve) => (answer = resolve)));
    renderAuth("/sign-in");
    const user = await signIn();
    await user.click(screen.getByRole("button", { name: "Working…" }));
    answer({ state: "enroll_required" });
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/enroll"));
    expect(calls.login).toHaveBeenCalledTimes(1);
  });

  it("offers the password link, carrying next", async () => {
    renderAuth("/sign-in?next=%2Fdevices");
    expect(screen.getByRole("link", { name: "Forgot your password?" })).toHaveAttribute(
      "href",
      "/forgot-password?next=%2Fdevices",
    );
  });

  it("asks for nothing when the page opens: no /me, no session check", () => {
    renderAuth("/sign-in");
    for (const fn of Object.values(calls)) expect(fn).not.toHaveBeenCalled();
  });
});

describe("forgetting the password", () => {
  it("says the same thing whether or not the address has an account", async () => {
    const user = userEvent.setup();
    renderAuth("/forgot-password");
    await user.type(screen.getByLabelText("Email address"), "nobody@example.test");
    await user.click(screen.getByRole("button", { name: "Send the link" }));

    expect(calls.forgotPassword).toHaveBeenCalledWith({ email: "nobody@example.test" });
    expect(await screen.findByRole("status")).toHaveTextContent(
      /If that address belongs to an account here, a link is on its way\. It works once, for 30 minutes\./,
    );
    expect(screen.getByRole("status")).toHaveTextContent(/second factor is still asked for/);
  });

  it("says a wait when the server asks for one", async () => {
    calls.forgotPassword.mockRejectedValue(new ApiError(429, ""));
    const user = userEvent.setup();
    renderAuth("/forgot-password");
    await user.type(screen.getByLabelText("Email address"), "dana@example.test");
    await user.click(screen.getByRole("button", { name: "Send the link" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many attempts. Wait a minute and try again.");
  });
});
