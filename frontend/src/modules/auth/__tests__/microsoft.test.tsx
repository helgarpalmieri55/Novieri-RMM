/**
 * Sign in with Microsoft (own-auth phase 2, Task 12; design §4): the button
 * leaves only for Microsoft, the return page sends the code once in a body,
 * and every refusal is one sentence.
 */
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../../i18n/core";
import { expectNoEnglish } from "../../../i18n/test-utils";
import { ApiError } from "../../../lib/api";
import { navigation } from "../../../lib/session";
import { checkedAuthorizeUrl, microsoftReturnOf, rememberMicrosoftReturn, takeMicrosoftReturn } from "../microsoft";
import { authApi, calls, installDefaults, renderAuth, where } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));

const AUTHORIZE = "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?client_id=x";
const GOOD = "Ultramarine-Kettle-7319";
let assign: ReturnType<typeof vi.spyOn>;
let replace: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  installDefaults();
  window.sessionStorage.clear();
  assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
  replace = vi.spyOn(navigation, "replace").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the return address", () => {
  it("is read once from the fragment", () => {
    expect(microsoftReturnOf("#code=abc&state=xyz")).toEqual({ code: "abc", state: "xyz" });
    expect(microsoftReturnOf("#error=access_denied&error_description=no&state=xyz")).toEqual({
      error: "access_denied",
      state: "xyz",
    });
    expect(microsoftReturnOf("#error=access_denied")).toEqual({ error: "access_denied", state: null });
    expect(microsoftReturnOf("")).toBeNull();
    expect(microsoftReturnOf("#code=abc")).toBeNull();
  });

  it("remembers where to go back, once, and never a page without a session", () => {
    rememberMicrosoftReturn({ intent: "sign_in", back: "/portal" });
    expect(takeMicrosoftReturn()).toEqual({ intent: "sign_in", back: "/portal" });
    expect(takeMicrosoftReturn()).toEqual({ intent: "sign_in", back: "/" });
    rememberMicrosoftReturn({ intent: "sign_in", back: "//evil.example" });
    expect(takeMicrosoftReturn().back).toBe("/");
  });
});

