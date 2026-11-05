/**
 * The console's `ApiClient`, as `ApiProvider` builds it outside fixture mode.
 *
 * `X-CLIENT-ID` IS VALIDATED WHERE IT IS SENT. The server answers a malformed
 * `X-Client-Id` with 400 on every route, `/me` included
 * (`backend/app/api/deps.py`), so a choice it cannot read would leave the
 * console unable even to learn that it should drop it. A real choice is
 * always an id from `/me`; what this boundary refuses is a value that was
 * never a client's -- a fixture-style id (`cli-logistics`, as the fixtures'
 * customer was until harmony had to choose it) outside fixture mode, a bug.
 * Fixture mode never builds an `ApiClient` (it answers from a proxy), so its
 * ids are untouched by this.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InvitePage } from "../modules/auth/InvitePage";
import { RecoverPage } from "../modules/auth/RecoverPage";
import { SignInPage } from "../modules/auth/SignInPage";

import { ApiError, type ApiClient } from "./api";
import { ApiProvider, useApi } from "./ApiContext";
import { setSelectedClient } from "./clientChoice";
import { isSessionEnded, sessionEnded, sessionPerson } from "./sessionEnd";

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";

function stubFetch(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response("[]", { status: 200 })));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function sent(fetchMock: ReturnType<typeof vi.fn>, call = 0): Headers {
  return new Headers((fetchMock.mock.calls[call] as [string, RequestInit])[1].headers);
}

/** The client `ApiProvider` hands every page. */
function providedApi(): ApiClient {
  let api: ApiClient | null = null;
  function Probe() {
    api = useApi();
    return null;
  }
  render(
    <ApiProvider>
      <Probe />
    </ApiProvider>,
  );
  if (api === null) throw new Error("ApiProvider rendered nothing");
  return api;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ApiProvider's X-Client-Id", () => {
  it("names the chosen client when it is a client id", async () => {
    const fetchMock = stubFetch();
    const api = providedApi();
    setSelectedClient(ACME);
    await api.listDevices();
    expect(sent(fetchMock).get("X-Client-Id")).toBe(ACME);
  });

  it("is not sent for a choice that is not a client id", async () => {
    const fetchMock = stubFetch();
    const api = providedApi();
    for (const notAnId of ["cli-logistics", `${ACME}\n`, `{${ACME}}`, ` ${ACME}`, "all"]) {
      setSelectedClient(notAnId);
      await api.listDevices();
    }
    expect(fetchMock).toHaveBeenCalledTimes(5);
    for (let call = 0; call < 5; call += 1) expect(sent(fetchMock, call).has("X-Client-Id")).toBe(false);
  });
});

/**
 * A 401 IS THE SESSION ENDING, NOT A NAVIGATION (own-auth design §2.4, plan
 * decision D20). RE-POINTED (own-auth Task 10): this file mocked `./auth`,
 * whose `signIn()` was `onUnauthorized` -- a redirect from any page on any
 * 401, the sign-in page's own language save included. The provider now marks
 * the session ended and the guard and the shell decide what the person sees.
 */
describe("ApiProvider's onUnauthorized", () => {
  it("marks the session ended on a 401 that /me confirms, and navigates nowhere", async () => {
    // RE-POINTED (final re-review, I1): the end is confirmed by one `/me`
    // first, so it lands a round trip later than the 401.
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"code":"auth_required"}', { status: 401 })));
    const api = providedApi();
    expect(isSessionEnded()).toBe(false);
    await expect(api.listDevices()).rejects.toBeInstanceOf(ApiError);
    await waitFor(() => expect(isSessionEnded()).toBe(true));
    expect(window.location.pathname).toBe("/");
  });
});

/**
 * ONE STALE 401 IS NOT THE END (final re-review, I1). A password change gives
 * the caller's session a new token in its own transaction (K2), so a request
 * that left with the old cookie a moment earlier is answered 401 while the
 * session lives on under the new one. The socket already asked `/me` before
 * believing a 4401 (`./ws.ts`); HTTP now does the same, once, coalesced, on a
 * raw request that cannot itself end anything.
 */
