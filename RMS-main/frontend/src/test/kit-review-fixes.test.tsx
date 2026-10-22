/**
 * THE KIT-LEVEL FINDINGS OF THE ALL-ROUTES VISUAL REVIEW (M4, M5, M7, M11,
 * M15, M16), each guarded where it was fixed.
 *
 * All 87 console routes were read at 1440x900 and at 390x844. Six of the
 * findings were not about any one page: they were about a piece of the kit
 * behaving differently in two places, or behaving the same way in a place
 * where that was wrong. A page-level fix for any of them would have been a
 * fork, so they were fixed in `shell/components.css`, `shell/components.tsx`
 * and `shell/viz.tsx` -- and in `docs/design/itms-prototype-v3.html` first,
 * which is what the design-authority assertions below read.
 *
 * THE CSS ASSERTIONS READ TEXT, not a rendered layout, for the reason
 * `responsive.test.ts` states at its head: jsdom implements no cascade and
 * evaluates no media query, so `getComputedStyle` there reports values no
 * browser would paint. What a browser does with these rules is the harmony
 * gate's job; that a rule says what it must is this file's.
 */

import { render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { EmptyState } from "../components/emptystate";
import { Pager, StatsGrid } from "../shell/components";
import { Chips } from "../shell/viz";

const REPO = resolve(import.meta.dirname, "../../..");
/** A stylesheet with its comments stripped: these files quote the very
 *  declarations they replace, and a regex happily matches the prose. */
const read = (path: string) =>
  readFileSync(resolve(REPO, path), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

const COMPONENTS = read("frontend/src/shell/components.css");
const DESIGN = read("docs/design/itms-prototype-v3.html");

/** The body of one rule, anchored so `.tab` cannot match `.tabs`. */
function rule(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const found = new RegExp(`(?:^|\\n)\\s*${escaped}\\s*\\{([^}]*)\\}`).exec(css);
  return found?.[1] ?? "";
}

/** The body of every `@media (max-width: Npx)` block, by N. */
function widthBlocks(css: string): Map<number, string> {
  const blocks = new Map<number, string>();
  const opener = /@media\s*\(max-width:\s*(\d+)px\)\s*\{/g;
  let match: RegExpExecArray | null;
  while ((match = opener.exec(css)) !== null) {
    let depth = 1;
    let at = opener.lastIndex;
    while (at < css.length && depth > 0) {
      if (css[at] === "{") depth += 1;
      else if (css[at] === "}") depth -= 1;
      at += 1;
    }
    blocks.set(Number(match[1]), css.slice(opener.lastIndex, at - 1));
  }
  return blocks;
}

const STEPS = widthBlocks(COMPONENTS);

/**
 * The sheet with every `@media` block removed -- the rules that apply at every
 * width. Written because it was needed: a first cut of the M4 assertions below
 * searched the whole file, and the base rule could be deleted outright while
 * the copy inside the 1300px step kept them green. A rule that exists only
 * inside a media query is not the rule the desktop layout is made of.
 */
const UNCONDITIONAL = (() => {
  let css = COMPONENTS;
  const opener = /@media[^{]*\{/;
  for (;;) {
    const match = opener.exec(css);
    if (!match) return css;
    let depth = 1;
    let at = match.index + match[0].length;
    while (at < css.length && depth > 0) {
      if (css[at] === "{") depth += 1;
      else if (css[at] === "}") depth -= 1;
      at += 1;
    }
    css = css.slice(0, match.index) + css.slice(at);
  }
})();

/* ------------------------------------------------------------------- M11 */

/**
 * M11. `/budget` folds its tab strip into two rows at 390px, `/devices/:id`
 * into three, `/licenses` into two. `.tabs` rules along its OWN bottom edge,
 * which under a folded strip is the bottom of the LAST row -- so every row
 * above it had nothing under it and read as detached from the page.
 *
 * The line is drawn by each tab now, as a 1px background strip at the bottom
 * of its border box. Two properties make that work and both are asserted: it
 * is a BACKGROUND, so the element's own 2px bottom border paints over it and
 * `.on` needs no second rule; and it is positioned against the BORDER box, so
 * it lands exactly where the container's line already sits and the last row is
 * pixel-identical to what it was.
 */
describe("M11: a folded tab strip is ruled under every row", () => {
  const tab = rule(UNCONDITIONAL, ".tabs a, .tabs .tab");

  it("paints the strip's hairline on the tab itself, in the strip's own ink", () => {
    expect(tab, "no `.tabs a, .tabs .tab` rule in components.css").not.toBe("");
    expect(tab).toMatch(/background-image:\s*linear-gradient\(var\(--line-2\), var\(--line-2\)\)/);
    expect(tab).toMatch(/background-size:\s*100% 1px/);
  });

  it("aligns it with the container's line rather than 2px above it", () => {
    // `background-origin: padding-box` is the initial value, and the padding
    // box stops 2px short of the border box's bottom -- the line would sit
    // above the container's own, and the last row would show two.
    expect(tab).toMatch(/background-origin:\s*border-box/);
    expect(tab).toMatch(/background-position:\s*0 100%/);
    expect(tab).toMatch(/background-repeat:\s*no-repeat/);
  });

  it("keeps the accent underline the only mark on the active tab", () => {
    // A background paints UNDER the element's border. If this were a
    // border-bottom colour instead, `.on` would need to unset it and the last
    // row's hairline would double to 2px.
    const active = rule(UNCONDITIONAL, ".tabs a.on, .tabs .tab.active");
    expect(active).toMatch(/border-bottom-color:\s*var\(--accent\)/);
    expect(active).not.toMatch(/background/);
  });

  it("leaves no column gap for the hairline to break across", () => {
    // 2px of column gap chopped the per-row line into dashes. The ROW gap
    // stays: the rows are still 2px apart.
    const tabs = rule(UNCONDITIONAL, ".tabs");
    expect(tabs).toMatch(/gap:\s*2px 0/);
  });

  it("is transcribed from the design file rather than leading it", () => {
    expect(DESIGN).toMatch(/\.tabs\{[^}]*gap:2px 0/);
    expect(DESIGN).toMatch(
      /\.tabs a\{[^}]*background-image:linear-gradient\(var\(--line-2\),var\(--line-2\)\)/,
    );
    expect(DESIGN).toMatch(/\.tabs a\{[^}]*background-origin:border-box/);
  });
});

/* -------------------------------------------------------------- M4 and M5 */

/**
 * M4. The two two-tile rows in the console were drawn by two different grids:
 * `/discovered` declares four tracks and got ~280px tiles with the right half
 * of the row empty; `/service-desk/my-work` declares two and got ~570px. A
 * three-tile row fills the width in thirds and a four-tile row in quarters, so
 * the answer the rest of the console already gives is halves.
 */
describe("M4: a two-tile row is two halves, whatever count was declared", () => {
  // THE `:not([data-keep-tracks])` IS NOT DECORATION. The stretch cannot tell
  // an author who asked for four tracks from one who said nothing, because
  // `StatsGrid`'s `columns` DEFAULTS to 4 -- so without this the rule silently
  // reverses `/discovered`, which ships two tiles on purpose (the design draws
  // three; the third's only destination was the page it sits on) and keeps the
  // quarter width so two ordinary counters do not read as the page headline.
  const TWO = ".stats-grid[data-columns]:not([data-keep-tracks]):where(:has(> :nth-child(2):last-child))";

  it("sizes a grid with exactly two children in two tracks, at every width", () => {
    // UNCONDITIONAL, not just inside a step: the finding was measured at
    // 1440px, where no media query applies.
    expect(rule(UNCONDITIONAL, TWO)).toMatch(
      /grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/,
    );
  });

  it("comes after the per-count rules, which it has to outrank on order", () => {
    // Same (0,2,0) as `[data-columns="4"]`: source order is the whole
    // mechanism, exactly as it is for the folding steps below.
    const declared = UNCONDITIONAL.indexOf('.stats-grid[data-columns="6"]');
    expect(declared).toBeGreaterThan(-1);
    expect(UNCONDITIONAL.indexOf(TWO)).toBeGreaterThan(declared);
  });

  it("is wrapped in `:where` so the folding steps still outrank it", () => {
    // Unwrapped, `:has(...)` makes this (0,3,0) and the two-tile row stops
    // folding at 640px -- two 160px tiles on a handset.
    expect(UNCONDITIONAL).toContain(TWO);
    expect(COMPONENTS).not.toMatch(/\.stats-grid\[data-columns\]:has\(/);
  });

  it("holds through the 1300px step, where three tracks for two tiles returns", () => {
    expect(STEPS.get(1300) ?? "").toContain(":has(> :nth-child(2):last-child)");
  });

  it("does not survive the handset step, where a tile takes the row", () => {
    expect(STEPS.get(640) ?? "").not.toContain(":has(");
    expect(STEPS.get(640) ?? "").toMatch(
      /\.stats-grid\[data-columns\][\s\S]*grid-template-columns:\s*minmax\(0, 1fr\)/,
    );
  });
});

/**
 * M5. Where one tile in a row carries a two-line caption and another carries
 * none, grid's default `stretch` drew the caption-less tile at the tall one's
 * height with the bottom band empty -- `/ipam`, `/enrollment`,
 * `/backup/repositories`. A tile is as tall as what is in it; `.tile`'s
 * `min-height: 92px` is what keeps the ordinary row uniform.
 */
describe("M5: a tile is as tall as its own content", () => {
  it("stops the tile row stretching every tile to the tallest", () => {
    expect(rule(UNCONDITIONAL, ".stats-grid")).toMatch(/align-items:\s*start/);
    expect(rule(UNCONDITIONAL, ".tiles")).toMatch(/align-items:\s*start/);
  });

  it("keeps the floor that makes a row of equal tiles equal", () => {
    const tile = read("frontend/src/shell/viz.css");
    expect(rule(tile, ".tile")).toMatch(/min-height:\s*92px/);
  });

  it("is transcribed from the design file rather than leading it", () => {
    expect(DESIGN).toMatch(/\.tiles\{[^}]*align-items:start/);
  });
});

/* ------------------------------------------------------------------- M15 */

/**
 * M15. `/assistant/autonomy` draws three options in each of nine cells with
 * `Chips`, and `.chips` wraps -- right for a filter strip across a page, wrong
 * in a table cell. Every cell wrapped 2 + 1 at 1440px, so `Needs approval` sat
 * alone on a second line and no option lined up with the same option in the
 * column beside it.
 */
describe("M15: chips can be a choice group instead of a filter strip", () => {
  it("stacks one option per row, every option the width of the widest", () => {
    const stacked = rule(UNCONDITIONAL, ".chips.stacked");
    expect(stacked, "no `.chips.stacked` rule in components.css").not.toBe("");
    expect(stacked).toMatch(/flex-direction:\s*column/);
    expect(stacked).toMatch(/align-items:\s*stretch/);
  });

  it("is transcribed from the design file rather than leading it", () => {
    expect(DESIGN).toMatch(/\.chips\.stacked \{[^}]*flex-direction: column/);
  });

  it("is asked for by the caller and off by default", () => {
    const { rerender } = render(
      <Chips items={["Off", "Suggest only", "Needs approval"]} active={1} onSelect={() => {}} label="Level" />,
    );
    expect(screen.getByRole("group", { name: "Level" })).toHaveClass("chips");
    expect(screen.getByRole("group", { name: "Level" })).not.toHaveClass("stacked");

    rerender(
      <Chips
        items={["Off", "Suggest only", "Needs approval"]}
        active={1}
        onSelect={() => {}}
        label="Level"
        stacked
      />,
    );
    expect(screen.getByRole("group", { name: "Level" })).toHaveClass("stacked");
  });

  it("still says which option is chosen, stacked or not", () => {
    // The 390px half of the finding: the selected state of two columns could
    // not be seen at all. `aria-pressed` is what carries it for anything not
    // looking at pixels, and stacking must not disturb it.
    render(
      <Chips
        items={["Off", "Suggest only", "Needs approval"]}
        active={2}
        onSelect={() => {}}
        label="Admin, high risk"
        stacked
      />,
    );
    expect(screen.getByRole("button", { name: "Needs approval" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Off" })).toHaveAttribute("aria-pressed", "false");
  });
});

/* ------------------------------------------------------------------- M16 */

/**
 * M16. `/devices` at 390px set "Showing 1-14 of 14 devices." in the monospace
 * face beside the two pager buttons, where it broke across three lines; the
 * comparable counts elsewhere ("7 of 7", "4 shown") sit inline. The kit is
 * where that face came from: `Pager` wrapped its own count in `.mono`, and the
 * feet that do not use `Pager` copied it.
 */
describe("M16: a result count is prose, not an identifier", () => {
  it("renders the count in `.count`, never in the monospace face", () => {
    const { container } = render(
      <Pager page={0} pageCount={3} onPrevious={() => {}} onNext={() => {}} />,
    );
    expect(container.querySelector(".table-foot .count")).toHaveTextContent("Page 1 of 3");
    expect(container.querySelector(".mono")).toBeNull();
  });

  it("keeps the caller's own sentence in the same slot", () => {
    const { container } = render(
      <Pager page={0} pageCount={2} onPrevious={() => {}} onNext={() => {}}>
        Showing 1–14 of 14 devices.
      </Pager>,
    );
    expect(container.querySelector(".count")).toHaveTextContent("Showing 1–14 of 14 devices.");
  });

  it("keeps the one property the mono face was buying: figures that do not jitter", () => {
    const count = rule(UNCONDITIONAL, ".table-foot .count");
    expect(count, "no `.table-foot .count` rule in components.css").not.toBe("");
    expect(count).toMatch(/font-variant-numeric:\s*tabular-nums/);
    expect(count, "the face is the page's own").not.toMatch(/font-family/);
  });

  it("is transcribed from the design file rather than leading it", () => {
    expect(DESIGN).toMatch(/\.table-foot \.count \{[^}]*font-variant-numeric: tabular-nums/);
  });
});

/* -------------------------------------------------------------------- M7 */

/**
 * M7. Three treatments for "there is nothing here": the kit's `EmptyState`,
 * a bare sentence with no icon and no emphasis (`/security`'s "Raw event" ->
 * "Select an event."), and nothing at all (`/ipam/search`). `EmptyState` is
 * the one that is right; what this asserts is that it can express the SHORT
 * PROMPT the `/security` panel needs -- a resting state with no way out to
 * offer -- without a caller having to fake one.
 */
describe("M7: EmptyState expresses a prompt with no action", () => {
  it("renders icon, headline and explanation and nothing else", () => {
    const { container } = render(
      <EmptyState
        icon="search"
        title="No event selected"
        body="Choose a row in the list above to see the fields stored with it."
      />,
    );
    expect(screen.getByRole("heading", { name: "No event selected" })).toBeInTheDocument();
    expect(
      screen.getByText("Choose a row in the list above to see the fields stored with it."),
    ).toBeInTheDocument();
    expect(container.querySelector(".empty-icon")).not.toBeNull();
    // No way out is offered and none is faked: an empty state "phrased as a
    // dead end" is a defect in its own right, so the slot has to stay
    // genuinely absent rather than render an empty button.
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("takes the rung the panel around it leaves free", () => {
    // `/security`'s "Raw event" is a TITLED panel, so the default `h3` is
    // right there; an untitled panel needs `2` or the heading order skips.
    const { rerender } = render(<EmptyState icon="search" title="Nothing yet" body="Why." />);
    expect(screen.getByRole("heading", { name: "Nothing yet", level: 3 })).toBeInTheDocument();
    rerender(<EmptyState icon="search" title="Nothing yet" body="Why." headingLevel={2} />);
    expect(screen.getByRole("heading", { name: "Nothing yet", level: 2 })).toBeInTheDocument();
  });

  it("lets a grid keep its declared tracks, so the stretch cannot overrule a page", () => {
    // The escape hatch, asserted from both ends: the CSS excludes the marked
    // grid, and `StatsGrid` only emits the marker when asked.
    expect(UNCONDITIONAL).toContain(":not([data-keep-tracks])");
    const { container, rerender } = render(
      <StatsGrid columns={4} keepTrackWidth>
        <i />
        <i />
      </StatsGrid>,
    );
    expect(container.querySelector(".stats-grid")).toHaveAttribute("data-keep-tracks");
    rerender(
      <StatsGrid columns={4}>
        <i />
        <i />
      </StatsGrid>,
    );
    expect(container.querySelector(".stats-grid")).not.toHaveAttribute("data-keep-tracks");
  });
});
