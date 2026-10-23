// The figure a report is worth drawing, chosen by report key.
//
// THE FINDING. Auditor B, [HIGH], /reports and /reports/sla-attainment: "There
// is no data visualisation anywhere in the reporting module ... `svg: 0,
// canvas: 0, tables: 1` on /reports/sla-attainment after a successful run ...
// SLA attainment, patch compliance and backup coverage are all fundamentally
// comparative questions -- 'which queue is worst?', 'how much of the fleet is
// uncovered?'. A flat table answers none of those without the reader doing the
// arithmetic. This is the one module where a chart is the deliverable."
//
// **NOTHING HERE COMPUTES ANYTHING** (spec §11, and the structural sweep in
// `__tests__/reporting.test.tsx` that enforces it over this folder). Every
// number in a figure is a value the server already put in `totals`, SELECTED by
// name and never added, divided or scaled. The bar heights are the chart
// component's business, and it is handed the raw counts.
//
// **A SPEC WHOSE KEYS THE RESPONSE DOES NOT CARRY DRAWS NOTHING.** `figureFor`
// returns null the moment a named total is missing rather than substituting a
// zero -- the same rule as the audit's other reporting [HIGH]: "Nine zeros are
// indistinguishable from nine genuine measurements of zero." A chart with an
// invented zero bar is that defect in a second medium.
//
// WHY A PER-REPORT TABLE AND NOT SOMETHING DERIVED. `totals` is a flat
// `Record<string, number>`; which of its keys are comparable to each other, and
// under what axis label, is knowledge the wire format does not carry. Drawing
// every total as one bar would put `devices` beside `stale_devices` and
// `queue_priority_pairs` beside `tickets` -- categories on different scales
// measuring different things, which is a misleading chart rather than a missing
// one. Four reports ship; this table has a row for each, plus one for
// every key the catalogue publishes, and each is checked against the response at
// render time.

import { translate } from "../../i18n/core";
import type { MessageKey } from "../../i18n/core";

export interface FigureSeries {
  label: string;
  values: number[];
}

export interface ReportFigure {
  /** What the figure is OF -- the window is appended by the caller. */
  caption: string;
  categories: string[];
  series: FigureSeries[];
  /** Named on the axis, as both audits require. */
  unit: string;
}

interface FigureSpec {
  caption: MessageKey;
  unit: MessageKey;
  categories: readonly MessageKey[];
  /** `keys[i]` is this series' value for `categories[i]`. */
  series: readonly { label: MessageKey; keys: readonly string[] }[];
}

// The words are catalogue keys, read when `figureFor` runs (at render), so a
// language switch re-words the figure; nothing here holds a sentence.
const PATCH_COMPLIANCE: FigureSpec = {
  caption: "reporting.figures.patch.caption",
  unit: "reporting.figures.patch.unit",
  categories: [
    "reporting.figures.patch.all",
    "reporting.figures.patch.clean",
    "reporting.figures.patch.never",
  ],
  series: [
    {
      label: "reporting.figures.patch.series",
      keys: ["devices", "devices_with_zero_missing", "devices_never_scanned"],
    },
  ],
};

const FIGURES: Readonly<Record<string, FigureSpec>> = {
  // The audit's own prescription: "a grouped bar of met vs breached per queue".
  // Per CLOCK rather than per queue, because `totals` is the aggregate over the
  // whole match and the per-queue split lives in `rows` -- grouping the rows
  // here would be the client-side aggregation §11 forbids. Met vs breached is
  // the comparison; response vs resolution is the pair of promises.
  "sla-attainment": {
    caption: "reporting.figures.sla.caption",
    unit: "reporting.figures.sla.unit",
    categories: ["reporting.figures.sla.response", "reporting.figures.sla.resolution"],
    series: [
      { label: "reporting.figures.sla.met", keys: ["response_met", "resolution_met"] },
      { label: "reporting.figures.sla.breached", keys: ["response_breached", "resolution_breached"] },
    ],
  },
  // "a stacked bar of covered/uncovered with the percentage called out" -- the
  // four buckets are exhaustive and mutually exclusive server-side, so they are
  // drawn side by side rather than stacked, and no percentage is computed here.
  "backup-coverage": {
    caption: "reporting.figures.backup.caption",
    unit: "reporting.figures.backup.unit",
    categories: [
      "reporting.figures.backup.covered",
      "reporting.figures.backup.stale",
      "reporting.figures.backup.failing",
      "reporting.figures.backup.unprotected",
    ],
    series: [
      {
        label: "reporting.figures.backup.series",
        keys: ["covered", "stale", "failing", "unprotected"],
      },
    ],
  },
  "patch-compliance": PATCH_COMPLIANCE,
  "fleet-posture": {
    caption: "reporting.figures.fleet.caption",
    unit: "reporting.figures.fleet.unit",
    categories: [
      "reporting.figures.fleet.all",
      "reporting.figures.fleet.stale",
      "reporting.figures.fleet.alerts",
    ],
    series: [
      {
        label: "reporting.figures.fleet.series",
        keys: ["devices", "stale_devices", "devices_with_open_alerts"],
      },
    ],
  },
};

/**
 * The figure for this report, or null when there is nothing honest to draw.
 *
 * Null on: a report with no spec (a definition added server-side that this
 * table does not know), and a response missing any total the spec names.
 */
export function figureFor(
  reportKey: string,
  totals: Readonly<Record<string, number>>,
): ReportFigure | null {
  const spec = FIGURES[reportKey];
  if (spec == null) return null;

  const series: FigureSeries[] = [];
  for (const one of spec.series) {
    const values: number[] = [];
    for (const key of one.keys) {
      const value = totals[key];
      // NOT `?? 0`. See the header: a substituted zero is a measurement the
      // server never made.
      if (typeof value !== "number") return null;
      values.push(value);
    }
    series.push({ label: translate(one.label), values });
  }
  return {
    caption: translate(spec.caption),
    unit: translate(spec.unit),
    categories: spec.categories.map((key) => translate(key)),
    series,
  };
}
