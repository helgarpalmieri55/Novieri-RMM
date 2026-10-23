/**
 * WCAG contrast and type-scale guards for the shipped stylesheets.
 *
 * WHY THIS EXISTS. Both UX auditors measured the same class of defect from the
 * live DOM: table headers at 3.49:1, status pills at 3.96:1 and 4.05:1, the
 * sidebar's section labels at 3.9:1, the gold pill at 2.31:1 -- all against the
 * 4.5:1 that WCAG 2.2 SC 1.4.3 requires for text below 18.66px. Auditor A's fix
 * note asks for exactly this: "re-run a token-level contrast check in CI rather
 * than eyeballing." And the v3 redesign arrived with two of its own: the rail's
 * group captions at 4.44:1 and the mut pill at 4.39:1, both found by this file
 * before a page was ported and both corrected in the design file first.
 *
 * The ratios below are COMPUTED from the token values in `index.css`, not
 * transcribed. Lighten a token, or point a pill at a different one, and the
 * arithmetic here changes and the test fails. That is the whole point: the
 * comment blocks in the CSS quote numbers, and a quoted number rots.
 *
 * TWO HALVES, AND NEITHER IS SUFFICIENT ALONE. The tables measure token pairs
 * the design paints. The scans read what the shell's stylesheets REALLY name
 * for a rail label, a control border, a focus ring, a notice, a health state,
 * and measure that against the ground it sits on -- so a rule pointed at a
 * decorative hairline fails here with the pair and the ratio rather than
 * passing quietly. The scans match rules by what they ARE (a selector that
 * names a control, a rule on the rail) rather than by a fixed list of class
 * names, because the class names changed in the v3 port and will change
 * again; the criterion does not.
 *
 * ALIASES ARE RESOLVED. `index.css` carries a second `:root` of legacy names
 * (`--red: var(--sev-crit)`) so unmigrated modules keep resolving. A rule
 * that still paints `var(--red)` is graded here at the v3 colour it actually
 * renders, which is the only honest measurement of what an operator sees.
 *
 * WHAT THIS DOES NOT COVER. It reads declared values, so it cannot see a colour
 * composited by opacity, painted by a gradient, or written inline in a
 * component's Tailwind class (see `jsx-tokens.test.ts` for the last). The
 * browser-side harmony and axe passes over the rendered console are the
 * complement to this, and are not this.
 */

import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const REPO = resolve(import.meta.dirname, "../../..");
const APP_CSS = resolve(REPO, "frontend/src/index.css");
const SHELL_DIR = resolve(REPO, "frontend/src/shell");
const shellCss = (name: string) => resolve(SHELL_DIR, name);
const BASE_CSS = shellCss("base.css");
const SHELL_LAYOUT_CSS = shellCss("shell.css");
/** Every stylesheet under `src/shell`, so a sheet added by the port is scanned without being listed. */
const SHELL_CSS = readdirSync(SHELL_DIR)
  .filter((name) => name.endsWith(".css"))
  .sort()
  .map((name) => shellCss(name));