describe("the button", () => {
  it("on the console's page asks for a technician's sign-in and leaves for Microsoft", async () => {
    calls.microsoftStart.mockResolvedValue({ authorize_url: AUTHORIZE });
    const user = userEvent.setup();
    renderAuth("/sign-in?next=%2Fdevices");
    await user.click(screen.getByRole("button", { name: "Sign in with Microsoft" }));
    expect(calls.microsoftStart).toHaveBeenCalledWith({ intent: "sign_in" }, {});
    await waitFor(() => expect(assign).toHaveBeenCalledWith(AUTHORIZE));
    expect(takeMicrosoftReturn()).toEqual({ intent: "sign_in", back: "/devices" });
  });

  it("on the portal's page asks the same sign-in and comes back to the portal", async () => {
    calls.microsoftStart.mockResolvedValue({ authorize_url: AUTHORIZE });
    const user = userEvent.setup();
    renderAuth("/portal/sign-in");
    await user.click(screen.getByRole("button", { name: "Sign in with Microsoft" }));
    expect(calls.microsoftStart).toHaveBeenCalledWith({ intent: "sign_in" }, {});
    await waitFor(() => expect(assign).toHaveBeenCalledWith(AUTHORIZE));
    expect(takeMicrosoftReturn()).toEqual({ intent: "sign_in", back: "/portal" });
  });

  it("never leaves for anywhere but Microsoft", async () => {
    calls.microsoftStart.mockResolvedValue({ authorize_url: "https://evil.example/authorize" });
    const user = userEvent.setup();
    renderAuth("/sign-in");
    await user.click(screen.getByRole("button", { name: "Sign in with Microsoft" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The sign-in with Microsoft did not finish. Start it again.");
    expect(assign).not.toHaveBeenCalled();
  });

  it("says so where Microsoft is not set up", async () => {
    calls.microsoftStart.mockRejectedValue(new ApiError(409, '{"code":"microsoft_unavailable"}'));
    const user = userEvent.setup();
    renderAuth("/sign-in");
    await user.click(screen.getByRole("button", { name: "Sign in with Microsoft" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Signing in with Microsoft is not set up here.");
  });

  it("leaves once: after a successful start it stays off, and a second click starts no second transaction", async () => {
    calls.microsoftStart.mockResolvedValue({ authorize_url: AUTHORIZE });
    const user = userEvent.setup();
    renderAuth("/sign-in");
    const button = screen.getByRole("button", { name: "Sign in with Microsoft" });
    await user.click(button);
    await waitFor(() => expect(assign).toHaveBeenCalledWith(AUTHORIZE));
    expect(button).toBeDisabled();
    fireEvent.click(button);
    await user.click(button);
    expect(calls.microsoftStart).toHaveBeenCalledOnce();
    expect(assign).toHaveBeenCalledOnce();
  });

  it("comes back to life only when the browser restores the page from its cache (Back from Microsoft)", async () => {
    calls.microsoftStart.mockResolvedValue({ authorize_url: AUTHORIZE });
    const user = userEvent.setup();
    renderAuth("/sign-in");
    const button = screen.getByRole("button", { name: "Sign in with Microsoft" });
    await user.click(button);
    await waitFor(() => expect(assign).toHaveBeenCalledWith(AUTHORIZE));
    act(() => {
      window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: false }));
    });
    expect(button).toBeDisabled();
    act(() => {
      window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true }));
    });
    expect(button).toBeEnabled();
    expect(button).toHaveAccessibleName("Sign in with Microsoft");
  });

  it("is offered on an invitation only when the invitation says so", async () => {
    calls.getInvite.mockResolvedValue({
      email: "ana@acme.test",
      display_name: "Ana Ruiz",
      kind: "contact",
      password_minimum: 15,
      passkey: true,
      microsoft: true,
    });
    calls.microsoftStart.mockResolvedValue({ authorize_url: AUTHORIZE });
    const user = userEvent.setup();
    renderAuth("/invite#tok-contact-1");
    await user.click(await screen.findByRole("button", { name: "Accept with Microsoft" }));
    expect(calls.microsoftStart).toHaveBeenCalledWith({ intent: "invite", token: "tok-contact-1" }, {});
    // The return is kept just before leaving: read it once the page has left.
    await waitFor(() => expect(assign).toHaveBeenCalledWith(AUTHORIZE));
    expect(takeMicrosoftReturn()).toEqual({ intent: "invite", back: "/portal" });
  });

  it("is not offered on an invitation that does not say so", async () => {
    const user = userEvent.setup();
    renderAuth("/invite#tok-staff-1");
    await screen.findByLabelText("New password");
    expect(screen.queryByRole("button", { name: "Accept with Microsoft" })).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("New password"), GOOD);
    expect(calls.microsoftStart).not.toHaveBeenCalled();
  });

  it("takes a technician's new password to Microsoft as well (C3)", async () => {
    calls.getInvite.mockResolvedValue({
      email: "dana@example.test",
      display_name: "Dana Okafor",
      kind: "staff",
      password_minimum: 12,
      passkey: false,
      microsoft: true,
    });
    calls.microsoftStart.mockResolvedValue({ authorize_url: AUTHORIZE });
    const user = userEvent.setup();
    renderAuth("/invite#tok-staff-1");
    expect(await screen.findByText(/your way back if Microsoft ever stops working/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Accept with Microsoft" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Use at least 12 characters.");
    expect(calls.microsoftStart).not.toHaveBeenCalled();
    await user.type(screen.getByLabelText("New password"), GOOD);
    await user.type(screen.getByLabelText("The same password again"), GOOD);
    await user.click(screen.getByRole("button", { name: "Accept with Microsoft" }));
    expect(calls.microsoftStart).toHaveBeenCalledWith({ intent: "invite", token: "tok-staff-1", password: GOOD }, {});
    await waitFor(() => expect(assign).toHaveBeenCalledWith(AUTHORIZE));
    expect(takeMicrosoftReturn()).toEqual({ intent: "invite", back: "/" });
  });
});

