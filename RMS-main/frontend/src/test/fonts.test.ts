/**
 * Guards the self-hosted Inter typeface (UI port spec §3.2).
 *
 * WHY THIS EXISTS. `fonts.css` is hand-written, not generated, so nothing
 * stops a future edit from pointing a `src:` at Google Fonts "just for one
 * environment" or "temporarily" -- exactly the fix the spec forbids, because
 * this platform does not phone home (`docs/product.md` §5) and frequently
 * runs on networks with no egress at all. `tokens.test.ts` already asserts
 * `index.css` reaches no external origin; this file makes the same promise
 * about `fonts.css` and adds the checks specific to shipping real font
 * files: they exist, they are not placeholders, and the license that
 * permits bundling them ships too.
 *
 * NON-VACUITY. A test that greps a file for a pattern and asserts "no
 * matches" passes just as happily against an empty file, a deleted file, or
 * a glob that resolved to nothing -- forever, silently. Every check below
 * that could pass vacuously is paired with a positive assertion that proves
 * the thing it is checking actually exists first.
 */

import { readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const REPO = resolve(import.meta.dirname, "../../..");
const FRONTEND = resolve(REPO, "frontend");
const FONTS_CSS = resolve(FRONTEND, "src/fonts.css");
const FONTS_DIR = resolve(FRONTEND, "src/assets/fonts");
const LICENSE = resolve(FONTS_DIR, "OFL.txt");

const WOFF2_FILES = [
  resolve(FONTS_DIR, "inter-latin-wght-normal.woff2"),
  resolve(FONTS_DIR, "inter-latin-ext-wght-normal.woff2"),
];

// A byte floor well under real Inter subsets (tens of KB) but far above
// anything a zero-byte or truncated placeholder file could reach, so a
// broken download fails loudly here instead of shipping silently.
const MIN_WOFF2_BYTES = 10_000;

describe("self-hosted Inter", () => {
  const css = readFileSync(FONTS_CSS, "utf8");

  it("declares at least one @font-face for Inter", () => {
    // NON-VACUITY for every check below that scans `css` for `@font-face`
    // properties: if this count were ever 0 (file emptied, renamed, the
    // `@font-face` blocks deleted), the "no remote src" and "font-display"
    // assertions would pass on nothing rather than catch anything.
    const matches = css.match(/@font-face\s*\{/g) ?? [];
    expect(matches.length).toBeGreaterThan(0);
  });

  it("names Inter as the font-family", () => {
    expect(css).toMatch(/font-family:\s*"Inter"/);
  });

  it("every font-display is swap", () => {
    const declared = css.match(/font-display:\s*([\w-]+)/g) ?? [];
    expect(declared.length).toBeGreaterThan(0); // non-vacuity
    for (const decl of declared) {
      expect(decl).toBe("font-display: swap");
    }
  });

  it("ships a unicode-range for each @font-face, so subsets don't overlap blindly", () => {
    // Matches actual rule openers (`@font-face {`), not the prose above that
    // also says the words "@font-face" and "unicode-range" while explaining
    // why they're there -- a bare `/@font-face/` would double-count those.
    const faces = css.match(/@font-face\s*\{/g) ?? [];
    const ranges = css.match(/unicode-range:/g) ?? [];
    expect(faces.length).toBeGreaterThan(0); // non-vacuity
    expect(ranges.length).toBe(faces.length);
  });

  it("references only same-origin relative paths -- no remote src, anywhere in the file", () => {
    // Deliberately scans the WHOLE file, not just `src:` lines: a remote
    // reference in a comment is harmless, but a regex scoped too narrowly to
    // `src:` would also miss a sneaky `@import url(https://...)` above the
    // @font-face blocks. Same shape of check as tokens.test.ts's "fetches no
    // font or stylesheet from a third party".
    const remote = css.match(/https?:\/\/|\/\/[a-z0-9.-]+\.[a-z]{2,}|@import\s+url\(/gi);
    expect(remote, `fonts.css reaches off-origin: ${remote?.join(", ")}`).toBeNull();
  });

  it("every url() in fonts.css points at a relative ./assets/fonts/*.woff2 file", () => {
    const urls = [...css.matchAll(/url\(["']?([^"')]+)["']?\)/g)].map((m) => m[1] ?? "");
    expect(urls.length).toBeGreaterThan(0); // non-vacuity
    for (const url of urls) {
      expect(url.startsWith("./assets/fonts/")).toBe(true);
      expect(url.endsWith(".woff2")).toBe(true);
    }
  });

  it("index.css imports fonts.css by a plain relative import, not @import url(...)", () => {
    const indexCss = readFileSync(resolve(FRONTEND, "src/index.css"), "utf8");
    expect(indexCss).toContain('@import "./fonts.css";');
  });

  it("every woff2 file the stylesheet references exists on disk and is not a placeholder", () => {
    expect(WOFF2_FILES.length).toBeGreaterThan(0); // non-vacuity
    for (const file of WOFF2_FILES) {
      const stat = statSync(file); // throws (fails the test) if missing
      expect(stat.isFile()).toBe(true);
      expect(
        stat.size,
        `${file} is ${stat.size} bytes -- looks like a placeholder, not a real font`,
      ).toBeGreaterThan(MIN_WOFF2_BYTES);
    }
  });

  it("the OFL license text ships alongside the fonts", () => {
    const stat = statSync(LICENSE); // throws if missing
    expect(stat.isFile()).toBe(true);
    const text = readFileSync(LICENSE, "utf8");
    expect(text).toContain("SIL OPEN FONT LICENSE");
    expect(text).toContain("Inter Project Authors");
  });
});
