/**
 * The two charts this product's analytics modules were missing entirely.
 *
 * WHAT THE AUDIT MEASURED. `/reports/sla-attainment` after a successful run:
 * `svg: 0, canvas: 0, tables: 1`. `/budget` Dashboard: `svg: 2` — both of them
 * KPI-tile icons — `canvas: 0, tables: 0`. Its conclusion is the reason this
 * file exists: "the product has no data visualisation anywhere in its two
 * analytics modules", in the one module "where a chart is the deliverable".
 *
 * HAND-ROLLED SVG, AND WHY. This platform does not phone home and many
 * deployments have no egress at all (`docs/product.md` §5) — the same
 * constraint that made Inter self-hosted. A charting library is a large
 * dependency for two chart types, and the two the audits ask for are named
 * precisely: "a grouped bar of met vs breached per queue with a target line",
 * and "plan/actual/committed as a grouped bar per category with a variance
 * callout, and a cumulative burn line". That is one bar chart and one line
 * chart. When a third shape is genuinely needed, revisit the dependency —
 * not before.
 *
 * NOT COLOUR ALONE, which both findings state as a requirement. Every series
 * carries a fill AND a distinct hatch, the legend names it in text, and the
 * whole figure is mirrored by a real (visually hidden) data table. That table
 * is also what a screen reader reads: an `aria-label` summarising a chart is a
 * paraphrase, and a paraphrase is not the data.
 *
 * ZERO IS NOT NO DATA. Both chart components render nothing when handed
 * nothing, so the surrounding page shows an `<EmptyState>` instead of an axis
 * with no bars. "Nine zeros are indistinguishable from nine genuine
 * measurements of zero... someone will screenshot this as proof of 100%
 * attainment."
 */

import { useId, useLayoutEffect, useRef, useState } from "react";

import { useT } from "../i18n/context";
import { formatCount } from "../lib/format";

/**
 * ONE USER UNIT IS ONE CSS PIXEL, AND THAT IS THE WHOLE OF THIS FIX.
 *
 * Both charts drew into a fixed `viewBox="0 0 640 260"` under
 * `.chart-canvas { width: 100% }`, so every `<text>` inside them was scaled
 * by `renderedWidth / 640`: `.chart-tick`'s `--t-sm` painted at ~21px in a
 * 1120px panel and at ~6px in a 342px one. Two review rounds filed it as an
 * a11y finding on /budget and /reports/rpt-1 and NEITHER GATE COULD SEE IT --
 * the computed `font-size` is 12px at every width, because the scaling
 * happens in the viewport transform and not in the cascade.
 *
 * So the drawing is measured instead: the figure reports its own width, the
 * viewBox is that width, and the type paints at exactly the token size on
 * every screen.
 *
 * `MIN_WIDTH` IS 300 AND USED TO BE 560, WHICH IS THE WHOLE OF THE SECOND
 * FIX. A floor of 560 inside a ~330px handset card is a drawing half again
 * wider than the box it sits in, and `.chart-scroll` scrolling it was the
 * theory; what a browser review of /budget and /reports/rpt-1 at 390x844
 * actually found was a plot cut off at the card's edge -- `Hardware refresh`
 * sliced down the middle, `Services` off-screen entirely, an axis label
 * ending at `Nothing outsta` -- with no visible affordance saying that
 * anything was reachable to the right. A chart that has to be discovered by
 * dragging is a chart nobody reads. 300 is a genuine floor rather than a
 * comfortable one: below it the y-axis pad alone takes a quarter of the box.
 * What 560 was really buying was room for category labels, and `fitLabel`
 * buys that directly and at any width, by shortening the label to its
 * group instead of widening the drawing past its card.
 *
 * `FALLBACK_WIDTH` is what renders before the first measurement and in an
 * environment with no layout at all (jsdom, where `clientWidth` is 0): the
 * design's own 640, so a test sees the geometry it has always seen.
 */
const MIN_WIDTH = 300;
const FALLBACK_WIDTH = 640;
const HEIGHT = 260;
const PAD = { top: 18, right: 16, bottom: 46, left: 62 };
const PLOT_HEIGHT = HEIGHT - PAD.top - PAD.bottom;

