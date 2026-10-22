/**
 * The charts, and the two things that make them charts rather than decoration:
 * the numbers are readable without seeing them, and the series are separable
 * without seeing colour.
 */

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { axisCeiling, BarChart, fitLabel, LineChart } from "./chart";

// The shape auditor B asked for by name: "a grouped bar of met vs breached per
// queue with a target line".
const ATTAINMENT = {
  title: "SLA attainment, 1 Jul – 19 Aug 2026",
  categories: ["Service desk", "Field ops", "Security"],
  series: [
    { label: "Response met", values: [42, 18, 7] },
    { label: "Response breached", values: [3, 9, 1] },
  ],
  unit: "tickets",
};

describe("BarChart", () => {
  it("draws a bar per series per category", () => {
    const { container } = render(<BarChart {...ATTAINMENT} />);
    // Two rects per bar -- the fill and the hatch over it -- across 3x2 bars.
    const bars = container.querySelectorAll(".chart-canvas rect");
    expect(bars.length).toBe(12);
  });

  it("carries the numbers as a table, not only as pixels", () => {
    // An `aria-label` summarising a chart is a paraphrase, and a paraphrase is
    // not the data. This is what a screen reader actually reads.
    render(<BarChart {...ATTAINMENT} />);
    const table = screen.getByRole("table");
    const row = within(table).getByRole("row", { name: /Field ops/ });
    expect(within(row).getByText("18")).toBeInTheDocument();
    expect(within(row).getByText("9")).toBeInTheDocument();
  });

  it("hides that table on a wrapper, because a table cannot be made 1px wide", () => {
    // A TABLE'S USED WIDTH IS `max(specified, min-content)` -- CSS does not
    // let one shrink below what its columns need -- so `class="sr-only"` on
    // the `<table>` itself was a floor the browser ignored. Measured in
    // Chromium at 427.78px with the utility fully applied, which at 1440
    // pushed 314px of nothing into the document's horizontal scroll and on a
    // 390px handset made the whole console lay out at 469 CSS px and scale
    // down to fit. WCAG 2.2 SC 1.4.10 Reflow. A `<div>` has no such floor.
    render(<BarChart {...ATTAINMENT} />);
    const table = screen.getByRole("table");
    expect(table).not.toHaveClass("sr-only");
    expect(table.parentElement).toHaveClass("sr-only");
    // And the semantics survive the move, which is the reason for a wrapper
    // rather than a plain `<div role="table">` reconstruction.
    expect(within(table).getByRole("columnheader", { name: "Category" })).toBeInTheDocument();
  });

  it("names the metric and the window in the figure's caption", () => {
    // "put the metric name and time window in the chart title".
    render(<BarChart {...ATTAINMENT} />);
    expect(screen.getByText("SLA attainment, 1 Jul – 19 Aug 2026")).toBeInTheDocument();
  });

  it("labels the axis with its unit, at its own type size rather than the drawing's", () => {
    // OUTSIDE THE `<svg>`, and that is the assertion. A `<text>` inside a
    // viewBox is scaled by `renderedWidth / viewBoxWidth`, so the unit painted
    // at ~21px in a wide panel and ~6px in a narrow one while its computed
    // `font-size` stayed 12px at both -- invisible to every gate. It is HTML
    // now, in the figure, under the caption.
    const { container } = render(<BarChart {...ATTAINMENT} />);
    const label = container.querySelector(".chart-axis-label");
    expect(label?.textContent).toBe("tickets");
    expect(label?.closest("svg")).toBeNull();
  });

  it("draws at the width it was measured at, so one user unit is one pixel", () => {
    // THE FALSIFIER for the fix above: a viewBox whose width is a constant
    // while the element is sized by CSS is exactly the defect. The two
    // numbers must be the same, whatever the number is.
    const { container } = render(<BarChart {...ATTAINMENT} />);
    const svg = container.querySelector("svg.chart-canvas");
    const box = svg?.getAttribute("viewBox")?.split(" ")[2];
    expect(box).toBe(svg?.getAttribute("width"));
  });

  // -------------------------------------------------------------------------
  // THE DRAWING IS AS WIDE AS THE CARD, AT BOTH ENDS OF THE LADDER. A browser
  // review of /budget and /reports/rpt-1 found the same figure failing twice
  // over: at 1440 the plot and its grid stopped ~500px short of the card's
  // right edge, leaving ~44% of the panel blank; at 390 it ran PAST the card's
  // edge, slicing one category down the middle and putting the next one
  // off-screen with nothing to say it was there.
  //
  // BREAKS AGAINST: a constant width, at either end.
  // -------------------------------------------------------------------------
  it("draws as wide as the box it is measured in, at desktop width", () => {
    const width = vi.spyOn(Element.prototype, "clientWidth", "get").mockReturnValue(1120);
    try {
      const { container } = render(<BarChart {...ATTAINMENT} />);
      const svg = container.querySelector("svg.chart-canvas");
      expect(svg?.getAttribute("width")).toBe("1120");
      expect(svg?.getAttribute("viewBox")).toBe("0 0 1120 260");
      // And the drawing reaches the far edge: the last gridline ends one right
      // pad in from the measured width, not at a constant.
      const gridline = container.querySelector(".chart-canvas line");
      expect(Number(gridline?.getAttribute("x2"))).toBeGreaterThan(1000);
    } finally {
      width.mockRestore();
    }
  });

  it("fits inside a handset card rather than overflowing it", () => {
    // A ~330px card is what /budget and /reports/rpt-1 give this figure at
    // 390x844. The floor must be under that number, or the drawing is wider
    // than the box on every handset -- which is what shipped.
    const width = vi.spyOn(Element.prototype, "clientWidth", "get").mockReturnValue(330);
    try {
      const { container } = render(<BarChart {...ATTAINMENT} />);
      const svg = container.querySelector("svg.chart-canvas");
      expect(Number(svg?.getAttribute("width"))).toBeLessThanOrEqual(330);
    } finally {
      width.mockRestore();
    }
  });

  it("shortens a category label to its group rather than letting it run off the drawing", () => {
    // An SVG `<text>` neither wraps nor truncates, so a label longer than its
    // group draws over its neighbour and out through the edge. The review read
    // one as `Nothing outsta`, cut mid-word by the card. An ellipsis is what
    // the design's handset tables do, and the full string stays in the table
    // this figure carries.
    const width = vi.spyOn(Element.prototype, "clientWidth", "get").mockReturnValue(330);
    try {
      const { container } = render(
        <BarChart
          {...ATTAINMENT}
          categories={["Hardware refresh", "Services", "Nothing outstanding"]}
        />,
      );
      const labels = [...container.querySelectorAll(".chart-canvas text")].map(
        (node) => node.textContent ?? "",
      );
      expect(labels).toContain("Hardware re\u2026");
      expect(labels).not.toContain("Hardware refresh");
      expect(labels).toContain("Nothing out\u2026");
      expect(labels).not.toContain("Nothing outstanding");
      // The datum itself is not truncated: the table is the accessible content.
      const table = screen.getByRole("table");
      expect(within(table).getByRole("row", { name: /Hardware refresh/ })).toBeInTheDocument();
    } finally {
      width.mockRestore();
    }
  });

  // -------------------------------------------------------------------------
  // THE CAP THIS FIX REMOVED, GUARDED AGAINST COMING BACK. Both pages wrapped
  // the figure in `<div style={{ maxWidth: 640 }}>` while the kit still scaled
  // its own text with its box; the kit fixed that (`one user unit is one CSS
  // pixel`) and the wrappers outlived their reason, pinning the plot at 640
  // inside cards over 1100px wide. A cap on a fluid chart is invisible from
  // inside the component, so it is asserted from outside it.
  //
  // BREAKS AGAINST: either wrapper restored.
  // -------------------------------------------------------------------------
  it("is not pinned to a fixed width by the pages that draw it", () => {
    const root = resolve(import.meta.dirname, "..");
    for (const page of ["modules/budget/BudgetDashboard.tsx", "modules/reporting/ReportView.tsx"]) {
      const source = readFileSync(resolve(root, page), "utf8");
      // Comments recount the cap by name; only a live style may not carry it.
      const styles = source.match(/style=\{\{[^}]*\}\}/g) ?? [];
      for (const style of styles) {
        expect(style, `${page} caps a width in JSX`).not.toMatch(/maxWidth:\s*\d/);
      }
    }
  });

  it("puts the drawing in a scroll container a keyboard can reach", () => {
    // Below the drawing's floor the box scrolls; a scroll container that is
    // not a tab stop is axe `scrollable-region-focusable` and SC 2.1.1.
    const { container } = render(<BarChart {...ATTAINMENT} />);
    expect(container.querySelector(".chart-scroll")).toHaveAttribute("tabindex", "0");
  });

  it("distinguishes the series by more than colour", () => {
    // "don't encode meaning in colour alone" -- both findings say it. Each
    // series gets a hatch pattern AND a text label in the legend.
    const { container } = render(<BarChart {...ATTAINMENT} />);
    expect(container.querySelectorAll("pattern").length).toBe(2);
    const legend = container.querySelector(".chart-legend");
    expect(legend?.textContent).toContain("Response met");
    expect(legend?.textContent).toContain("Response breached");
  });

  it("draws the target line and names it", () => {
    render(<BarChart {...ATTAINMENT} target={{ value: 40, label: "Target 40" }} />);
    expect(screen.getByText("Target 40")).toBeInTheDocument();
  });

  it("renders nothing at all when there is nothing to measure", () => {
    // "Nine zeros are indistinguishable from nine genuine measurements of
    // zero." An axis with no bars is the same lie with better manners, so the
    // caller has to show an empty state instead.
    const { container } = render(<BarChart {...ATTAINMENT} categories={[]} series={[]} />);
    expect(container.querySelector("svg")).toBeNull();
    expect(container.querySelector("figure")).toBeNull();
  });

  it("still draws a genuine measurement of zero", () => {
    // The other half of the same distinction: zeros that WERE measured are
    // data, and must appear.
    render(
      <BarChart
        {...ATTAINMENT}
        categories={["Security"]}
        series={[{ label: "Response breached", values: [0] }]}
      />,
    );
    const row = within(screen.getByRole("table")).getByRole("row", { name: /Security/ });
    expect(within(row).getByText("0")).toBeInTheDocument();
  });

  it("hides the drawing from assistive tech, since the table carries it", () => {
    const { container } = render(<BarChart {...ATTAINMENT} />);
    expect(container.querySelector(".chart-canvas")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("LineChart", () => {
  it("draws one polyline per series with a point per category", () => {
    const { container } = render(
      <LineChart
        title="Cumulative spend, FY2026"
        categories={["Q1", "Q2", "Q3", "Q4"]}
        series={[
          { label: "Plan", values: [10, 20, 30, 40] },
          { label: "Actual", values: [12, 19, 34, 0] },
        ]}
        unit="USD thousands"
      />,
    );
    expect(container.querySelectorAll("polyline").length).toBe(2);
    expect(container.querySelectorAll("circle").length).toBe(8);
  });

  it("separates the lines by dash pattern as well as colour", () => {
    const { container } = render(
      <LineChart
        title="Cumulative spend, FY2026"
        categories={["Q1", "Q2"]}
        series={[
          { label: "Plan", values: [10, 20] },
          { label: "Actual", values: [12, 19] },
        ]}
        unit="USD thousands"
      />,
    );
    const lines = [...container.querySelectorAll("polyline")];
    expect(lines[0]).not.toHaveAttribute("stroke-dasharray");
    expect(lines[1]).toHaveAttribute("stroke-dasharray");
  });

  it("does not divide by zero on a single point", () => {
    const { container } = render(
      <LineChart
        title="One reading"
        categories={["Q1"]}
        series={[{ label: "Actual", values: [5] }]}
        unit="USD"
      />,
    );
    const point = container.querySelector("circle");
    expect(Number(point?.getAttribute("cx"))).toBeGreaterThan(0);
    expect(Number.isNaN(Number(point?.getAttribute("cx")))).toBe(false);
  });
});

describe("axisCeiling", () => {
  it("rounds up to something a person can read off a gridline", () => {
    expect(axisCeiling(42)).toBe(50);
    expect(axisCeiling(7)).toBe(10);
    expect(axisCeiling(180)).toBe(200);
    expect(axisCeiling(2100)).toBe(2500);
  });

  it("never returns zero, which would make every bar full height", () => {
    expect(axisCeiling(0)).toBe(1);
    expect(axisCeiling(-5)).toBe(1);
    expect(axisCeiling(Number.NaN)).toBe(1);
  });
});

describe("fitLabel", () => {
  it("leaves a label that fits alone", () => {
    expect(fitLabel("Services", 200)).toBe("Services");
  });

  it("ends a label that does not fit with an ellipsis, not mid-word at a card edge", () => {
    const fitted = fitLabel("Nothing outstanding", 60);
    expect(fitted.endsWith("\u2026")).toBe(true);
    expect(fitted.length).toBeLessThan("Nothing outstanding".length);
    expect("Nothing outstanding".startsWith(fitted.slice(0, -1))).toBe(true);
  });

  it("does not leave a space stranded before the ellipsis", () => {
    expect(fitLabel("Hardware refresh", 66)).toBe("Hardware\u2026");
  });

  it("survives a group with no room in it at all", () => {
    expect(fitLabel("Services", 0)).toBe("");
    expect(fitLabel("Services", 9)).toBe("\u2026");
  });
});
