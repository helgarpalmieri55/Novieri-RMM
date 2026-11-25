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
