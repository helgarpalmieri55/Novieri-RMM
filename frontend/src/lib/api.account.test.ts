/**
 * The account routes (own authentication design §2, §7) and `X-Client-Id`.
 *
 * YOUR OWN ACCOUNT IS NOT A CLIENT'S. `/me/*` and `/auth/step-up/*` are the
 * person's own rows -- their password, factors, remembered devices and
 * sessions -- and a client chosen in the top bar says nothing about them, so
 * those requests never carry the header (`RequestRoute.global`).
 *
 * THE ADMINISTRATORS' ROUTES ARE CONFIGURATION EVERY CLIENT SHARES. Inviting,
 * resending, cancelling, resetting someone's factors, signing them out and the
 * session settings need their code on every client (`GLOBAL_WRITES`,
 * `backend/tests/test_global_writes.py`), so they are in `globalRoutes.json`:
 * narrowed, the server answers 403 to the very person allowed to act.
 */

import type { AuthenticationResponseJSON } from "@simplewebauthn/browser";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "./api";
import { isGlobalRoute } from "./globalRoutes";

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";

const ASSERTION: AuthenticationResponseJSON = {
  id: "Y3JlZA",
  rawId: "Y3JlZA",
  response: { clientDataJSON: "e30", authenticatorData: "YXV0aA", signature: "c2ln" },
  clientExtensionResults: {},
  type: "public-key",
};

function stubFetch(): ReturnType<typeof vi.fn> {
  const fetchMock = vi
    .fn()
    .mockImplementation(() => Promise.resolve(new Response("{}", { status: 200 })));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** The nth `fetch`: its request line, its headers and its body. */
function sent(fetchMock: ReturnType<typeof vi.fn>, call: number): { line: string; headers: Headers; body: unknown } {
  const [url, init] = fetchMock.mock.calls[call] as [string, RequestInit];
  return {
    line: `${init.method ?? "GET"} ${url}`,
    headers: new Headers(init.headers),
    body: typeof init.body === "string" ? JSON.parse(init.body) : undefined,
  };
}

/** A console with Acme chosen in the top bar. */
function narrowed(): ApiClient {
  return new ApiClient(vi.fn(), "/api/v1", () => ACME);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("your own account is never narrowed to a client", () => {
  it("asks each route by its own method and path, and none of them carries X-Client-Id", async () => {
    const fetchMock = stubFetch();
    const api = narrowed();
    await api.getMySecurity();
    await api.changePassword({ current: "old phrase here", new: "a much longer phrase" });
    await api.removePasskey("pk 1");
    await api.removeTotp();
    await api.regenerateRecoveryCodes();
    await api.revokeTrustedDevice("td-1");
    await api.endMySession("s-1");
    await api.stepUpTotp("123456");
    await api.stepUpPasskeyOptions();
    await api.stepUpPasskey(ASSERTION);
    await api.stepUpRecovery("7KQ2M-X9D4P");

    const requests = fetchMock.mock.calls.map((_, at) => sent(fetchMock, at));
    expect(requests.map((request) => request.line)).toEqual([
      "GET /api/v1/me/security",
      "PUT /api/v1/me/password",
      // An id is one path segment, whatever it holds.
      "DELETE /api/v1/me/passkeys/pk%201",
      "DELETE /api/v1/me/totp",
      "POST /api/v1/me/recovery-codes",
      "DELETE /api/v1/me/trusted-devices/td-1",
      "DELETE /api/v1/me/sessions/s-1",
      "POST /api/v1/auth/step-up/totp",
      "POST /api/v1/auth/step-up/passkey/options",
      "POST /api/v1/auth/step-up/passkey",
      "POST /api/v1/auth/step-up/recovery",
    ]);
    for (const request of requests) expect(request.headers.has("X-Client-Id"), request.line).toBe(false);
  });

  it("sends each body as the server reads it", async () => {
    const fetchMock = stubFetch();
    const api = narrowed();
    await api.changePassword({ current: "old phrase here", new: "a much longer phrase" });
    await api.stepUpTotp("123456");
    await api.stepUpPasskey(ASSERTION);
    await api.stepUpRecovery("7KQ2M-X9D4P");
    expect([0, 1, 2, 3].map((at) => sent(fetchMock, at).body)).toEqual([
      { current: "old phrase here", new: "a much longer phrase" },
      { code: "123456" },
      { credential: ASSERTION },
      { code: "7KQ2M-X9D4P" },
    ]);
  });
});

describe("the administrators' account routes are configuration every client shares", () => {
  it("are listed as global, by method and path", () => {
    for (const [method, path] of [
      ["POST", "/users/invite"],
      ["POST", "/users/u-1/invite/resend"],
      ["DELETE", "/users/u-1/invite"],
      ["POST", "/users/u-1/factors/reset"],
      ["DELETE", "/users/u-1/sessions"],
      ["PUT", "/settings/sessions"],
    ] as const) {
      expect(isGlobalRoute(method, path), `${method} ${path}`).toBe(true);
    }
  });

  it("so a client chosen in the top bar does not travel with them", async () => {
    const fetchMock = stubFetch();
    const api = narrowed();
    await api.inviteUser({ email: "new@example.com", display_name: "New", grants: [] });
    await api.resendInvite("u-1");
    await api.cancelInvite("u-1");
    await api.resetUserFactors("u-1");
    await api.endUserSessions("u-1");
    await api.putSessionSettings({
      staff_idle_minutes: 30,
      staff_max_hours: 12,
      remember_device_enabled: true,
      remember_device_days: 15,
      remembered_max_hours: 168,
      contact_idle_minutes: 60,
      contact_max_days: 7,
    });
    const requests = fetchMock.mock.calls.map((_, at) => sent(fetchMock, at));
    expect(requests.map((request) => request.line)).toEqual([
      "POST /api/v1/users/invite",
      "POST /api/v1/users/u-1/invite/resend",
      "DELETE /api/v1/users/u-1/invite",
      "POST /api/v1/users/u-1/factors/reset",
      "DELETE /api/v1/users/u-1/sessions",
      "PUT /api/v1/settings/sessions",
    ]);
    for (const request of requests) expect(request.headers.has("X-Client-Id"), request.line).toBe(false);
  });

  it("reads the session settings from their own route", async () => {
    const fetchMock = stubFetch();
    await narrowed().getSessionSettings();
    expect(sent(fetchMock, 0).line).toBe("GET /api/v1/settings/sessions");
  });
});