/** The width the SVG should be drawn at, measured from the box it sits in. */
function useDrawnWidth(): [ref: (node: HTMLDivElement | null) => void, width: number] {
  const box = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(FALLBACK_WIDTH);

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const measure = () => {
      // `clientWidth` is 0 where there is no layout; keep the fallback rather
      // than dividing the plot by nothing.
      const seen = Math.round(el.clientWidth);
      if (seen > 0) setWidth(Math.max(MIN_WIDTH, seen));
    };
    measure();
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(measure) : null;
    observer?.observe(el);
    return () => observer?.disconnect();
  }, []);

  // A callback ref, so the measurement happens the moment the node exists
  // rather than one paint later.
  return [
    (node) => {
      box.current = node;
    },
    width,
  ];
}

/** Series fills, in order. Drawn from the palette's ink tones, which clear
 *  4.5:1 on the surface behind them (cycle 9). */
/* v3's palette has one accent and four severities that double as series
 * inks; each series also carries a distinct hatch, so the fills are never the
 * only thing telling two bars apart. */
const SERIES_INK = ["var(--accent)", "var(--sev-run)", "var(--sev-warn)", "var(--sev-ok)"];

export interface Series {
  /** Named in the legend and in the data table. Never colour alone. */
  label: string;
  values: readonly number[];
}

export interface ChartProps {
  /** The metric and its window: "SLA attainment, 1 Jul – 19 Aug 2026". */
  title: string;
  /** X-axis categories. `series[n].values[i]` belongs to `categories[i]`. */
  categories: readonly string[];
  series: readonly Series[];
  /** The unit, named on the axis: "tickets", "USD". */
  unit: string;
  /** How a value reads. Defaults to a separated count. */
  format?: (value: number) => string;
  /** An optional threshold line, e.g. the SLA target. */
  target?: { value: number; label: string };
}

/** Nice-ish round ceiling for an axis, so gridlines land on readable numbers. */
export function axisCeiling(max: number): number {
  if (!Number.isFinite(max) || max <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(max));
  for (const step of [1, 2, 2.5, 5, 10]) {
    const candidate = step * magnitude;
    if (candidate >= max) return candidate;
  }
  return 10 * magnitude;
}

function ticks(ceiling: number): number[] {
  return [0, 0.25, 0.5, 0.75, 1].map((fraction) => fraction * ceiling);
}

/**
 * One glyph of `--t-sm` (12px), averaged over mixed-case Inter.
 *
 * AN ESTIMATE ON PURPOSE. The exact answer needs a text measurement, which
 * needs a laid-out document -- the thing this component is careful never to
 * depend on, because jsdom has none and the fallback geometry is what the
 * tests read. Erring high shortens a label by a character it might have kept;
 * erring low overlaps two labels into an unreadable smudge. This errs high.
 */
const GLYPH = 6.6;

/**
 * A category label, shortened to the width it has.
 *
 * THE CLIPPING WAS NOT THE CHART'S TO SUFFER. A `<text>` in an SVG neither
 * wraps nor truncates: it draws its full string from its anchor and runs
 * wherever that takes it, over its neighbour and out through the edge of the
 * viewBox. So a drawing narrow enough to fit its card had to be a drawing
 * whose labels fit their groups, and this is that -- an ellipsis, which is
 * what the design's own handset tables do to a name too long for its column,
 * and not a hard cut mid-word at a card boundary. The full string is never
 * lost: `DataTable` carries every category verbatim, and that is what a
 * screen reader is reading in the first place.
 */
export function fitLabel(label: string, available: number): string {
  const max = Math.floor(available / GLYPH);
  if (max <= 0) return "";
  if (label.length <= max) return label;
  if (max === 1) return "\u2026";
  return `${label.slice(0, max - 1).trimEnd()}\u2026`;
}

interface TableProps {
  title: string;
  categories: readonly string[];
  series: readonly Series[];
  unit: string;
  format: (value: number) => string;
}

