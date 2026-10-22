/**
 * The console is light-only, and nothing may reintroduce a `dark:` variant.
 *
 * WHY THIS IS A DEFECT AND NOT A DEAD CLASS. Tailwind's `dark:` variant
 * defaults to `prefers-color-scheme`, and it keeps working whatever
 * `color-scheme` the page declares. So on an operator whose OS is set to dark,
 * every surviving `dark:bg-slate-900` fires — against a palette that has no
 * dark half — and the console half-inverts: dark panels, dark borders, and the
 * design's light text on top of them. 155 of these were removed across 47
 * files during the UI port; without a guard, the next component copied from an
 * older one brings them straight back.
 *
 * The approved design (`docs/design/itms-prototype-v3.html`) is light-only:
 * it has no dark palette, no `prefers-color-scheme` block and no
 * `[data-theme]` hook, and `index.css` declares `color-scheme: light` for it.
 * If dark mode is ever wanted, it starts there — with a palette — not with a
 * variant scattered through component files.
 */

import { describe, expect, it } from "vitest";

// Source text of every component and module file. `eager: true` with `?raw`
// gives the contents without executing anything.
const SOURCES = import.meta.glob("../{components,modules,shell}/**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

describe("light-only", () => {
  it("reads a meaningful number of source files", () => {
    // NON-VACUITY. A glob that matched nothing would make the sweep below pass
    // for ever, which is the failure mode every source-sweep guard has.
    expect(Object.keys(SOURCES).length).toBeGreaterThan(50);
  });

  it("has no Tailwind dark: variant anywhere in the app", () => {
    const offenders: string[] = [];
    for (const [path, source] of Object.entries(SOURCES)) {
      source.split("\n").forEach((line, index) => {
        // Skip prose: the only legitimate mentions are comments explaining
        // this rule.
        const code = line.replace(/\/\/.*$/, "").replace(/\/\*.*?\*\//g, "");
        if (/\bdark:[a-z[]/.test(code)) offenders.push(`${path}:${index + 1}`);
      });
    }
    expect(
      offenders,
      `the console is light-only; these reintroduce a dark variant: ${offenders.join(", ")}`,
    ).toEqual([]);
  });

  it("uses the design's palette, not Tailwind's", () => {
    // 701 slate/red/amber/emerald utilities were remapped onto the design's
    // own variables during the first UI port. They are not interchangeable:
    // the design's --muted is #606a7a and Tailwind's slate-500 is #64748b, so
    // a page mixing them has two greys that are nearly the same and visibly
    // not. One source for a colour, and it is
    // `docs/design/itms-prototype-v3.html`.
    const PALETTE = /\b(?:text|bg|border|ring|fill|stroke|divide)-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}\b/;
    const offenders: string[] = [];
    for (const [path, source] of Object.entries(SOURCES)) {
      source.split("\n").forEach((line, index) => {
        const code = line.replace(/\/\/.*$/, "").replace(/\/\*.*?\*\//g, "");
        if (PALETTE.test(code)) offenders.push(`${path}:${index + 1}`);
      });
    }
    expect(
      offenders,
      `use the design's variables (text-[var(--muted)]) not Tailwind's palette: ${offenders.join(", ")}`,
    ).toEqual([]);
  });
});
