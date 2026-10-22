/**
 * The design tokens in `src/index.css` are a TRANSCRIPTION of the approved
 * prototype, and this test is what makes that a fact rather than an intention.
 *
 * It parses the `:root` block out of BOTH files -- `docs/design/itms-prototype-v3.html`
 * (the operator's design, approved 2026-09-07, whose `:root` is one declaration
 * per line for exactly this reason) and `src/index.css` -- and requires them to
 * be identical, property for property and value for value.
 *
 * ONLY THE FIRST `:root` OF EACH FILE. `index.css` carries a second block of
 * legacy aliases (`--red: var(--sev-crit)` and so on) so the modules keep
 * resolving while they migrate; that block is a compatibility surface, and
 * this test reads past it on purpose -- an alias is a reference, not a colour,
 * and the "invents no colour" check below is what keeps it that way.
 *
 * WHY THIS EXISTS. "Keep the colors on the html file" is a requirement, and
 * requirements that live only in a reviewer's memory are the ones that rot.
 * Someone nudging `--primary` a shade to fix a contrast complaint, or a later
 * prototype revision landing while the app keeps the old values, both produce
 * a UI that is *nearly* the approved design -- the failure mode nobody
 * notices until the operator does.
 *
 * The comparison is deliberately strict about VALUES and lenient about
 * FORMATTING: whitespace inside a value is collapsed (`0 12px 32px rgba(27,
 * 22, 41, .07)` may be re-indented by a formatter) but `.07` is not treated as
 * equal to `0.07`, because that is a change to the file, and this test's job
 * is to notice changes.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// `import.meta.dirname` rather than `__dirname`: this file is ESM, and the
// CommonJS global does not exist under Vite's transform.
const REPO = resolve(import.meta.dirname, "../../..");
const PROTOTYPE = resolve(REPO, "docs/design/itms-prototype-v3.html");
const APP_CSS = resolve(REPO, "frontend/src/index.css");

/** Pull the FIRST `:root { ... }` block out of a file and parse its declarations. */
function rootTokens(path: string): Map<string, string> {
  const source = readFileSync(path, "utf8");
  const start = source.indexOf(":root {");
  if (start === -1) throw new Error(`no ':root {' block in ${path}`);
  const end = source.indexOf("}", start);
  if (end === -1) throw new Error(`unterminated ':root' block in ${path}`);

  const tokens = new Map<string, string>();
  for (const line of source.slice(start + ":root {".length, end).split("\n")) {
    const declaration = line.trim().replace(/;$/, "");
    if (!declaration.startsWith("--")) continue;
    const colon = declaration.indexOf(":");
    if (colon === -1) continue;
    const name = declaration.slice(0, colon).trim();
    const value = declaration.slice(colon + 1).trim().replace(/\s+/g, " ");
    tokens.set(name, value);
  }
  return tokens;
}

describe("design tokens", () => {
  const design = rootTokens(PROTOTYPE);
  const app = rootTokens(APP_CSS);

  it("the reference actually parsed, so the comparisons below mean something", () => {
    // NON-VACUITY. An empty map compares equal to an empty map, and every
    // assertion in this file would pass against a prototype that failed to
    // parse -- or one someone deleted.
    expect(design.size).toBeGreaterThan(25);
    expect(design.get("--accent")).toBe("#1f5fd1");
    expect(design.get("--t-body")).toBe("13.5px");
  });

  it("defines every token the design defines", () => {
    const missing = [...design.keys()].filter((name) => !app.has(name));
    expect(missing, `tokens in the design but not in index.css: ${missing.join(", ")}`).toEqual(
      [],
    );
  });

  it("carries the design's value for every one of them", () => {
    const drifted = [...design.entries()]
      .filter(([name]) => app.has(name))
      .filter(([name, value]) => app.get(name) !== value)
      .map(([name, value]) => `${name}: design=${value} app=${app.get(name)}`);
    expect(drifted, `these no longer match the approved design: ${drifted.join(" | ")}`).toEqual(
      [],
    );
  });

  it("invents no colour of its own", () => {
    // A token in the app that the design does not have is how a palette grows
    // sideways. Non-colour additions are fine -- the app legitimately needs
    // layout variables the mock never had -- so this is scoped to values that
    // look like colours.
    const invented = [...app.entries()]
      .filter(([name]) => !design.has(name))
      .filter(([, value]) => /^#|^rgb|^hsl/.test(value))
      .map(([name, value]) => `${name}: ${value}`);
    expect(invented, `colours not in the approved design: ${invented.join(", ")}`).toEqual([]);
  });

  it("carries the three edits the port made to the design file, in the design file", () => {
    // Spec §2 names three deliberate edits to v3 and requires each to be made
    // in the DESIGN first. Reading them from the design rather than from
    // index.css is the point: if someone "fixes" the app and forgets the
    // design, the transcription test above fails; if someone reverts the
    // design, this one does.
    expect(design.get("--line-control")).toBe("#828895");
    expect(design.get("--sans")?.startsWith("Inter,")).toBe(true);
    // `--rail-dim` is the group captions' ink and must be text-legible on the
    // rail; the ratio itself is computed in contrast.test.ts. Here: it is not
    // the value v3 shipped with, which measured 4.44:1.
    expect(design.get("--rail-dim")).not.toBe("#6d838c");
  });

  it("names the KPI figure's size, the one size outside the five-step ramp", () => {
    expect(design.get("--t-kpi")).toBe("26px");
    expect(app.get("--t-kpi")).toBe("26px");
  });

  it("commits to the design's colour scheme rather than claiming dark support", () => {
    // The prototype is light-only. Declaring `light dark` with no dark palette
    // makes the browser render form controls and scrollbars dark against a
    // light page -- see the UI port spec §3.1.
    const css = readFileSync(APP_CSS, "utf8");
    expect(css).toContain("color-scheme: light;");
    expect(css).not.toContain("color-scheme: light dark");
  });

  it("fetches no font or stylesheet from a third party", () => {
    // The platform does not phone home (`docs/product.md` §5), and many
    // deployments have no egress at all. A webfont link would break both.
    const css = readFileSync(APP_CSS, "utf8");
    const external = css.match(/@import\s+url\(|fonts\.googleapis|fonts\.gstatic|https?:\/\//g);
    expect(external, `index.css reaches off-origin: ${external?.join(", ")}`).toBeNull();
  });
});
