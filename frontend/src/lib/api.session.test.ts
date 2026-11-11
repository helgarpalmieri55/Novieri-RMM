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
 * And the background mark, `X-ITMS-Background: 1` (`describe("the background
 * mark")`): pinned to the server's own name and value, and set on every raw
 * `fetch` under `src/` by the one builder.
 *
 * And the one new semantic: a 401 on a request made with the session tells
 * `onUnauthorized` (the session ended); a 401 on a sign-in route
 * (`anonymous`) is that route's own answer and keeps the server's body.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError, BACKGROUND_HEADER, CSRF_HEADER, apiErrorCode, requestHeaders } from "./api";
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

/**
 * THE BACKGROUND MARK (final re-review, P1; fix round 1 re-review, R1-M1 and
 * R1-M2). `X-ITMS-Background: 1` tells the server a request was not a
 * person's, so it does not renew the session. Two silent ways to lose it:
 * the name or value drifting from the server's -- every request renews again,
 * and both suites stay green -- and a raw `fetch` that bypasses the one
 * builder that sets it.
 */
describe("the background mark", () => {
  const SESSIONS_PY = resolve(import.meta.dirname, "../../../backend/app/core/auth/sessions.py");

  it("is named, and valued, exactly as the server reads it", () => {
    expect(BACKGROUND_HEADER).toBe("X-ITMS-Background");
    const server = readFileSync(SESSIONS_PY, "utf8");
    const name = /^BACKGROUND_HEADER = "([^"]+)"$/m.exec(server)?.[1];
    const value = /getlist\(BACKGROUND_HEADER\) == \["([^"]+)"\]/.exec(server)?.[1];
    expect(name, `no BACKGROUND_HEADER constant in ${SESSIONS_PY}`).toBeDefined();
    expect(value, `no background() comparison in ${SESSIONS_PY}`).toBeDefined();
    expect(BACKGROUND_HEADER).toBe(name);
    // No input yet in this test, so the builder marks the request.
    expect(requestHeaders().get(BACKGROUND_HEADER)).toBe(value);
  });

  /**
   * Raw `fetch` calls allowed to go without the builder, with the reason. A
   * new one fails until it is reviewed here.
   */
  const UNBUILT_FETCHES: Record<string, string> = {};

  /** The console's own source under `src/`, tests and their helpers aside. */
  function consoleSources(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) return name === "__tests__" || name === "test" ? [] : consoleSources(path);
      return /\.tsx?$/.test(name) && !/\.test\./.test(name) ? [path] : [];
    });
  }

  /** The source with its comments blanked: a `fetch (` in prose is not a call. */
  function code(text: string): string {
    return text.replace(/(^|\n)[ \t]*\/\*[\s\S]*?\*\//g, "$1").replace(/(^|[\s;,{}()\]])\/\/[^\n]*/g, "$1");
  }

  /**
   * Whether a call's headers come from `requestHeaders(`: written in the call,
   * or passed as a name whose nearest declaration before the call is
   * `= requestHeaders(` and that nothing reassigns or deletes from on the way
   * -- the shape every raw request in `api.ts` has.
   */
  function builtHeaders(text: string, at: number, call: string): boolean {
    if (/\bheaders:\s*requestHeaders\(/.test(call)) return true;
    const passed = /\bheaders(?:\s*:\s*([\w$]+))?\s*[,}]/.exec(call);
    if (passed === null) return false;
    const name = passed[1] ?? "headers";
    const before = text.slice(0, at);
    const declared = [...before.matchAll(new RegExp(`\\b(?:const|let|var)\\s+${name}\\b\\s*(?::[^=;]+)?=\\s*`, "g"))].at(-1);
    if (declared === undefined) return false;
    const rest = before.slice(declared.index + declared[0].length);
    if (!rest.startsWith("requestHeaders(")) return false;
    return !new RegExp(`\\b${name}\\s*=(?!=)|\\b${name}\\.delete\\(`).test(rest);
  }

  /** `text`: given for the probes below; otherwise the console's own source. */
  function fetchCalls(sources?: readonly { file: string; text: string }[]): { file: string; call: string; built: boolean }[] {
    const root = resolve(import.meta.dirname, "..");
    const files = sources ?? consoleSources(root).map((path) => ({ file: relative(root, path), text: readFileSync(path, "utf8") }));
    return files.flatMap(({ file, text: raw }) => {
      const text = code(raw);
      return [...text.matchAll(/(?<![\w$.])(?:(?:window|globalThis|self)\.)?fetch\s*\(/g)].map((found) => {
        let depth = 0;
        let end = found.index + found[0].length - 1;
        for (; end < text.length; end += 1) {
          if (text[end] === "(") depth += 1;
          if (text[end] === ")") {
            depth -= 1;
            if (depth === 0) break;
          }
        }
        const call = text.slice(found.index, end + 1);
        return { file, call, built: builtHeaders(text, found.index, call) };
      });
    });
  }

  const unbuilt = (calls: ReturnType<typeof fetchCalls>) =>
    calls.filter(({ built }) => !built).map(({ file, call }) => `${file}: ${call.split("\n")[0]}`);

  it("is set on every raw fetch in the console, by the one builder that sets it", () => {
    const calls = fetchCalls();
    // Non-vacuity: api.ts's own, SetupPage's two and the session check.
    expect(calls.length).toBeGreaterThanOrEqual(21);
    expect(calls.map(({ file }) => file)).toEqual(expect.arrayContaining(["lib/sessionCheck.ts", "modules/setup/SetupPage.tsx"]));
    const found = unbuilt(calls);
    expect(found.filter((entry) => !(entry.split(":")[0]! in UNBUILT_FETCHES))).toEqual([]);
    expect(Object.keys(UNBUILT_FETCHES).filter((file) => !found.some((entry) => entry.startsWith(`${file}:`)))).toEqual([]);
  });

  it("goes red on a fetch whose headers are not the builder's (non-vacuity for the scan)", () => {
    const probes = [
      { file: "a.ts", text: 'await fetch("/api/v1/me", { credentials: "same-origin" });' },
      { file: "b.ts", text: 'await fetch("/api/v1/me", { headers: { Accept: "application/json" } });' },
      { file: "c.ts", text: 'const headers = new Headers();\nawait fetch("/api/v1/me", { headers });' },
      { file: "d.ts", text: 'let headers = requestHeaders();\nheaders = new Headers();\nawait fetch("/api/v1/me", { headers });' },
      { file: "e.ts", text: 'const headers = requestHeaders();\nheaders.delete("X-ITMS-Background");\nawait fetch("/x", { headers });' },
      { file: "f.ts", text: 'await window.fetch("/api/v1/me");' },
    ];
    expect(unbuilt(fetchCalls(probes)).map((entry) => entry.split(":")[0])).toEqual(["a.ts", "b.ts", "c.ts", "d.ts", "e.ts", "f.ts"]);
    const fine = [
      { file: "g.ts", text: 'await fetch("/x", { headers: requestHeaders({ Accept: "application/json" }) });' },
      { file: "h.ts", text: 'const headers = requestHeaders(undefined, route);\nconst r = await fetch(url, { ...init, headers, credentials: "same-origin" });' },
      { file: "i.ts", text: '// a comment that says fetch (`elsewhere`)\nconst x = 1;' },
    ];
    expect(unbuilt(fetchCalls(fine))).toEqual([]);
  });

  it("has no other way out to the server: no XMLHttpRequest, sendBeacon or EventSource", () => {
    const root = resolve(import.meta.dirname, "..");
    const found = consoleSources(root).flatMap((path) =>
      /\bXMLHttpRequest\b|\bsendBeacon\b|\bEventSource\b/.test(readFileSync(path, "utf8")) ? [relative(root, path)] : [],
    );
    expect(found).toEqual([]);
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
