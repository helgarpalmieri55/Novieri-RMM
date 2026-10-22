/**
 * The breakpoints, guarded at the stylesheet level.
 *
 * WHAT THE AUDIT MEASURED, and why a count is the right guard: "498 total
 * rules and 13 media-query blocks, of which only TWO are the application's own
 * responsive CSS -- both `(width <= 1300px)`." The other eleven were Tailwind's
 * inert container tiers and preference queries. A console that ships one
 * breakpoint has not made a layout decision about narrow screens; it has
 * avoided making one, and the `width=device-width` in `index.html` then
 * promises a phone the opposite.
 *
 * These assertions read the CSS as text rather than through a rendered layout,
 * because jsdom implements no cascade and evaluates no media query -- it will
 * happily report `getComputedStyle` values that no browser would ever paint.
 * Text is the honest thing to assert here. What a browser does with these rules
 * is a job for a real viewport, and that is stated rather than implied.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { en } from "../i18n/en";

const REPO = resolve(import.meta.dirname, "../../..");
/**
 * Read a stylesheet with its comments removed. These files carry long prose
 * blocks that quote the very declarations being replaced -- `min-width: 1120px`
 * appears in the comment explaining why it is gone -- and a regex over the raw
 * text happily matches the explanation instead of the rule.
 */
