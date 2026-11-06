/**
 * First boot — the screen a buyer sees before the platform has an owner.
 *
 * WHY THE GATE IS ABOVE THE AUTH GATE, AND WHY THAT IS A TEST RATHER THAN A
 * COMMENT. `RequireAuth` sends a visitor with no session straight to the
 * sign-in page. On an installation with zero users that is a dead end: there
 * is no account to sign in as, no way back, and nothing on screen that says
 * so. So the setup check has to happen BEFORE anything can send the visitor
 * away, and the assertion that proves it is no navigation ever starting.
 * (RE-POINTED, own-auth Task 10: the seam was `lib/auth.ts`'s `signIn`, a
 * redirect to Keycloak; it is `lib/session.ts`'s `navigation` now.)
 *
 * The four cases below are the ones the plan names (Task 10), and each of them
 * is a way the screen has failed in products that shipped it:
 *   1. it does not appear on a fresh platform      -> the runbook comes back;
 *   2. it appears on a claimed one                 -> every sign-in is hijacked;
 *   3. a wrong code renders nothing                -> a typo looks like a crash;
 *   4. a double-click claims twice                 -> two organizations, one of
 *      which nobody can reach.
 *
 * THE OWNER'S PASSWORD IS CHOSEN HERE (own-auth design §7; coordinator decision
 * for own-auth Task 11): the claim carries it, checked like every staff
 * password, and the page then goes to sign in, where the first second factor
 * is added. No invitation is mailed for the owner, so the screen that said
 * whether one went is gone; its two tests are re-pointed below.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { navigation } = await import("../../../lib/session");
const { SetupPage } = await import("../SetupPage");
const { default: App, queryClient } = await import("../../../App");

/** Every whole-document navigation the console started: to sign in, here. */
let assign: ReturnType<typeof vi.spyOn>;

/** Every claim the stub received, as the page actually sent it. */
const claims: { code: string; email: string; display_name: string; organization_name: string; password: string }[] =
  [];

const PASSWORD = "a long phrase I can remember";

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
  assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
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
      // No session: the guard's question, once the platform has an owner.
      if (url.endsWith("/api/v1/me")) return respond({ status: 401, body: { code: "auth_required" } });
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
  vi.restoreAllMocks();
});

async function fillTheForm(
  user: ReturnType<typeof userEvent.setup>,
  code: string,
  { password = PASSWORD, again = password }: { password?: string; again?: string } = {},
) {
  await user.type(await screen.findByLabelText("Install code"), code);
  await user.type(screen.getByLabelText("Your name"), "Dana Okafor");
  await user.type(screen.getByLabelText("Your email address"), "dana@example.com");
  await user.type(screen.getByLabelText("New password"), password);
  await user.type(screen.getByLabelText("The same password again"), again);
  await user.type(screen.getByLabelText("Organization name"), "Toeshee");
}

describe("the setup screen", () => {
  it("is what a fresh installation shows, instead of a redirect to sign in", async () => {
    statusReply = { status: 200, body: { needed: true } };
    render(<App />);

    expect(await screen.findByLabelText("Install code")).toBeInTheDocument();
    // The whole point of checking before the auth gate. A redirect here would
    // land on a sign-in page for a platform with no accounts in it.
    expect(assign).not.toHaveBeenCalled();
  });

  it("stays out of the way once the platform has an owner", async () => {
    statusReply = { status: 200, body: { needed: false } };
    render(<App />);

    await waitFor(() => expect(assign).toHaveBeenCalledTimes(1));
    expect(assign).toHaveBeenCalledWith("/sign-in");
    expect(screen.queryByLabelText("Install code")).not.toBeInTheDocument();
  });

  it("keeps the ordinary sign-in working when the status call fails", async () => {
    // A backend that cannot answer must not strand every operator on a setup
    // form for a platform that was set up months ago.
    statusReply = { status: 500, body: { detail: "boom" } };
    render(<App />);

    await waitFor(() => expect(assign).toHaveBeenCalledTimes(1));
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
      password: PASSWORD,
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
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/sign-in"));
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

  // RE-POINTED (own-auth Task 10, then the coordinator's Task 11 decision).
  // This was "says so when sign-in cannot even be started" (a redirect to
  // Keycloak that fetched first), then a "Go to sign in" button. The owner
  // has a password now, so the page goes to sign in by itself once the
  // claim is accepted -- in one whole-document load, so the setup check is
  // asked again and finds the installation claimed.
  it("goes to this console's sign-in page in one load once the installation is claimed", async () => {
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

    await waitFor(() => expect(assign).toHaveBeenCalledTimes(1));
    expect(assign).toHaveBeenCalledWith("/sign-in");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  // RE-POINTED: was "says plainly when the invitation could not be sent". The
  // owner is no longer mailed an invitation; what can go wrong instead is the
  // password, and it is refused before anything is claimed.
  it.each([
    ["short", "eleven char", "eleven char", "Use at least 12 characters."],
    ["mismatched", PASSWORD, `${PASSWORD}!`, "The two passwords are not the same."],
  ])("refuses a %s password before claiming anything", async (_, password, again, sentence) => {
    const user = userEvent.setup();
    render(<SetupPage />);
    await fillTheForm(user, "install-code", { password, again });
    await user.click(screen.getByRole("button", { name: /finish setup/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(sentence);
    expect(claims).toHaveLength(0);
  });

  it("shows the server's sentence when it refuses the password, and keeps the form", async () => {
    const sentence = "That password is on the list of passwords already exposed in breaches.";
    const user = userEvent.setup();
    render(<SetupPage />);
    await fillTheForm(user, "install-code");
    claimReply({ status: 400, body: { code: "password_rejected", detail: sentence } });
    await user.click(screen.getByRole("button", { name: /finish setup/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(sentence);
    expect(screen.getByLabelText<HTMLInputElement>("Install code")).toHaveValue("install-code");
    expect(assign).not.toHaveBeenCalled();
  });
});
