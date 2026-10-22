/**
 * First boot — the screen a buyer sees before the platform has an owner.
 *
 * WHY THE GATE IS ABOVE THE AUTH GATE, AND WHY THAT IS A TEST RATHER THAN A
 * COMMENT. `RequireAuth` redirects an unauthenticated visitor straight to the
 * identity provider. On an installation with zero users that redirect is a dead
 * end: there is no account to sign in as, no way back, and nothing on screen
 * that says so. So the setup check has to happen BEFORE anything can trigger a
 * redirect, and the assertion that proves it is `signIn` never being called.
 *
 * The four cases below are the ones the plan names (Task 10), and each of them
 * is a way the screen has failed in products that shipped it:
 *   1. it does not appear on a fresh platform      -> the runbook comes back;
 *   2. it appears on a claimed one                 -> every sign-in is hijacked;
 *   3. a wrong code renders nothing                -> a typo looks like a crash;
 *   4. a double-click claims twice                 -> two organizations, one of
 *      which nobody can reach.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const signIn = vi.fn();

vi.mock("../../../lib/auth", () => ({
  signIn: () => signIn(),
  completeSignIn: () => Promise.resolve(),
  isAuthenticated: () => false,
  initAuth: () => Promise.resolve(null),
  signOut: () => Promise.resolve(),
  accessToken: () => null,
  startAccountAction: () => Promise.resolve(),
  oidcAuthority: "https://itms.example/realms/it-suite",
  userManager: { events: { addUserLoaded: () => {}, addUserUnloaded: () => {} } },
}));

const { SetupPage } = await import("../SetupPage");
const { default: App, queryClient } = await import("../../../App");

/** Every claim the stub received, as the page actually sent it. */
const claims: { code: string; email: string; display_name: string; organization_name: string }[] =
  [];

type Reply = { status: number; body: unknown };

let statusReply: Reply;
/** Resolved with the claim's reply. A promise, so a test can hold the request
 * in flight and click again -- which is what a double-click IS. */
let claimReply: (reply: Reply) => void;
let claimPending: Promise<Reply>;

