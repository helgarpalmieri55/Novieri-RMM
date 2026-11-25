/**
 * THE KIT STYLESHEET'S ADDITIONS (kit requests batch 1), each guarded where it
 * was made.
 *
 * WHY TEXT AND NOT A RENDER, as `chrome-responsive.test.ts` says at its head:
 * jsdom implements no cascade and evaluates no media query, so the honest
 * claim a test can make about a stylesheet is "this rule is present". What a
 * browser does with it is measured in one (`a11y-browser.mjs` over
 * `/__preview`, harmony over the routes), and the batch's report records it.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

/** Comments stripped: they quote the declarations they explain. */
const read = (path: string) =>
  readFileSync(resolve(import.meta.dirname, path), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

const KIT = read("./kit.css");

/** The body of a rule, by selector, anchored so `.chip` does not match `.chips`. */
function rule(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\>]/g, "\\$&");
  const found = new RegExp(`(?:^|[},])\\s*${escaped}\\s*\\{([^}]*)\\}`, "m").exec(css);
  return found?.[1] ?? "";
}

describe("the parser sees real rules", () => {
  it("reads declarations rather than an empty string", () => {
    // NON-VACUITY: a `rule()` that returned "" for everything would fail every
    // assertion below with the same message. Anchored on rules that predate
    // this batch and are not among the things asserted.
    expect(rule(KIT, ".list-chips")).toContain("flex-wrap: wrap");
    expect(rule(KIT, ".form-section")).toContain("margin-bottom: 16px");
  });
});

describe("FormSection's lead paragraph (kit requests batch 1, item 3)", () => {
  it("sits flush at the top of the body, with the fields' gap below it", () => {
    expect(rule(KIT, ".form-section-lead")).toMatch(/margin:\s*0 0 12px/);
  });

  it("keeps the page description's measure, so a long lead is read as a paragraph", () => {
    expect(rule(KIT, ".form-section-lead")).toMatch(/max-width:\s*70ch/);
  });
});

describe("a page's and a record's title balance their lines (kit requests batch 1, item 2)", () => {
  it("balances the page head's h1, so a long title does not leave one word on its last line", () => {
    // The detector's `unbalanced-heading` on /service-desk/tickets/tkt-1 and
    // /service-desk/admin at 390 (O2's K1).
    expect(rule(KIT, ".ph h1")).toMatch(/text-wrap:\s*balance/);
  });

  it("reaches every head the kit draws: the page's, the record's and the form's are all `.ph`", () => {
    const source = (name: string) => readFileSync(resolve(import.meta.dirname, name), "utf8");
    expect(source("./components.tsx")).toMatch(/<section className="ph">\s*<div className="ph-row">\s*<div>\s*<h1>/);
    expect(source("./detailKit.tsx")).toMatch(/<section className="ph detail-head">/);
    expect(source("./formKit.tsx")).toMatch(/<section className="ph">/);
  });
});

describe("ListToolbar: the chips flow beside the search (kit requests batch 1, item 1)", () => {
  // /devices' toolbar was four rows tall at 1440: the search took the whole
  // first row, because `.list-chips` was ONE flex item, and a flex line breaks
  // on an item's max-content width, so the chips never fit beside it. A search
  // basis alone (O1's proposal) left it as tall: the batch 1 report's
  // Chromium measurement over /__preview, with a replica of that toolbar.
  it("lets each chip group be a flex item of the bar, so a group fits beside the search on its own width", () => {
    expect(rule(KIT, ".filterbar > .list-chips")).toMatch(/display:\s*contents/);
  });

  it("gives the search a basis and a ceiling beside chips, so it neither crowds them out nor stretches across the row", () => {
    const search = rule(KIT, ".filterbar:has(> .list-chips:not(:empty)) > .srch-wrap");
    expect(search).toMatch(/flex:\s*1 1 320px/);
    expect(search).toMatch(/max-width:\s*480px/);
  });

  it("leaves a bar whose chips draw nothing as it was (batch 1 review, Minor 1)", () => {
    // `ListToolbar` draws `.list-chips` whenever it is handed `chips`, and a
    // ClientFilter alone draws nothing with a client chosen above or fewer
    // than two clients: that bar has no chip beside the search, so no ceiling.
    expect(rule(KIT, ".filterbar:has(> .list-chips) > .srch-wrap")).toBe("");
  });

  it("leaves a toolbar with no chips as it was: the search still takes the row", () => {
    // Non-vacuity in the other direction: the ceiling is scoped to a bar that
    // holds chips, never written on every `.filterbar > .srch-wrap`.
    expect(rule(KIT, ".filterbar > .srch-wrap")).toBe("");
  });
});

describe("the leave question's own layer (kit requests batch 2, G1)", () => {
  const OVERLAYS = read("./overlays.css");
  const z = (css: string, selector: string) => Number(/z-index:\s*(\d+)/.exec(rule(css, selector))?.[1] ?? NaN);

  it("reads the layers it is ranked against", () => {
    // NON-VACUITY: NaN compares false with everything, so a renamed rule
    // would pass every ranking below by failing to be read.
    expect(z(OVERLAYS, ".modal")).toBe(95);
    expect(z(OVERLAYS, ".overlay")).toBe(70);
    expect(z(OVERLAYS, ".toast-stack")).toBe(110);
  });

  it("is a stacking context of its own, so its scrim and panel stack inside it", () => {
    const layer = rule(KIT, ".leave-layer");
    expect(layer).toMatch(/position:\s*relative/);
    expect(layer).toMatch(/z-index:\s*\d+/);
  });

  it("sits above every dialog and its scrim, whatever the document's order", () => {
    expect(z(KIT, ".leave-layer")).toBeGreaterThan(z(OVERLAYS, ".modal"));
    expect(z(KIT, ".leave-layer")).toBeGreaterThan(z(OVERLAYS, ".overlay"));
  });

  it("stays under the toasts, which report what just happened", () => {
    expect(z(KIT, ".leave-layer")).toBeLessThan(z(OVERLAYS, ".toast-stack"));
  });
});

describe("a read-only field looks read-only (kit requests batch 2, K5)", () => {
  const COMPONENTS = read("./components.css");

  it("is sunken: --surface-2 behind the value, so it does not look editable", () => {
    expect(rule(COMPONENTS, ".input-field[readonly]")).toMatch(/background-color:\s*var\(--surface-2\)/);
  });

  it("paints only the colour, so a select's chevron image is never wiped by a shorthand", () => {
    expect(rule(COMPONENTS, ".input-field[readonly]")).not.toMatch(/(^|[;\s])background:/);
  });

  it("is on /__preview, so the browser's a11y pass measures it", () => {
    const preview = readFileSync(resolve(import.meta.dirname, "../preview/PreviewPage.tsx"), "utf8");
    expect(preview).toMatch(/<input className=\{inputClass\}[^>]*\breadOnly\b/);
  });
});

describe("notes wrap without a straggler (kit requests batch 3, B3-4)", () => {
  // O7's K4: the detector reported one word alone on a note's last line at
  // 390, in notes and page descriptions. `pretty` evens the last lines of a
  // paragraph; `balance` is for headings (`.ph h1`, above).
  const COMPONENTS = read("./components.css");

  it("evens the last lines of a note, a form hint and a page's description", () => {
    expect(rule(COMPONENTS, ".note, .form-hint, .ph .sub")).toMatch(/text-wrap:\s*pretty/);
  });
});
