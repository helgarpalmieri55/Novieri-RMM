/**
 * The handset width, for the one piece of chrome that MOVES at it rather than
 * restyling: the client selector, which the top bar has no room for at 390px
 * (`AppShell`, `shell.css`'s 640px block).
 *
 * jsdom has no `matchMedia`, so the hook answers "not a handset" there --
 * every other shell test keeps the desktop bar it was written against -- and
 * these tests stub one in.
 */

import { act, renderHook } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HANDSET_QUERY, useHandset } from "./handset";

/** A `matchMedia` whose answer the test moves, telling its listeners. */
function stubMedia(initially: boolean) {
  const listeners = new Set<() => void>();
  let matches = initially;
  const queries: string[] = [];
  vi.stubGlobal("matchMedia", (query: string) => {
    queries.push(query);
    return {
      get matches() {
        return matches;
      },
      media: query,
      addEventListener: (_: string, listener: () => void) => listeners.add(listener),
      removeEventListener: (_: string, listener: () => void) => listeners.delete(listener),
    };
  });
  return {
    queries,
    set(next: boolean) {
      matches = next;
      listeners.forEach((listener) => listener());
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useHandset", () => {
  it("is not a handset where the browser cannot say (jsdom has no matchMedia)", () => {
    expect(typeof window.matchMedia).not.toBe("function");
    const { result } = renderHook(() => useHandset());
    expect(result.current).toBe(false);
  });

  it("asks the stylesheet's own breakpoint, and follows it", () => {
    const media = stubMedia(true);
    const { result } = renderHook(() => useHandset());
    expect(result.current).toBe(true);
    expect(new Set(media.queries)).toEqual(new Set([HANDSET_QUERY]));

    act(() => media.set(false));
    expect(result.current).toBe(false);
  });

  it("names the width of the shell.css block that styles the selector in the drawer", () => {
    // One breakpoint, said twice (CSS for the look, JS for the placement):
    // held to the same number here so neither can move alone.
    expect(HANDSET_QUERY).toBe("(max-width: 640px)");
    const css = readFileSync(resolve(import.meta.dirname, "shell.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    const opener = css.indexOf("@media (max-width: 640px) {");
    expect(opener, "no 640px block in shell.css").toBeGreaterThan(-1);
    const next = css.indexOf("@media", opener + 1);
    const block = css.slice(opener, next === -1 ? undefined : next);
    expect(block).toMatch(/\.sidebar-client\s*\{/);
    // The bar's own cap at this width is gone: the selector is not in the bar here.
    expect(block).not.toMatch(/\n\s*\.top-client\s*\{/);
  });
});

describe("the selector in the bar, wider than a handset", () => {
  it("is capped by a rule that outranks `.select-field`'s own width", () => {
    // `.select-field { width: 100%; max-width: 100% }` is in components.css,
    // which is imported AFTER shell.css: a bare `.top-client` ties it on
    // specificity and loses. Measured before this rule: computed max-width
    // `100%`, and in Spanish at 1440px the select took 302px, 98 of them from
    // the search field. Two classes win honestly.
    const css = readFileSync(resolve(import.meta.dirname, "shell.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    const rule = /\n\.top-actions \.top-client\s*\{([^}]*)\}/.exec(css)?.[1];
    expect(rule, "no `.top-actions .top-client` rule in shell.css").toBeDefined();
    expect(rule).toMatch(/max-width:\s*220px/);
    expect(rule).toMatch(/width:\s*auto/);
  });
});
