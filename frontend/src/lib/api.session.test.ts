/**
 * What every request carries now that the session is a cookie (own-auth
 * design §1.1, §6.4).
 *
 * THREE PROPERTIES, EACH A LINE IN `api.ts` THAT A TEST HERE FAILS WITHOUT:
 *
 * 1. `X-ITMS-Request: 1` on every request, the raw file ones included
 *    (`requestHeaders`' `headers.set(CSRF_HEADER, "1")`). The server refuses a
 *    write carried by the session cookie without it -- 403 `{"code": "csrf"}`
 *    -- because a page on another site can make a browser send the cookie
 *    but cannot add a header of its own without a preflight nobody answers.
 *    Without the line, every write the console makes is refused.
 * 2. `credentials: "same-origin"` on every `fetch`, so the cookie travels and
 *    nothing can widen it to another origin.
 * 3. No `Authorization`: there is no token to send.
 *
 * And the one new semantic: a 401 on a request made with the session tells
 * `onUnauthorized` (the session ended); a 401 on a sign-in route
 * (`anonymous`) is that route's own answer and keeps the server's body.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError, CSRF_HEADER, apiErrorCode, requestHeaders } from "./api";
import { setSelectedClient } from "./clientChoice";

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";

function stubFetch(response: () => Response = () => new Response("{}", { status: 200 })): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(response()));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function init(fetchMock: ReturnType<typeof vi.fn>, call = 0): RequestInit {
  return (fetchMock.mock.calls[call] as [string, RequestInit])[1];
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the console's request mark", () => {
  it("is named as the server names it", () => {
    expect(CSRF_HEADER).toBe("X-ITMS-Request");
  });

  it("is on every header set the builder makes, whatever the caller passed", () => {
    expect(requestHeaders().get("X-ITMS-Request")).toBe("1");
    expect(requestHeaders({ "X-ITMS-Request": "0" }).get("X-ITMS-Request")).toBe("1");
    expect(requestHeaders({ Accept: "application/json" }).get("Accept")).toBe("application/json");
  });

  it("travels on a JSON write, a raw upload and a raw download", async () => {
    const fetchMock = stubFetch();
    const api = new ApiClient(vi.fn());
    await api.updateMe({ locale: "es" });
    await api.uploadPackageArtifact("p1", new File(["x"], "agent.msi"));
    await api.exportPatchReport();
    for (const call of [0, 1, 2]) {
      expect(new Headers(init(fetchMock, call).headers).get("X-ITMS-Request")).toBe("1");
    }
  });
});

describe("the session cookie", () => {
  it("is sent same-origin, with no bearer token, on JSON and raw requests alike", async () => {
    const fetchMock = stubFetch();
    const api = new ApiClient(vi.fn());
    await api.me();
    await api.uploadPackageArtifact("p1", new File(["x"], "agent.msi"));
    for (const call of [0, 1]) {
      expect(init(fetchMock, call).credentials).toBe("same-origin");
      expect(new Headers(init(fetchMock, call).headers).has("Authorization")).toBe(false);
    }
  });

  it("is asked for by every fetch in api.ts", () => {
    // A raw path that forgot it would still work today -- `same-origin` is
    // the browser's default -- and silently stop the day someone sets a
    // default of their own. Read from the source, the way
    // `api.clientScope.test.ts` holds every call to its route.
    const source = readFileSync(resolve(import.meta.dirname, "api.ts"), "utf8");
    const calls: string[] = [];
    for (const found of source.matchAll(/(?<![\w.])fetch\(/g)) {
      const at = found.index;
      let depth = 0;
      let end = at + "fetch".length;
      for (; end < source.length; end += 1) {
        if (source[end] === "(") depth += 1;
        if (source[end] === ")") {
          depth -= 1;
          if (depth === 0) break;
        }
      }
      calls.push(source.slice(at, end + 1));
    }
    expect(calls.length).toBeGreaterThanOrEqual(18);
    expect(calls.filter((call) => !call.includes('credentials: "same-origin"'))).toEqual([]);
  });
});

describe("a 401", () => {
  it("on a request made with the session tells onUnauthorized", async () => {
    stubFetch(() => new Response('{"code":"auth_required"}', { status: 401 }));
    const onUnauthorized = vi.fn();
    await expect(new ApiClient(onUnauthorized).listDevices()).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("on signing out is the route's own answer: onUnauthorized is not told, the body is kept", async () => {
    const fetchMock = stubFetch(() => new Response('{"code":"auth_required"}', { status: 401 }));
    const onUnauthorized = vi.fn();
    const failure = await new ApiClient(onUnauthorized).logout().catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).message).toBe('{"code":"auth_required"}');
    expect(onUnauthorized).not.toHaveBeenCalled();
    const [url, sent] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/auth/logout");
    expect(sent.method).toBe("POST");
  });
});

describe("a sign-in route", () => {
  it("carries no X-Client-Id with a client chosen: signing in belongs to no client", async () => {
    const fetchMock = stubFetch(() => new Response(null, { status: 204 }));
    setSelectedClient(ACME);
    await new ApiClient(vi.fn(), "/api/v1", () => ACME).logout();
    expect(new Headers(init(fetchMock).headers).has("X-Client-Id")).toBe(false);
  });

  it("reads an empty success as nothing rather than failing on it", async () => {
    stubFetch(() => new Response("", { status: 200 }));
    await expect(new ApiClient(vi.fn()).logout()).resolves.toBeUndefined();
  });
});

describe("a refusal's code (own-auth Task 6)", () => {
  it("is read from the door's body and from FastAPI's, and is null otherwise", () => {
    expect(apiErrorCode(new ApiError(403, '{"code":"step_up_required"}'))).toBe("step_up_required");
    expect(apiErrorCode(new ApiError(403, '{"detail":{"code":"csrf"}}'))).toBe("csrf");
    expect(apiErrorCode(new ApiError(502, "<html>bad gateway</html>"))).toBeNull();
    expect(apiErrorCode(new TypeError("Failed to fetch"))).toBeNull();
  });

  it("of a 403 -- csrf or step_up_required -- is the caller's to act on, and ends no session", async () => {
    for (const code of ["csrf", "step_up_required"]) {
      stubFetch(() => new Response(JSON.stringify({ code }), { status: 403 }));
      const onUnauthorized = vi.fn();
      const failure = await new ApiClient(onUnauthorized).updateMe({ locale: "es" }).catch((error: unknown) => error);
      expect(apiErrorCode(failure)).toBe(code);
      expect(onUnauthorized).not.toHaveBeenCalled();
    }
  });
});
