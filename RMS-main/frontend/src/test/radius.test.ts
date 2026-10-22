/**
 * WHY THIS FILE EXISTS.
 *
 * v3 (`docs/design/itms-prototype-v3.html`) has a radius ladder of exactly
 * three rungs -- control 6px, panel 10px, pill 999px -- and a type ramp of
 * exactly five steps -- 22 / 15 / 13.5 / 12 / 10.5 -- plus one named size for
 * the KPI figure, 26px. The console's own CSS follows both. Tailwind's stock
 * scales do not: `rounded-sm` is 2px, `rounded-lg` 8px, `text-sm` 14px,
 * `text-lg` 18px, and none of those is a step the design contains. Hundreds of
 * boxes and headings across the modules are built from those utilities rather
 * than from the kit, so every one of them would sit off the ladder or off the
 * ramp BY CONSTRUCTION, next to a `.panel` at 10px and an `h2` at 15px. That
 * is what "the boxes are all different" and "the fonts are different" look
 * like to someone using the product, and fixing them one at a time would not
 * stop the next one landing wrong.
 *
 * `@theme inline` in `index.css` remaps both scales onto the design. This pins
 * that, because the failure is invisible: delete a mapping and nothing errors,
 * nothing looks broken in isolation, and the drift returns one utility at a
 * time.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const REPO = resolve(import.meta.dirname, "../../..");
const CSS = readFileSync(resolve(REPO, "frontend/src/index.css"), "utf8");

/** The ladder, verbatim from v3's `:root` and DESIGN.md's `rounded:` block. */
const LADDER: Record<string, string> = {
  "--r-ctl": "6px",
  "--r-panel": "10px",
  "--r-pill": "999px",
};

/** Which Tailwind utility must resolve to which rung. */
const SCALE: Record<string, string> = {
  "--radius-sm": "--r-ctl",
  "--radius-md": "--r-ctl",
  "--radius-lg": "--r-panel",
  "--radius-xl": "--r-panel",
  "--radius-2xl": "--r-panel",
  "--radius-full": "--r-pill",
};

function declaration(name: string): string | undefined {
  return new RegExp(`^\\s*${name}:\\s*([^;]+);`, "m").exec(CSS)?.[1]?.trim();
}

/** The type ramp, verbatim from v3's `:root`, plus the one named size outside it. */
const RAMP: Record<string, string> = {
  "--t-h1": "22px",
  "--t-h2": "15px",
  "--t-body": "13.5px",
  "--t-sm": "12px",
  "--t-cap": "10.5px",
  "--t-kpi": "26px",
};

/** Which Tailwind utility must reach which step. */
const TYPE_SCALE: Record<string, string> = {
  "--text-xs": "var(--t-sm)",
  "--text-sm": "var(--t-body)",
  "--text-base": "var(--t-body)",
  "--text-lg": "var(--t-h2)",
  "--text-xl": "var(--t-h1)",
  "--text-2xl": "var(--t-kpi)",
};

describe("the type ramp", () => {
  it.each(Object.entries(RAMP))("declares %s as %s", (token, value) => {
    expect(declaration(token)).toBe(value);
  });

  it.each(Object.entries(TYPE_SCALE))(
    "maps the Tailwind utility %s onto the ramp (%s)",
    (utility, rung) => {
      expect(declaration(utility)).toBe(rung);
    },
  );

  it("leaves no type utility on a size the ramp does not contain", () => {
    // 18px and 20px are stock Tailwind's text-lg/text-xl, and 14px and 16px
    // are stock text-sm/text-base. The ramp contains none of the four, and 48
    // <h2>s were drawn at 18px before the scale was first mapped.
    for (const off of ["14px", "16px", "18px", "20px"]) {
      const offenders = [...CSS.matchAll(/^\s*(--text-[a-z0-9]+):\s*([^;]+);/gm)]
        .filter((m) => m[2]?.trim() === off)
        .map((m) => m[1]);
      expect(offenders, `${off} is not a step DESIGN.md documents`).toEqual([]);
    }
  });

  it("resolves every text utility to a ramp step, never to a literal", () => {
    // The same one-hop rule the radius ladder has below: a utility that names
    // a literal is a second place for the ramp to live.
    const steps = new Set(Object.keys(RAMP));
    for (const utility of [...CSS.matchAll(/^\s*(--text-[a-z0-9]+):\s*([^;]+);/gm)].map((m) => m[1] ?? "")) {
      const declared = declaration(utility);
      const referenced = /^var\((--[a-z0-9-]+)\)$/.exec(declared ?? "")?.[1];
      expect(referenced, `${utility} paints a literal (${declared}) instead of a ramp step`).toBeDefined();
      expect(steps, `${utility} reaches ${referenced}, which is not a ramp step`).toContain(referenced ?? "");
    }
  });
});

describe("the radius ladder", () => {
  it.each(Object.entries(LADDER))("declares %s as %s", (token, value) => {
    expect(declaration(token)).toBe(value);
  });

  it.each(Object.entries(SCALE))(
    "maps the Tailwind utility %s onto the ladder rung %s",
    (utility, rung) => {
      expect(declaration(utility)).toBe(`var(${rung})`);
    },
  );

  it("leaves no Tailwind radius utility on a value the ladder does not contain", () => {
    const rungs = new Set(Object.values(LADDER));
    for (const utility of Object.keys(SCALE)) {
      const declared = declaration(utility);
      expect(declared, `${utility} is not declared at all`).toBeDefined();
      // Resolve one hop: every utility points at a ladder token, never a literal.
      const referenced = /^var\((--[a-z-]+)\)$/.exec(declared ?? "")?.[1];
      expect(referenced, `${utility} paints a literal (${declared}) instead of a rung`).toBeDefined();
      expect(rungs).toContain(LADDER[referenced ?? ""]);
    }
  });
});
