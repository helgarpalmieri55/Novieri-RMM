/**
 * The harmony gate's allowlist is READ FROM `index.css` when the gate runs
 * (v3 port spec §5), so the parser that reads it is the one thing that can
 * silently turn the gate into a rubber stamp: a regex that misses the `:root`
 * block passes nothing through and flags everything, and one that admits
 * every literal in a comment passes everything. This pins both directions
 * against a small stylesheet with known contents, and then against the real
 * file for non-vacuity.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { buildAllowlist, parseColor, readTokens } from "../../scripts/harmony.mjs";

const SAMPLE = `
:root {
  --t-h1: 22px;
  --t-body: 13.5px;
  --r-ctl: 6px;
  --r-pill: 999px;
  --ink: #141619;
  --accent-ink: #fff;
  --sev-crit-s: #fdecee;
  --shadow: 0 1px 2px rgba(15,26,32,.05), 0 6px 20px rgba(15, 26, 32, 0.06);
  /* --t-ghost: 99px; a token mentioned in prose is not a token */
}
:root {
  --canvas: var(--bg, #f5f6f9);
  --red: var(--ink);
}
@theme inline {
  --text-sm: var(--t-body);
  --radius-md: var(--r-ctl);
  --color-ink: var(--ink);
}
`;

describe("parseColor", () => {
  it("normalises every spelling Chrome or the stylesheet can use", () => {
    expect(parseColor("#141619")).toEqual([20, 22, 25, 1]);
    expect(parseColor("#fff")).toEqual([255, 255, 255, 1]);
    expect(parseColor("#ffffff80")?.[3]).toBeCloseTo(0.5, 1);
    expect(parseColor("rgb(20, 22, 25)")).toEqual([20, 22, 25, 1]);
    expect(parseColor("rgba(15, 26, 32, 0.05)")).toEqual([15, 26, 32, 0.05]);
    expect(parseColor("rgba(15,26,32,.05)")).toEqual([15, 26, 32, 0.05]);
    expect(parseColor("rgb(15 26 32 / 0.5)")).toEqual([15, 26, 32, 0.5]);
    expect(parseColor("transparent")).toEqual([0, 0, 0, 0]);
    expect(parseColor("color(srgb 1 0 0)")).toEqual([255, 0, 0, 1]);
  });

  it("refuses what is not a colour", () => {
    expect(parseColor("none")).toBeNull();
    expect(parseColor("currentcolor")).toBeNull();
    expect(parseColor("#12345")).toBeNull();
  });
});

describe("readTokens", () => {
  it("resolves var() chains and ignores comments", () => {
    const tokens = readTokens(SAMPLE);
    expect(tokens.get("--t-h1")).toBe("22px");
    expect(tokens.get("--text-sm")).toBe("13.5px");
    expect(tokens.get("--radius-md")).toBe("6px");
    expect(tokens.get("--red")).toBe("#141619");
    expect(tokens.get("--canvas")).toBe("#f5f6f9");
    expect(tokens.has("--t-ghost")).toBe(false);
  });
});

describe("buildAllowlist", () => {
  const allow = buildAllowlist(SAMPLE);

  it("takes font sizes from --t-* and --text-*, plus the KPI figure", () => {
    expect([...allow.fontSizes].sort()).toEqual(["13.5px", "22px", "26px"].sort());
  });

  it("takes radii from --r-* and --radius-*, plus the structural ones", () => {
    expect([...allow.radii].sort()).toEqual(["0px", "2px", "3px", "50%", "6px", "999px"].sort());
  });

  it("takes every colour literal, including the alpha ones inside a shadow", () => {
    const has = (text: string) => {
      const parsed = parseColor(text);
      return parsed !== null && allow.colors.some((c) => c.every((v, i) => Math.abs(v - (parsed[i] as number)) < 0.02));
    };
    expect(has("#141619")).toBe(true);
    expect(has("rgb(255, 255, 255)")).toBe(true);
    expect(has("rgba(15, 26, 32, 0.05)")).toBe(true);
    expect(has("#f5f6f9")).toBe(true);
    expect(has("rgba(0, 0, 0, 0)")).toBe(true);
    expect(has("#000000")).toBe(false);
    expect(has("#fdecef")).toBe(false);
  });
});

describe("the real index.css", () => {
  const allow = buildAllowlist(readFileSync(resolve(import.meta.dirname, "../index.css"), "utf8"));

  it("yields an allowlist the gate can measure against", () => {
    // NON-VACUITY, the same floor `harmony.mjs` refuses to run below.
    expect(allow.fontSizes.size).toBeGreaterThanOrEqual(3);
    expect(allow.radii.size).toBeGreaterThanOrEqual(5);
    expect(allow.colors.length).toBeGreaterThanOrEqual(5);
    expect(allow.tokens.has("--ink")).toBe(true);
    expect(allow.tokens.has("--surface")).toBe(true);
  });
});
