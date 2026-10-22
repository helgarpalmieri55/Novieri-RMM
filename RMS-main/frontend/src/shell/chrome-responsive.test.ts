/**
 * Chrome and responsive guards added in UX remediation cycle 12.
 *
 * WHY HERE AND NOT IN `src/test/responsive.test.ts`. That file owns the
 * breakpoint inventory from cycle 10 and is guarded by a different slice of
 * this programme; these are the four declarations cycle 12 added, kept beside
 * the stylesheets they assert on.
 *
 * WHY TEXT AND NOT A RENDER. jsdom implements no cascade and evaluates no
 * media query -- `getComputedStyle` there reports values no browser would
 * paint, so a layout assertion against it is worse than none. The honest
 * claim a test can make about a stylesheet is "this rule is present"; what a
 * browser does with it is a job for a real viewport, and saying so is part of
 * the guard.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

/** Comments stripped: every one of these files quotes, in prose, the exact
 *  declaration it replaced, and a regex over raw text matches the explanation
 *  rather than the rule. */
const read = (path: string) =>
  readFileSync(resolve(import.meta.dirname, path), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

const SHELL = read("./shell.css");
const COMPONENTS = read("./components.css");
const BASE = read("./base.css");

/** The body of a rule, by selector, anchored so `.tab` does not match `.tabs`. */
function rule(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\>]/g, "\\$&");
  const found = new RegExp(`(?:^|[},])\\s*${escaped}\\s*\\{([^}]*)\\}`, "m").exec(css);
  return found?.[1] ?? "";
}

describe("the parser sees real rules", () => {
  it("reads declarations rather than an empty string", () => {
    // NON-VACUITY. `rule()` returning "" for everything would fail each
    // assertion below with an identical message, and a renamed file would look
    // exactly like a deleted declaration. Anchor on rules that predate cycle
    // 12 and are not among the things being asserted.
    expect(rule(COMPONENTS, ".grid")).toContain("display: grid");
    expect(rule(BASE, "table")).toContain("border-collapse");
    expect(rule(SHELL, ".collapse-btn")).toContain("width: 30px");
  });
});

describe("the asymmetric dashboard row sizes to its content", () => {
  it("does not stretch the short column to the tall one's height", () => {
    // "roughly 800px of pure white space sits below the last job row ... the
    // emptiness reads as 'content failed to load' rather than 'this card is
    // short'." Grid's default is `stretch`, so the absence of this line IS the
    // defect -- there is nothing else to remove.
    expect(rule(COMPONENTS, ".grid-2")).toMatch(/align-items:\s*start/);
  });

  it("leaves the symmetric grids stretching, where equal heights are the design", () => {
    // Non-vacuity in the other direction: a blanket `align-items: start` on
    // `.grid` would satisfy the assertion above and make every peer row in the
    // console ragged.
    expect(rule(COMPONENTS, ".grid")).not.toMatch(/align-items/);
    expect(rule(COMPONENTS, ".grid-3")).not.toMatch(/align-items/);
    expect(rule(COMPONENTS, ".grid-4")).not.toMatch(/align-items/);
  });
});

describe("tables scroll inside their card", () => {
  it("keeps the horizontal scroll on the wrapper", () => {
    expect(rule(COMPONENTS, ".table-wrap")).toMatch(/overflow-x:\s*auto/);
  });

  it("gives the table a floor, so a narrow viewport scrolls instead of squashing", () => {
    // Half a fix is what shipped before this: `overflow-x: auto` on a wrapper
    // around an auto-layout table never scrolls, because such a table never
    // exceeds its box -- it compresses columns until `linux Ubuntu 26.04 LTS`
    // is a stack of one-word lines. The floor is what makes the wrapper's
    // overflow reachable.
    const floor = /min-width:\s*(\d+)px/.exec(rule(COMPONENTS, ".table-wrap > table"))?.[1];
    expect(floor, "no min-width on `.table-wrap > table`").toBeDefined();
    expect(Number(floor)).toBeGreaterThanOrEqual(480);
    // ...and not so high that it forces a scrollbar on a desktop card.
    expect(Number(floor)).toBeLessThanOrEqual(760);
  });
});

/**
 * The type ramp's steps, read from `index.css` rather than retyped, so this
 * file cannot disagree with the design about what a legal size is.
 * `contrast.test.ts` grades the same ladder across every shell sheet; this
 * file exists for the SHORTHAND (`font: 10px/1.7 ...`), which a `font-size:`
 * sweep does not see and which shipped audit hashes at 10px for two cycles.
 */
const RUNGS = new Set(
  [...readFileSync(resolve(import.meta.dirname, "../index.css"), "utf8").matchAll(/--t-[a-z0-9]+:\s*(\d+(?:\.\d+)?)px/g)].map(
    (match) => Number(match[1]),
  ),
);

