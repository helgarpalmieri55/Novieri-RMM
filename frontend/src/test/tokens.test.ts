/**
 * THE TOKENS ARE A TRANSCRIPTION, AND THREE FILES MUST AGREE.
 *
 * `DESIGN.md` v4 ("Novieri", 2026-11-23) is the design authority: its YAML
 * frontmatter names every token by its custom-property name without the
 * dashes. The FIRST `:root` of `src/index.css` is the transcription the
 * console paints. `docs/design/marca/novieri-tokens.json` is the brand the
 * operator chose (direction A, "Cordillera"): every value in it has to reach
 * the console exactly, under the token that does its job.
 *
 * The v3 prototype (`docs/design/itms-prototype-v3.html`) was the authority
 * from 2026-09-07 to 2026-11-23 and is history now; nothing here reads it.
 *
 * ONLY THE FIRST `:root` of index.css. The second is the legacy-alias block
 * (`--red: var(--sev-crit)`), a compatibility surface: an alias is a
 * reference, not a colour, and "invents no colour" below keeps it that way.
 * Values are compared without whitespace and case, so a formatter cannot
 * fail this; `.07` is still not `0.07`, because that is a change.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const REPO = resolve(import.meta.dirname, "../../..");
const DESIGN_MD = resolve(REPO, "DESIGN.md");
const BRAND_JSON = resolve(REPO, "docs/design/marca/novieri-tokens.json");
const APP_CSS = resolve(REPO, "frontend/src/index.css");

/** Pull the FIRST `:root { ... }` block out of a stylesheet and parse its declarations. */
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
    tokens.set(declaration.slice(0, colon).trim(), declaration.slice(colon + 1).trim().replace(/\s+/g, " "));
  }
  return tokens;
}

/** The YAML frontmatter of a markdown file, as lines. */
function frontmatter(markdown: string): string[] {
  const body = /^---\n([\s\S]*?)\n---/.exec(markdown)?.[1];
  if (body === undefined) throw new Error("DESIGN.md has no frontmatter");
  return body.split("\n");
}

/** One flat frontmatter block (`colors:`, `rounded:`): key -> value, quotes removed. */
export function frontmatterBlock(markdown: string, block: string): Map<string, string> {
  const out = new Map<string, string>();
  let inside = false;
  for (const line of frontmatter(markdown)) {
    if (/^\S/.test(line)) {
      inside = line.trim() === `${block}:`;
      continue;
    }
    if (!inside) continue;
    const match = /^ {2}([a-z0-9-]+):\s*"?([^"]*?)"?\s*$/.exec(line);
    if (match) out.set(match[1] as string, (match[2] as string).trim());
  }
  return out;
}

/** `typography.<role>.fontSize` for every role. */
function fontSizes(markdown: string): Map<string, string> {
  const out = new Map<string, string>();
  let inside = false;
  let role: string | null = null;
  for (const line of frontmatter(markdown)) {
    if (/^\S/.test(line)) {
      inside = line.trim() === "typography:";
      role = null;
      continue;
    }
    if (!inside) continue;
    const head = /^ {2}([a-z0-9-]+):\s*$/.exec(line);
    if (head) {
      role = head[1] as string;
      continue;
    }
    const size = /^ {4}fontSize:\s*(\S+)\s*$/.exec(line);
    if (size && role) out.set(role, size[1] as string);
  }
  return out;
}

/** `typography.code.fontFamily`: the code stack, which `--mono` transcribes. */
function codeFamily(markdown: string): string {
  let inside = false;
  let role: string | null = null;
  for (const line of frontmatter(markdown)) {
    if (/^\S/.test(line)) {
      inside = line.trim() === "typography:";
      role = null;
      continue;
    }
    if (!inside) continue;
    const head = /^ {2}([a-z0-9-]+):\s*$/.exec(line);
    if (head) {
      role = head[1] as string;
      continue;
    }
    const family = /^ {4}fontFamily:\s*(.+?)\s*$/.exec(line);
    if (family && role === "code") return family[1] as string;
  }
  throw new Error("DESIGN.md names no typography.code.fontFamily");
}

const same = (value: string) => value.toLowerCase().replace(/\s+/g, "");