describe("a 401, confirmed before it ends the session", () => {
  function stubServer(answer: (url: string) => Response | Error): ReturnType<typeof vi.fn> {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const response = answer(String(input));
      if (response instanceof Error) throw response;
      return response;
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }
  const meCalls = (fetchMock: ReturnType<typeof vi.fn>) =>
    fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/api/v1/me")).length;

  it("leaves the session when /me still answers for the same person", async () => {
    sessionPerson("u1");
    const fetchMock = stubServer((url) =>
      url.endsWith("/api/v1/me") ? new Response(JSON.stringify({ user_id: "u1" }), { status: 200 }) : new Response("", { status: 401 }),
    );
    const api = providedApi();
    await expect(api.listDevices()).rejects.toBeInstanceOf(ApiError);
    await waitFor(() => expect(meCalls(fetchMock)).toBe(1));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(isSessionEnded()).toBe(false);
  });

  it("ends it when /me names somebody else", async () => {
    sessionPerson("u1");
    stubServer((url) =>
      url.endsWith("/api/v1/me") ? new Response(JSON.stringify({ user_id: "u2" }), { status: 200 }) : new Response("", { status: 401 }),
    );
    const api = providedApi();
    await expect(api.listDevices()).rejects.toBeInstanceOf(ApiError);
    await waitFor(() => expect(isSessionEnded()).toBe(true));
  });

  it("leaves it when the check cannot reach the server", async () => {
    sessionPerson("u1");
    const fetchMock = stubServer((url) => (url.endsWith("/api/v1/me") ? new TypeError("Failed to fetch") : new Response("", { status: 401 })));
    const api = providedApi();
    await expect(api.listDevices()).rejects.toBeInstanceOf(ApiError);
    await waitFor(() => expect(meCalls(fetchMock)).toBe(1));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(isSessionEnded()).toBe(false);
  });

  it("asks /me once for a burst of 401s, and the check itself ends nothing on its own say-so", async () => {
    sessionPerson("u1");
    let answerMe!: (response: Response) => void;
    const fetchMock = vi.fn((input: RequestInfo | URL) =>
      String(input).endsWith("/api/v1/me")
        ? new Promise<Response>((resolve) => (answerMe = resolve))
        : Promise.resolve(new Response("", { status: 401 })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const api = providedApi();
    await Promise.all([api.listDevices(), api.listJobs(), api.getCurrentOrg()].map((call) => call.catch(() => {})));
    await waitFor(() => expect(meCalls(fetchMock)).toBe(1));
    answerMe(new Response("", { status: 401 }));
    await waitFor(() => expect(isSessionEnded()).toBe(true));
    expect(meCalls(fetchMock)).toBe(1);
  });
});

/**
 * THE KEEP-ALIVE NEEDS A SESSION TO KEEP (fix round 1 re-review, R1-M3). It
 * is mounted at the app's root, so it also hears the sign-in, invite and
 * recover pages, where there is none: the first keystroke of a password sent
 * `GET /me`, its 401 sent the confirming `/me`, and the sign-in tab was
 * marked ended and told every other tab, once a minute of typing.
 */
describe("the keep-alive, with no session", () => {
  function stubAnonymousServer(): ReturnType<typeof vi.fn> {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/auth/invite/details")) {
        return new Response(JSON.stringify({ email: "dana@example.test", display_name: "Dana Okafor" }), { status: 200 });
      }
      if (url.endsWith("/api/v1/auth/recover/details")) return new Response(JSON.stringify({ email: "owner@example.test" }), { status: 200 });
      if (url.endsWith("/api/v1/me")) return new Response(JSON.stringify({ code: "auth_required" }), { status: 401 });
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }
  const asked = (fetchMock: ReturnType<typeof vi.fn>, path: string) =>
    fetchMock.mock.calls.filter(([url]) => String(url).endsWith(path)).length;

  function page(path: string) {
    const url = new URL(path, "https://itms.example");
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={queryClient}>
        <ApiProvider>
          <MemoryRouter initialEntries={[{ pathname: url.pathname, hash: url.hash }]}>
            <Routes>
              <Route path="/sign-in" element={<SignInPage />} />
              <Route path="/invite" element={<InvitePage />} />
              <Route path="/recover" element={<RecoverPage />} />
            </Routes>
          </MemoryRouter>
        </ApiProvider>
      </QueryClientProvider>,
    );
  }

  it.each([
    ["the sign-in page", "/sign-in", "Password"],
    ["the invite page", "/invite#tok-invite-1", "New password"],
    ["the recover page", "/recover#tok-recover-1", null],
  ])("sends no /me while a person types on %s", async (_name, path, field) => {
    const fetchMock = stubAnonymousServer();
    const user = userEvent.setup();
    const { container } = page(path);
    if (field !== null) await user.type(await screen.findByLabelText(field), "correct horse battery staple");
    else {
      await screen.findByRole("button", { name: "Recover my account" });
      await user.keyboard("{Tab}abc");
    }
    await user.pointer({ keys: "[MouseLeft]", target: container });
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(asked(fetchMock, "/api/v1/me")).toBe(0);
    expect(isSessionEnded()).toBe(false);
  });

  it("sends one once there is a session (non-vacuity for the above)", async () => {
    const fetchMock = stubAnonymousServer();
    sessionPerson("u1");
    const user = userEvent.setup();
    page("/sign-in");
    await user.type(await screen.findByLabelText("Password"), "x");
    await waitFor(() => expect(asked(fetchMock, "/api/v1/me")).toBeGreaterThan(0));
  });

  it("sends none once the session has ended: the notice is not a session", async () => {
    const fetchMock = stubAnonymousServer();
    sessionPerson("u1");
    sessionEnded();
    const user = userEvent.setup();
    page("/sign-in");
    await user.type(await screen.findByLabelText("Password"), "x");
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(asked(fetchMock, "/api/v1/me")).toBe(0);
  });
});