const read = (path: string) =>
  readFileSync(resolve(REPO, path), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

const INDEX = read("frontend/src/index.css");
const SHELL = read("frontend/src/shell/shell.css");
const COMPONENTS = read("frontend/src/shell/components.css");

/** The body of every `@media (max-width: Npx)` block in a stylesheet, by N. */
function widthBlocks(css: string): Map<number, string> {
  const blocks = new Map<number, string>();
  const opener = /@media\s*\(max-width:\s*(\d+)px\)\s*\{/g;
  for (let match = opener.exec(css); match; match = opener.exec(css)) {
    const width = Number(match[1]);
    let depth = 1;
    let at = match.index + match[0].length;
    const from = at;
    while (at < css.length && depth > 0) {
      if (css[at] === "{") depth += 1;
      else if (css[at] === "}") depth -= 1;
      at += 1;
    }
    blocks.set(width, (blocks.get(width) ?? "") + css.slice(from, at - 1));
  }
  return blocks;
}

const SHELL_BLOCKS = widthBlocks(SHELL);
const COMPONENT_BLOCKS = widthBlocks(COMPONENTS);

describe("the parser sees real rules", () => {
  it("finds the design's own 1300px breakpoint, which was always there", () => {
    // NON-VACUITY, and a specific one: if `widthBlocks` returned an empty map
    // -- a brace-counting bug, a renamed file -- every "has a breakpoint"
    // assertion below would fail loudly, but every "contains a selector"
    // assertion would too, and the two failures look alike. Anchor on a rule
    // that predates this cycle so a parser fault is distinguishable from a
    // missing breakpoint.
    expect(COMPONENT_BLOCKS.get(1300)).toContain(".stats-grid");
    expect(SHELL_BLOCKS.get(1300)).toContain(".profile-copy");
  });

  it("counts more app-authored breakpoints than the audit found", () => {
    // The audit's number was two, both at 1300px. Distinct widths, not rule
    // count: five rules at one width is still one layout decision.
    const widths = new Set([...SHELL_BLOCKS.keys(), ...COMPONENT_BLOCKS.keys()]);
    expect([...widths].sort((a, b) => a - b)).toEqual([640, 1024, 1300]);
  });
});

describe("the page is allowed to be narrow", () => {
  it("no longer floors the body at desktop width", () => {
    // `min-width: 1120px` is what turned a 390px phone into a horizontally
    // scrolling desktop layout with the browser's zoom-out fallback disabled.
    const floor = /body\s*\{[^}]*?min-width:\s*(\d+)px/s.exec(INDEX);
    const px = floor?.[1];
    expect(px, "no body min-width found in index.css").toBeDefined();
    expect(Number(px)).toBeLessThanOrEqual(640);
  });
});

describe("the sidebar becomes a drawer below 1024px", () => {
  const narrow = () => {
    const block = SHELL_BLOCKS.get(1024);
    expect(block, "shell.css declares no 1024px breakpoint").toBeDefined();
    return block!;
  };

  it("takes the sidebar off canvas and reclaims its gutter", () => {
    expect(narrow()).toMatch(/\.sidebar\s*\{[^}]*translateX\(-100%\)/s);
    expect(narrow()).toMatch(/\.app-shell\s*\{[^}]*margin-left:\s*0/s);
  });

  it("takes the closed drawer out of the tab order, not merely off screen", () => {
    // A nav that is only `translateX(-100%)` still holds twenty-eight tab
    // stops in front of the page content -- the exact defect the skip link
    // exists to work around, reintroduced by the fix for a different one.
    expect(narrow()).toMatch(/\.sidebar\s*\{[^}]*visibility:\s*hidden/s);
    expect(narrow()).toMatch(/body\.nav-open\s+\.sidebar\s*\{[^}]*visibility:\s*visible/s);
  });

  it("shows the hamburger only where the sidebar is hidden", () => {
    expect(SHELL).toMatch(/\.nav-toggle\s*\{[^}]*display:\s*none/s);
    expect(narrow()).toMatch(/\.nav-toggle\s*\{[^}]*display:\s*flex/s);
  });

  it("gives that control a real target size", () => {
    // WCAG 2.5.5 asks 44px; the audit measured the only existing control at
    // 30x30.
    const toggle = /\.nav-toggle\s*\{([^}]*)\}/s.exec(SHELL)?.[1] ?? "";
    expect(Number(/width:\s*(\d+)px/.exec(toggle)?.[1])).toBeGreaterThanOrEqual(44);
    expect(Number(/height:\s*(\d+)px/.exec(toggle)?.[1])).toBeGreaterThanOrEqual(44);
  });

  it("stops the page behind the drawer from scrolling", () => {
    expect(narrow()).toMatch(/body\.nav-open\s*\{[^}]*overflow:\s*hidden/s);
  });

  it("retires the collapsed rail, which a drawer has no room for", () => {
    expect(narrow()).toMatch(/\.collapse-btn\s*\{[^}]*display:\s*none/s);
    // 232px is v3's rail; the alias `--sidebar-w` in index.css carries it.
    expect(narrow()).toMatch(/body\.sidebar-collapsed\s*\{[^}]*--sidebar-w:\s*232px/s);
  });
});

describe("the grids keep folding below the design's last step", () => {
  it("halves the multi-column grids at 1024px", () => {
    const block = COMPONENT_BLOCKS.get(1024) ?? "";
    for (const selector of [".stats-grid", ".grid-3", ".grid-4", ".cards"]) {
      expect(block, `${selector} has no 1024px step`).toContain(selector);
    }
    expect(block).toMatch(/\.grid-2\s*\{\s*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  });

  it("collapses to a single column at 640px", () => {
    const block = COMPONENT_BLOCKS.get(640) ?? "";
    expect(block).toMatch(/grid-template-columns:\s*minmax\(0,\s*1fr\)/);
    for (const selector of [".stats-grid", ".grid-2", ".grid-3", ".grid-4", ".cards"]) {
      expect(block, `${selector} has no 640px step`).toContain(selector);
    }
  });
});

/**
 * THE COMMAND PALETTE SURVIVES THE DRAWER BREAKPOINT.
 *
 * `.topbar-find` was `display: none` below 1024px, and that removed three
 * things at once: the field, the `/` shortcut (which asks the element
 * `checkVisibility()` before it fires, precisely so it cannot open a panel
 * anchored to something invisible) and the "Ask the Assistant" row. Five
 * review groups filed it against every route in the console; spec S1 lists
 * the palette among the top-bar functions that must survive.
 *
 * WHAT IS ASSERTED IS THE SHAPE OF THE FIX, not a pixel: that the control is
 * still laid out at that breakpoint, and that the two things which gave way
 * instead are the marked shortcuts. jsdom evaluates no media query, so the
 * rendered result belongs to `scripts/harmony.mjs`, which measures the bar at
 * 390x844 with a coarse pointer -- the run that caught the 402px overflow
 * this trade exists to answer.
 */
describe("the chrome below the drawer breakpoint", () => {
  it("collapses the search rather than removing it", () => {
    const block = SHELL_BLOCKS.get(1024) ?? "";
    expect(block, "no 1024px block in shell.css").not.toBe("");
    expect(block).toContain(".topbar-find");
    // THE FALSIFIER, named: this is the declaration that was there, and the
    // one a later "it does not fit" would reach for again.
    expect(block).not.toMatch(/\.topbar-find\s*\{[^}]*display:\s*none/);
    // It has to be able to grow back, or it is a 36px box nobody can type in.
    expect(block).toMatch(/\.topbar-find:focus-within/);
  });

  it("has nothing left to stand down, because the docs link is gone at every width", () => {
    // Seven 44px targets did not fit in 390px -- measured, the bar ran 402px
    // at a 390px coarse-pointer viewport -- so /docs was hidden at this step
    // while /assistant stayed. The operator then removed the /docs link
    // outright (2026-09-09), so there is no longer a marked shortcut for this
    // breakpoint to hide.
    //
    // ASSERTED AS AN ABSENCE IN BOTH PLACES, because a dangling rule and a
    // dangling marker fail differently: a `.top-shortcut` rule with no element
    // is dead CSS nobody notices, and a `top-shortcut` class in the shell with
    // no rule is an element that silently stops hiding.
    const block = SHELL_BLOCKS.get(640) ?? "";
    expect(block, "the 640px block is empty, so this asserts nothing").not.toEqual("");
    expect(block).not.toMatch(/\.top-shortcut\s*\{/);
    const shellTsx = read("frontend/src/shell/AppShell.tsx");
    expect(shellTsx).not.toContain("top-shortcut");
    // The link's name is a catalogue entry now; the shortcut survives when the
    // shell still renders that key and the English entry still says it.
    expect(shellTsx, "the assistant shortcut itself must survive").toContain("shell.app.askAssistant");
    expect(en.shell.app.askAssistant).toBe("Ask the Assistant");
  });
});

/**
 * THE ACCOUNT BLOCK BELONGS IN THE CORNER, AND ONE PROPERTY PUTS IT THERE.
 *
 * `.topbar` is a flex row whose only growable child is the search, capped at
 * 520px. With nothing claiming the slack, every control packed left and on a
 * wide viewport the avatar floated mid-bar with roughly 600px of empty surface
 * to its right -- which is what the operator saw on the deployed console and
 * reported as "the profile should be in the top right corner".
 *
 * `margin-right: auto` on that one growable child hands the slack to the gap
 * after it, pushing everything downstream flush right. Removing it restores
 * the defect EXACTLY, and until this test existed nothing caught that: 394
 * tests passed with the property deleted. jsdom lays nothing out, so this is
 * asserted on the declaration -- and on the design file agreeing, because the
 * design had the same defect and was corrected first.
 */
describe("the top bar puts its trailing controls in the corner", () => {
  const DESIGN = read("docs/design/itms-prototype-v3.html");

  it("gives the slack to the search, so everything after it is pushed right", () => {
    const find = /\.topbar-find\s*\{([\s\S]*?)\n\}/.exec(SHELL)?.[1];
    expect(find, "no `.topbar-find` rule in shell.css").toBeDefined();
    expect(find).toMatch(/margin-right:\s*auto/);
    // NON-VACUITY: the property only does this job on a child that grows.
    expect(find).toMatch(/flex:\s*1/);
  });

  it("follows the design file, which carries the same correction", () => {
    const srch = /\.top \.srch\{([^}]*)\}/.exec(DESIGN)?.[1];
    expect(srch, "no `.top .srch` rule in the design").toBeDefined();
    expect(srch).toMatch(/margin-right:auto/);
  });

  it("keeps the console's row spacing equal to the design's, rather than drifting", () => {
    // "everything here looks too tight" -- the operator, on the deployed bar.
    // The fix is a gap, and a gap is a design value: the design file moved
    // first and this holds the two to the same number so the next change to
    // either cannot quietly diverge.
    const designGap = /\.top\{[^}]*gap:(\d+)px/.exec(DESIGN)?.[1];
    const consoleGap = /\.topbar \{[\s\S]*?\n  gap: (\d+)px;/.exec(SHELL)?.[1];
    expect(designGap, "no gap on `.top` in the design").toBeDefined();
    expect(consoleGap, "no gap on `.topbar` in shell.css").toBeDefined();
    expect(consoleGap).toBe(designGap);
    expect(Number(consoleGap)).toBeGreaterThanOrEqual(16);
  });
});

describe("touch targets", () => {
  it("applies the 44px floor where the pointer is a finger", () => {
    // Scoped to `pointer: coarse` rather than to a width: the guideline is
    // about fingers, and inflating every control on a desktop monitor would
    // cost the density an operator works in all day for no accessibility gain.
    const coarse = /@media\s*\(pointer:\s*coarse\)\s*\{([\s\S]*?)\n\}/.exec(SHELL)?.[1];
    expect(coarse, "no (pointer: coarse) block in shell.css").toBeDefined();
    expect(coarse).toMatch(/min-height:\s*44px/);
    for (const selector of [".nav-item", ".btn", ".tab", ".top-icon-btn"]) {
      expect(coarse, `${selector} keeps its desktop height on touch`).toContain(selector);
    }
  });
});

/**
 * THE PAGE HEAD'S ACTION ROW, WHICH WAS THE LAST HORIZONTAL OVERFLOW IN THE
 * PRODUCT.
 *
 * MEASURED, in Chromium, over `/__preview` with `/patch` on it:
 *
 *     430px  documentElement.scrollWidth 437 > clientWidth 430
 *     320px  documentElement.scrollWidth 437 > clientWidth 320
 *
 * 437 in both, because the number is not a function of the viewport: it is the
 * document's minimum content width. `.head-actions` was `flex: 0 0 auto` with
 * no `flex-wrap`, so the three controls in `/patch`'s head — Policies, Export
 * evidence, Scan — measured 272px of max-content that could neither shrink nor
 * fold, and 272 plus the title block plus `.page-head`'s 20px gap plus the
 * `.main` padding is 437 at every width below it. WCAG 2.2 SC 1.4.10 asks for
 * no horizontal scrolling at 320px CSS width.
 *
 * WHAT THESE ASSERTIONS ARE AND ARE NOT. jsdom implements no cascade and lays
 * nothing out, so nothing here proves the page reflows — the numbers above and
 * the ones after the fix come from a browser, and `scripts/a11y-browser.mjs`
 * is what re-measures them on demand. What a text assertion CAN hold is the
 * declaration that was absent, which is the part that was actually wrong: a
 * row that may not wrap and may not shrink cannot reflow no matter what is in
 * it. This is the same division of labour the file's header states.
 *
 * NO NEW BREAKPOINT, deliberately. `flex-wrap` is width-agnostic — it folds
 * when the content stops fitting rather than when a number says it should —
 * and a media query here would have to guess how many controls a head carries,
 * which varies by page and by permission.
 */
describe("the page head folds instead of scrolling the document", () => {
  /**
   * The declarations of a rule, outside any media block, normalised. Takes
   * the selector under either of its names: the kit's (`.page-head`,
   * `.head-actions`) or v3's (`.ph-row`, `.ph-act`), whichever the sheet
   * declares, so the port can rename without this describe going blind.
   */
  const rule = (css: string, ...selectors: string[]): string => {
    for (const selector of selectors) {
      const at = css.search(new RegExp(`(^|\\n)${selector.replace(/\./g, "\\.")}\\s*\\{`));
      if (at >= 0) return css.slice(at, css.indexOf("}", at)).replace(/\s+/g, " ");
    }
    expect.fail(`no top-level rule for any of ${selectors.join(", ")}`);
  };
  const HEAD = [".page-head", ".ph-row"] as const;
  const ACTIONS = [".head-actions", ".ph-act"] as const;

  it("lets the action row wrap within itself", () => {
    // THE FALSIFIER: put `flex-wrap: nowrap` back and this fails, which is the
    // exact state the 437px measurement was taken in.
    expect(rule(COMPONENTS, ...ACTIONS)).toMatch(/flex-wrap:\s*wrap/);
  });

  it("lets the action row be squeezed, so wrapping inside it can happen at all", () => {
    // `flex: 0 0 auto` sizes the row to max-content and refuses every offer of
    // less. A child with `flex-wrap` of its own — which `/patch`'s head has —
    // therefore never receives a width small enough to wrap at. Shrinking is
    // what makes the wrap above reachable.
    const declarations = rule(COMPONENTS, ...ACTIONS);
    expect(declarations).toMatch(/flex:\s*0 1 auto/);
    expect(declarations).not.toMatch(/flex:\s*0 0 auto/);
  });

  it("drops the whole row below the title when the title needs the line", () => {
    expect(rule(COMPONENTS, ...HEAD)).toMatch(/flex-wrap:\s*wrap/);
  });

  it("keeps the actions at the right-hand end once they wrap", () => {
    // `justify-content` on the row itself: `.page-head`'s `space-between` puts
    // the BLOCK on the right, and says nothing about how a second line of
    // buttons inside it aligns. Without this they left-align under the first
    // line, which reads as a different component.
    expect(rule(COMPONENTS, ...ACTIONS)).toMatch(/justify-content:\s*flex-end/);
  });

  it("transcribes all four from the design file rather than leading it", () => {
    // DESIGN.md's first Do: `docs/design/itms-prototype-v3.html` is the
    // authority and the console is the transcription. A fix applied only here
    // is a fork. v3 draws the page head as `.ph-row` and its actions as
    // `.ph-act`; both gained these declarations in the design file first
    // (v3 was a desktop mock and had `flex: none`, which cannot wrap).
    const design = readFileSync(
      resolve(REPO, "docs/design/itms-prototype-v3.html"),
      "utf8",
    ).replace(/\/\*[\s\S]*?\*\//g, "");
    const head = /\.ph-act\s*\{([^}]*)\}/.exec(design)?.[1] ?? "";
    expect(head).toMatch(/flex-wrap:\s*wrap/);
    expect(head).toMatch(/justify-content:\s*flex-end/);
    expect(head).toMatch(/flex:\s*0 1 auto/);
    expect(/\.ph-row\s*\{([^}]*)\}/.exec(design)?.[1] ?? "").toMatch(/flex-wrap:\s*wrap/);
  });
});

/**
 * THE TILE ROW FOLDS, AND THE SPECIFICITY IS THE WHOLE MECHANISM.
 *
 * `StatsGrid` passed its column count as `style={{ gridTemplateColumns }}`. An
 * inline declaration outranks every stylesheet rule short of `!important`, so
 * the three steps this file already asserts EXIST had never once applied to a
 * tile row: every page with tiles rendered N columns down to 320px, and a
 * four-tile row became four 96px slivers on a handset.
 *
 * The count is now `data-columns`, which fixes nothing on its own -- a bare
 * `.stats-grid` step is (0,1,0) and would lose to a (0,2,0) attribute rule
 * exactly as it lost to the inline style. What makes it work is that the steps
 * carry the attribute too, so both sides are (0,2,0) and SOURCE ORDER decides:
 * the media query is later in the file, so it wins.
 *
 * That is a property nothing else in this suite can see, and reverting either
 * half silently restores the bug. Hence both directions are asserted.
 */
describe("the tile row's column count can be overridden by its own media queries", () => {
  const components = read("frontend/src/shell/components.css");

  it("carries the count as an attribute, never as an inline style", () => {
    const tsx = read("frontend/src/shell/components.tsx");
    const grid = tsx.slice(tsx.indexOf("export function StatsGrid"));
    // TO THE NEXT TOP-LEVEL DECLARATION, not to the first `\n}`. That shortcut
    // read the function correctly only while its props were a one-line inline
    // type; the moment a prop grew a doc comment the type annotation closed
    // with its own `\n})` and the slice ended BEFORE the JSX -- so the
    // assertion below stopped seeing the element it is about and failed on a
    // component that had not changed. A parser that silently stops reading is
    // worse than one that throws.
    const end = grid.search(/\n(?:export |\/\*\*)/);
    const body = end > 0 ? grid.slice(0, end) : grid;
    expect(body, "slice found no StatsGrid body").toContain("return (");
    expect(body).toContain("data-columns");
    expect(body, "an inline grid-template-columns outranks every media query").not.toMatch(
      /gridTemplateColumns/,
    );
  });

  it("gives every step the attribute, so the base rules cannot outrank them", () => {
    // Each step must be `.stats-grid[data-columns]` — matching the (0,2,0) of
    // the per-count rules above it. A bare `.stats-grid` here is the bug.
    for (const width of [1300, 1024, 640]) {
      const block = COMPONENT_BLOCKS.get(width) ?? "";
      expect(block, `no stats-grid step at ${width}px`).toContain(".stats-grid");
      expect(
        block,
        `the ${width}px step is bare \`.stats-grid\` and loses to [data-columns]`,
      ).toContain(".stats-grid[data-columns]");
    }
  });

  it("enumerates the counts a caller may pass, and the steps come after them", () => {
    for (const n of [1, 2, 3, 4, 5, 6]) {
      expect(components).toContain(`.stats-grid[data-columns="${n}"]`);
    }
    // Source order is what breaks the (0,2,0) tie, so it is asserted.
    expect(components.indexOf('.stats-grid[data-columns="4"]')).toBeLessThan(
      components.indexOf(".stats-grid[data-columns] {"),
    );
  });
});
