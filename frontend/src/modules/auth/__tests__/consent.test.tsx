/**
 * `/oauth/authorize` — an AI app asks the person to approve it (own-auth design
 * §5.2; plan 2026-11-06, Task 4).
 *
 * The page never reads the request itself: it sends the query, as the browser
 * has it, to the server, and shows what the server says. It names the host an
 * app cannot fake before the name it gave itself (B3-10), asks for the second
 * factor when the server wants it (C1), and leaves on its own ONLY with an
 * approval's code (B3-15): an error, a refusal or a decline waits for a click.
 * The approval's redirect carries the authorization code, which must not stay
 * in the mutation cache.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { navigation } from "../../../lib/session";
import { APPROVED, CONSENT, authApi, calls, installDefaults, mutationCacheText, renderAuth } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));

const QUERY =
  "response_type=code&client_id=itms_x&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback" +
  "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM&code_challenge_method=S256&state=st4te";
const PAGE = `/oauth/authorize?${QUERY}`;
const DENIED =
  "https://claude.ai/api/mcp/auth_callback?error=access_denied&state=st4te&iss=https%3A%2F%2Fitms.example";
const STEP_UP = () =>
  new ApiError(403, JSON.stringify({ detail: "confirm your second factor again to do this", code: "step_up_required" }));

let assign: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  installDefaults();
  assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("approving an AI app", () => {
  it("names the host first, the app's own name as its claim, its return and the account", async () => {
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "Connect the app at claude.ai" })).toBeInTheDocument();
    expect(calls.oauthAuthorizeDetails).toHaveBeenCalledWith(QUERY);
    expect(screen.getByText("It calls itself “Claude”.")).toBeInTheDocument();
    expect(
      screen.getByText("It registered itself here, and any app can register with any name: trust the address, not the name."),
    ).toBeInTheDocument();
    expect(screen.getByText("Your answer goes back to claude.ai.")).toBeInTheDocument();
    expect(screen.getByText("You are signed in as dana@example.test.")).toBeInTheDocument();
    expect(
      screen.getByText(
        "If you approve, it can act as you in ITMS: everything your role allows, including running actions on managed computers.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "The connection lasts while the app uses it and ends after 30 days unused. You can end it at any time in Settings → AI connections.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(`App id: ${CONSENT.client.client_id}`)).toBeInTheDocument();
    // A registered app publishes no document, and returns to an https site.
    expect(screen.queryByText(/Its description is published by/)).toBeNull();
    expect(screen.queryByText(/only answers on this computer/)).toBeNull();
  });

  it("names the host that publishes a client metadata document, and where it answers", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({
      ...CONSENT,
      client: {
        ...CONSENT.client,
        kind: "metadata_document",
        client_id: "https://claude.example/m.json",
        client_host: "claude.example",
        redirect_host: "answers.example",
      },
    });
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "Connect the app at claude.example" })).toBeInTheDocument();
    expect(screen.getByText("Its description is published by claude.example.")).toBeInTheDocument();
    expect(screen.getByText("Your answer goes back to answers.example.")).toBeInTheDocument();
    expect(screen.queryByText(/registered itself here/)).toBeNull();
  });

  it("warns when the app only returns to this computer", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({
      ...CONSENT,
      client: { ...CONSENT.client, redirect_host: "localhost", loopback_only: true },
    });
    renderAuth(PAGE);
    expect(
      await screen.findByText(
        "This app only answers on this computer (localhost). Approve it only if you started it yourself, on this computer, just now.",
      ),
    ).toBeInTheDocument();
  });

  it("says when the app gave no name", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({ ...CONSENT, client: { ...CONSENT.client, name: null } });
    renderAuth(PAGE);
    expect(await screen.findByText("It gave no name.")).toBeInTheDocument();
  });

  it("approves, goes where the server says, and keeps no code in the mutation cache", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderAuth(PAGE);
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(APPROVED));
    expect(calls.oauthAuthorizeDecision).toHaveBeenCalledWith(QUERY, true);
    expect(mutationCacheText(queryClient)).not.toContain("c0de-never-kept");
  });

  it("asks for the second factor when the server wants it, then sends the approval once more", async () => {
    calls.oauthAuthorizeDecision
      .mockRejectedValueOnce(STEP_UP())
      .mockResolvedValueOnce({ outcome: "redirect", redirect_to: APPROVED });
    const user = userEvent.setup();
    renderAuth(PAGE);
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    expect(assign).not.toHaveBeenCalled();
    await user.type(within(dialog).getByLabelText("Code from your authenticator app"), "123456");
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(APPROVED));
    expect(calls.stepUpTotp).toHaveBeenCalledWith("123456");
    expect(calls.oauthAuthorizeDecision).toHaveBeenCalledTimes(2);
  });

  it("closing the confirmation approves nothing and goes nowhere", async () => {
    calls.oauthAuthorizeDecision.mockRejectedValueOnce(STEP_UP());
    const user = userEvent.setup();
    renderAuth(PAGE);
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(calls.oauthAuthorizeDecision).toHaveBeenCalledTimes(1);
    expect(assign).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  it("answers once: both buttons are disabled while the decision is on its way", async () => {
    calls.oauthAuthorizeDecision.mockReturnValue(new Promise(() => undefined));
    const user = userEvent.setup();
    renderAuth(PAGE);
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    expect(screen.getByRole("button", { name: "Working…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Decline" })).toBeDisabled();
    expect(calls.oauthAuthorizeDecision).toHaveBeenCalledTimes(1);
  });
});

describe("everything else waits for the person (B3-15)", () => {
  it("a decline shows where it goes and leaves only when asked", async () => {
    calls.oauthAuthorizeDecision.mockResolvedValue({
      outcome: "back",
      reason: "declined",
      detail: "You declined. The app will be told.",
      redirect_host: "claude.ai",
      redirect_to: DENIED,
    });
    const user = userEvent.setup();
    renderAuth(PAGE);
    await user.click(await screen.findByRole("button", { name: "Decline" }));
    expect(await screen.findByRole("heading", { name: "You did not connect this app" })).toBeInTheDocument();
    expect(calls.oauthAuthorizeDecision).toHaveBeenCalledWith(QUERY, false);
    expect(assign).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Back to claude.ai" }));
    expect(assign).toHaveBeenCalledWith(DENIED);
  });

  it("an error the app should hear is shown with its host, and never followed on its own", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({
      outcome: "back",
      reason: "error",
      detail: "This AI app's request is malformed.",
      redirect_host: "claude.ai",
      redirect_to: DENIED,
    });
    const user = userEvent.setup();
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "This app's request cannot be used" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("This AI app's request is malformed.");
    // A beat later, still here: nothing navigated by itself.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(assign).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Back to claude.ai" }));
    expect(assign).toHaveBeenCalledTimes(1);
    expect(assign).toHaveBeenCalledWith(DENIED);
  });

  it("says why the person cannot connect AI apps, and goes back only when asked", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({
      outcome: "back",
      reason: "denied",
      detail: "requires mcp.use",
      redirect_host: "claude.ai",
      redirect_to: DENIED,
    });
    const user = userEvent.setup();
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "You cannot connect AI apps here" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("requires mcp.use");
    expect(assign).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Back to claude.ai" }));
    expect(assign).toHaveBeenCalledWith(DENIED);
  });

  it("a bad client or redirect is shown, with no way back to an address it does not trust", async () => {
    calls.oauthAuthorizeDetails.mockResolvedValue({
      outcome: "refused",
      error: "invalid_redirect_uri",
      detail: "The address this AI app asked to go back to is not one it registered.",
    });
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "This connection request cannot be used" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("The address this AI app asked to go back to is not one it registered.");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Back to/ })).toBeNull();
    expect(assign).not.toHaveBeenCalled();
  });

  it("a passing failure says to try again, and trying again asks once more", async () => {
    calls.oauthAuthorizeDetails
      .mockResolvedValueOnce({
        outcome: "refused",
        error: "temporarily_unavailable",
        detail: "This AI app's description could not be read just now.",
      })
      .mockResolvedValueOnce(CONSENT);
    const user = userEvent.setup();
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "This request cannot be checked right now" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "Connect the app at claude.ai" })).toBeInTheDocument();
    expect(calls.oauthAuthorizeDetails).toHaveBeenCalledTimes(2);
  });

  // A3-24: the server not answering is not a verdict on the request.
  it.each([
    ["a 5xx", new ApiError(503, JSON.stringify({ detail: "unavailable" }))],
    ["no answer at all", new TypeError("Failed to fetch")],
  ])("a details request that got %s says to try again", async (_what, failure) => {
    calls.oauthAuthorizeDetails.mockRejectedValueOnce(failure).mockResolvedValueOnce(CONSENT);
    const user = userEvent.setup();
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "This request cannot be checked right now" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "Connect the app at claude.ai" })).toBeInTheDocument();
    expect(calls.oauthAuthorizeDetails).toHaveBeenCalledTimes(2);
  });

  it("a details request the server refused is for good, with nothing to try again", async () => {
    calls.oauthAuthorizeDetails.mockRejectedValueOnce(
      new ApiError(422, JSON.stringify({ detail: "the request is not valid" })),
    );
    renderAuth(PAGE);
    expect(await screen.findByRole("heading", { name: "This connection request cannot be used" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("a decision that could not be checked just now offers to ask again", async () => {
    // Re-review f3-1, Minor 4: the transient answer of the decision too.
    calls.oauthAuthorizeDecision.mockResolvedValueOnce({
      outcome: "refused",
      error: "temporarily_unavailable",
      detail: "This AI app's description could not be read just now.",
    });
    const user = userEvent.setup();
    renderAuth(PAGE);
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("heading", { name: "This request cannot be checked right now" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "Connect the app at claude.ai" })).toBeInTheDocument();
    expect(calls.oauthAuthorizeDetails).toHaveBeenCalledTimes(2);
    expect(assign).not.toHaveBeenCalled();
  });

  it("shows the server's refusal of the decision and lets the person try nothing else", async () => {
    calls.oauthAuthorizeDecision.mockResolvedValue({
      outcome: "refused",
      error: "invalid_client",
      detail: "This AI app is not registered here, or its registration expired. Connect it again from the app.",
    });
    const user = userEvent.setup();
    renderAuth(PAGE);
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This AI app is not registered here");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(assign).not.toHaveBeenCalled();
  });
});