/**
 * The data behind a chart, as a table.
 *
 * `sr-only` rather than a disclosure: this IS the figure's accessible content,
 * and it should be read in place rather than found.
 *
 * THE `sr-only` GOES ON A WRAPPER, NOT ON THE `<table>`, AND THAT IS THE WHOLE
 * FIX. A table's used width is `max(specified, min-content)` -- CSS does not
 * let a table shrink below the width its columns need -- so `width: 1px` on a
 * `<table>` is a floor it ignores, and `overflow: hidden` on a table box does
 * not reliably clip either. Measured: this element computed to 427.78px with
 * the utility fully applied to it. On a 390px handset that is what Chromium's
 * shrink-to-fit uses for the minimum page scale, so the entire console was
 * laid out at 469 CSS px and scaled down to fit; at wider viewports the same
 * table (there, at `width: 100%` plus `white-space: nowrap`) put 314px of
 * nothing into the document's horizontal scroll. WCAG 2.2 SC 1.4.10 Reflow.
 *
 * A `<div>` has no such floor: it is 1px, it clips, and the table inside it
 * keeps every bit of its semantics -- caption, `scope="col"`, `scope="row"`
 * -- because clipping is a paint concern and the accessibility tree is not
 * built from pixels. The companion half of this lives in `shell/base.css`,
 * which redeclares `.sr-only` unlayered so an unlayered element rule cannot
 * out-cascade it again.
 */
