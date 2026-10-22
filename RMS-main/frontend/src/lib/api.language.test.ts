import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "./api";
import { setActiveLanguage } from "../i18n/core";

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) return walk(p);
    return /\.(ts|tsx)$/.test(name) && !/\.test\./.test(name) ? [p] : [];
  });
}

describe("Accept-Language", () => {
  afterEach(() => { vi.restoreAllMocks(); setActiveLanguage("en"); });

  it("is sent on every request, in the active language", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status: 200 }));
    setActiveLanguage("es");
    await new ApiClient(() => "tok", () => {}).me();
    const headers = new Headers(fetchMock.mock.calls[0]![1]!.headers);
    expect(headers.get("Accept-Language")).toBe("es");
  });

  it("no source file builds request headers without the shared builder", () => {
    // Every `new Headers(` outside the builder is a raw fetch path that would
    // silently send no language.
    const offenders = walk("src").filter((file) => {
      const text = readFileSync(file, "utf8");
      return /new Headers\(/.test(text) && !/requestHeaders\(/.test(text);
    });
    expect(offenders).toEqual([]);
  });

  it("every fetch( call sits in a function that builds its headers with requestHeaders(", () => {
    // A file that mentions the builder once says nothing about its other
    // fetches. For each `fetch(` look back to the enclosing method or
    // function start (a line opening one at the file's method indent) and
    // require the builder between that start and the call.
    const offenders: string[] = [];
    for (const file of walk("src").filter((f) => !/__tests__/.test(f))) {
      const lines = readFileSync(file, "utf8").split("\n");
      lines.forEach((line, i) => {
        if (!/(^|[^.\w])fetch\(/.test(line) || /^\s*(\/\/|\*)/.test(line)) return;
        let start = i;
        while (start > 0 && !/^( {2})?(export |private |public |async |function )[^=]*\(.*\{\s*$|^ {2}(private |public )?(async )?\w+(<[^>]*>)?\(.*\{\s*$|^(export )?(async )?function /.test(lines[start]!)) start -= 1;
        const body = lines.slice(start, i + 3).join("\n");
        if (!/requestHeaders\(/.test(body)) offenders.push(`${file}:${i + 1}`);
      });
    }
    expect(offenders).toEqual([]);
  });
});