/** WCAG 2.2 relative luminance of an `#rrggbb` colour. */
export function luminance(hex: string): number {
  const channel = (at: number) => {
    const c = parseInt(hex.slice(at, at + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
}

/** WCAG 2.2 contrast ratio between two `#rrggbb` colours. Order-independent. */
export function contrast(a: string, b: string): number {
  const first = luminance(a);
  const second = luminance(b);
  const hi = Math.max(first, second);
  const lo = Math.min(first, second);
  return (hi + 0.05) / (lo + 0.05);
}

/** `#rgb` -> `#rrggbb`; anything else passes through. */
const expand = (value: string): string =>
  /^#[0-9a-f]{3}$/i.test(value) ? `#${[...value.slice(1)].map((c) => c + c).join("")}` : value.toLowerCase();

/**
 * Every declaration in every `:root` block of index.css: hex literals from the
 * design's block, `var()` references from the alias block.
 */
function tokens(): Map<string, string> {
  const source = readFileSync(APP_CSS, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  const found = new Map<string, string>();
  for (const block of source.matchAll(/:root\s*\{([^}]*)\}/g)) {
    for (const line of (block[1] ?? "").split("\n")) {
      const match = /^\s*(--[\w-]+):\s*([^;]+);/.exec(line);
      const [, name, value] = match ?? [];
      if (name && value) found.set(name, value.trim());
    }
  }
  return found;
}

const TOKENS = tokens();

/** The v3 token a name finally lands on, following aliases. */
function canonical(name: string): string {
  let at = name;
  for (let hops = 0; hops < 8; hops += 1) {
    const value = TOKENS.get(at);
    if (!value) throw new Error(`index.css declares no ${at} (reached from ${name})`);
    const next = /^var\((--[\w-]+)\)$/.exec(value)?.[1];
    if (!next) return at;
    at = next;
  }
  throw new Error(`${name} aliases in a loop`);
}

/** The `#rrggbb` a token paints, through any aliases. */
function colour(name: string): string {
  const value = TOKENS.get(canonical(name));
  if (!value || !/^#[0-9a-f]{6}$/i.test(value)) {
    throw new Error(`${name} does not resolve to a colour: ${value}`);
  }
  return value.toLowerCase();
}

/**
 * A stylesheet, parsed into its rules with comments stripped and nesting
 * (media blocks, layers) flattened. Every earlier version of this file
 * located rules with `line.startsWith("<selector> {")`, which silently found
 * nothing when a formatter moved a brace; this walks the braces instead.
 */
interface Rule {
  file: string;
  selector: string;
  body: string;
  /** Character offset in the (comment-stripped) source, for ordering. */
  at: number;
}

function parseRules(path: string): Rule[] {
  const file = path.slice(path.lastIndexOf("/") + 1);
  const source = readFileSync(path, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  const rules: Rule[] = [];
  let head = "";
  for (let i = 0; i < source.length; i += 1) {
    const ch = source[i] ?? "";
    if (ch === "{") {
      const selector = head.trim();
      head = "";
      if (selector.startsWith("@")) continue; // media/layer/supports: descend
      let depth = 1;
      let end = i + 1;
      while (end < source.length && depth > 0) {
        if (source[end] === "{") depth += 1;
        else if (source[end] === "}") depth -= 1;
        end += 1;
      }
      rules.push({ file, selector, body: source.slice(i + 1, end - 1).trim(), at: i });
      i = end - 1;
    } else if (ch === "}") {
      head = "";
    } else {
      head += ch;
    }
  }
  return rules;
}

const RULES: Rule[] = SHELL_CSS.flatMap((path) => parseRules(path));

/** The value a rule gives a property (the LAST declaration wins, as in CSS). */
function property(rule: Rule, name: string): string | undefined {
  let found: string | undefined;
  for (const declaration of rule.body.split(";")) {
    const colon = declaration.indexOf(":");
    if (colon === -1) continue;
    if (declaration.slice(0, colon).trim() === name) found = declaration.slice(colon + 1).trim();
  }
  return found;
}

/**
 * The colour a declaration paints, or why it cannot be graded.
 * `{ token }` for `var(--x)`, `{ literal }` for a hex, `null` for
 * `transparent`/`none`/`inherit`/`currentColor`, which name no colour.
 */
type Painted = { token: string } | { literal: string } | null;
function painted(value: string | undefined): Painted {
  if (!value) return null;
  const token = /var\((--[\w-]+)\)/.exec(value)?.[1];
  if (token) return { token };
  const literal = /#[0-9a-f]{3}\b|#[0-9a-f]{6}\b/i.exec(value)?.[0];
  if (literal) return { literal: expand(literal) };
  if (/\bwhite\b/.test(value)) return { literal: "#ffffff" };
  return null;
}
const hex = (paint: Painted): string | undefined =>
  paint === null ? undefined : "token" in paint ? colour(paint.token) : paint.literal;
const describePaint = (paint: Painted): string =>
  paint === null ? "nothing" : "token" in paint ? `${paint.token} ${colour(paint.token)}` : `literal ${paint.literal}`;

/** Does one of a rule's comma-separated selectors match? */
const selects = (rule: Rule, pattern: RegExp): boolean =>
  rule.selector.split(",").some((part) => pattern.test(part.trim()));

/**
 * The rail: the one dark ground in the product. A rule is "on the rail" if any
 * of its selectors is scoped there. `.nav-toggle` is the hamburger in the top
 * bar and is excluded by name.
 */
const ON_RAIL = /(^|[\s>+~])(\.rail\b|\.sidebar\b|\.brand\b|\.nav-(?!toggle)|\.tenant-|\.collapse-btn\b)/;

/**
 * Every (text, background) pair the console paints from tokens, with what
 * draws it in v3. A pair is listed here because a rule sets that colour on
 * that background -- so adding a pill means adding a row.
 */
const TEXT_ON_BACKGROUND: ReadonlyArray<readonly [string, string, string]> = [
  // The three inks, on each of the three light surfaces they land on.
  ["--ink", "--surface", "body copy, .lrow .ttl b, .tile .v"],
  ["--ink", "--surface-2", "the same, in a hovered row"],
  ["--ink", "--bg", "an h1 on the page, .empty b"],
  ["--ink-2", "--surface", ".lrow .cell, .chip, the top bar's icon buttons"],
  ["--ink-2", "--surface-2", ".lrow .cell in a hovered row"],
  ["--ink-2", "--bg", "a value drawn straight onto the page"],
  ["--muted", "--surface", ".tile .k, .tile .z, .lhead, .panel-h p, .inp .ph, .note"],
  ["--muted", "--surface-2", ".ttl small inside a hovered row"],
  ["--muted", "--bg", ".crumb, .ph .sub, .empty, page-level metadata"],
  // The severity inks: each on its own wash (pills, tinted tiles) and on white
  // (.tile.ok .v, the gutter's neighbour text, .bar labels).
  ["--sev-crit", "--sev-crit-s", ".pill.p-crit, .tile.crit .v and .k"],
  ["--sev-crit", "--surface", ".tile.crit's icon on a white tile, red text in a cell"],
  ["--sev-warn", "--sev-warn-s", ".pill.p-warn, .tile.warn .v and .k"],
  ["--sev-warn", "--surface", "an amber value in a cell"],
  ["--sev-ok", "--sev-ok-s", ".pill.p-ok"],
  ["--sev-ok", "--surface", ".tile.ok .v"],
  ["--sev-run", "--sev-run-s", ".pill.p-run"],
  ["--sev-run", "--surface", "a running job's count"],
  ["--sev-mut", "--sev-mut-s", ".pill.p-mut"],
  ["--sev-mut", "--surface", "the same tone as a standalone value"],
  // The secondary ink on a tinted tile's wash: `.tile .z` keeps --muted when
  // the tile turns crit or warn, so the sub-line is graded on both washes.
  ["--muted", "--sev-crit-s", ".tile.crit .z"],
  ["--muted", "--sev-warn-s", ".tile.warn .z"],
  // The accent as text: links, ghost buttons, the active tab, the info pill.
  ["--accent", "--surface", "a, .btn.ghost, .tabs a.on's underline label"],
  ["--accent", "--bg", "a link on the page background"],
  ["--accent", "--surface-2", "a link inside a hovered row"],
  ["--accent", "--accent-s", ".pill.p-info, .chip.on"],
  // Ink on the accent fill: the primary button's label, the brand chip.
  ["--accent-ink", "--accent", ".btn.pri, .brand i"],
];

/**
 * The rail. Its own two grounds, and the tones drawn on them.
 *
 * SEPARATE FROM THE TABLE ABOVE because the rail is the one dark surface in
 * the product: every pair here is light-on-dark, and mixing them into the
 * light-surface list would hide which direction each ratio runs. Both
 * grounds, because a link lifts from `--rail` to `--rail-2` on hover and when
 * active, and its `.ct` count stays `--rail-dim` while it does.
 */
const TEXT_ON_RAIL: ReadonlyArray<readonly [string, string, string]> = [
  ["--rail-ink", "--rail", ".rail a at rest"],
  ["--rail-ink", "--rail-2", "the same, hovered"],
  ["--rail-dim", "--rail", ".rail .grp, the 10.5px uppercase group caption; .rail .ct"],
  ["--rail-dim", "--rail-2", ".rail .ct inside a hovered or active link"],
  ["--rail-hot", "--rail", ".rail .ct.hot, the alert count"],
  ["--rail-hot", "--rail-2", "the same, hovered"],
  ["--accent-ink", "--rail", ".brand, an active link's label"],
  ["--accent-ink", "--rail-2", "an active link's label on its own ground"],
];

/** The avatar palette: white initials on each saturated fill, at 12px or less. */
const AVATAR_FILLS = ["--av-1", "--av-2", "--av-3", "--av-4", "--av-5", "--av-6"] as const;

describe("WCAG 1.4.3 contrast, computed from the shipped tokens", () => {
  it("parsed the token file, so the assertions below are about something", () => {
    // NON-VACUITY. Every `it.each` row below reads through `colour()`, which
    // throws on a missing token -- but a map that parsed as empty would make
    // the whole suite throw identically for every row, which reads as one bug
    // rather than "the guard is not running". Fail loudly here instead.
    expect(TOKENS.size).toBeGreaterThan(25);
    expect(TOKENS.get("--surface")).toBe("#ffffff");
    // And the alias block parsed too, or the resolution below is untested.
    expect(TOKENS.get("--red")).toBe("var(--sev-crit)");
    expect(colour("--red")).toBe(colour("--sev-crit"));
    expect(canonical("--gold-ink")).toBe("--sev-warn");
  });

  it("computes ratios that match hand-verified references", () => {
    // The arithmetic itself needs a witness. Black on white is exactly 21:1 by
    // definition, white on white exactly 1:1, and the mid grey below was
    // checked against an independent calculator.
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrast("#ffffff", "#ffffff")).toBeCloseTo(1, 5);
    expect(contrast("#767676", "#ffffff")).toBeCloseTo(4.54, 2);
  });

  it("would fail the palette the auditors measured", () => {
    // THE FALSIFIER. If this test cannot see the original defect, its passing
    // on the corrected palette proves nothing. These are the exact values and
    // ratios both audits recorded from the live DOM.
    expect(contrast("#878590", "#fbfafc")).toBeLessThan(4.5); // table headers, 3.49:1
    expect(contrast("#148a55", "#e8f7ef")).toBeLessThan(4.5); // "ok" pill, 3.96:1
    expect(contrast("#d63f52", "#fff0f2")).toBeLessThan(4.5); // "down" pill, 4.05:1
    expect(contrast("#d19a18", "#fff5d8")).toBeLessThan(4.5); // gold pill, 2.31:1
    expect(contrast("#777380", "#14131a")).toBeLessThan(4.5); // sidebar labels, 3.9:1
    expect(contrast("#9a99a4", "#ffffff")).toBeLessThan(4.5); // the old --muted-2
    expect(contrast("#777581", "#f8f8fa")).toBeLessThan(4.5); // .topbar-search, 4.26:1
    expect(contrast("#8b8994", "#ffffff")).toBeLessThan(4.5); // .kbd, 3.44:1
    // And the two pairs v3 itself shipped with, corrected in the design file
    // on 2026-09-07 (its `:root` comment records the edit).
    expect(contrast("#6d838c", "#0f1a20")).toBeLessThan(4.5); // v3 --rail-dim on --rail, 4.44:1
    expect(contrast("#667080", "#eef0f4")).toBeLessThan(4.5); // v3 --sev-mut on --sev-mut-s, 4.39:1
    expect(contrast("#667080", "#fdecee")).toBeLessThan(4.5); // v3 --muted on --sev-crit-s, 4.39:1
  });

  it.each(TEXT_ON_BACKGROUND)("%s on %s clears 4.5:1 (%s)", (foreground, background) => {
    const ratio = contrast(colour(foreground), colour(background));
    expect(
      ratio,
      `${foreground} ${colour(foreground)} on ${background} ${colour(background)} is ${ratio.toFixed(2)}:1`,
    ).toBeGreaterThanOrEqual(4.5);
  });

  it.each(TEXT_ON_RAIL)("%s on %s clears 4.5:1 (%s)", (foreground, background) => {
    const ratio = contrast(colour(foreground), colour(background));
    expect(
      ratio,
      `${foreground} ${colour(foreground)} on ${background} ${colour(background)} is ${ratio.toFixed(2)}:1`,
    ).toBeGreaterThanOrEqual(4.5);
  });

  it.each(AVATAR_FILLS)("white initials on %s clear 4.5:1", (fill) => {
    // Initials are 10-12px, so the strict bar applies; there is no large-text
    // relaxation anywhere in this design because nothing but the 26px KPI
    // figure is large, and that is measured at the strict bar above anyway.
    const ratio = contrast(colour("--accent-ink"), colour(fill));
    expect(ratio, `white on ${fill} ${colour(fill)} is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5);
  });

  it("keeps the hairlines and the rail tones off the light-surface text list, where they would fail", () => {
    // THE FALSIFIER FOR THE SPLIT. The rail tones are legible on the rail and
    // nowhere else; the hairlines are legible nowhere. If someone moves one
    // into TEXT_ON_BACKGROUND it must go red, otherwise the two tables are just
    // decoration.
    for (const token of ["--line", "--line-2", "--rail-ink", "--rail-dim"]) {
      expect(contrast(colour(token), colour("--surface")), `${token} unexpectedly legible on white`).toBeLessThan(4.5);
      expect(TEXT_ON_BACKGROUND.map(([fg]) => fg)).not.toContain(token);
    }
    // And the mirror image: the accent is a 2px edge on the rail, never text.
    expect(contrast(colour("--accent"), colour("--rail-2"))).toBeLessThan(4.5);
    expect(TEXT_ON_RAIL.map(([fg]) => fg)).not.toContain("--accent");
  });

  it("holds the list header's caption above the floor on every ground a row can have", () => {
    // `.lhead` is --muted at 10.5px uppercase. It sits on --surface, and the
    // row it heads lifts to --surface-2 on hover, so both are graded. The
    // first prototype painted its header ground as a literal (#fbfafc, 3.49:1
    // with the muted of the day); v3 paints tokens, so the pair is in the
    // table above as well -- this is the row's own witness.
    for (const ground of ["--surface", "--surface-2"]) {
      expect(contrast(colour("--muted"), colour(ground))).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("holds the search field's two kinds of text above the floor", () => {
    // The field is an `<input>`: --muted is the placeholder, --ink is what the
    // operator types. Both on the field's own white, and on the tinted ground
    // a field lands on inside a row or a toolbar. The first prototype painted
    // the field's ground as a literal (#f8f8fa) and a browser pass measured
    // 4.26:1 there while this suite was green; v3 paints --surface.
    for (const ground of ["--surface", "--surface-2", "--bg"]) {
      expect(contrast(colour("--muted"), colour(ground))).toBeGreaterThanOrEqual(4.5);
      expect(contrast(colour("--ink"), colour(ground))).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("draws every search field, placeholder and shortcut hint from tokens", () => {
    // THE RAIL'S LESSON, APPLIED TO THE RULE THAT FIRST REPEATED IT. The
    // arithmetic above is only meaningful if these rules actually name the
    // token; scraping whatever literal they happen to hold would grade them
    // against themselves, which is exactly how #777581 survived one sweep
    // and every run of this file after it. Matched by what the rule IS -- a
    // search field, a placeholder, a keyboard hint -- not by a class list.
    const searchish = RULES.filter(
      (rule) =>
        selects(rule, /(\.srch|\.topbar-search|\.search-field|\.inp\b|\.kbd|::placeholder)/) &&
        !selects(rule, ON_RAIL) &&
        property(rule, "color") !== undefined,
    );
    expect(searchish.length, "found no search field, placeholder or hint rule that paints a colour").toBeGreaterThanOrEqual(2);
    for (const rule of searchish) {
      const paint = painted(property(rule, "color"));
      expect(paint && "token" in paint, `${rule.file} \`${rule.selector}\` paints a literal, not a text token: ${property(rule, "color")}`).toBe(true);
      const ink = hex(paint) ?? "";
      const ratio = contrast(ink, colour("--surface"));
      expect(ratio, `${rule.file} \`${rule.selector}\` paints ${describePaint(paint)}, ${ratio.toFixed(2)}:1 on --surface`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("draws the rail from tokens that are legible on both of its grounds", () => {
    // THE LESSON, AND IT COST A RED TEST. The rail's label colour used to be
    // a hex literal chosen against a near-black sidebar, with the measured
    // ratio written in a comment beside it. The moment the rail changed hue
    // that literal measured 4.38:1 and the comment was a lie -- but nothing
    // failed, because the guard scraped the literal and checked THAT, so it
    // was grading the rail against itself.
    //
    // TEXT_ON_RAIL above grades the tokens; this reads every rule scoped to
    // the rail that sets a `color` and requires it to be one of those tokens
    // (or white), then grades it on both grounds anyway. Neither half is
    // sufficient alone.
    const rail = parseRules(SHELL_LAYOUT_CSS).filter(
      (rule) => selects(rule, ON_RAIL) && property(rule, "color") !== undefined,
    );
    expect(rail.length, "found no rail rule that paints a colour").toBeGreaterThanOrEqual(3);
    for (const rule of rail) {
      const paint = painted(property(rule, "color"));
      if (paint === null) continue; // `inherit`/`currentColor`: graded at the rule it inherits from
      if ("literal" in paint) {
        expect(paint.literal, `\`${rule.selector}\` paints a literal on the rail: ${property(rule, "color")}`).toBe("#ffffff");
      }
      for (const ground of ["--rail", "--rail-2"]) {
        const ratio = contrast(hex(paint) ?? "", colour(ground));
        expect(
          ratio,
          `\`${rule.selector}\` paints ${describePaint(paint)}, which on ${ground} ${colour(ground)} is ${ratio.toFixed(2)}:1`,
        ).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("draws every notice from tokens, and its ink clears the floor on its own wash", () => {
    // THE SAME LESSON AS THE RAIL, ON THE COMPONENT THAT LEARNED IT NEXT.
    // `.notice` and its tones once carried nine hex literals between them, so
    // a token pair could sit in the table measuring 6.90:1 while the box an
    // operator actually read was painted from something else entirely. All
    // tones are one family or none: every `.notice*` rule that paints a
    // colour must name a token, and its ink is graded on its own wash (or on
    // the base rule's wash when a tone sets only the ink).
    const notices = RULES.filter((rule) => selects(rule, /^\.notice(\b|[.:])/));
    const base = notices.find((rule) => rule.selector.trim() === ".notice");
    expect(base, "no `.notice` rule in the shell stylesheets").toBeDefined();
    const withColour = notices.filter((rule) => property(rule, "color") !== undefined);
    expect(withColour.length, "found no .notice rule that paints an ink").toBeGreaterThanOrEqual(3);
    for (const rule of notices) {
      const literals = rule.body.match(/#[0-9a-f]{3,8}\b/gi);
      expect(literals, `${rule.file} \`${rule.selector}\` still paints hex literals: ${literals?.join(", ")}`).toBeNull();
    }
    for (const rule of withColour) {
      const ink = painted(property(rule, "color"));
      const wash =
        painted(property(rule, "background") ?? property(rule, "background-color")) ??
        painted(property(base as Rule, "background") ?? property(base as Rule, "background-color"));
      expect(ink && "token" in ink, `\`${rule.selector}\` paints its ink with a literal`).toBe(true);
      expect(wash, `\`${rule.selector}\` has no wash to grade against`).not.toBeNull();
      const ratio = contrast(hex(ink) ?? "", hex(wash) ?? "");
      expect(
        ratio,
        `\`${rule.selector}\` paints ${describePaint(ink)} on ${describePaint(wash)}: ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(4.5);
    }
  });
});

/**
 * SC 1.4.11 NON-TEXT CONTRAST, WHICH IS A DIFFERENT CRITERION FROM EVERYTHING
 * ABOVE AND HAS ITS OWN FLOOR.
 *
 * WHAT IT REQUIRES. "Visual information required to identify user interface
 * components and their states" must reach 3:1 against adjacent colour. That is
 * about the BORDER of a text input, not the text inside it -- and a guard that
 * only ever measured (text, background) pairs could not see it.
 * `docs/product.md` §11 makes WCAG 2.2 AA binding, so a criterion nothing
 * measures is a requirement nothing holds.
 *
 * WHAT BREAKING IT WOULD MEAN. v3 arrived drawing every button, input, chip
 * and toggle track in `--line-2`: 1.49:1 on `--surface`, 1.38:1 on `--bg`. On
 * a white panel that hairline is the ONLY mark saying "this is a field you can
 * type into" -- so at those ratios a low-vision operator sees a blank card.
 * The design file gained `--line-control` for exactly those four rules.
 * Regressing this does not make the console look slightly worse; it makes
 * controls stop being findable.
 *
 * THREE SURFACES, AND THE DARKEST IS THE ONE THAT BINDS. A control lands on
 * `--surface` inside a panel, on `--surface-2` in a tinted row and on `--bg`
 * when a page draws one straight onto the background. It has to clear the
 * floor on all three, so all three are listed rather than the convenient one.
 *
 * WHAT IS DELIBERATELY ABSENT. Decorative separators. 1.4.11 exempts them in
 * as many words -- a rule between rows, the edge of a panel, the underline of
 * a tab strip identify nothing and require no contrast -- and darkening them
 * would cost the design its lightness for a criterion that never asked.
 * `--line` and `--line-2` keep exactly those jobs, and the guard below asserts
 * they never draw a control.
 *
 * ALSO DELIBERATELY ABSENT: DISABLED CONTROLS. 1.4.11 exempts inactive
 * components explicitly, which is why `.btn:disabled` and `disabled:opacity-50`
 * are skipped by the scan rather than listed as failures. It is the
 * criterion's own carve-out, not an oversight.
 */
const NON_TEXT_FLOOR = 3;

/** Every surface a control can be drawn on. */
const CONTROL_SURFACES = ["--surface", "--surface-2", "--bg"] as const;

/**
 * The v3 tokens allowed to carry a control boundary or a control state, and
 * what draws each. A token is listed here because a stylesheet paints a
 * control's border, focus ring or outline with it. Legacy names resolve to one
 * of these through the alias block before being checked.
 */
const NON_TEXT: ReadonlyArray<readonly [string, string]> = [
  ["--line-control", "the resting border of every input, select, chip and bordered button; the toggle's track"],
  ["--muted", "the hover border those same controls may lift to"],
  ["--ink-2", "a pressed or emphasised control edge"],
  ["--accent", "the focus ring; the checked fill of every checkbox and radio; the active tab's edge; the toggle's on-track"],
  ["--accent-2", "the deeper accent a FILLED button steps its ground and edge to on hover"],
  ["--sev-crit", "the danger button's edge"],
  ["--sev-warn", "a warning-toned control's edge"],
  ["--sev-ok", "a confirming control's edge"],
  ["--sev-run", "a running control's edge"],
];

/**
 * A rule paints a control boundary if any of its selectors names a control
 * and the rule sets a border, outline or box-shadow ring with a colour.
 *
 * SCANNED, NOT ASSUMED, and that is the half that catches a token applied to
 * four of five controls. NON_TEXT says which colours are permissible; this
 * reads what each rule really names and computes THAT against every surface
 * the control can sit on, so pointing one field back at a decorative hairline
 * fails here with the pair and the ratio rather than passing quietly. Matched
 * by what a control IS (an input, a button, the kit's classes for them) so the
 * port's renames do not silently empty the list; the non-vacuity floor below
 * is what stops a rename emptying it quietly.
 */
const CONTROL =
  /(^|[\s>+~(,])(input|select|textarea|button)\b|\.(btn|inp|ib|chip|tog|srch|search-field|select-field|input-field|nav-toggle|close-btn|picker|picker-option|confirm-typing|menu-item|top-icon-btn|profile-btn|top-enrol|topbar-search)(?![\w-])/;

/**
 * The second matcher, independent of any class name: whatever a rule is
 * called, if it answers `:focus-visible` by painting an outline it is a
 * focusable thing drawing its boundary, and 1.4.11 grades it. This is what
 * keeps a tile or an option row in the table after a rename, and what caught
 * the first version of CONTROL dropping `.stat-card:focus-visible` and
 * `.picker-option:focus-visible` (the `(?![\w-])` after `picker` rejects
 * `picker-option`, and no tile name was listed at all).
 *
 * The tile is deliberately NOT named in CONTROL. It is a panel that happens
 * to be focusable: its resting border is v3's own `1px solid var(--line)`
 * (itms-prototype-v3.html `.tile`), the same hairline every card closes
 * with, and 1.4.11 does not ask a card edge to clear 3:1. Only its ring is a
 * control boundary, and this matcher is what grades it.
 */
const FOCUS_RING = /:focus-visible\b/;
const paintsOutline = (rule: Rule): boolean => property(rule, "outline") !== undefined || property(rule, "outline-color") !== undefined;

interface Boundary {
  rule: Rule;
  property: string;
  paint: Painted;
}

function boundaries(): Boundary[] {
  const found: Boundary[] = [];
  for (const rule of RULES) {
    const control = selects(rule, CONTROL) || (selects(rule, FOCUS_RING) && paintsOutline(rule));
    if (!control || selects(rule, ON_RAIL)) continue;
    if (/:disabled|\[disabled\]|\.disabled/.test(rule.selector)) continue; // 1.4.11's own carve-out
    for (const name of ["border", "border-color", "outline", "outline-color", "border-top", "border-bottom", "border-left", "border-right"]) {
      const value = property(rule, name);
      if (value === undefined) continue;
      if (/\b(none|0|transparent|inherit|currentcolor)\b/i.test(value) && !/var\(|#/.test(value)) continue;
      found.push({ rule, property: name, paint: painted(value) });
    }
  }
  return found;
}

const BOUNDARIES = boundaries();
const label = (b: Boundary) => `${b.rule.file} \`${b.rule.selector}\` ${b.property}`;

describe("WCAG 1.4.11 non-text contrast, computed from the shipped tokens", () => {
  it("would fail the borders this replaced", () => {
    // THE FALSIFIER. These are the measurements that opened the finding, on
    // each surface a field lands on: the first prototype's hairlines, and
    // v3's own `--line-2` as it arrived on every button and input.
    expect(contrast("#d6d9e2", "#ffffff")).toBeLessThan(NON_TEXT_FLOOR); // --line-strong, 1.41:1
    expect(contrast("#d6d9e2", "#f4f5f8")).toBeLessThan(NON_TEXT_FLOOR); // the same, 1.29:1
    expect(contrast("#e7e9f0", "#ffffff")).toBeLessThan(NON_TEXT_FLOOR); // --line, 1.21:1
    expect(contrast("#cfd4dc", "#ffffff")).toBeLessThan(NON_TEXT_FLOOR); // v3 --line-2 on .btn/.inp, 1.49:1
    expect(contrast("#cfd4dc", "#f5f6f9")).toBeLessThan(NON_TEXT_FLOOR); // the same on --bg, 1.38:1
    expect(contrast("#cfc9da", "#ffffff")).toBeLessThan(NON_TEXT_FLOOR); // the old .btn:hover, 1.61:1
    expect(contrast("#f0cbd1", "#ffffff")).toBeLessThan(NON_TEXT_FLOOR); // the old .btn.danger, 1.48:1
  });

  it.each(
    NON_TEXT.flatMap(([token, what]) => CONTROL_SURFACES.map((surface) => [token, surface, what] as const)),
  )("%s on %s clears 3:1 (%s)", (token, surface) => {
    const ratio = contrast(colour(token), colour(surface));
    expect(
      ratio,
      `${token} ${colour(token)} on ${surface} ${colour(surface)} is ${ratio.toFixed(2)}:1`,
    ).toBeGreaterThanOrEqual(NON_TEXT_FLOOR);
  });

  it("scanned real control boundaries, so the rows below are about something", () => {
    // NON-VACUITY. A selector pattern that matched nothing would make every
    // assertion below pass for ever, which is the failure every source-sweep
    // guard has. Buttons, fields and the top bar's icon buttons alone are
    // more than this.
    expect(BOUNDARIES.length).toBeGreaterThanOrEqual(6);
    expect(BOUNDARIES.some((b) => /\.btn\b/.test(b.rule.selector)), "no .btn boundary found").toBe(true);
    expect(
      BOUNDARIES.some((b) => /input|\.inp\b|\.search-field|\.topbar-search|\.srch/.test(b.rule.selector)),
      "no text-field boundary found",
    ).toBe(true);
    // The two focus rings the explicit table used to grade and the first
    // pattern silently dropped: the KPI tile (the kit's `.stat-card`, v3's
    // `.tile`) and the picker's option row. Neither is a button or a field
    // by name, so a matcher built only from control names loses them, and a
    // ring that later regresses to `--line-2` passes by absence.
    const ring = (b: Boundary) => /:focus-visible\b/.test(b.rule.selector) && /outline/.test(b.property);
    expect(
      BOUNDARIES.some((b) => ring(b) && /\.(stat-card|tile)\b/.test(b.rule.selector)),
      "no tile (.stat-card or .tile) focus-visible outline found",
    ).toBe(true);
    expect(
      BOUNDARIES.some((b) => ring(b) && /\.picker-option\b/.test(b.rule.selector)),
      "no .picker-option focus-visible outline found",
    ).toBe(true);
  });

  it.each(BOUNDARIES.map((b) => [label(b), b] as const))("%s draws a boundary at 3:1 on every surface", (_name, b) => {
    expect(b.paint && "token" in b.paint, `${label(b)} paints a literal, not a token: ${property(b.rule, b.property)}`).toBe(true);
    for (const surface of CONTROL_SURFACES) {
      const ratio = contrast(hex(b.paint) ?? "", colour(surface));
      expect(
        ratio,
        `${label(b)} draws ${describePaint(b.paint)}, which on ${surface} ${colour(surface)} is ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(NON_TEXT_FLOOR);
    }
  });

  it("gives every control a focus ring in the accent, and draws the button's outside its fill", () => {
    // A ring that exists is checked above like any boundary; this is that it
    // EXISTS, and where. v3 has one accent, so a ring drawn against the
    // primary button's own fill would be accent on accent -- 1:1, invisible on
    // the one button an operator is most likely to be tabbing to. A positive
    // `outline-offset` lifts it onto the page, which is the surface the row
    // above measures it against; drop the offset, or make it negative, and
    // that row silently starts grading the ring against a surface it is no
    // longer touching.
    const rings = BOUNDARIES.filter((b) => /:focus(-visible)?\b/.test(b.rule.selector) && /outline/.test(b.property));
    expect(rings.length, "no control paints a focus outline").toBeGreaterThanOrEqual(1);
    for (const ring of rings) {
      expect(ring.paint && "token" in ring.paint && canonical(ring.paint.token), `${label(ring)} is not the accent`).toBe("--accent");
    }
    const button = rings.find((b) => /\.btn\b/.test(b.rule.selector));
    expect(button, "no `.btn` focus-visible outline in the shell stylesheets").toBeDefined();
    const offset = /outline-offset:\s*(-?\d+(?:\.\d+)?)px/.exec((button as Boundary).rule.body)?.[1];
    expect(offset, `.btn's focus rule declares no outline-offset: ${(button as Boundary).rule.body}`).toBeDefined();
    expect(Number(offset)).toBeGreaterThan(0);
  });

  it("keeps the decorative hairlines out of the control-boundary table", () => {
    // THE LOOPHOLE GUARD, and the exact edit it is here to stop: somebody needs
    // a lighter field border, moves `--line-2` into NON_TEXT, and the suite
    // stays green because the table is the only thing defining what a control
    // boundary is. Neither hairline clears the floor, so neither may ever
    // appear in it -- separating rows and closing panels is all they do. The
    // legacy alias `--line-strong` resolves to `--line-2` and is caught by the
    // same check.
    for (const decorative of ["--line", "--line-2"]) {
      expect(contrast(colour(decorative), colour("--surface"))).toBeLessThan(NON_TEXT_FLOOR);
      expect(NON_TEXT.map(([token]) => token)).not.toContain(decorative);
      const painted = BOUNDARIES.filter((b) => b.paint && "token" in b.paint && canonical(b.paint.token) === decorative);
      expect(painted.map(label), `control boundaries drawn with the decorative ${decorative}`).toEqual([]);
    }
  });

  it("names every scanned boundary in the table above", () => {
    // The two halves have to agree. A rule painting a colour that is not
    // declared permissible is either a new control tone somebody forgot to
    // record, or the wrong token -- both worth a red test.
    const permitted = new Set(NON_TEXT.map(([token]) => token));
    const stray = BOUNDARIES.filter((b) => !(b.paint && "token" in b.paint && permitted.has(canonical(b.paint.token)))).map(
      (b) => `${label(b)} -> ${describePaint(b.paint)}`,
    );
    expect(stray, `control boundaries painting a token NON_TEXT does not list: ${stray.join(", ")}`).toEqual([]);
  });
});

/**
 * THE CHECKED STATE OF A CHECKBOX OR A RADIO, WHICH IS THE HALF OF A NATIVE
 * CONTROL THIS DESIGN SYSTEM OWNS.
 *
 * WHAT WAS WRONG. Roughly thirty checkboxes and radios ship in this console --
 * the one that picks which discovered devices get approved, the one that picks
 * which paths a restore writes over -- and not one of them declared a colour.
 * An unstyled checkbox fills with `AccentColor`: the accent the OPERATOR's
 * desktop happens to be set to, which every platform ships in pale variants.
 * So the mark saying "this device is in the blast radius" was drawn in a colour
 * this product did not choose, cannot measure, and which differs between two
 * people looking at the same screen.
 *
 * WHAT THIS MEASURES, AND WHAT IT DELIBERATELY DOES NOT. `accent-color` sets
 * the CHECKED fill and nothing else. `border` and `background` are the
 * declarations that would harden the unchecked edge, and authoring either one
 * on a native checkbox switches Blink and WebKit out of native appearance
 * altogether, taking the checkmark with it. So the unchecked boundary stays as
 * the user agent draws it, which is where SC 1.4.11 leaves it: the criterion
 * excepts information "where the appearance of the component is determined by
 * the user agent and not modified by the author". The fill IS modified by the
 * author now, so the fill is what owes 3:1 -- on every surface a control can
 * land on, the same three the boundary table uses and for the same reason.
 */
describe("the checked state of a checkbox or radio, at SC 1.4.11's floor", () => {
  /** The one rule in base.css that declares an accent, with its selector. */
  const accentRule = (): Rule => {
    const declaring = parseRules(BASE_CSS).filter((rule) => property(rule, "accent-color") !== undefined);
    expect(declaring.length, `base.css should declare exactly one accent-color rule, found ${declaring.length}`).toBe(1);
    return declaring[0] ?? { file: "base.css", selector: "", body: "", at: 0 };
  };

  const accentToken = (): string => {
    const paint = painted(property(accentRule(), "accent-color"));
    expect(paint && "token" in paint, `the accent rule paints a literal or a keyword, not a token: ${accentRule().body}`).toBe(true);
    return paint && "token" in paint ? paint.token : "";
  };

  it("declares the accent on the radio as well as the checkbox", () => {
    // BOTH TYPES, because they were both unstyled and `accent-color` covers
    // both. A rule that named only the checkbox would leave the restore
    // dialog's "original / alternate location" radios still answering to the
    // operating system, and every ratio below would still be green.
    const { selector } = accentRule();
    for (const type of ["checkbox", "radio"]) {
      expect(selector, `the accent rule does not select input[type="${type}"]`).toContain(`input[type="${type}"]`);
    }
  });

  it.each(CONTROL_SURFACES)("the checked fill clears 3:1 on %s", (surface) => {
    const token = accentToken();
    const ratio = contrast(colour(token), colour(surface));
    expect(
      ratio,
      `the checkbox accent draws ${token} ${colour(token)}, which on ${surface} ${colour(surface)} is ${ratio.toFixed(2)}:1`,
    ).toBeGreaterThanOrEqual(NON_TEXT_FLOOR);
  });

  it("would fail the accents this takes out of the operating system's hands", () => {
    // THE FALSIFIER. The state before this rule was not a bad colour, it was
    // an UNKNOWN one -- and pale accents are shipped options, not exotic
    // settings. If the floor above cannot reject them it is not a floor.
    expect(contrast("#ffcc00", "#ffffff")).toBeLessThan(NON_TEXT_FLOOR); // a yellow accent, 1.51:1
    expect(contrast("#5ac8fa", "#ffffff")).toBeLessThan(NON_TEXT_FLOOR); // a light blue one, 1.90:1
    // And the two tokens somebody reaching for "the brand colour" might grab
    // by mistake: the wash the tick sits near, and the hairline beside it.
    expect(contrast(colour("--accent-s"), colour("--surface"))).toBeLessThan(NON_TEXT_FLOOR);
    expect(contrast(colour("--line"), colour("--surface"))).toBeLessThan(NON_TEXT_FLOOR);
  });

  it("names the accent in the table of permissible control tones", () => {
    // The two halves have to agree here for the same reason they do for
    // borders: a tone applied to a control and recorded nowhere is a tone
    // nobody reviews the next time it is reused.
    expect(NON_TEXT.map(([token]) => token)).toContain(canonical(accentToken()));
  });
});

/**
 * THE TOP BAR'S HEALTH GLYPH: STATUS HUES THAT WERE ONCE MEASURED BY HAND,
 * INTO A COMMENT.
 *
 * WHAT WAS WRONG. The shell's health block carried the sentence "--green
 * 5.02:1, --gold 4.28:1, --red 5.51:1, all clear of SC 1.4.11's 3:1 floor",
 * computed at the time with this file's own formula and then written down.
 * That is exactly the failure this file exists to stop. Repoint the alert
 * state at a softer amber and the sentence stays true-looking while the glyph
 * stops being legible.
 *
 * WHY 1.4.11 AND NOT 1.4.3. The glyph is not text. It is "visual information
 * required to identify user interface components and their STATES" -- the
 * criterion's own words, and state is the whole of what this control does --
 * so its floor is 3:1.
 *
 * EVERY STATE, INCLUDING THE ONE THAT MEANS "NOBODY MEASURED THIS". `unknown`
 * is the state that must never be mistaken for the green one. Leaving it out
 * would grade three of the four silhouettes an operator can actually see.
 *
 * BOTH GROUNDS, BECAUSE A CONTROL OWES ITS RATIO IN EVERY STATE IT CAN BE IN.
 * The icon button paints its own ground and may lift to a wash under the
 * pointer; both are read from the rules rather than named, so repainting the
 * button moves them with it.
 */
describe("the top bar's health glyph, at SC 1.4.11's floor", () => {
  const shell = parseRules(SHELL_LAYOUT_CSS);
  const ICON_BUTTON = /^\.(ib|top-icon-btn)\b/;

  /** The icon button's rule, and its hover rule if it has one. */
  const iconButton = (state: "" | ":hover"): Rule | undefined =>
    shell.find(
      (rule) =>
        rule.selector.split(",").some((part) => {
          const s = part.trim();
          return ICON_BUTTON.test(s) && (state ? /:hover\b/.test(s) : !/:|\s|\.health|\.dot|\.pip/.test(s.replace(ICON_BUTTON, "")));
        }),
    );

  const ground = (rule: Rule | undefined): string | undefined =>
    hex(painted(property(rule as Rule, "background") ?? property(rule as Rule, "background-color")));

  /**
   * The two grounds the glyph is drawn on. Read from the rules rather than
   * named, so repainting the button moves these with it.
   */
  const GROUNDS: ReadonlyArray<readonly [string, string]> = (() => {
    const rest = iconButton("");
    const out: Array<readonly [string, string]> = [];
    const restGround = ground(rest);
    if (restGround) out.push(["at rest", restGround]);
    const hoverGround = ground(iconButton(":hover"));
    if (hoverGround) out.push(["hovered", hoverGround]);
    return out;
  })();

  /** Every rule that paints a health state's tint: selector names `health-<state>` and sets a colour. */
  const HEALTH_STATES = shell.filter(
    (rule) => /\.health-[a-z]+/.test(rule.selector) && !/\.[\w-]*(pip|dot)\b/.test(rule.selector) && property(rule, "color") !== undefined,
  );

  it("reads the grounds the glyph is really drawn on", () => {
    // NON-VACUITY, and it is not decorative: every row below measures against
    // whatever the icon button's rule paints, so a pattern that silently
    // matched the wrong rule would grade the hues against a colour they never
    // touch. v3 draws the button on --surface.
    expect(iconButton(""), "no `.ib` or `.top-icon-btn` rule in shell.css").toBeDefined();
    expect(GROUNDS.map(([, g]) => g)[0]).toBe(colour("--surface"));
  });

  it("paints every state the glyph can be in, and there are at least four", () => {
    // ok, alerts (or warn), down (or crit), unknown. A shell that dropped one
    // would grade fewer silhouettes than an operator can see.
    const names = new Set(HEALTH_STATES.flatMap((rule) => rule.selector.match(/\.health-([a-z]+)/g) ?? []));
    expect([...names].length, `health states found: ${[...names].join(", ")}`).toBeGreaterThanOrEqual(4);
    expect([...names].some((n) => /unknown/.test(n)), "no `unknown` state: nobody-measured-this must be drawable").toBe(true);
  });

  it.each(HEALTH_STATES.map((rule) => [rule.selector, rule] as const))("`%s` clears 3:1 on every ground", (_selector, rule) => {
    const paint = painted(property(rule, "color"));
    // THE RAIL'S LESSON, on the newest control to repeat it: scraping a literal
    // grades the rule against itself.
    expect(paint && "token" in paint, `\`${rule.selector}\` paints a literal, not a token: ${property(rule, "color")}`).toBe(true);
    for (const [where, g] of GROUNDS) {
      const ratio = contrast(hex(paint) ?? "", g);
      expect(ratio, `\`${rule.selector}\` paints ${describePaint(paint)}, which ${where} on ${g} is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(
        NON_TEXT_FLOOR,
      );
    }
  });

  it("keeps the unknown state's pip visible in its own right", () => {
    // THE GAP THE ROWS ABOVE CANNOT SEE. The filled pips take the state's tint,
    // so measuring the tint measures them. The `unknown` pip must be a
    // different SILHOUETTE -- hollow, or a ring -- so that with the colour
    // taken away it cannot read as the filled green one, and whichever mark
    // it draws (its ring, or its fill) is what nobody would otherwise measure.
    const pip = shell.find((rule) => /\.health-unknown/.test(rule.selector) && /\.[\w-]*(pip|dot)\b/.test(rule.selector));
    expect(pip, "no pip rule for the unknown state: its silhouette is undefined").toBeDefined();
    const mark =
      painted(property(pip as Rule, "border-color")) ??
      painted(property(pip as Rule, "border")) ??
      painted(property(pip as Rule, "background") ?? property(pip as Rule, "background-color"));
    expect(mark, `the unknown pip paints no ring and no fill: ${(pip as Rule).body}`).not.toBeNull();
    const ink = hex(mark) ?? "";
    for (const [where, g] of GROUNDS) {
      // A hollow pip is white on the button: its ring is the mark. A white
      // fill on a white ground is the case this exists to catch.
      const ratio = contrast(ink, g);
      expect(ratio, `the unknown pip's mark is ${describePaint(mark)}, which ${where} on ${g} is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(
        NON_TEXT_FLOOR,
      );
    }
  });

  it("would fail the softer status tones somebody would reach for", () => {
    // THE FALSIFIER. The plausible wrong edit is not an absurd colour: it is
    // one of this design's OWN washes, picked because it reads as "the green
    // one" in a token list. All fail the floor on the button's white, so if
    // these rows cannot reject them they are not a floor.
    for (const wash of ["--sev-ok-s", "--sev-warn-s", "--sev-crit-s", "--accent-s"]) {
      expect(contrast(colour(wash), colour("--surface")), `${wash} unexpectedly clears the non-text floor`).toBeLessThan(NON_TEXT_FLOOR);
    }
    // And the pale amber the audit measured at 2.31:1 as a pill, which is the
    // sort of value a "make the warning less shouty" edit lands on.
    expect(contrast("#d19a18", "#ffffff")).toBeLessThan(NON_TEXT_FLOOR);
  });
});

/**
 * HOVER FEEDBACK, WHICH IS NOT SC 1.4.11 AND IS STILL A REQUIREMENT.
 *
 * A tile's border is deliberately decorative -- what identifies it is the
 * label, the numeral and the icon, and 1.4.11 is satisfied by a focus ring.
 * None of that says anything about whether a person can tell which of six
 * tiles is under their pointer, and for a while they could not: hover stepped
 * `--line` to `--line-strong`, two hairlines that measure 1.16:1 against EACH
 * OTHER. That is not a contrast failure against any criterion; it is a state
 * change the eye does not register.
 *
 * So this measures the STEP: for every `X:hover` rule that repaints a border
 * colour, the border must move at least 2:1 from its resting colour. Only a
 * hover that also repaints the GROUND (`background`, `background-color`) is
 * excused, because a wash is a second carrier the eye does register. A lift
 * or a shadow is not one: the finding that opened this was a tile whose hover
 * already translated 1px and gained a shadow, and that was exactly what the
 * eye did not register. The first rewrite of this guard treated `transform`,
 * `box-shadow` and `color` as excusing carriers too, and since every hover in
 * the shell sheets lifts or casts, the pair list came out EMPTY and the
 * describe passed with zero rows; the non-vacuity floor below is there so it
 * cannot do that again. The 2:1 floor is set well above the 1.16:1 that
 * opened the finding and well below the 2.90:1 that `--line` to
 * `--line-control` lands at, so it fails on a regression rather than a
 * repaint.
 */
describe("a hover that changes only a border changes it visibly", () => {
  const borderOf = (rule: Rule): Painted => painted(property(rule, "border-color") ?? property(rule, "border"));

  /** (rest, hover) pairs where the hover rule paints only a border colour. */
  const PAIRS = RULES.flatMap((hover) => {
    const single = hover.selector.split(",").map((s) => s.trim());
    return single
      .filter((s) => /:hover$/.test(s))
      .flatMap((s) => {
        const restSelector = s.replace(/:hover$/, "");
        // A HOVER MAY BE NARROWER THAN THE RULE IT STEPS FROM. `.tile` sets
        // the resting border and `a.tile:hover` steps it, because only a
        // tile with a destination should answer the pointer. Looking only
        // for a rule named exactly `a.tile` would find nothing and drop the
        // pair -- and a dropped pair is a hover this guard stops measuring,
        // which is the failure mode the non-vacuity check below exists for.
        // So the type qualifier is stripped as a second attempt: strictly
        // MORE pairs are found, never fewer.
        const unqualified = restSelector.replace(/^[a-z]+(?=[.:#[])/, "");
        const named = (r: Rule, want: string) => r.selector.split(",").map((x) => x.trim()).includes(want);
        const rest =
          RULES.find((r) => r.file === hover.file && named(r, restSelector)) ??
          RULES.find((r) => r.file === hover.file && named(r, unqualified));
        const hoverBorder = borderOf(hover);
        if (!rest || !hoverBorder || !borderOf(rest)) return [];
        const repaintsGround = ["background", "background-color"].some(
          (p) => property(hover, p) !== undefined && property(hover, p) !== property(rest, p),
        );
        return repaintsGround ? [] : [[restSelector, rest, hover] as const];
      });
  });

  it("found at least one hover that repaints a border without a ground wash", () => {
    // NON-VACUITY. `it.each` over an empty list runs nothing and reports
    // nothing, which is how the previous version of this describe passed
    // while `.stat-card:hover` -- the very rule that opened the finding --
    // was excluded for having a transform. The tile hover is still in
    // `components.css`; if this fails, either the parser lost it or the
    // exclusion above grew back.
    expect(PAIRS.length, "no border-only hover pairs were found in the shell sheets").toBeGreaterThanOrEqual(1);
    // `a.tile` since 2026-09-08: the tile hover is qualified to the link
    // form, because a tile without a destination must not light up under the
    // pointer. The pair is still found and still graded; only its name moved.
    expect(
      PAIRS.some(([s]) => s === ".stat-card" || s === ".tile" || s === "a.tile"),
      "no tile hover pair found",
    ).toBe(true);
  });

  it.each(PAIRS.map(([selector, rest, hover]) => [selector, rest, hover] as const))(
    "`%s` steps its border far enough between rest and hover to be seen",
    (selector, rest, hover) => {
      const step = contrast(hex(borderOf(rest)) ?? "", hex(borderOf(hover)) ?? "");
      expect(step, `${selector} rest-to-hover border step is ${step.toFixed(2)}:1`).toBeGreaterThan(2);
    },
  );

  it("would fail the step it replaced", () => {
    // THE FALSIFIER. The old pair, measured against each other rather than
    // against the card, is what the floor above has to reject -- and v3's own
    // two hairlines against each other, for the next person who tries it.
    expect(contrast("#e7e9f0", "#d6d9e2")).toBeLessThan(2);
    expect(contrast(colour("--line"), colour("--line-2"))).toBeLessThan(2);
  });

  it("does not let a later variant override a hover it just gained", () => {
    // A variant rule (`.tile.urgent`) that sets its own border-color at equal
    // specificity and LATER in the file beats the plain `:hover` rule, so the
    // tile a person is most likely to be reaching for is the one with no hover
    // feedback. For every hover pair above, every later single-class variant
    // that repaints the border must have a hover of its own.
    const offenders: string[] = [];
    for (const [selector, , hover] of PAIRS) {
      const variants = RULES.filter(
        (r) =>
          r.file === hover.file &&
          r.at > hover.at &&
          r.selector.split(",").some((s) => new RegExp(`^${selector.replace(/[.]/g, "\\.")}\\.[\\w-]+$`).test(s.trim())) &&
          borderOf(r) !== null,
      );
      for (const variant of variants) {
        const name = variant.selector.split(",").map((s) => s.trim()).find((s) => s.startsWith(selector)) ?? variant.selector;
        const covered = RULES.some((r) => r.selector.split(",").map((s) => s.trim()).includes(`${name}:hover`));
        if (!covered) offenders.push(`${variant.file} \`${name}\` repaints the border after \`${selector}:hover\` and has no hover of its own`);
      }
    }
    expect(offenders).toEqual([]);
  });
});

describe("type scale floor", () => {
  // WCAG allows the relaxed 3:1 ratio only for text at 18.66px bold or 24px
  // regular, so everything this console draws is small text -- which is why
  // the contrast work above and the size floor here are the same finding.
  //
  // THE FLOOR IS THE RAMP'S SMALLEST STEP. Auditor A asked for 12px; the first
  // port held it. v3 sets its uppercase, letter-spaced captions (`.k`, `.grp`,
  // `.lhead`) at `--t-cap`, 10.5px, and the operator approved it. The design
  // is the authority, so the floor moves to the step the design contains and
  // NOTHING may go below it -- v3 itself shipped one 9px label (`.wk-h`) and
  // it was corrected in the design file first. The 4.5:1 text floor above is
  // what makes 10.5px legible at all, and every caption pair is graded there.
  const RAMP = new Map<string, number>([
    ["--t-h1", 22],
    ["--t-h2", 15],
    ["--t-body", 13.5],
    ["--t-sm", 12],
    ["--t-cap", 10.5],
    ["--t-kpi", 26],
  ]);
  const FLOOR_PX = Math.min(...RAMP.values());

  // THE RAMP IS A LADDER, NOT A FLOOR, and grading against `FLOOR_PX` alone
  // let anything above the smallest step through. `11px` is on no rung of this
  // design and was accepted by every row below, because 11 > 10.5. The rows
  // now require MEMBERSHIP: a literal must be a step the design actually
  // contains. `FLOOR_PX` survives only in the message text and in the row that
  // pins the ramp to the tokens.
  const RUNGS = new Set(RAMP.values());

  it("declares the ramp it grades against", () => {
    // Read from the tokens rather than trusted: if the design's smallest step
    // moves, this floor moves with it and the rows below say so.
    for (const [token, px] of RAMP) expect(TOKENS.get(token), `${token}`).toBe(`${px}px`);
    expect(FLOOR_PX).toBe(10.5);
  });

  it.each(SHELL_CSS.map((path) => [path.slice(path.lastIndexOf("/") + 1), path] as const))(
    "%s declares no text below the ramp's smallest step",
    (_name, path) => {
      const source = readFileSync(path, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
      const undersized = [...source.matchAll(/font(?:-size)?:\s*(?:\d{3}\s+)?(\d+(?:\.\d+)?)px/g)]
        .map((match) => Number(match[1] ?? Number.NaN))
        .filter((px) => !RUNGS.has(px));
      expect(undersized, `font sizes off the ramp: ${undersized.join(", ")}`).toEqual([]);
    },
  );

  it("catches a size written in JSX, not only one written in CSS", () => {
    // THE GAP THIS CLOSES, and it was a live one: the tab count shipped as an
    // inline `fontSize: "9px"` in components.tsx and a stylesheet sweep never
    // saw it. A floor that only holds in the files you remembered to check is
    // not a floor.
    const sources = [resolve(REPO, "frontend/src/shell"), resolve(REPO, "frontend/src/components"), resolve(REPO, "frontend/src/preview")].flatMap(
      (dir) =>
        readdirSync(dir)
          .filter((name) => /\.tsx?$/.test(name) && !/\.test\./.test(name))
          .map((name) => resolve(dir, name)),
    );

    const undersized = sources.flatMap((path) =>
      [...readFileSync(path, "utf8").matchAll(/fontSize:\s*"(\d+(?:\.\d+)?)px"/g)]
        .map((match) => Number(match[1] ?? Number.NaN))
        .filter((px) => !RUNGS.has(px))
        .map((px) => `${path.slice(path.lastIndexOf("/") + 1)}: ${px}px`),
    );

    expect(sources.length, "found no components to scan").toBeGreaterThan(5);
    expect(undersized, `inline font sizes off the ramp: ${undersized.join(", ")}`).toEqual([]);
  });

  it("reads real declarations, not an empty match set", () => {
    // NON-VACUITY. A regex that matched nothing would satisfy every row above.
    // Counted as literals OR ramp tokens, because a ported sheet paints
    // `var(--t-sm)` where the old one wrote `12px`.
    const all = SHELL_CSS.flatMap((path) => [
      ...readFileSync(path, "utf8").matchAll(/font(?:-size)?:\s*(?:\d{3}\s+)?(?:\d+(?:\.\d+)?px|var\(--t-[a-z0-9]+\))/g),
    ]);
    expect(all.length).toBeGreaterThan(20);
  });

  it("gives table cells the body size, not the caption or metadata rung", () => {
    // A cell is content the operator reads, so it takes `--t-body`; captions
    // (`th`, `.lhead`) take `--t-cap`. Accepts the token or a literal at or
    // above the body step.
    const cell = parseRules(BASE_CSS).find((rule) => rule.selector.split(",").map((s) => s.trim()).includes("td"));
    const size = property(cell as Rule, "font-size");
    expect(size, "no td font-size in base.css").toBeDefined();
    const token = /^var\((--t-[a-z0-9]+)\)$/.exec(size ?? "")?.[1];
    const px = token ? (RAMP.get(token) ?? Number.NaN) : Number(/^(\d+(?:\.\d+)?)px$/.exec(size ?? "")?.[1] ?? Number.NaN);
    // A RUNG, AND NOT BELOW THE BODY RUNG -- two conditions, because either
    // alone is satisfiable by something wrong. The floor moved 14 -> 13.5 when
    // the ramp did, and the auditor's rule it came from ("14px for any content
    // the user reads") is not weakened by that: 13.5 IS this design's reading
    // size, approved with the ramp, and it is graded at 4.5:1 above. What the
    // old `>= 13.5` also accepted was 14, 16, 20 -- sizes on no rung, which is
    // how a cell drifts off the design one literal at a time.
    expect(RUNGS.has(px), `td is ${size}, which is on no rung of the ramp`).toBe(true);
    expect(px, `td is ${size}`).toBeGreaterThanOrEqual(RAMP.get("--t-body") ?? Number.NaN);
  });
});