function DataTable({ title, categories, series, unit, format }: TableProps) {
  const t = useT();
  return (
    <div className="sr-only">
      <table>
        <caption>{`${title} (${unit})`}</caption>
        <thead>
          <tr>
            <th scope="col">{t("components.chart.category")}</th>
            {series.map((one) => (
              <th scope="col" key={one.label}>
                {one.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {categories.map((category, index) => (
            <tr key={category}>
              <th scope="row">{category}</th>
              {series.map((one) => (
                <td key={one.label}>{format(one.values[index] ?? 0)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Legend({ series, patternIds }: { series: readonly Series[]; patternIds: readonly string[] }) {
  return (
    <ul className="chart-legend">
      {series.map((one, at) => (
        <li key={one.label}>
          <svg width="14" height="14" aria-hidden="true">
            <rect width="14" height="14" rx="3" fill={SERIES_INK[at % SERIES_INK.length]} />
            {patternIds[at] ? (
              <rect width="14" height="14" rx="3" fill={`url(#${patternIds[at]})`} />
            ) : null}
          </svg>
          <span>{one.label}</span>
        </li>
      ))}
    </ul>
  );
}

/** Hatches that distinguish the series for anyone who cannot separate the fills. */
function Hatches({ ids }: { ids: readonly string[] }) {
  const strokes: (string | null)[] = [
    null,
    "M0 6 l6 -6 M-2 2 l4 -4 M4 8 l4 -4",
    "M0 0 l6 6 M-2 4 l4 4 M4 -2 l4 4",
    "M3 3 m-1.4 0 a1.4 1.4 0 1 0 2.8 0 a1.4 1.4 0 1 0 -2.8 0",
  ];
  return (
    <defs>
      {ids.map((id, at) => {
        const d = strokes[at % strokes.length];
        return (
          <pattern id={id} key={id} width="6" height="6" patternUnits="userSpaceOnUse">
            {d ? <path d={d} stroke="var(--hatch-ink)" strokeWidth="1.1" fill="none" /> : null}
          </pattern>
        );
      })}
    </defs>
  );
}

function highestOf(series: readonly Series[], target?: ChartProps["target"]): number {
  const values = series.flatMap((one) => one.values.filter((value) => Number.isFinite(value)));
  return Math.max(...values, target?.value ?? 0, 0);
}

/**
 * Grouped bars — met vs breached per queue, plan vs actual vs committed per
 * category. Both audits name this shape explicitly.
 */
export function BarChart({ title, categories, series, unit, format = formatCount, target }: ChartProps) {
  const base = useId().replace(/:/g, "");
  const patternIds = series.map((_, at) => `${base}-hatch-${at}`);
  const [boxRef, WIDTH] = useDrawnWidth();

  // NOTHING TO DRAW IS THE CALLER'S PROBLEM, and deliberately so: an axis with
  // no bars looks like a measurement of zero.
  if (categories.length === 0 || series.length === 0) return null;

  const plotWidth = WIDTH - PAD.left - PAD.right;
  const ceiling = axisCeiling(highestOf(series, target));
  const y = (value: number) => PAD.top + PLOT_HEIGHT - (value / ceiling) * PLOT_HEIGHT;
  const groupWidth = plotWidth / categories.length;
  // THE CAP RISES WITH THE DRAWING, because the drawing is no longer pinned
  // at 640. Three categories across a 1140px card leave 354px per group; at
  // the old 38px cap two series drew 76px of bar in it and the plot read as
  // a mostly empty grid. 56 is the width at which a group still reads as a
  // group rather than as a block.
  const barWidth = Math.min(56, (groupWidth * 0.72) / series.length);

  return (
    <figure className="chart">
      <figcaption className="chart-title">{title}</figcaption>
      {/* THE UNIT IS A CAPTION LINE, NOT A DRAWING. It was a `<text>` inside
          the viewBox, so it scaled with everything else and -- sitting below
          the category row -- read as an orphaned "EUR" floating between the
          axis and the legend. Here it is HTML at its token size, on its own
          line under the metric it qualifies, and still inside the `<figure>`,
          so a caller's "names the currency the bars are in" is answered.
          Outside the figcaption on purpose: the caption is the metric and its
          window, and callers assert exactly that string. */}
      <p className="chart-axis-label chart-unit">{unit}</p>
      {/* A TAB STOP, for the same reason `.table-wrap` is one: below the
          drawing's floor this box scrolls, and a scroll container a keyboard
          cannot reach is axe `scrollable-region-focusable` and SC 2.1.1. It
          carries no name and no role -- the drawing inside is `aria-hidden`
          and the figure's own `DataTable` is what a screen reader reads. */}
      <div className="chart-scroll" ref={boxRef} tabIndex={0}>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        width={WIDTH}
        height={HEIGHT}
        className="chart-canvas"
        preserveAspectRatio="xMinYMin meet"
        aria-hidden="true"
      >
        <Hatches ids={patternIds} />

        {ticks(ceiling).map((value) => (
          <g key={value}>
            <line x1={PAD.left} x2={WIDTH - PAD.right} y1={y(value)} y2={y(value)} stroke="var(--line)" />
            <text x={PAD.left - 8} y={y(value) + 4} textAnchor="end" className="chart-tick">
              {format(value)}
            </text>
          </g>
        ))}

        {target ? (
          <g>
            <line
              x1={PAD.left}
              x2={WIDTH - PAD.right}
              y1={y(target.value)}
              y2={y(target.value)}
              stroke="var(--sev-crit)"
              strokeWidth="2"
              strokeDasharray="6 4"
            />
            <text x={WIDTH - PAD.right} y={y(target.value) - 6} textAnchor="end" className="chart-target">
              {target.label}
            </text>
          </g>
        ) : null}

        {categories.map((category, index) => {
          const groupLeft = PAD.left + index * groupWidth;
          const inset = (groupWidth - barWidth * series.length) / 2;
          return (
            <g key={category}>
              {series.map((one, at) => {
                const value = one.values[index] ?? 0;
                const top = y(Math.max(0, value));
                const height = Math.max(0, PAD.top + PLOT_HEIGHT - top);
                const left = groupLeft + inset + at * barWidth;
                return (
                  <g key={one.label}>
                    <rect
                      x={left}
                      y={top}
                      width={barWidth - 3}
                      height={height}
                      fill={SERIES_INK[at % SERIES_INK.length]}
                      rx="3"
                    />
                    <rect
                      x={left}
                      y={top}
                      width={barWidth - 3}
                      height={height}
                      fill={`url(#${patternIds[at]})`}
                      rx="3"
                    />
                  </g>
                );
              })}
              <text
                x={groupLeft + groupWidth / 2}
                y={HEIGHT - PAD.bottom + 18}
                textAnchor="middle"
                className="chart-tick"
              >
                {fitLabel(category, groupWidth - 4)}
              </text>
            </g>
          );
        })}

      </svg>
      </div>

      <Legend series={series} patternIds={patternIds} />
      <DataTable title={title} categories={categories} series={series} unit={unit} format={format} />
    </figure>
  );
}

/**
 * A trend over an ordered window — "is attainment improving?", the cumulative
 * burn against the fiscal calendar.
 */
export function LineChart({ title, categories, series, unit, format = formatCount, target }: ChartProps) {
  const [boxRef, WIDTH] = useDrawnWidth();
  if (categories.length === 0 || series.length === 0) return null;

  const plotWidth = WIDTH - PAD.left - PAD.right;
  const ceiling = axisCeiling(highestOf(series, target));
  const y = (value: number) => PAD.top + PLOT_HEIGHT - (value / ceiling) * PLOT_HEIGHT;
  const x = (index: number) =>
    categories.length === 1
      ? PAD.left + plotWidth / 2
      : PAD.left + (index / (categories.length - 1)) * plotWidth;
  // Points are spaced `plotWidth / (n - 1)` apart and their labels are centred
  // on them, so that spacing is exactly the room each label has before it
  // touches its neighbour.
  const labelRoom = categories.length === 1 ? plotWidth : plotWidth / (categories.length - 1);

  return (
    <figure className="chart">
      <figcaption className="chart-title">{title}</figcaption>
      {/* See `BarChart`: the unit is a caption line at its token size, not a
          text node inside a scaled viewBox. */}
      <p className="chart-axis-label chart-unit">{unit}</p>
      {/* A TAB STOP, for the same reason `.table-wrap` is one: below the
          drawing's floor this box scrolls, and a scroll container a keyboard
          cannot reach is axe `scrollable-region-focusable` and SC 2.1.1. It
          carries no name and no role -- the drawing inside is `aria-hidden`
          and the figure's own `DataTable` is what a screen reader reads. */}
      <div className="chart-scroll" ref={boxRef} tabIndex={0}>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        width={WIDTH}
        height={HEIGHT}
        className="chart-canvas"
        preserveAspectRatio="xMinYMin meet"
        aria-hidden="true"
      >
        {ticks(ceiling).map((value) => (
          <g key={value}>
            <line x1={PAD.left} x2={WIDTH - PAD.right} y1={y(value)} y2={y(value)} stroke="var(--line)" />
            <text x={PAD.left - 8} y={y(value) + 4} textAnchor="end" className="chart-tick">
              {format(value)}
            </text>
          </g>
        ))}

        {target ? (
          <line
            x1={PAD.left}
            x2={WIDTH - PAD.right}
            y1={y(target.value)}
            y2={y(target.value)}
            stroke="var(--sev-crit)"
            strokeWidth="2"
            strokeDasharray="6 4"
          />
        ) : null}

        {series.map((one, at) => (
          <g key={one.label}>
            <polyline
              points={one.values.map((value, index) => `${x(index)},${y(value)}`).join(" ")}
              fill="none"
              stroke={SERIES_INK[at % SERIES_INK.length]}
              strokeWidth="2.5"
              // A dash pattern per series, so the lines stay separable in
              // greyscale and for anyone who cannot tell the hues apart.
              strokeDasharray={at === 0 ? undefined : `${4 + at * 3} ${3 + at}`}
            />
            {one.values.map((value, index) => (
              <circle
                key={`${one.label}-${index}`}
                cx={x(index)}
                cy={y(value)}
                r="3.5"
                fill={SERIES_INK[at % SERIES_INK.length]}
              />
            ))}
          </g>
        ))}

        {categories.map((category, index) => (
          <text
            key={category}
            x={x(index)}
            y={HEIGHT - PAD.bottom + 18}
            textAnchor="middle"
            className="chart-tick"
          >
            {fitLabel(category, labelRoom)}
          </text>
        ))}

      </svg>
      </div>

      <Legend series={series} patternIds={series.map(() => "")} />
      <DataTable title={title} categories={categories} series={series} unit={unit} format={format} />
    </figure>
  );
}
