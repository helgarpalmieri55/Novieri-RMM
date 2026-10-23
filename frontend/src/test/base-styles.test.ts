/**
 * The element-level styles must stay present, and the rules the console
 * needed beyond the design must follow the design rather than lead it.
 *
 * THE BUG THIS EXISTS FOR, WHICH SHIPPED AND WAS INVISIBLE. Two CSS extraction
 * passes selected rules from the first design BY CLASS NAME. Every rule the
 * design writes against a bare element -- `table`, `th`, `td`, `button`, `a`,
 * `svg`, and the form-control font reset -- was dropped silently. Nothing
 * caught it: the DOM was right, the class names were right, `tsc` was clean
 * and 541 tests passed. In a browser the table headers ran together with no
 * padding, no borders and no colour, because `th` and `td` had no styling
 * whatsoever.
 *
 * A test cannot see "it looks wrong". It CAN see "the rule is missing", which
 * is the part that was actually absent -- and it can see "the two files
 * disagree", which is how a value drifts from the design without anyone
 * deciding it should.
 *
 * DESIGN AUTHORITY: `docs/design/itms-prototype-v3.html` (2026-09-07). The
 * rules the console needs that the mock had no occasion to draw live in its
 * "console additions" block, written in v3's vocabulary; the sheets under
 * `src/shell` transcribe them. Every comparison below reads that block.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { en } from "../i18n/en";
import { kindOf } from "../shell/viz";

const BASE = resolve(import.meta.dirname, "../shell/base.css");
const COMPONENTS = resolve(import.meta.dirname, "../shell/components.css");
const OVERLAYS = resolve(import.meta.dirname, "../shell/overlays.css");
const DESIGN = resolve(import.meta.dirname, "../../../docs/design/itms-prototype-v3.html");

/**
 * The declarations of one rule, by selector, normalised and sorted.
 *
 * Anchored on `selector {` at the start of a TRIMMED line so `.modal` cannot
 * match `.modal.open` or `.modal-body`, and so the design file's indentation
 * reads the same as the stylesheet's none. The design's console-additions
 * block is written `selector { ... }` one rule per line for exactly this
 * reader; its mock rules (`.btn{...}`) are not, and are not compared here.
 */