/** A dotted path into the brand JSON, as a string. */
function brandValue(brand: unknown, path: string): string {
  let at: unknown = brand;
  for (const part of path.split(".")) {
    if (typeof at !== "object" || at === null || !(part in at)) throw new Error(`novieri-tokens.json has no ${path}`);
    at = (at as Record<string, unknown>)[part];
  }
  if (typeof at !== "string") throw new Error(`novieri-tokens.json ${path} is not a string`);
  return at;
}

/** Every `#rrggbb` under the brand's `color` subtree. */
function brandColours(node: unknown): string[] {
  if (typeof node === "string") return /^#[0-9a-f]{6}$/i.test(node) ? [node] : [];
  if (typeof node !== "object" || node === null) return [];
  return Object.values(node).flatMap(brandColours);
}

/**
 * WHERE EACH BRAND VALUE LANDS: the token that does the job the brand names.
 * The brand's `rail.activeBackground` equals its `rail.hover` and lands on the
 * same `--rail-2`; `severity.info` is the accent; `brand.*` are the logo's own
 * colours, which are the accent, the ink, the muted ink and white.
 */
const BRAND_MAP: ReadonlyArray<readonly [string, string]> = [
  ["--bg", "color.page.background"],
  ["--surface", "color.page.surface"],
  ["--surface-2", "color.page.surfaceHover"],
  ["--line", "color.page.border"],
  ["--line-control", "color.page.borderControl"],
  ["--ink", "color.page.text"],
  ["--muted", "color.page.textSecondary"],
  ["--rail", "color.rail.background"],
  ["--rail-2", "color.rail.hover"],
  ["--rail-2", "color.rail.activeBackground"],
  ["--rail-ink", "color.rail.text"],
  ["--rail-dim", "color.rail.textDim"],
  ["--rail-mark", "color.rail.activeMark"],
  ["--accent", "color.accent.base"],
  ["--accent", "color.accent.focusRing"],
  ["--accent", "color.severity.info.ink"],
  ["--accent-2", "color.accent.hover"],
  ["--accent-3", "color.accent.pressed"],
  ["--accent-s", "color.accent.wash"],
  ["--accent-s", "color.severity.info.wash"],
  ["--accent-ink", "color.accent.ink"],
  ["--sev-ok", "color.severity.ok.ink"],
  ["--sev-ok-s", "color.severity.ok.wash"],
  ["--sev-warn", "color.severity.warning.ink"],
  ["--sev-warn-s", "color.severity.warning.wash"],
  ["--sev-crit", "color.severity.critical.ink"],
  ["--sev-crit-s", "color.severity.critical.wash"],
  ["--sev-mut", "color.severity.unknown.ink"],
  ["--sev-mut-s", "color.severity.unknown.wash"],
  ["--r-ctl", "radius.control"],
  ["--r-ctl", "radius.chip"],
  ["--r-panel", "radius.panel"],
  ["--r-badge", "radius.badge"],
  ["--shadow", "shadow.resting"],
];

/** The brand writes a size with its weight ("22px/600") or its case ("10.5px uppercase 0.1em"). */
const BRAND_SIZES: ReadonlyArray<readonly [string, string]> = [
  ["--t-h1", "type.h1"],
  ["--t-h2", "type.h2"],
  ["--t-body", "type.body"],
  ["--t-sm", "type.small"],
  ["--t-cap", "type.caption"],
  ["--t-kpi", "type.kpi"],
];

/** The ramp's roles in DESIGN.md, and the token each one is. */
const RAMP: Readonly<Record<string, string>> = {
  h1: "--t-h1",
  h2: "--t-h2",
  body: "--t-body",
  small: "--t-sm",
  caption: "--t-cap",
  kpi: "--t-kpi",
};

