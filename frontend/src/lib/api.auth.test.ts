/**
 * The wire the sign-in pages speak (own-auth plan, Tasks 7 and 8): path,
 * method and body of every `ApiClient` method of Task 11, and the two
 * properties they share -- a 401 is the step's answer, not a session that
 * ended (`anonymous`), and none carries `X-Client-Id`.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError } from "./api";

const CLIENT = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";

function stubFetch(status = 200, body: unknown = {}): ReturnType<typeof vi.fn> {
  const fetchMock = vi
    .fn()
    .mockImplementation(() => Promise.resolve(new Response(body === null ? "" : JSON.stringify(body), { status })));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const CREDENTIAL = { id: "Y3JlZA", rawId: "Y3JlZA", type: "public-key", response: {}, clientExtensionResults: {} } as never;

const CASES: [string, (api: ApiClient) => Promise<unknown>, string, string, unknown][] = [
  ["login", (api) => api.login({ email: "a@b.test", password: "pw" }), "POST", "/api/v1/auth/login", { email: "a@b.test", password: "pw" }],
  ["factorTotp", (api) => api.factorTotp({ code: "123456", remember_device: true }), "POST", "/api/v1/auth/factor/totp", { code: "123456", remember_device: true }],
  ["factorPasskeyOptions", (api) => api.factorPasskeyOptions(), "POST", "/api/v1/auth/factor/passkey/options", undefined],
  ["factorPasskey", (api) => api.factorPasskey({ credential: CREDENTIAL, remember_device: false }), "POST", "/api/v1/auth/factor/passkey", { credential: CREDENTIAL, remember_device: false }],
  ["factorRecovery", (api) => api.factorRecovery({ code: "7K3QD-9XM2P", remember_device: false }), "POST", "/api/v1/auth/factor/recovery", { code: "7K3QD-9XM2P", remember_device: false }],
  ["enrollTotpStart", (api) => api.enrollTotpStart(), "POST", "/api/v1/auth/enroll/totp/start", undefined],
  ["enrollTotpConfirm", (api) => api.enrollTotpConfirm("123456"), "POST", "/api/v1/auth/enroll/totp/confirm", { code: "123456" }],
  ["enrollPasskeyOptions", (api) => api.enrollPasskeyOptions(), "POST", "/api/v1/auth/enroll/passkey/options", undefined],
  ["enrollPasskey", (api) => api.enrollPasskey(CREDENTIAL, "Work laptop"), "POST", "/api/v1/auth/enroll/passkey", { credential: CREDENTIAL, name: "Work laptop" }],
  ["enrollFinish", (api) => api.enrollFinish(), "POST", "/api/v1/auth/enroll/finish", undefined],
  ["forgotPassword", (api) => api.forgotPassword({ email: "a@b.test" }), "POST", "/api/v1/auth/password/forgot", { email: "a@b.test" }],
  ["resetPassword", (api) => api.resetPassword({ token: "t", password: "pw" }), "POST", "/api/v1/auth/password/reset", { token: "t", password: "pw" }],
  ["getInvite", (api) => api.getInvite("tok-1"), "POST", "/api/v1/auth/invite/details", { token: "tok-1" }],
  ["acceptInvite", (api) => api.acceptInvite({ token: "t", password: "pw" }), "POST", "/api/v1/auth/invite/accept", { token: "t", password: "pw" }],
  ["logoutEverywhere", (api) => api.logoutEverywhere(), "POST", "/api/v1/auth/logout-everywhere", undefined],
  ["getRecovery", (api) => api.getRecovery("tok-1"), "POST", "/api/v1/auth/recover/details", { token: "tok-1" }],
  ["acceptRecovery", (api) => api.acceptRecovery({ token: "tok-1" }), "POST", "/api/v1/auth/recover/accept", { token: "tok-1" }],
  // While an emailed link set the password, the recovery asks for a new one (operator decision 2026-11-05).
  ["acceptRecovery with a new password", (api) => api.acceptRecovery({ token: "t", password: "pw" }), "POST", "/api/v1/auth/recover/accept", { token: "t", password: "pw" }],
];

/** The four enrol routes as My security calls them, with the full console session (`EnrollFrom`). */
const SIGNED_IN: [string, (api: ApiClient) => Promise<unknown>, string, string, unknown][] = [
  ["enrollTotpStart", (api) => api.enrollTotpStart({ signedIn: true }), "POST", "/api/v1/auth/enroll/totp/start", undefined],
  ["enrollTotpConfirm", (api) => api.enrollTotpConfirm("123456", { signedIn: true }), "POST", "/api/v1/auth/enroll/totp/confirm", { code: "123456" }],
  ["enrollPasskeyOptions", (api) => api.enrollPasskeyOptions({ signedIn: true }), "POST", "/api/v1/auth/enroll/passkey/options", undefined],
  ["enrollPasskey", (api) => api.enrollPasskey(CREDENTIAL, "Work laptop", { signedIn: true }), "POST", "/api/v1/auth/enroll/passkey", { credential: CREDENTIAL, name: "Work laptop" }],
];