function respond({ status, body }: Reply): Response {
  return {
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response;
}

beforeEach(() => {
  // The application's query client is module-level -- one per browser tab,
  // which is right in production and wrong here: `setup/status` is held with
  // `staleTime: Infinity`, so without this the SECOND render in this file
  // answers from the FIRST test's cache and the fetch stub below is never
  // consulted.
  queryClient.clear();
  signIn.mockReset().mockResolvedValue(undefined);
  claims.length = 0;
  statusReply = { status: 200, body: { needed: false } };
  claimPending = new Promise<Reply>((resolve) => {
    claimReply = resolve;
  });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/setup/status")) return respond(statusReply);
      if (url.endsWith("/api/v1/setup/claim")) {
        // NOT `JSON.parse(init?.body ?? "{}")`: a page that sent no body at all
        // would be recorded as an empty claim and every field assertion below
        // would pass against a request that never carried them.
        claims.push(JSON.parse(String(init?.body)));
        return respond(await claimPending);
      }
      throw new Error(`unexpected fetch: ${url}`);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function fillTheForm(user: ReturnType<typeof userEvent.setup>, code: string) {
  await user.type(await screen.findByLabelText("Install code"), code);
  await user.type(screen.getByLabelText("Your name"), "Dana Okafor");
  await user.type(screen.getByLabelText("Your email address"), "dana@example.com");
  await user.type(screen.getByLabelText("Organization name"), "Toeshee");
}

describe("the setup screen", () => {
  it("is what a fresh installation shows, instead of a redirect to sign in", async () => {
    statusReply = { status: 200, body: { needed: true } };
    render(<App />);

    expect(await screen.findByLabelText("Install code")).toBeInTheDocument();
    // The whole point of checking before the auth gate. A redirect here would
    // land on a sign-in page for a platform with no accounts in it.
    expect(signIn).not.toHaveBeenCalled();
  });

  it("stays out of the way once the platform has an owner", async () => {
    statusReply = { status: 200, body: { needed: false } };
    render(<App />);

    await waitFor(() => expect(signIn).toHaveBeenCalledTimes(1));
    expect(screen.queryByLabelText("Install code")).not.toBeInTheDocument();
  });

  it("keeps the ordinary sign-in working when the status call fails", async () => {
    // A backend that cannot answer must not strand every operator on a setup
    // form for a platform that was set up months ago.
    statusReply = { status: 500, body: { detail: "boom" } };
    render(<App />);

    await waitFor(() => expect(signIn).toHaveBeenCalledTimes(1));
    expect(screen.queryByLabelText("Install code")).not.toBeInTheDocument();
  });

  it("shows the refusal when the code is wrong, and keeps the form", async () => {
    const user = userEvent.setup();
    render(<SetupPage />);
    await fillTheForm(user, "not-the-code");
    claimReply({
      status: 403,
      body: { detail: "that install code is not correct." },
    });
    await user.click(screen.getByRole("button", { name: /finish setup/i }));

    expect(await screen.findByText(/that install code is not correct\./)).toBeInTheDocument();
    // Not a blank page and not a dead end: the code they typed is still there
    // to correct.
    expect(screen.getByLabelText<HTMLInputElement>("Install code")).toHaveValue("not-the-code");
    expect(claims).toHaveLength(1);
    expect(claims[0]).toMatchObject({
      code: "not-the-code",
      email: "dana@example.com",
      display_name: "Dana Okafor",
      organization_name: "Toeshee",
    });
  });

  it("claims once however many times the button is clicked", async () => {
    const user = userEvent.setup();
    render(<SetupPage />);
    await fillTheForm(user, "install-code");

    const submit = screen.getByRole("button", { name: /finish setup/i });
    await user.click(submit);
    await user.click(submit);
    await user.click(submit);

    // Still in flight -- the second and third clicks land exactly where a
    // double-click does, and two accepted claims are two organizations.
    expect(claims).toHaveLength(1);

    claimReply({
      status: 201,
      body: {
        organization_id: "00000000-0000-0000-0000-0000000000aa",
        organization_slug: "toeshee",
        user_id: "00000000-0000-0000-0000-0000000000bb",
        email: "dana@example.com",
        invitation_sent: true,
        invitation_error: null,
      },
    });
    expect(await screen.findByText(/dana@example\.com/)).toBeInTheDocument();
    expect(claims).toHaveLength(1);
  });

  it("claims once when both submissions land in the same frame", async () => {
    // The click test above is satisfied by a button that goes `disabled`, and
    // a disabled button is applied on the NEXT render. Two submissions inside
    // one frame -- what a real double-click is, and what pressing Enter twice
    // does -- both see the old render. Submitting the form directly is the only
    // way to exercise that ordering, and it is the guard the state flag cannot
    // provide.
    const user = userEvent.setup();
    const { container } = render(<SetupPage />);
    await fillTheForm(user, "install-code");

    const form = container.querySelector("form");
    expect(form).not.toBeNull();
    fireEvent.submit(form as HTMLFormElement);
    fireEvent.submit(form as HTMLFormElement);

    expect(claims).toHaveLength(1);
  });

  it("names the field a 422 rejected, rather than reporting the status code", async () => {
    // THE LOCAL COPY THIS SCREEN USED TO CARRY handled `detail` only as a
    // STRING, so a schema rejection -- whose `detail` is a LIST -- fell
    // through to "the server answered 422". That is the one refusal where the
    // server knows exactly which box is wrong and the operator was told
    // nothing. Routed through `describeSubmitError` (cycle 4), it names it.
    const user = userEvent.setup();
    render(<SetupPage />);
    await fillTheForm(user, "install-code");
    claimReply({
      status: 422,
      body: {
        detail: [
          {
            type: "value_error",
            loc: ["body", "email"],
            msg: "value is not a valid email address",
            input: "dana@example.com",
          },
        ],
      },
    });
    await user.click(screen.getByRole("button", { name: /finish setup/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Email: value is not a valid email address/);
    expect(alert.textContent).not.toMatch(/answered 422/);
    // The payload itself never reaches the screen, and neither does the value
    // the user submitted (`input` is deliberately never read).
    expect(alert.textContent).not.toContain("value_error");
    expect(alert.textContent).not.toContain('"loc"');
  });

  it("says so when sign-in cannot even be started, rather than doing nothing", async () => {
    // A FAILURE SWALLOWED WHOLE: `onClick={() => void signIn()}` discarded the
    // rejection. `signinRedirect` fetches the provider's discovery document
    // before it navigates, so an identity provider that is not up yet -- the
    // likeliest state of a freshly installed appliance -- produced a button
    // that visibly did nothing, on the last screen of an installation.
    signIn.mockRejectedValue(new Error("Failed to fetch the OIDC metadata"));
    const user = userEvent.setup();
    render(<SetupPage />);
    await fillTheForm(user, "install-code");
    claimReply({
      status: 201,
      body: {
        organization_id: "00000000-0000-0000-0000-0000000000aa",
        organization_slug: "toeshee",
        user_id: "00000000-0000-0000-0000-0000000000bb",
        email: "dana@example.com",
        invitation_sent: true,
        invitation_error: null,
      },
    });
    await user.click(screen.getByRole("button", { name: /finish setup/i }));
    await user.click(await screen.findByRole("button", { name: /go to sign in/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/could not be started/i);
    expect(alert).toHaveTextContent(/Failed to fetch the OIDC metadata/);
  });

  it("says plainly when the invitation could not be sent", async () => {
    // The honest version of success: the organization exists, and the person
    // who just claimed it has no way to sign in until email works.
    const user = userEvent.setup();
    render(<SetupPage />);
    await fillTheForm(user, "install-code");
    claimReply({
      status: 201,
      body: {
        organization_id: "00000000-0000-0000-0000-0000000000aa",
        organization_slug: "toeshee",
        user_id: "00000000-0000-0000-0000-0000000000bb",
        email: "dana@example.com",
        invitation_sent: false,
        invitation_error: "The invitation email could not be sent.",
      },
    });
    await user.click(screen.getByRole("button", { name: /finish setup/i }));

    expect(
      await screen.findByText(/The invitation email could not be sent\./),
    ).toBeInTheDocument();
  });
});
