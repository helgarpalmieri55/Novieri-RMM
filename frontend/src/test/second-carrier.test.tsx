/**
 * "RUNNING" AND "INFORMATION" SHARE THE COBALT (DESIGN.md v4, D2; the
 * operator confirmed the merge on 2026-11-24), so wherever the two are drawn
 * without a badge's icon, something other than the hue must tell them apart
 * (The Second Carrier Rule; WCAG 1.4.1). The four places are the ones the
 * pre-flight review named (I-3): a chart's series, the heat strip (IPAM's
 * address grid: DHCP is "info", an assigned address "run"), the donut and the
 * timeline bar.
 */
import { render } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { Donut, Timeline } from "../shell/viz";

const SRC = resolve(import.meta.dirname, "..");
const read = (path: string) => readFileSync(resolve(SRC, path), "utf8");

/** Every `--x: value;` of every `:root` in index.css, so an alias resolves to its paint. */
function tokens(): Map<string, string> {
  const out = new Map<string, string>();
  for (const block of read("index.css").matchAll(/:root\s*\{([^}]*)\}/g)) {
    for (const line of (block[1] as string).split("\n")) {
      const match = /^\s*(--[a-z0-9-]+):\s*(.+?);\s*$/.exec(line);
      if (match) out.set(match[1] as string, match[2] as string);
    }
  }
  return out;
}

/** `var(--sev-run)` -> `#2747c9`, following aliases. */
function paint(value: string, table: Map<string, string>): string {
  let at = value.trim();
  for (let hops = 0; hops < 5; hops += 1) {
    const ref = /^var\((--[a-z0-9-]+)\)$/.exec(at);
    if (!ref) return at.toLowerCase();
    at = (table.get(ref[1] as string) ?? "(missing)").trim();
  }
  throw new Error(`alias loop at ${value}`);
}

/** The declarations of one rule (`selector {`), without the trailing `;`. */
function rule(css: string, selector: string): string[] {
  const at = css.indexOf(`${selector} {`);
  if (at === -1) throw new Error(`no rule ${selector}`);
  return css
    .slice(css.indexOf("{", at) + 1, css.indexOf("}", at))
    .split(";")
    .map((declaration) => declaration.trim())
    .filter(Boolean);
}

describe("running and information, drawn without a badge (DESIGN.md v4, D2)", () => {
  const table = tokens();

  it("resolve to the same cobalt, which is why a second carrier is owed at all", () => {
    expect(paint("var(--sev-run)", table)).toBe(paint("var(--accent)", table));
  });

  it("gives every chart series its own paint, never the same one twice", () => {
    const inks =
      /const SERIES_INK = \[([^\]]*)\]/
        .exec(read("components/chart.tsx"))?.[1]
        ?.split(",")
        .map((ink) => ink.trim().replace(/^"|"$/g, "")) ?? [];
    expect(inks.length).toBeGreaterThanOrEqual(4);
    const paints = inks.map((ink) => paint(ink, table));
    expect(new Set(paints).size, paints.join(", ")).toBe(paints.length);
  });

  it("rings the information cell of a heat strip, so a DHCP address is not an assigned one by colour alone", () => {
    const css = read("shell/viz.css");
    const info = rule(css, ".heat .h-info");
    const run = rule(css, ".heat .h-run");
    const ground = (declarations: string[]) =>
      paint(declarations.find((d) => d.startsWith("background:"))?.slice("background:".length) ?? "", table);
    expect(ground(info)).toBe(ground(run));
    expect(info.some((d) => /^box-shadow:\s*inset\b/.test(d))).toBe(true);
    expect(run.some((d) => d.startsWith("box-shadow:"))).toBe(false);
  });

  it("puts the kind's icon beside a donut's label, and the two kinds' icons differ", () => {
    const icon = (kind: "run" | "info") =>
      render(<Donut pct={40} kind={kind} label="Backups" />).container.querySelector(".don-k svg")?.innerHTML;
    const running = icon("run");
    const information = icon("info");
    expect(running).toBeTruthy();
    expect(information).toBeTruthy();
    expect(running).not.toBe(information);
  });

  it("keeps the icon every timeline row already carries, for the same reason", () => {
    const { container } = render(
      <Timeline
        ticks={["00:00", "12:00"]}
        runs={[
          { at: "a", kind: "run", label: "Patch", start: 0, width: 50 },
          { at: "b", kind: "info", label: "Scan", start: 50, width: 50 },
        ]}
      />,
    );
    const icons = [...container.querySelectorAll(".tl-k svg")].map((svg) => svg.innerHTML);
    expect(icons).toHaveLength(2);
    expect(icons[0]).not.toBe(icons[1]);
  });
});