describe("the invitation's one guard", () => {
  const CONTACT_INVITE = {
    email: "ana@acme.test",
    display_name: "Ana Ruiz",
    kind: "contact",
    password_minimum: 15,
    passkey: false,
    microsoft: true,
  };
  const STAFF_INVITE = {
    email: "dana@example.test",
    display_name: "Dana Okafor",
    kind: "staff",
    password_minimum: 12,
    passkey: false,
    microsoft: true,
  };
  const DEAD = new ApiError(410, '{"code":"invalid_link","detail":"this link is not valid"}');
  const INVALID = "This invitation has expired, was already used, or was cancelled. Ask the person who invited you for a new one.";

  it("turns a dead invitation into the invitation's own state, from the Microsoft button too (a contact)", async () => {
    calls.getInvite.mockResolvedValue(CONTACT_INVITE);
    calls.microsoftStart.mockRejectedValue(DEAD);
    const user = userEvent.setup();
    renderAuth("/invite#tok-contact-1");
    await user.click(await screen.findByRole("button", { name: "Accept with Microsoft" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(INVALID);
    expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept with Microsoft" })).not.toBeInTheDocument();
  });

  it("turns a dead invitation into the invitation's own state, from the Microsoft button too (a technician)", async () => {
    calls.getInvite.mockResolvedValue(STAFF_INVITE);
    calls.microsoftStart.mockRejectedValue(DEAD);
    const user = userEvent.setup();
    renderAuth("/invite#tok-staff-1");
    await user.type(await screen.findByLabelText("New password"), GOOD);
    await user.type(screen.getByLabelText("The same password again"), GOOD);
    await user.click(screen.getByRole("button", { name: "Accept with Microsoft" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(INVALID);
    expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
  });

  it("does not ask Microsoft while a technician's password is being accepted", async () => {
    calls.getInvite.mockResolvedValue(STAFF_INVITE);
    let finish: (value: { state: "enroll_required" }) => void = () => undefined;
    calls.acceptInvite.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const user = userEvent.setup();
    renderAuth("/invite#tok-staff-1");
    await user.type(await screen.findByLabelText("New password"), GOOD);
    await user.type(screen.getByLabelText("The same password again"), GOOD);
    const microsoft = screen.getByRole("button", { name: "Accept with Microsoft" });
    await user.click(screen.getByRole("button", { name: "Set password and continue" }));
    expect(calls.acceptInvite).toHaveBeenCalledOnce();
    expect(microsoft).toBeDisabled();
    fireEvent.click(microsoft);
    expect(calls.microsoftStart).not.toHaveBeenCalled();
    await act(async () => finish({ state: "enroll_required" }));
  });

  it("does not accept the password while Microsoft is being asked", async () => {
    calls.getInvite.mockResolvedValue(STAFF_INVITE);
    let finish: (value: { authorize_url: string }) => void = () => undefined;
    calls.microsoftStart.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const user = userEvent.setup();
    renderAuth("/invite#tok-staff-1");
    await user.type(await screen.findByLabelText("New password"), GOOD);
    await user.type(screen.getByLabelText("The same password again"), GOOD);
    const submit = screen.getByRole("button", { name: "Set password and continue" });
    await user.click(screen.getByRole("button", { name: "Accept with Microsoft" }));
    expect(calls.microsoftStart).toHaveBeenCalledOnce();
    expect(submit).toBeDisabled();
    fireEvent.submit(submit.closest("form") as HTMLFormElement);
    expect(calls.acceptInvite).not.toHaveBeenCalled();
    await act(async () => finish({ authorize_url: AUTHORIZE }));
    expect(assign).toHaveBeenCalledWith(AUTHORIZE);
  });
});

describe("the origin barrier", () => {
  it("lets Microsoft's own address through, and refuses every look-alike", () => {
    expect(checkedAuthorizeUrl(AUTHORIZE)).toBe(AUTHORIZE);
    for (const lookAlike of [
      "http://login.microsoftonline.com/organizations/oauth2/v2.0/authorize",
      "https://login.microsoftonline.com@evil.example/organizations/oauth2/v2.0/authorize",
      "https://login.microsoftonline.com.evil.example/organizations/oauth2/v2.0/authorize",
      "https://login.microsoftonline.com:8443/organizations/oauth2/v2.0/authorize",
      "//login.microsoftonline.com/organizations/oauth2/v2.0/authorize",
      "not an address",
    ]) {
      expect(checkedAuthorizeUrl(lookAlike), lookAlike).toBeNull();
    }
  });

  it("does not leave for a look-alike, and says the sign-in did not finish", async () => {
    calls.microsoftStart.mockResolvedValue({
      authorize_url: "https://login.microsoftonline.com@evil.example/organizations/oauth2/v2.0/authorize",
    });
    const user = userEvent.setup();
    renderAuth("/sign-in");
    await user.click(screen.getByRole("button", { name: "Sign in with Microsoft" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The sign-in with Microsoft did not finish. Start it again.");
    expect(assign).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem("itms.microsoft.return")).toBeNull();
  });
});

describe("the return page", () => {
  it("sends the code once and lands where the person was going", async () => {
    rememberMicrosoftReturn({ intent: "sign_in", back: "/portal" });
    calls.microsoftComplete.mockResolvedValue({ state: "signed_in" });
    renderAuth("/sign-in/microsoft#code=abc&state=xyz");
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/portal"));
    expect(calls.microsoftComplete).toHaveBeenCalledExactlyOnceWith({ code: "abc", state: "xyz" });
    expect(where.location?.hash).toBe("");
  });

  it("goes on to the second factor, carrying the page", async () => {
    rememberMicrosoftReturn({ intent: "sign_in", back: "/devices" });
    calls.microsoftComplete.mockResolvedValue({ state: "factor_required", methods: ["totp"], remember_device_days: 15 });
    renderAuth("/sign-in/microsoft#code=abc&state=xyz");
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/factor"));
    expect(where.location?.search).toBe("?next=%2Fdevices");
  });

  it("after linking, goes back to the security page", async () => {
    rememberMicrosoftReturn({ intent: "link", back: "/profile/security" });
    calls.microsoftComplete.mockResolvedValue({ state: "linked" });
    renderAuth("/sign-in/microsoft#code=abc&state=xyz");
    await waitFor(() => expect(where.location?.pathname).toBe("/profile/security"));
  });

  it("says one sentence for an account nobody here has linked, and offers to sign in another way", async () => {
    rememberMicrosoftReturn({ intent: "sign_in", back: "/portal" });
    calls.microsoftComplete.mockRejectedValue(new ApiError(401, '{"code":"microsoft_not_linked"}'));
    renderAuth("/sign-in/microsoft#code=abc&state=xyz");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This Microsoft account is not linked to anybody here. Sign in another way and link it from your profile, or use your invitation.",
    );
    expect(screen.getByRole("link", { name: "Back to sign in" })).toHaveAttribute(
      "href",
      "/portal/sign-in?next=%2Fportal",
    );
  });

  it("says Microsoft's own refusal, and only tells the server to drop the transaction (A2-70)", async () => {
    calls.microsoftComplete.mockRejectedValue(new ApiError(400, '{"code":"microsoft_failed"}'));
    renderAuth("/sign-in/microsoft#error=access_denied&error_description=The%20user%20cancelled&state=xyz");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Microsoft did not sign you in. Try again, or sign in another way.",
    );
    await waitFor(() => expect(calls.microsoftComplete).toHaveBeenCalledExactlyOnceWith({ code: "", state: "xyz" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Microsoft did not sign you in.");
    expect(screen.queryByText(/The user cancelled/)).not.toBeInTheDocument();
  });

  it("after a link whose confirmation ran out on the way, says to link again", async () => {
    rememberMicrosoftReturn({ intent: "link", back: "/profile/security" });
    calls.microsoftComplete.mockRejectedValue(new ApiError(403, '{"code":"step_up_required"}'));
    renderAuth("/sign-in/microsoft#code=abc&state=xyz");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Too long passed since you confirmed it was you. Link the account again from Sign-in and security.",
    );
    expect(screen.getByRole("link", { name: "Back to Sign-in and security" })).toHaveAttribute("href", "/profile/security");
  });

  it("asks nothing after a reload, which has no code", async () => {
    renderAuth("/sign-in/microsoft");
    expect(await screen.findByRole("alert")).toHaveTextContent("The sign-in with Microsoft did not finish. Start it again.");
    expect(calls.microsoftComplete).not.toHaveBeenCalled();
  });

  it("after a technician's invitation, goes on to the first factor", async () => {
    rememberMicrosoftReturn({ intent: "invite", back: "/" });
    calls.microsoftComplete.mockResolvedValue({ state: "enroll_required" });
    renderAuth("/sign-in/microsoft#code=abc&state=xyz");
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/enroll"));
    expect(where.location?.search).toBe("");
    expect(replace).not.toHaveBeenCalled();
  });

  it("sends the code once under StrictMode's double mount", async () => {
    rememberMicrosoftReturn({ intent: "sign_in", back: "/devices" });
    renderAuth("/sign-in/microsoft#code=abc&state=xyz", undefined, { strict: true });
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/devices"));
    expect(calls.microsoftComplete).toHaveBeenCalledExactlyOnceWith({ code: "abc", state: "xyz" });
  });

  it("forgets where to go back once it has been read", async () => {
    rememberMicrosoftReturn({ intent: "sign_in", back: "/devices" });
    renderAuth("/sign-in/microsoft#code=abc&state=xyz");
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/devices"));
    expect(window.sessionStorage.getItem("itms.microsoft.return")).toBeNull();
  });
});

describe("in Spanish", () => {
  it("says Microsoft's refusal in Spanish, and offers the way back", async () => {
    setActiveLanguage("es");
    const { container } = renderAuth("/sign-in/microsoft#error=access_denied&state=xyz");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Microsoft no te dejó entrar. Inténtalo de nuevo, o entra de otra forma.",
    );
    expect(screen.getByRole("link", { name: "Volver a entrar" })).toHaveAttribute("href", "/sign-in");
    expectNoEnglish(container);
  });

  it("offers the button on the sign-in page in Spanish", async () => {
    setActiveLanguage("es");
    const user = userEvent.setup();
    const { container } = renderAuth("/sign-in");
    await user.click(screen.getByRole("button", { name: "Entrar con Microsoft" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Entrar con Microsoft no está configurado aquí.");
    expectNoEnglish(container);
  });
});