function declarations(css: string, selector: string): string[] {
  const lines = css.replace(/\/\*[\s\S]*?\*\//g, "").split("\n");
  const start = lines.findIndex((line) => line.trim().startsWith(`${selector} {`));
  if (start < 0) return [];
  const end = lines.findIndex((line, i) => i >= start && line.trimEnd().endsWith("}"));
  const rule = lines.slice(start, end + 1).join("\n");
  const body = rule.slice(rule.indexOf("{") + 1, rule.lastIndexOf("}"));
  return body
    .split(";")
    .map((one) => one.replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .sort();
}

describe("element-level styles", () => {
  const css = readFileSync(BASE, "utf8");

  it("is not empty, so the assertions below mean something", () => {
    // NON-VACUITY: an empty file would satisfy nothing below by containing
    // nothing, and `toContain` on an empty string simply fails -- but the
    // count check is the honest guard against a file trimmed to one rule.
    expect(css.length).toBeGreaterThan(500);
  });

  it.each([
    ["table", "border-collapse"],
    ["th", "padding"],
    ["td", "padding"],
    ["button, input, select, textarea", "font"],
    ["a", "text-decoration"],
    ["svg", "display"],
  ])("styles bare `%s`", (selector, property) => {
    // ANCHORED AT LINE START, then read to the closing brace: `indexOf("a {")`
    // matches inside `textarea {`, and a single-line check cannot see the
    // properties of a multi-line rule like `th`.
    const lines = css.split("\n");
    const start = lines.findIndex((line) => line.startsWith(`${selector} {`));
    expect(
      start,
      `no rule for \`${selector}\` -- the class-based extractor drops these`,
    ).toBeGreaterThanOrEqual(0);
    const end = lines.findIndex((line, i) => i >= start && line.trimEnd().endsWith("}"));
    expect(lines.slice(start, end + 1).join("\n")).toContain(property);
  });

  it("gives the table header v3's caption look and the cell v3's row look", () => {
    // v3 has no `<table>`; its list is `.lhead` over `.lrow .cell`. The
    // console keeps table semantics and transcribes those two looks onto
    // `th` and `td`, so the two read as one list.
    expect(declarations(css, "th")).toContain("font-size: var(--t-cap)");
    expect(declarations(css, "th")).toContain("text-transform: uppercase");
    expect(declarations(css, "td")).toContain("font-size: var(--t-body)");
    expect(declarations(css, "td")).toContain("border-bottom: 1px solid var(--line)");
  });

  it("loads BEFORE the class rules that override it", () => {
    // Order is load-bearing: `.pill` and `.btn` refine `button` and `span`,
    // and a base layer imported afterwards would win the cascade on equal
    // specificity.
    expect(readFileSync(COMPONENTS, "utf8").trimStart()).toMatch(/^@import "\.\/base\.css";/);
  });
});

/**
 * THE SAME DEFECT AS `th`, ON THE DESTRUCTIVE PATH.
 *
 * `ConfirmDialog` and the shared `Dialog` both render `class="modal open"`,
 * and `.modal`, `.modal.open` and `.modal-body` were once never transcribed
 * at all: the rendered result was a `position: static` block in the document
 * flow, below the fold, with the scrim painted ON TOP of it. Twelve unit
 * tests passed on it throughout, because jsdom has no cascade and no layout.
 *
 * v3's mock never opens a modal, so the rules were ADDED TO THE DESIGN in its
 * vocabulary (`--surface`, `--line`, `--r-panel`, `--shadow`) and are
 * transcribed from there. jsdom still cannot see the geometry --
 * `scripts/a11y-browser.mjs` measures it in Chromium and asserts
 * centred/fixed/above-the-scrim. What a test here CAN see is the part that
 * was actually absent: the rules, and that they still say what the design
 * says.
 */
describe("the modal's own rules, which the first port left in the design file", () => {
  const overlays = readFileSync(OVERLAYS, "utf8");
  const design = readFileSync(DESIGN, "utf8");

  it.each([".modal", ".modal.open", ".modal-body"])(
    "transcribes `%s` from the design, declaration for declaration",
    (selector) => {
      const ported = declarations(overlays, selector);
      expect(ported, `no \`${selector}\` rule in overlays.css`).not.toEqual([]);
      expect(declarations(design, selector), `no \`${selector}\` rule in the design`).not.toEqual([]);
      expect(ported).toEqual(declarations(design, selector));
    },
  );

  it("puts the dialog above its own scrim", () => {
    // THE FAILURE MODE, STATED AS A NUMBER. `.overlay` was z-index 70 and the
    // dialog had no `z-index` at all, so the dim painted over the thing it
    // was dimming for. Comparing the two is the assertion; hardcoding 95 on
    // its own would pass just as happily with the scrim raised to 200.
    const z = (selector: string) =>
      Number(
        declarations(overlays, selector)
          .find((one) => one.startsWith("z-index:"))
          ?.split(":")[1]
          ?.trim(),
      );
    expect(z(".overlay")).toBeGreaterThan(0);
    expect(z(".modal")).toBeGreaterThan(z(".overlay"));
  });

  it("takes the dialog out of the document flow", () => {
    expect(declarations(overlays, ".modal")).toContain("position: fixed");
  });
});

/**
 * `.sr-only` HAS TO BEAT AN UNLAYERED ELEMENT RULE, and the reason is a
 * cascade-layer defeat no guard reading stylesheets as text could have seen.
 *
 * Tailwind's `sr-only` lives in the `utilities` layer. `table { width: 100% }`
 * in base.css is unlayered, and unlayered author CSS beats any layered rule
 * regardless of specificity -- so the charts' screen-reader tables laid out at
 * the full width of their containing block and put 314px of nothing into the
 * document's horizontal scroll at 1440, on every screen that draws a chart.
 * WCAG 2.2 SC 1.4.10.
 */
describe("the visually-hidden utility, against this file's own element rules", () => {
  const css = readFileSync(BASE, "utf8");
  const design = readFileSync(DESIGN, "utf8");

  it("redeclares `.sr-only` here, unlayered, as the design declares it", () => {
    expect(css).toMatch(/^\.sr-only \{/m);
    const rule = declarations(css, ".sr-only");
    expect(rule).toContain("position: absolute");
    expect(rule).toContain("width: 1px");
    expect(rule).toContain("overflow: hidden");
    expect(rule).toEqual(declarations(design, ".sr-only"));
  });

  it("keeps this file out of every cascade layer, which is the whole mechanism", () => {
    // A class (0,1,0) beats a type selector (0,0,1) ONLY inside the same
    // layer. Wrap this file in `@layer base` and Tailwind's utilities -- and
    // its preflight -- start winning and losing by layer order instead, which
    // is precisely the fight `.sr-only` just lost.
    expect(css.replace(/\/\*[\s\S]*?\*\//g, "")).not.toMatch(/@layer/);
  });

  it("still declares the element rule it has to out-rank, so this is not vacuous", () => {
    // The falsifier. If `table { width: 100% }` ever leaves this file the
    // guard above stops being about anything, and it should be re-derived
    // rather than kept green by accident.
    expect(declarations(css, "table")).toContain("width: 100%");
  });
});

/**
 * THE BUTTON'S STATES, WHICH A MOCK NEVER DRAWS. A disabled button used to be
 * pixel-identical to an enabled one, `cursor: pointer` included; a hovered one
 * lifted by a pixel the eye did not register; a focused one fell back to the
 * user agent's ring. v3 draws none of the three, so they were added to the
 * design in its own vocabulary and are transcribed from there.
 *
 * NOT A CONTRAST ASSERTION. SC 1.4.11 exempts inactive components from the
 * 3:1 boundary floor explicitly and `contrast.test.ts` records that
 * exemption; what is required is that the state be distinguishable, so that
 * is what is checked.
 */
describe("the button's states", () => {
  const components = readFileSync(COMPONENTS, "utf8");
  const design = readFileSync(DESIGN, "utf8");

  it.each([".btn:hover", ".btn.primary", ".btn.danger", ".btn:focus-visible", ".btn:disabled"])(
    "transcribes `%s` from the design, declaration for declaration",
    (selector) => {
      const ported = declarations(components, selector);
      expect(ported, `no \`${selector}\` rule in components.css`).not.toEqual([]);
      expect(ported).toEqual(declarations(design, selector));
    },
  );

  it("changes the fill, the edge, the label and the cursor when disabled -- not just one of them", () => {
    const rule = declarations(components, ".btn:disabled");
    expect(rule).toContain("cursor: not-allowed");
    for (const property of ["color:", "background:", "border-color:", "box-shadow:"]) {
      expect(rule.some((one) => one.startsWith(property)), `${property} is unchanged`).toBe(true);
    }
  });

  it("puts the disabled rule after the variants it has to flatten, since they share its specificity", () => {
    // `.btn:disabled`, `.btn.pri`, `.btn.primary`, `.btn.danger` and
    // `.btn:hover` are all (0,2,0). Source order is the entire mechanism by
    // which a disabled primary stops being accent-filled, so move this rule
    // up the file and the fix silently stops working.
    const at = (needle: string) => components.indexOf(`\n${needle} {`);
    for (const earlier of [".btn:hover", ".btn.pri", ".btn.primary", ".btn.danger"]) {
      expect(at(earlier), `no \`${earlier}\` rule`).toBeGreaterThan(0);
      expect(at(".btn:disabled")).toBeGreaterThan(at(earlier));
    }
  });

  it("draws the focus ring outside the button, where it is not accent on accent", () => {
    // `.btn.pri` is filled with the one accent v3 has; a ring drawn against
    // that fill is 1:1. `contrast.test.ts` grades the ring against the page.
    const offset = declarations(components, ".btn:focus-visible").find((one) => one.startsWith("outline-offset:"));
    expect(offset).toBeDefined();
    expect(Number(/(-?\d+)px/.exec(offset ?? "")?.[1])).toBeGreaterThan(0);
  });
});

/**
 * THE CHIP'S DISABLED STATE, WHICH `Chips` NEVER DREW UNTIL A SETTINGS TAB
 * NEEDED ONE. Every tab offering a fixed enumeration to a reader who could
 * not save had hand-rolled a whole parallel `<select disabled>` beside the
 * live chip group, because a chip had nothing to render for "inert" — the
 * gap `ChipsField`'s own doc records. `.chip:disabled` is added to the
 * design in its own vocabulary (`.btn:disabled`'s own four properties,
 * transcribed) and is transcribed from there, the same shape as "the
 * button's states" above.
 */
describe("the chip's disabled state", () => {
  const components = readFileSync(COMPONENTS, "utf8");
  const design = readFileSync(DESIGN, "utf8");

  it("transcribes `.chip:disabled` from the design, declaration for declaration", () => {
    const ported = declarations(components, ".chip:disabled");
    expect(ported, "no `.chip:disabled` rule in components.css").not.toEqual([]);
    expect(ported).toEqual(declarations(design, ".chip:disabled"));
  });

  it("changes the fill, the edge and the ink, and reads as inert rather than merely grey", () => {
    const rule = declarations(components, ".chip:disabled");
    expect(rule).toContain("cursor: not-allowed");
    for (const property of ["color:", "background:", "border-color:"]) {
      expect(rule.some((one) => one.startsWith(property)), `${property} is unchanged`).toBe(true);
    }
  });

  it("puts the disabled rule after `.chip.on`, since a selected-and-disabled chip has to flatten to it", () => {
    // `.chip.on` and `.chip:disabled` are both (0,2,0): source order is the
    // entire mechanism by which a disabled, previously-selected chip stops
    // being accent-filled, the same reason `.btn:disabled` sits after
    // `.btn.on` above.
    const at = (needle: string) => components.indexOf(`\n${needle} {`);
    expect(at(".chip.on"), "no `.chip.on` rule").toBeGreaterThan(0);
    expect(at(".chip:disabled")).toBeGreaterThan(at(".chip.on"));
  });
});

/**
 * THE TILE IS A LINK. A number on a dashboard is a promise that there is a
 * list behind it, so the console's tile renders as one and has to answer the
 * pointer and the keyboard. v3 draws tiles at rest only; the hover and the
 * ring were added to the design and are transcribed from there.
 */
describe("the tile's states", () => {
  const viz = readFileSync(resolve(import.meta.dirname, "../shell/viz.css"), "utf8");
  const design = readFileSync(DESIGN, "utf8");

  it.each(["a.tile:hover", "a.tile.crit:hover", "a.tile.warn:hover", ".tile:focus-visible"])(
    "transcribes `%s` from the design, declaration for declaration",
    (selector) => {
      const ported = declarations(viz, selector);
      expect(ported, `no \`${selector}\` rule in viz.css`).not.toEqual([]);
      expect(ported).toEqual(declarations(design, selector));
    },
  );
});

/**
 * A `<code class="code">` IN A SENTENCE MUST ESTABLISH ITS OWN BLOCK.
 *
 * `.code` sets `padding: 12px`. On an INLINE box that padding does not grow
 * the line box (CSS 2.1 §10.6.1) -- it bleeds 12px above and 12px below a
 * ~20px line and paints the chip's background over whatever text is there.
 * On the enrolment screen that was the sentence telling the operator to copy
 * the token before it disappears, covered by the token.
 *
 * WHAT MAKES THIS FALSIFIABLE RATHER THAN DECORATIVE: the scoping. Widen the
 * selector to `.code` and every `<pre class="code">` in the product shrinks
 * from a full-width block to its content width, so the second test below is
 * the one that fails if someone "simplifies" the first.
 */
describe("the inline code chip", () => {
  const components = readFileSync(COMPONENTS, "utf8");
  const design = readFileSync(DESIGN, "utf8");

  it("establishes a block, and follows the design file rather than leading it", () => {
    const ported = declarations(components, "code.code");
    expect(ported, "no `code.code` rule in components.css").not.toEqual([]);
    expect(ported).toContain("display: inline-block");
    expect(ported).toEqual(declarations(design, "code.code"));
  });

  it("does not reach the block usages, which must stay full width", () => {
    // `<pre class="code">` is the block spelling -- JobsPage, attributes,
    // AgentReleases, the enrolment token -- and `<code class="code">` the
    // inline one. The split is exact, so the fix can be scoped exactly.
    expect(declarations(components, ".code")).not.toContain("display: inline-block");
    expect(declarations(components, ".code")).toEqual(declarations(design, ".code"));
  });
});

/**
 * A COMPONENT OWNS THE STYLESHEET IT PAINTS WITH.
 *
 * `confirm.tsx` renders `.overlay` and `.modal` and once imported neither. The
 * rules reached it only because `overlays.css` is imported by CommandPalette,
 * Drawer and Toaster, all of which `AppShell` happens to mount -- so every
 * confirmation in the product was styled by a side effect of three unrelated
 * components being on screen. Rendered outside that shell it drew as an
 * unstyled static block below the fold.
 */
describe("the confirmation dialog's stylesheet", () => {
  it("is imported by the component that paints with it", () => {
    const source = readFileSync(
      resolve(import.meta.dirname, "../components/confirm.tsx"),
      "utf8",
    );
    expect(source).toMatch(/import\s+["']\.\.\/shell\/overlays\.css["']/);
  });

  it("is where the rules it needs actually live", () => {
    const overlays = readFileSync(OVERLAYS, "utf8");
    for (const selector of [".overlay", ".modal"]) {
      expect(declarations(overlays, selector), `no \`${selector}\` rule`).not.toEqual([]);
    }
  });
});

/**
 * THE DIALOG FIELD CONTAINER, AND THE MODIFIER THAT PROVES IT MUST EXIST.
 *
 * `.form-grid` is the dialog field container and `.field.full` spans it.
 * The first port carried the modifier and not the grid, so `grid-column: 1 /
 * -1` named a grid that existed nowhere and every dialog stacked its fields
 * with NOTHING between them. A rule that selects nothing fails silently; a
 * modifier that positions within a grid that is not there fails silently
 * too, and both had been doing so since the port.
 */
describe("the dialog form grid", () => {
  const components = readFileSync(COMPONENTS, "utf8");
  const design = readFileSync(DESIGN, "utf8");

  it("transcribes `.form-grid` from the design, declaration for declaration", () => {
    const ported = declarations(components, ".form-grid");
    expect(ported, "no `.form-grid` rule in components.css").not.toEqual([]);
    expect(ported).toEqual(declarations(design, ".form-grid"));
  });

  it("gives `.field.full` a grid to span, which is the whole reason it exists", () => {
    // `.field.full` declares `grid-column` and nothing else. If `.form-grid`
    // is ever dropped again, this modifier goes back to being a no-op on a
    // block-flow parent -- visibly identical to a plain field, and silent.
    expect(declarations(components, ".field.full")).toEqual(["grid-column: 1 / -1"]);
    expect(declarations(components, ".field.full")).toEqual(declarations(design, ".field.full"));
    expect(declarations(components, ".form-grid").join(" ")).toContain("display: grid");
  });

  it("drops the field's block-flow margin inside the grid, where the gap does that job", () => {
    expect(declarations(components, ".form-grid > .field")).toEqual(declarations(design, ".form-grid > .field"));
    expect(declarations(components, ".form-grid > .field")).toContain("margin-bottom: 0");
  });

  it("styles the console's caption span the way the design styles its label", () => {
    // `Field` wraps the control in the <label> and puts the caption in a
    // <span>; the design writes `.field label`. Both selectors carry the same
    // declarations, and the design records the span as a console addition.
    const span = declarations(design, ".field > span");
    expect(span, "no `.field > span` rule in the design").not.toEqual([]);
    expect(components).toMatch(/\.field label, \.field > span \{/);
  });
});

/**
 * THE CALLOUT'S TYPE SIZE, WHICH WAS ONCE OFF THE RAMP ENTIRELY.
 *
 * `.notice` is the one callout component in the product -- jobs, credentials,
 * enrolment, the error monitor, the ticket copilot and the patch panels all
 * paint with it -- and it once set 14px, a size the ramp does not contain.
 * A notice is a sentence, so it takes the sentence rung, `--t-body`; the two
 * tones set colour only, so the family cannot split in three sizes; and every
 * ink is graded on its own wash in `contrast.test.ts`.
 */
describe("the callout family", () => {
  const components = readFileSync(COMPONENTS, "utf8");
  const design = readFileSync(DESIGN, "utf8");
  const INDEX = resolve(import.meta.dirname, "../index.css");

  it("sets the body rung, which is the size the ramp actually contains", () => {
    // Written against the RAMP rather than against a number: `--t-body` is
    // the design's sentence size, and the console's `--text-body` utility
    // resolves to it, so a `.notice` at any other step is a fork.
    const index = readFileSync(INDEX, "utf8");
    expect(/--t-body:\s*13\.5px/.test(index), "no --t-body in index.css").toBe(true);
    expect(/--text-body:\s*var\(--t-body\)/.test(index), "--text-body is not the ramp's body step").toBe(true);
    expect(declarations(components, ".notice")).toContain("font-size: var(--t-body)");
  });

  it("is not on a size the ramp does not contain", () => {
    // THE FALSIFIER, named: 14px and 16px are the sizes it drifted to before.
    const rule = declarations(components, ".notice");
    expect(rule.some((one) => /^font-size: \d/.test(one))).toBe(false);
  });

  it("transcribes the whole rule from the design, declaration for declaration", () => {
    const ported = declarations(components, ".notice");
    expect(ported, "no `.notice` rule in components.css").not.toEqual([]);
    expect(ported).toEqual(declarations(design, ".notice"));
  });

  it("keeps the two tone variants on the same rule, so the family stays one size", () => {
    // NON-VACUITY for the row above: `.gold` and `.red` override colour only.
    // A `font-size` appearing on either is a family splitting in three.
    for (const selector of [".notice.gold", ".notice.red"]) {
      const rule = declarations(components, selector);
      expect(rule, `no \`${selector}\` rule`).not.toEqual([]);
      expect(rule.some((one) => one.startsWith("font-size:"))).toBe(false);
      expect(rule).toEqual(declarations(design, selector));
    }
  });
});

/**
 * EVERY STATE HAS A TONE, AND THE CERTIFICATE STATES KEEP THEIR COMPANY.
 *
 * The first design keyed `.status` off a slug of the label, so `revoked`,
 * `expired`, `expiring` and `renewing` matched no variant and fell through
 * to a colourless box -- a fleet list that coloured the healthy machine and
 * left the revoked one grey. v3 replaces the slug-keyed family with six
 * kinds, and `kindOf` is the one table that says which kind a word carries;
 * the base `.pill` stays colourless so nothing can wear a colour it has not
 * been given.
 */
describe("the pill family", () => {
  const components = readFileSync(COMPONENTS, "utf8");
  const design = readFileSync(DESIGN, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

  it.each([
    // A revoked or expired certificate is a failure that has already happened.
    ["revoked", "failed"],
    ["expired", "failed"],
    // An expiring one is a deadline, which is what `due` already means.
    ["expiring", "due"],
    // A renewing one is the platform reporting work it is doing by itself.
    ["renewing", "running"],
  ])("puts `%s` in the same kind as `%s`", (variant, alongside) => {
    expect(kindOf(variant)).toBe(kindOf(alongside));
  });

  it.each(["crit", "warn", "ok", "run", "mut", "info"])("paints `.p-%s` from tokens, as the design names it", (kind) => {
    const rule = declarations(components, `.p-${kind}`);
    expect(rule, `no \`.p-${kind}\` rule`).not.toEqual([]);
    expect(rule.some((one) => /^background: var\(--/.test(one))).toBe(true);
    expect(rule.some((one) => /^color: var\(--/.test(one))).toBe(true);
    expect(design).toContain(`.p-${kind}{`);
  });

  it("still leaves the bare `.pill` colourless, which is why the above matters", () => {
    // NON-VACUITY AND FALSIFIER IN ONE. If somebody "fixes" a missing kind
    // by giving `.pill` itself a fill, every kind passes for the wrong reason
    // and six kinds silently become one colour. The base rule is geometry.
    const base = declarations(components, ".pill");
    expect(base, "no `.pill` rule").not.toEqual([]);
    expect(base.some((one) => one.startsWith("background:"))).toBe(false);
    expect(base.some((one) => one.startsWith("color:"))).toBe(false);
  });

  it("has retired the slug-keyed `.status` family, so no state can fall through to it", () => {
    expect(components.replace(/\/\*[\s\S]*?\*\//g, "")).not.toMatch(/^\.status\b/m);
  });
});

/**
 * THE HORIZONTAL-SCROLL HINT, WHICH ONCE VEILED THE COLUMN IT POINTED AT.
 *
 * A wide table scrolls inside its card, and a sighted touch user got no cue
 * that the row continued. The first spelling of the cue was the obvious one:
 * a right-edge gradient, `linear-gradient(to right, transparent, var(--surface)
 * 72%)`, absolutely positioned over the scroller.
 *
 * The browser pass measured what that costs. Three person cells in the
 * trailing column of the harness page (`scripts/a11y-browser.mjs`, 390x844)
 * reported 1.28:1, 1.31:1 and 1.53:1 -- white initials on `--av-3` (#17703f,
 * 6.13:1 uncovered), `--av-4` (#9a6100, 5.14:1) and `--av-1` (#5b3fb0,
 * 7.55:1). The veil was reading as the background because the veil WAS the
 * background: the sweep blanks every glyph before it samples, so what it read
 * at those points was the gradient over the avatar's own fill -- #d8e7df,
 * #ebe0cd and #d4cdeb, each roughly a fifth of the tone under four fifths of
 * `--surface`.
 *
 * It took a while to see because the element is innocent from the inside.
 * `getComputedStyle` on the avatar returns the true fill; the overlay is a
 * SIBLING, so it appears in no opacity chain, no filter and no blend mode.
 * Only a screenshot sees it. That is the argument for the browser pass
 * existing at all, and it is why the two assertions below are written against
 * the two properties that make an overlay an overlay, rather than against the
 * one gradient that happened to be there.
 */
/**
 * PROSE GETS A MEASURE, AND THE NOTICE DID NOT HAVE ONE.
 *
 * `.ph .sub` has been capped at 70ch since the port. `.notice` never was, so on
 * a wide card its prose ran the full panel width -- roughly 1,380px at 1440,
 * about 190 characters a line, against the ~70 that is comfortable. The
 * operator ran an overlay tool over the deployed console and it flagged
 * exactly that on /devices, /add-devices and /device-groups.
 *
 * 76ch ON THE BOX, NOT 70, and the difference is the point: the box also
 * carries the glyph, the 10px gap and 28px of horizontal padding, so a 70ch
 * box would set text at about 64ch and read narrower than the subtitle beside
 * it. The arithmetic is why this is asserted rather than left to taste.
 */
describe("the callout's measure", () => {
  const components = readFileSync(COMPONENTS, "utf8");
  const design = readFileSync(DESIGN, "utf8");

  it("caps the notice, and follows the design file in doing so", () => {
    const ported = declarations(components, ".notice");
    expect(ported, "no `.notice` rule in components.css").not.toEqual([]);
    expect(ported).toContain("max-width: 76ch");
    expect(design, "the design file must carry the cap first").toMatch(
      /\.notice \{[^}]*max-width: 76ch/,
    );
  });

  it("is not narrower than the subtitle it sits beside", () => {
    // NON-VACUITY WITH A REASON. A cap that undercuts `.ph .sub`'s 70ch would
    // satisfy the row above while making the callout the narrowest prose on
    // the page -- the opposite defect, and one no tool would flag.
    const sub = /max-width:\s*(\d+)ch/.exec(declarations(components, ".ph .sub").join(" "))?.[1];
    const notice = /max-width:\s*(\d+)ch/.exec(declarations(components, ".notice").join(" "))?.[1];
    expect(sub, "no cap on `.ph .sub`").toBeDefined();
    expect(Number(notice)).toBeGreaterThanOrEqual(Number(sub));
  });
});

describe("the table's horizontal-scroll hint", () => {
  const components = readFileSync(COMPONENTS, "utf8");
  const design = readFileSync(DESIGN, "utf8");

  it("transcribes both rules from the design, declaration for declaration", () => {
    const hint = declarations(components, ".table-scroll-hint");
    expect(hint, "no `.table-scroll-hint` rule in components.css").not.toEqual([]);
    expect(hint).toEqual(declarations(design, ".table-scroll-hint"));
    expect(declarations(components, ".table-scroll")).toEqual(declarations(design, ".table-scroll"));
  });

  it("is laid out in flow, so it cannot come to sit on top of a cell", () => {
    // The defect was `position: absolute` on the hint plus `position:
    // relative` on the scroller to contain it. Either one alone is harmless;
    // it is the pair that makes an overlay, so both are asserted.
    expect(declarations(components, ".table-scroll-hint").join(" ")).not.toMatch(/position: (absolute|fixed)/);
    expect(declarations(components, ".table-scroll").join(" ")).not.toContain("position: relative");
  });

  it("paints no fill of its own, which is what a veil is made of", () => {
    // `background`, `background-image` and `backdrop-filter` all veil; the
    // hint needs none of them, because it sits beside the table rather than
    // over it. A cue that makes the content it points at unreadable is not a
    // cue, whatever it is drawn with.
    const hint = declarations(components, ".table-scroll-hint");
    expect(hint.some((one) => /^(background|backdrop-filter)\b/.test(one))).toBe(false);
  });

  it("still says something, so the three assertions above are not passing on an empty rule", () => {
    // NON-VACUITY. A deleted rule satisfies every prohibition above. The cue
    // has to still be a cue: it is right-aligned caption type in the muted
    // ink, and the component renders text into it.
    const hint = declarations(components, ".table-scroll-hint");
    expect(hint).toContain("font-size: var(--t-cap)");
    expect(hint).toContain("color: var(--muted)");
    expect(hint).toContain("justify-content: flex-end");
    const ui = readFileSync(resolve(import.meta.dirname, "../components/ui.tsx"), "utf8");
    // The words live in the catalogue now: the component renders the key, and
    // the English entry is the text this assertion has always been about.
    expect(ui).toMatch(/className="table-scroll-hint"[\s\S]{0,120}components\.ui\.scrollForMore/);
    expect(en.components.ui.scrollForMore).toBe("Scroll for more columns");
  });
});

/**
 * THE HELP SLOT'S BOX, AND WHY THE HELP NEEDED A RULE OF ITS OWN.
 *
 * `Field` wraps its control INSIDE the `<label>`, so anything rendered there
 * joins the control's accessible NAME -- `/agent-push/authorizations` shipped
 * a field whose accessible name was its whole help sentence. The help is
 * therefore a SIBLING of the label, wired with `aria-describedby`, and
 * `.field-wrap` is what makes caption / control / help one stack again.
 *
 * THE RULE THAT IS EASY TO FORGET IS `.field-wrap > .help`. The design styles
 * help as `.field .help`, a DESCENDANT selector, and moving the paragraph out
 * of the label moves it out of that selector's reach -- it would draw at body
 * size in full ink, reading as a second caption under the control rather than
 * as a hint. Nothing about that is visible to jsdom, to `tsc`, or to a unit
 * test on the component: the markup is right and the class is right. What a
 * test here CAN see is that the rule exists and still says what the design
 * says, which is the part that would actually be absent.
 */
describe("the field's help slot", () => {
  const components = readFileSync(COMPONENTS, "utf8");
  const design = readFileSync(DESIGN, "utf8");

  it.each([".field-wrap", ".field-wrap > .field", ".field-wrap.full", ".field-wrap > .help"])(
    "transcribes `%s` from the design, declaration for declaration",
    (selector) => {
      const ported = declarations(components, selector);
      expect(ported, `no \`${selector}\` rule in components.css`).not.toEqual([]);
      expect(declarations(design, selector), `no \`${selector}\` rule in the design`).not.toEqual([]);
      expect(ported).toEqual(declarations(design, selector));
    },
  );

  it("gives the help outside the label the type and ink it had inside it", () => {
    // THE FALSIFIER FOR THE ROW ABOVE, stated as the two properties that make
    // help read as help. Both files could agree on an empty rule.
    const outside = declarations(components, ".field-wrap > .help");
    const inside = declarations(components, ".field .help");
    expect(inside, "no `.field .help` rule to compare against").not.toEqual([]);
    for (const property of ["font-size:", "color:"]) {
      const want = inside.find((one) => one.startsWith(property));
      expect(outside, `${property} differs from \`.field .help\``).toContain(want);
    }
  });

  it("drops the wrapper's block-flow margin inside the grid, as the field's is dropped", () => {
    // `.field-wrap` carries a 14px bottom margin for block flow. In the grid
    // the 14px gap does that job, and a wrapper that kept its own margin would
    // drop every field carrying help 14px below the ones beside it -- the same
    // decision `.form-grid > .field` already records.
    expect(declarations(components, ".form-grid > .field-wrap")).toEqual(
      declarations(design, ".form-grid > .field-wrap"),
    );
    expect(declarations(components, ".form-grid > .field-wrap")).toContain("margin-bottom: 0");
  });

  it("moves the field's 560px measure out to the box that now holds it", () => {
    // `.field` caps itself at 560px; `.form-grid > .field` lifts the cap so a
    // field fills its cell. With help present the grid's child is
    // `.field-wrap`, so NEITHER selector matches the label any more -- a help
    // -bearing field would draw a 560px control beside a 670px one in the same
    // page-level grid row, and a `full` textarea in a modal would lose 42px to
    // the `.field.full` beside it. The wrapper takes the number over, and both
    // its children fill it.
    const capped = declarations(components, ".field").find((one) => one.startsWith("max-width:"));
    expect(capped, "no `max-width` on `.field` to move").toBeDefined();
    expect(declarations(components, ".field-wrap")).toContain(capped);
    expect(declarations(components, ".field-wrap > .field")).toContain("max-width: none");
  });

  it("lifts that measure inside the grid, exactly as the bare field's is lifted", () => {
    // The other half: in a grid the CELL is the measure. `.form-grid > .field`
    // already says so; the wrapper rule has to say the same thing or a field
    // carrying help is the one field in the row that will not fill its column.
    const lifted = declarations(components, ".form-grid > .field").find((one) =>
      one.startsWith("max-width:"),
    );
    expect(lifted, "no `max-width` on `.form-grid > .field` to mirror").toBeDefined();
    expect(declarations(components, ".form-grid > .field-wrap")).toContain(lifted);
  });

  it("bounds the help paragraph, which lost its cap by leaving the field", () => {
    // `.field .help` was capped only by sitting inside `.field`. Outside it,
    // in block flow, nothing bounds the measure -- a two-line hint would run
    // the full width of the page while the control above it stops at 560px.
    // The wrapper's cap is what puts it back, so this pins that the paragraph
    // has an ancestor carrying one and no cap of its own to fight it.
    expect(declarations(components, ".field-wrap")).toContain("max-width: 560px");
    expect(declarations(components, ".field-wrap > .help")).not.toContain("max-width: none");
  });

  it("puts the span modifier where the grid's child actually is", () => {
    // With help present the grid's child is the wrapper, not the label, so
    // `grid-column` has to be reachable on the wrapper. `.field.full` alone
    // would name a grid the label is no longer a child of -- silently, which
    // is exactly how `.form-grid`'s absence hid for a whole port.
    expect(declarations(components, ".field-wrap.full")).toEqual(["grid-column: 1 / -1"]);
    expect(declarations(components, ".field-wrap.full")).toEqual(declarations(components, ".field.full"));
  });
});

/**
 * THE GROUP HEADING A SETTINGS TAB HAS NOWHERE TO PUT, UNTIL NOW.
 *
 * v3's own settings screen names its sections with a styled div:
 * `<div style="font-weight:500"><icon> Maintenance window</div>`. The
 * console draws an `h3` in its place instead -- a titled `Panel` has already
 * spent the `h2` above it, and a screen reader navigates by headings, not by
 * font-weight -- but the type size, weight and colour are the mock's own
 * div rule, carried over unchanged. Only the element and the icon gap are
 * new, so those are exactly what a test here can lose without anyone
 * noticing: the markup would still be right, `tsc` would still be clean, and
 * the heading would draw at the wrong size in a browser.
 */
describe("a panel's fields, named and grouped", () => {
  const components = readFileSync(COMPONENTS, "utf8");
  const design = readFileSync(DESIGN, "utf8");

  it.each([".field-group", ".field-group > h3", ".field-group:last-child"])(
    "transcribes `%s` from the design, declaration for declaration",
    (selector) => {
      const ported = declarations(components, selector);
      expect(ported, `no \`${selector}\` rule in components.css`).not.toEqual([]);
      expect(declarations(design, selector), `no \`${selector}\` rule in the design`).not.toEqual([]);
      expect(ported).toEqual(declarations(design, selector));
    },
  );

  it("gives the heading the mock's own size, weight and colour", () => {
    // THE FALSIFIER FOR THE ROW ABOVE, stated as the properties a reader
    // would actually notice missing. Both files could agree on an empty rule.
    const heading = declarations(components, ".field-group > h3");
    for (const want of [
      `font-size: var(--t-body)`,
      "font-weight: 500",
      "color: var(--ink)",
    ]) {
      expect(heading, `\`.field-group > h3\` is missing \`${want}\``).toContain(want);
    }
  });
});

/**
 * THE DURATION CONTROL, WHICH RENDERED AS AN EMPTY 24px SLIVER AT EVERY
 * WIDTH. `.select-field { width: 100%; max-width: 100% }` above is
 * UNLAYERED, and `components/duration.tsx` used to size its unit `<select>`
 * with a Tailwind utility (`w-32`) that Tailwind v4 puts in `@layer
 * utilities` -- an unlayered rule beats a layered one regardless of
 * specificity, so that utility could never have won this cascade fight no
 * matter how it was spelled. A test cannot see the 24px/273px rendered
 * widths a real browser measured (jsdom has no cascade and no layout, which
 * is exactly why this shipped in the first place) -- what it CAN see is the
 * relationship that actually broke: whether the override lives in the same
 * unlayered bucket `.select-field` does, at higher specificity, rather than
 * in a layer that can never win there. `docs/design/itms-prototype-v3.html`,
 * "console additions, wave 9", is the authority; this pins the port
 * identical to it, declaration for declaration.
 */
describe("the duration control's own sizing, against the unlayered rule it has to out-rank", () => {
  const components = readFileSync(COMPONENTS, "utf8");
  const design = readFileSync(DESIGN, "utf8");

  it.each([".duration-field", ".duration-field > .input-field", ".duration-field > .select-field"])(
    "transcribes `%s` from the design, declaration for declaration",
    (selector) => {
      const ported = declarations(components, selector);
      expect(ported, `no \`${selector}\` rule in components.css`).not.toEqual([]);
      expect(declarations(design, selector), `no \`${selector}\` rule in the design`).not.toEqual([]);
      expect(ported).toEqual(declarations(design, selector));
    },
  );

  it("overrides the select's own width honestly -- same (unlayered) bucket, higher specificity", () => {
    // `.select-field` is a single class, specificity (0,1,0), declared on
    // the shared `.inp, .input-field, .select-field, .search-field,
    // textarea.input-field` rule -- not its own line, so this reads the raw
    // text rather than `declarations()` (which matches a selector at line
    // start only). A selector scoped under `.duration-field` is (0,2,0) and
    // wins in the same, unlayered bucket regardless of source order -- the
    // "scoped rule that wins honestly" the defect asked for, rather than
    // weakening `.select-field` itself (which 63 other selects in the
    // product still need at `width: 100%`).
    expect(components).toMatch(/\.select-field[^{]*\{[^}]*width: 100%/);
    expect(declarations(components, ".duration-field > .select-field")).toContain("width: auto");
    expect(components.replace(/\/\*[\s\S]*?\*\//g, "")).not.toMatch(/@layer/);
  });

  it("gives the number input a real flex-basis, not the 0% that collapsed it to its padding", () => {
    // THE FALSIFIER: a flex item's used main size comes from `flex-basis`,
    // not from `width`, once `flex-basis` is anything but `auto` -- so a
    // `flex-basis` of `0` (Tailwind's `flex-1`) is indistinguishable, to
    // layout, from never having overridden `.input-field`'s `width: 100%`
    // at all. The number has to be a real pixel floor.
    const rule = declarations(components, ".duration-field > .input-field");
    const basis = rule.find((one) => one.startsWith("flex:"));
    expect(basis, "no `flex` declaration on `.duration-field > .input-field`").toBeDefined();
    expect(Number(/flex:\s*\d+\s+\d+\s+(\d+)px/.exec(basis ?? "")?.[1])).toBeGreaterThan(0);
  });

  it("lets both children shrink below their content minimum, so the row fits a narrow panel", () => {
    // THE OVERFLOW DEFECT: a flex item's default `min-width: auto` floors a
    // shrinking item at its own min-content size regardless of how little
    // room is left, which is how the select came to sit ~13px outside a
    // 390px panel that is `overflow: hidden` -- past `.duration-field`'s own
    // row, never wide enough to reach `documentElement.scrollWidth`, which is
    // why the harmony gate called it clean.
    expect(declarations(components, ".duration-field")).toContain("min-width: 0");
    expect(declarations(components, ".duration-field > .input-field")).toContain("min-width: 0");
  });
});

/** Wave 10: the rail's second layer and the device page's scrolling tab strip. */
describe("the rail's open area and the scrolling tab strip", () => {
  const shell = readFileSync(resolve(import.meta.dirname, "../shell/shell.css"), "utf8");
  const components = readFileSync(COMPONENTS, "utf8");
  const design = readFileSync(DESIGN, "utf8");

  it.each([".nav-item.sub", ".nav-item.sub.active"])("transcribes `%s` from the design", (selector) => {
    const ported = declarations(shell, selector);
    expect(ported, `no \`${selector}\` rule in shell.css`).not.toEqual([]);
    expect(ported).toEqual(declarations(design, selector));
  });

  it.each([".tabs.scroll", ".tabs.scroll > .tab"])("transcribes `%s` from the design", (selector) => {
    const ported = declarations(components, selector);
    expect(ported, `no \`${selector}\` rule in components.css`).not.toEqual([]);
    expect(ported).toEqual(declarations(design, selector));
  });

  it("scrolls rather than folds, and keeps the hairline where the tabs sit", () => {
    const strip = declarations(components, ".tabs.scroll");
    expect(strip).toContain("flex-wrap: nowrap");
    expect(strip).toContain("overflow-x: auto");
    // The -1px overlap onto a border would be clipped by `overflow`.
    expect(declarations(components, ".tabs.scroll > .tab")).toContain("margin-bottom: 0");
  });
});