describe("the ramp holds for the shorthand too", () => {
  it("read the ramp at all, so the rows below are not grading against nothing", () => {
    // An empty `RUNGS` fails safe wherever a literal exists (everything is
    // "off the ramp"), but passes vacuously on a sheet that has none -- which
    // is two of the three rows below. A moved or renamed `index.css` is
    // therefore worth its own case rather than a silent pass.
    expect(RUNGS.size).toBe(6);
    expect([...RUNGS].sort((a, b) => a - b)).toEqual([10.5, 12, 13.5, 15, 22, 26]);
  });

  // THE GAP THIS EXISTS FOR. `src/test/contrast.test.ts` enforces the floor by
  // matching `font-size:`. `.code` declared `font: 10px/1.7 ui-monospace, ...`
  // -- the shorthand -- and carried enrolment tokens, audit hashes and the
  // JSON fallback at 10px for two cycles without the sweep ever seeing it.
  it.each([
    ["shell.css", SHELL],
    ["components.css", COMPONENTS],
    ["base.css", BASE],
  ])("%s writes no font size that is off the ramp, in either spelling", (_name, css) => {
    const sizes = [
      ...css.matchAll(/font-size:\s*(\d+(?:\.\d+)?)px/g),
      ...css.matchAll(/\bfont:\s*(?:\w+\s+)*?(\d+(?:\.\d+)?)px/g),
    ].map((match) => Number(match[1]));
    // A RUNG, NOT A FLOOR -- and the floor this row used to state was wrong
    // in both directions. `px < 12` REJECTED a legitimate `10.5px`, which is
    // `--t-cap`, a step this design ships; and it ACCEPTED `11px`, which is a
    // step it does not. The ramp is a ladder, so membership is the test.
    //
    // NON-VACUITY, and it is deliberately collective rather than per-file.
    // The v3 port names every size from the ramp (`var(--t-sm)`), so shell.css
    // and base.css carry no px literal at all -- that is the state this row
    // exists to reach, not a parser fault, and asserting a count per file
    // would only assert that tokens exist. What must not happen is the REGEX
    // matching nothing anywhere, which would mean a rename or a lost pattern;
    // `the shorthand it is meant to police` below pins that for the shorthand,
    // and components.css's `font: 12px/1.7 var(--mono)` is the live case.
    expect(sizes.filter((px) => !RUNGS.has(px))).toEqual([]);
  });

  it("still sees the shorthand it is meant to police", () => {
    // Non-vacuity for the row above: if the shorthand regex matched nothing,
    // `.code` could go back to 10px and every row would stay green.
    const shorthand = [...COMPONENTS.matchAll(/\bfont:\s*(?:\w+\s+)*?(\d+(?:\.\d+)?)px/g)];
    expect(shorthand.length).toBeGreaterThan(0);
  });
});

describe("the touch floor covers what the auditors measured", () => {
  const coarse = () => {
    const block = /@media\s*\(pointer:\s*coarse\)\s*\{([\s\S]*?)\n\}/.exec(SHELL)?.[1];
    expect(block, "no (pointer: coarse) block in shell.css").toBeDefined();
    return block!;
  };

  it("includes the form controls, which are most of the 49", () => {
    // "measuring every visible `button, a, input, select` on /settings, 49
    // have a rendered height below 44px." The cycle-10 list was buttons and
    // links; every text field and select in the console kept 36px.
    for (const selector of [".search-field", ".select-field", ".input-field", "textarea"]) {
      expect(coarse(), `${selector} keeps its 36px desktop height on touch`).toContain(selector);
    }
    expect(coarse()).toMatch(/min-height:\s*44px/);
  });

  it("fixes the collapse chevron on both axes, not only on height", () => {
    // "the 30x30 chevron fails on both axes" -- and a `min-height` floor alone
    // leaves it 30px wide, which is the axis the auditor called out.
    expect(coarse()).toMatch(/\.collapse-btn\s*\{[^}]*min-width:\s*44px/s);
  });
});

describe("motion is optional", () => {
  it("mutes the drawer, the rail and the hover lift when the user asks", () => {
    // Auditor A logged a `(prefers-reduced-motion: reduce)` block as a
    // positive; none was in this application's CSS, and cycle 10 then added a
    // full-height drawer that slides in over the page.
    const block = /@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([\s\S]*?)\n\}/.exec(
      SHELL,
    )?.[1];
    expect(block, "no (prefers-reduced-motion: reduce) block in shell.css").toBeDefined();
    for (const selector of [".sidebar", ".app-shell", ".btn"]) {
      expect(block, `${selector} keeps its animation under reduced motion`).toContain(selector);
    }
    expect(block).toMatch(/transition-duration:\s*\.01ms/);
  });

  it("does not remove the transition outright, which would strand the drawer", () => {
    // `visibility` is sequenced off the transform's duration (`transition:
    // transform .22s ease, visibility 0s linear .22s`). `transition: none`
    // never fires, so the closed drawer would stay visible and keep its tab
    // stops -- the defect the visibility swap exists to prevent.
    const block = /@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([\s\S]*?)\n\}/.exec(
      SHELL,
    )?.[1];
    expect(block).not.toMatch(/transition:\s*none/);
  });
});