describe("the sign-in routes", () => {
  it.each(CASES)("%s: method, path and body", async (_name, call, method, url, body) => {
    const fetchMock = stubFetch();
    await call(new ApiClient(vi.fn(), "/api/v1", () => CLIENT));
    const [sentUrl, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(sentUrl).toBe(url);
    expect(init.method ?? "GET").toBe(method);
    expect(init.body === undefined ? undefined : JSON.parse(init.body as string)).toEqual(body);
    // Signing in belongs to no client, and every request is the console's own.
    const headers = new Headers(init.headers);
    expect(headers.has("X-Client-Id")).toBe(false);
    expect(headers.get("X-ITMS-Request")).toBe("1");
    expect(init.credentials).toBe("same-origin");
  });

  it.each(CASES)("%s: a 401 is the step's answer, with its body, and no session ends", async (_name, call) => {
    stubFetch(401, { code: "auth_required" });
    const onUnauthorized = vi.fn();
    const failure = await call(new ApiClient(onUnauthorized)).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(401);
    expect(JSON.parse((failure as ApiError).message)).toEqual({ code: "auth_required" });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it.each(SIGNED_IN)("%s from My security: a 401 is the session ending, so it reaches onUnauthorized", async (_name, call) => {
    stubFetch(401, { code: "auth_required" });
    const onUnauthorized = vi.fn();
    const failure = await call(new ApiClient(onUnauthorized)).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(401);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it.each(SIGNED_IN)("%s from My security: same route, same body, still no X-Client-Id", async (_name, call, method, url, body) => {
    const fetchMock = stubFetch();
    await call(new ApiClient(vi.fn(), "/api/v1", () => CLIENT));
    const [sentUrl, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(sentUrl).toBe(url);
    expect(init.method ?? "GET").toBe(method);
    expect(init.body === undefined ? undefined : JSON.parse(init.body as string)).toEqual(body);
    expect(new Headers(init.headers).has("X-Client-Id")).toBe(false);
  });

  it("reads the 202 of a forgotten password with no body as done", async () => {
    stubFetch(202, null);
    await expect(new ApiClient(vi.fn()).forgotPassword({ email: "a@b.test" })).resolves.toBeUndefined();
  });

  it("hands back what the server answered, untouched", async () => {
    const options = { challenge: "c2lnbi1pbg", userVerification: "required", allowCredentials: [] };
    stubFetch(200, options);
    expect(await new ApiClient(vi.fn()).factorPasskeyOptions()).toEqual(options);
    stubFetch(200, { state: "factor_required", methods: ["passkey", "totp"], remember_device_days: 15 });
    expect(await new ApiClient(vi.fn()).login({ email: "a@b.test", password: "pw" })).toEqual({
      state: "factor_required",
      methods: ["passkey", "totp"],
      remember_device_days: 15,
    });
  });
});

/**
 * The consent page's two routes (plan 2026-11-06, Task 4; Appendix A.2): the
 * query travels as the browser had it, in the body, and the request is the
 * person's session's -- no client, and a 401 is that session ending.
 */
const QUERY = "response_type=code&client_id=itms_x&state=s";
const CONSENT_ROUTES: [string, (api: ApiClient) => Promise<unknown>, string, unknown][] = [
  ["oauthAuthorizeDetails", (api) => api.oauthAuthorizeDetails(QUERY), "/api/v1/oauth/authorize/details", { query: QUERY }],
  ["oauthAuthorizeDecision approving", (api) => api.oauthAuthorizeDecision(QUERY, true), "/api/v1/oauth/authorize/decision", { query: QUERY, approve: true }],
  ["oauthAuthorizeDecision declining", (api) => api.oauthAuthorizeDecision(QUERY, false), "/api/v1/oauth/authorize/decision", { query: QUERY, approve: false }],
];

describe("the consent page's routes", () => {
  it.each(CONSENT_ROUTES)("%s: POST, path and body, and no X-Client-Id", async (_name, call, url, body) => {
    const fetchMock = stubFetch();
    await call(new ApiClient(vi.fn(), "/api/v1", () => CLIENT));
    const [sentUrl, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(sentUrl).toBe(url);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual(body);
    expect(new Headers(init.headers).has("X-Client-Id")).toBe(false);
  });

  it.each(CONSENT_ROUTES)("%s: a 401 is the session ending, so it reaches onUnauthorized", async (_name, call) => {
    stubFetch(401, { code: "auth_required" });
    const onUnauthorized = vi.fn();
    const failure = await call(new ApiClient(onUnauthorized)).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });
});