describe("design tokens", () => {
  const design = readFileSync(DESIGN_MD, "utf8");
  const colours = frontmatterBlock(design, "colors");
  const radii = frontmatterBlock(design, "rounded");
  const sizes = fontSizes(design);
  const app = rootTokens(APP_CSS);
  const brand: unknown = JSON.parse(readFileSync(BRAND_JSON, "utf8"));

  it("parsed all three files, so the comparisons below mean something", () => {
    // NON-VACUITY: an empty map equals an empty map.
    expect(colours.size).toBeGreaterThan(25);
    expect(colours.get("accent")).toBe("#2747c9");
    expect(radii.get("r-badge")).toBe("4px");
    expect(sizes.get("body")).toBe("13.5px");
    expect(app.get("--accent")).toBe("#2747c9");
    expect(brandValue(brand, "color.accent.base")).toBe("#2747c9");
  });

  it("paints every colour DESIGN.md names, with DESIGN.md's value", () => {
    const drifted = [...colours]
      .filter(([name, value]) => same(app.get(`--${name}`) ?? "(missing)") !== same(value))
      .map(([name, value]) => `--${name}: DESIGN.md=${value} index.css=${app.get(`--${name}`) ?? "(missing)"}`);
    expect(drifted, drifted.join(" | ")).toEqual([]);
  });

  it("invents no colour DESIGN.md does not name", () => {
    const invented = [...app]
      .filter(([, value]) => /^#|^rgb|^hsl/i.test(value))
      .filter(([name]) => !colours.has(name.slice(2)))
      .map(([name, value]) => `${name}: ${value}`);
    expect(invented, `colours not in DESIGN.md: ${invented.join(", ")}`).toEqual([]);
  });

  it("declares the radius ladder DESIGN.md names", () => {
    expect(radii.size).toBe(4);
    for (const [name, value] of radii) expect(app.get(`--${name}`), `--${name}`).toBe(value);
  });

  it.each(Object.entries(RAMP))("declares the %s rung DESIGN.md names (%s)", (role, token) => {
    expect(app.get(token)).toBe(sizes.get(role));
  });

  it("keeps the two type stacks: Inter first, and the code stack DESIGN.md names", () => {
    // Kept from the v3 test, which asserted `--sans` starts with Inter: the
    // brand changed no face, and `fonts.test.ts` checks the @font-face, not
    // the stack a rule reads.
    expect(app.get("--sans")?.startsWith("Inter,")).toBe(true);
    expect(same(app.get("--mono") ?? "(missing)")).toBe(same(codeFamily(design)));
  });

  it.each(BRAND_MAP)("carries the brand's value in %s (%s)", (token, path) => {
    expect(same(app.get(token) ?? "(missing)")).toBe(same(brandValue(brand, path)));
  });

  it.each(BRAND_SIZES)("carries the brand's size in %s (%s)", (token, path) => {
    const size = brandValue(brand, path).split(/[/ ]/)[0];
    expect(app.get(token)).toBe(size);
  });

  it("puts every colour of the brand somewhere in the palette", () => {
    const values = new Set([...app.values()].map(same));
    const missing = brandColours((brand as { color: unknown }).color).filter((colour) => !values.has(same(colour)));
    expect(missing, `brand colours the console never paints: ${missing.join(", ")}`).toEqual([]);
  });

  it("keeps the four retired tones as aliases of the brand's, never as colours (DESIGN.md v4, D2)", () => {
    const css = readFileSync(APP_CSS, "utf8");
    const retired: ReadonlyArray<readonly [string, string]> = [
      ["--ink-2", "--ink"],
      ["--line-2", "--line"],
      ["--sev-run", "--accent"],
      ["--sev-run-s", "--accent-s"],
    ];
    for (const [alias, target] of retired) {
      expect(app.has(alias), `${alias} is back in the first :root`).toBe(false);
      expect(css).toMatch(new RegExp(`${alias}: var\\(${target}\\);`));
    }
    // D3: the rail's alert count is white on the critical badge now.
    expect(css).not.toContain("--rail-hot");
  });

  it("commits to the light colour scheme rather than claiming dark support", () => {
    const css = readFileSync(APP_CSS, "utf8");
    expect(css).toContain("color-scheme: light;");
    expect(css).not.toContain("color-scheme: light dark");
  });

  it("fetches no font or stylesheet from a third party", () => {
    const css = readFileSync(APP_CSS, "utf8");
    const external = css.match(/@import\s+url\(|fonts\.googleapis|fonts\.gstatic|https?:\/\//g);
    expect(external, `index.css reaches off-origin: ${external?.join(", ")}`).toBeNull();
  });
});
