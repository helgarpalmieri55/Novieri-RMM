/**
 * The pieces v3 introduced -- TRANSCRIBED from the design's own kit functions
 * (`tiles`, `spark`, `donut`, `heat`, `timeline`, `facts`, `kv`, `steps`,
 * `avatar`, `person`, `os`, `chips`, `toolbar` in
 * `docs/design/itms-prototype-v3.html`), each rendering the design's class
 * names and structure so a visual diff against the reference stays
 * meaningful. Styles are in `viz.css`.
 *
 * THREE RULES EVERY PIECE HERE HOLDS, from the port spec §1:
 *   - Colour is never the only carrier. A pill carries its kind's icon, a
 *     heat strip names its counts, a timeline row leads with the glyph.
 *   - Unmeasured is drawn as ABSENT: a donut with no reading has no arc and
 *     says `n/a`; a sparkline with one point draws nothing; a run with no
 *     span draws no bar. Never a zero.
 *   - The severity vocabulary is one closed set, `Kind`, and `kindOf` is the
 *     one place a wire value becomes a kind.
 */

import { Fragment, useId, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { useT } from "../i18n/context";
import { translate, type MessageKey } from "../i18n/core";
import { Icon } from "./icons";
import "./viz.css";

/* -------------------------------------------------------------------- kind */

/** The six severities v3 draws, and nothing else. */
export type Kind = "crit" | "warn" | "ok" | "run" | "mut" | "info";

/** The glyph each kind carries, so colour is never the only carrier. */
export const KIND_ICON: Record<Kind, string> = {
  crit: "circle-x",
  warn: "alert-triangle",
  ok: "circle-check",
  run: "progress",
  mut: "minus",
  info: "info-circle",
};

/**
 * The word for each kind, for assistive technology and for the places (a
 * heat strip's summary, a step's pill) that need the state as text.
 */
const KIND_LABEL_KEY = {
  crit: "shell.viz.kindCritical",
  warn: "shell.viz.kindWarning",
  ok: "shell.viz.kindOk",
  run: "shell.viz.kindRunning",
  mut: "shell.viz.kindNotApplicable",
  info: "shell.viz.kindInfo",
} as const satisfies Record<Kind, MessageKey>;

const HEAT_KIND_KEY = {
  crit: "shell.viz.heatKind.crit",
  warn: "shell.viz.heatKind.warn",
  ok: "shell.viz.heatKind.ok",
  run: "shell.viz.heatKind.run",
  mut: "shell.viz.heatKind.mut",
  info: "shell.viz.heatKind.info",
} as const satisfies Record<Kind, MessageKey>;

export function kindLabel(kind: Kind): string {
  return translate(KIND_LABEL_KEY[kind]);
}

/**
 * The wire's vocabulary, as kinds. Every status word the console has ever
 * rendered through a pill is here, grouped by what it MEANS rather than by
 * where it came from: a revoked certificate and a failed job are both
 * failures that have already happened; an expiring certificate and a due
 * ticket are both deadlines; a renewing certificate and a running job are
 * both the platform reporting work it is doing by itself. `base-styles.test`
 * pins the certificate states to their company.
 */
const KIND_OF: Record<string, Kind> = {
  // Failures, breaches, things that have already gone wrong.
  critical: "crit", crit: "crit", failed: "crit", failure: "crit", error: "crit", errored: "crit",
  high: "crit", urgent: "crit", overdue: "crit", offline: "crit", expired: "crit", revoked: "crit",
  breached: "crit", breach: "crit", dead: "crit", down: "crit", denied: "crit", refused: "crit",
  rejected: "crit", blocked: "crit", unhealthy: "crit", missing: "crit", stalled: "crit", escalated: "crit",
  p1: "crit", "sev-1": "crit", sev1: "crit", red: "crit",
  // Deadlines, degradation, things that need watching.
  warning: "warn", warn: "warn", pending: "warn", medium: "warn", due: "warn", expiring: "warn",
  degraded: "warn", "at-risk": "warn", stale: "warn", behind: "warn", partial: "warn", paused: "warn",
  suspended: "warn", waiting: "warn", "needs-attention": "warn", throttled: "warn", moderate: "warn",
  important: "warn", p2: "warn", gold: "warn", orange: "warn", amber: "warn", "action-required": "warn",
  // Good, done, current.
  success: "ok", succeeded: "ok", successful: "ok", healthy: "ok", online: "ok", completed: "ok",
  complete: "ok", done: "ok", low: "ok", ok: "ok", active: "ok", approved: "ok", resolved: "ok",
  closed: "ok", installed: "ok", current: "ok", verified: "ok", passed: "ok", pass: "ok", enrolled: "ok",
  valid: "ok", met: "ok", applied: "ok", ready: "ok", up: "ok", connected: "ok", enabled: "ok",
  delivered: "ok", sent: "ok", accepted: "ok", granted: "ok", green: "ok", p4: "ok", compliant: "ok",
  // The platform doing something right now.
  running: "run", "in-progress": "run", open: "run", scheduled: "run", renewing: "run", queued: "run",
  dispatched: "run", started: "run", syncing: "run", polling: "run", deploying: "run", applying: "run",
  scanning: "run", processing: "run", retrying: "run", patching: "run", provisioning: "run",
  executing: "run", assigned: "run", triaged: "run", new: "run", p3: "run", blue: "run", proposed: "run",
  // Plain information, no severity.
  info: "info", informational: "info", note: "info", purple: "info", violet: "info", primary: "info",
  // Nothing measured, nothing applicable, nothing happening.
  neutral: "mut", unknown: "mut", none: "mut", "n-a": "mut", "not-applicable": "mut", cancelled: "mut",
  canceled: "mut", archived: "mut", disabled: "mut", inactive: "mut", skipped: "mut", withdrawn: "mut",
  retired: "mut", muted: "mut", never: "mut", draft: "mut", unassigned: "mut", "not-enrolled": "mut",
};

/** A wire value as a slug: `cert_expiring` -> `cert-expiring`. */
/** The words `kindOf` knows, for the test that holds the status vocabulary complete. */
export const STATUS_WORDS: readonly string[] = Object.keys(KIND_OF);

export function slugOf(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/(^-|-$)/g, "");
}

/**
 * The kind a status word carries. Unknown words are `mut`, deliberately: a
 * state nobody has classified must not wear a colour it has not earned, and
 * the grey pill with the minus glyph is what "unclassified" looks like.
 * A prefixed value (`cert_revoked`, `state-degraded`) is matched on its last
 * word when the whole slug is unknown.
 */
export function kindOf(value: string): Kind {
  const slug = slugOf(value);
  const whole = KIND_OF[slug];
  if (whole) return whole;
  const last = slug.split("-").at(-1) ?? "";
  return KIND_OF[last] ?? "mut";
}

/** v3 `state(kind, text)`: the pill, with its kind's glyph in front. */
export function Pill({ kind, children }: { kind: Kind; children: ReactNode }) {
  return (
    <span className={`pill p-${kind}`} data-kind={kind}>
      <Icon name={KIND_ICON[kind]} size={13} />
      {children}
    </span>
  );
}

/* --------------------------------------------------------------- sparkline */

/**
 * v3 `spark(vals, w, h)`: an area, a line and a dot on the last point, in
 * `currentColor`. Fewer than two points draws nothing -- one value is not a
 * trend, and an axis with one dot on it reads as a measurement of zero.
 */
export function Sparkline({
  values,
  width = 104,
  height = 26,
  kind,
  className,
}: {
  values: readonly number[];
  width?: number;
  height?: number;
  kind?: Kind;
  className?: string;
}) {
  const t = useT();
  if (values.length < 2) return null;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const range = hi - lo || 1;
  const points = values.map((v, i) => [
    i * (width / (values.length - 1)),
    height - 2 - ((v - lo) / range) * (height - 6),
  ]);
  const d = `M${points.map((p) => `${p[0]?.toFixed(1)} ${p[1]?.toFixed(1)}`).join(" L")}`;
  const last = points[points.length - 1] ?? [0, 0];
  const cls = ["sp", kind ? `sp-${kind}` : "", className].filter(Boolean).join(" ");
  return (
    <svg
      className={cls}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={t("shell.viz.trend", { count: values.length, from: values[0] ?? "", to: values[values.length - 1] ?? "" })}
    >
      <path className="a" d={`${d} L${width} ${height} L0 ${height} Z`} />
      <path className="l" d={d} />
      <circle className="p" cx={last[0]?.toFixed(1)} cy={last[1]?.toFixed(1)} r="2.4" />
    </svg>
  );
}

/* -------------------------------------------------------------------- tile */

export interface TileProps {
  label: string;
  value: ReactNode;
  unit?: string;
  sub?: ReactNode;
  /** `kind` colours the whole tile; only crit and warn ever do, ok colours the figure. */
  kind?: Kind;
  spark?: readonly number[];
  /** Where the number lives. A tile with a destination renders as a link. */
  to?: string;
  icon?: string;
  className?: string;
  /**
   * Extra class names on the three parts. `StatCard` uses this to keep the
   * `.stat-label` / `.stat-value` hooks its module tests query; nothing else
   * should need it.
   */
  parts?: { label?: string; value?: string; sub?: string };
}

/**
 * v3 `tiles([...])`, one tile. The figure is `--t-kpi`, the one size outside
 * the five-step scale. `has-sp` makes room for the sparkline in the corner.
 */
export function Tile({ label, value, unit, sub, kind, spark, to, icon, className, parts }: TileProps) {
  const hasSpark = spark !== undefined && spark.length >= 2;
  const cls = ["tile", kind ?? "", hasSpark ? "has-sp" : "", className].filter(Boolean).join(" ");
  const body = (
    <>
      <div className={["k", parts?.label].filter(Boolean).join(" ")}>
        {icon ? <Icon name={icon} size={14} /> : null}
        {label}
      </div>
      <div className={["v", parts?.value].filter(Boolean).join(" ")}>
        {value}
        {unit ? <small>{unit}</small> : null}
      </div>
      {sub ? <div className={["z", parts?.sub].filter(Boolean).join(" ")}>{sub}</div> : null}
      {hasSpark ? <Sparkline values={spark} /> : null}
    </>
  );
  return to ? (
    <Link className={cls} to={to}>
      {body}
    </Link>
  ) : (
    <div className={cls}>{body}</div>
  );
}

/* ------------------------------------------------------------------- donut */

const DONUT_C = 2 * Math.PI * 26;

/**
 * v3 `donut(pct, kind, label, sub)`. `pct === null` is the unmeasured state:
 * no arc, `n/a` in the muted ink, whatever kind was asked for. A donut at 0%
 * with a full arc missing is what a measured zero looks like, and the two are
 * different facts.
 */
export function Donut({ pct, kind, label, sub }: { pct: number | null; kind: Kind; label: string; sub?: string }) {
  const t = useT();
  const measured = pct !== null && Number.isFinite(pct);
  const clamped = measured ? Math.max(0, Math.min(100, pct)) : 0;
  const shown = measured ? kind : "mut";
  return (
    <div className={`don ${shown}`}>
      <svg viewBox="0 0 60 60" aria-hidden="true">
        <circle cx="30" cy="30" r="26" className="trk" />
        {/* A MEASURED ZERO DRAWS NO ARC. `strokeDasharray: "0.0 163.4"` plus
            `.don .arc { stroke-linecap: round }` is a dot one stroke-width
            across -- a solid pip under a reading of 0%. The reading stays: the
            figure IS measured, and it is the arc that has nothing to say.
            Flattening the linecap instead would change the ends of every
            other arc, which is a design change rather than a fix. */}
        {measured && clamped > 0 ? (
          <circle
            cx="30"
            cy="30"
            r="26"
            className="arc"
            style={{ strokeDasharray: `${((DONUT_C * clamped) / 100).toFixed(1)} ${DONUT_C.toFixed(1)}` }}
          />
        ) : null}
      </svg>
      <div className="don-v">
        {measured ? (
          <>
            {Math.round(clamped)}
            <small>%</small>
          </>
        ) : (
          t("shell.viz.na")
        )}
      </div>
      <div className="don-k">{label}</div>
      {sub ? <div className="don-z">{sub}</div> : null}
    </div>
  );
}

/* -------------------------------------------------------------------- heat */

/**
 * v3 `heat(n, counts)`: one cell per thing. The cells are decoration; the
 * strip's accessible name is the count of each kind, so a screen reader hears
 * "286 items: 7 critical, 18 warning, 4 in progress, 257 ok" rather than 286
 * squares.
 */
export function Heat({ cells, columns, label }: { cells: readonly Kind[]; columns?: number; label?: string }) {
  const t = useT();
  const counts = new Map<Kind, number>();
  for (const kind of cells) counts.set(kind, (counts.get(kind) ?? 0) + 1);
  const summary = (["crit", "warn", "run", "info", "mut", "ok"] as const)
    .filter((kind) => counts.has(kind))
    .map((kind) => translate(HEAT_KIND_KEY[kind], { count: counts.get(kind) ?? 0 }))
    .join(", ");
  return (
    <div
      className="heat"
      role="img"
      aria-label={t("shell.viz.heatLabel", { count: cells.length, label: label ?? t("shell.viz.heatItems"), summary: summary || t("shell.viz.heatNone") })}
      data-columns={columns}
      style={columns ? { gridTemplateColumns: `repeat(${columns}, 10px)` } : undefined}
    >
      {cells.map((kind, at) => (
        <i key={at} className={`h-${kind}`} />
      ))}
    </div>
  );
}

/* ---------------------------------------------------------------- timeline */

export interface TimelineRun {
  /** The time or identifier on the left, in mono: `patch.apply`, `04:12`. */
  at: string;
  kind: Kind;
  label: string;
  /** The bar's position, as percentages of the track. Absent draws no bar. */
  start?: number;
  width?: number;
}

/** v3 `timeline(runs, ticks)`. */
export function Timeline({ runs, ticks }: { runs: readonly TimelineRun[]; ticks: readonly string[] }) {
  useT();
  return (
    <div className="tl">
      {runs.map((run, at) => (
        <div className="tl-row" key={`${run.at}-${run.label}-${at}`}>
          <div className="tl-k">
            <Icon name={KIND_ICON[run.kind]} size={14} />
            <span className="sr-only">{kindLabel(run.kind)}</span>
            <span className="mono">{run.at}</span>
            <span className="tl-h">{run.label}</span>
          </div>
          <div className="tl-track">
            {run.start !== undefined && run.width !== undefined ? (
              <span className={`tl-bar b-${run.kind}`} style={{ left: `${run.start}%`, width: `${run.width}%` }} />
            ) : null}
          </div>
        </div>
      ))}
      <div className="tl-axis" aria-hidden="true">
        {ticks.map((tick, at) => (
          <i key={`${tick}-${at}`} style={{ left: `${ticks.length > 1 ? (at * 100) / (ticks.length - 1) : 0}%` }}>
            {tick}
          </i>
        ))}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- facts, kv */

/** v3 `facts([...])`: a strip of caption/value cells. */
export function Facts({ items }: { items: readonly { k: string; v: ReactNode }[] }) {
  return (
    <dl className="facts">
      {items.map((item) => (
        <div key={item.k}>
          <dt className="k">{item.k}</dt>
          <dd className="v">{item.v}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * v3 `kv([...])`: a two-column definition list.
 *
 * THE TERM IS A `ReactNode`, not a string. A `dt` is often a state -- a
 * `StatusPill` with its kind's glyph -- and typing it as text sent two
 * modules off to write `.kv` markup by hand to get one, which is how the
 * definition list stops being one piece. The row index is the key because a
 * node cannot be one; the list is rendered from a fixed array at each call
 * site, never reordered, so the index is stable.
 */
export function KV({ items }: { items: readonly (readonly [ReactNode, ReactNode])[] }) {
  return (
    <dl className="kv">
      {items.map(([k, v], at) => (
        <Fragment key={at}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </Fragment>
      ))}
    </dl>
  );
}

/* ------------------------------------------------------------------- steps */

export type StepState = "done" | "now" | "todo";

const STEP_CLASS: Record<StepState, string> = { done: "ok", now: "now", todo: "" };
const STEP_PILL: Record<StepState, { kind: Kind; text: MessageKey }> = {
  done: { kind: "ok", text: "shell.viz.stepDone" },
  now: { kind: "run", text: "shell.viz.stepNow" },
  todo: { kind: "mut", text: "shell.viz.stepTodo" },
};

/**
 * v3 `steps([...])`. The state is carried three ways -- the bar, the pill and
 * the words -- so the list reads the same without colour.
 */
export function Steps({
  items,
}: {
  items: readonly {
    label: string;
    state: StepState;
    /** THE SEVERITY OF A STEP THAT IS NOT SIMPLY DONE OR PENDING. v3 draws
     *  `.step.crit`, and a step blocked on a warning has `.step.warn`
     *  beside it (design additions, wave 2). Without this a blocked step
     *  had to be called critical or say nothing; the state pill and the
     *  words still carry it, so the bar is never the only carrier. */
    kind?: Kind;
    detail?: ReactNode;
    action?: ReactNode;
  }[];
}) {
  const t = useT();
  return (
    <ol className="steps" style={{ listStyle: "none", margin: 0, padding: 0 }}>
      {items.map((step, at) => (
        <li
          className={`step ${STEP_CLASS[step.state]}${step.kind ? ` ${step.kind}` : ""}`.trim()}
          key={step.label}
          aria-current={step.state === "now" ? "step" : undefined}
        >
          <i className="sbar" />
          <span className="six mono">{String(at + 1).padStart(2, "0")}</span>
          <div className="sbody">
            <b>{step.label}</b>
            {step.detail ? <p>{step.detail}</p> : null}
          </div>
          <div className="sgo">
            {step.action ?? <Pill kind={STEP_PILL[step.state].kind}>{t(STEP_PILL[step.state].text)}</Pill>}
          </div>
        </li>
      ))}
    </ol>
  );
}

/* ------------------------------------------------------------------ people */

const AVATAR_TONES = 6;

/**
 * v3 `avatar(ini, i, s)`: initials on one of six avatar tones, chosen by
 * index so the same person is the same colour on every page. White initials
 * clear 4.5:1 on all six (recorded in the design's `:root`). The type size
 * is a ramp step, never `size * .4`: 24 × .4 is 9.6px, which is off the ramp
 * and under the floor.
 */
export function Avatar({ initials, index = 0, size = 24 }: { initials: string; index?: number; size?: number }) {
  const tone = ((index % AVATAR_TONES) + AVATAR_TONES) % AVATAR_TONES + 1;
  return (
    <span
      className="av"
      aria-hidden="true"
      style={{
        background: `var(--av-${tone})`,
        width: size,
        height: size,
        fontSize: size >= 28 ? "var(--t-sm)" : "var(--t-cap)",
      }}
    >
      {initials}
    </span>
  );
}

/** v3 `person(ini, i, name)`: an avatar and the name beside it. */
export function Person({ initials, index = 0, name }: { initials: string; index?: number; name: string }) {
  return (
    <span className="who">
      <Avatar initials={initials} index={index} size={22} />
      <span>{name}</span>
    </span>
  );
}

/* ---------------------------------------------------------------------- os */

const OS_ICON: Record<string, [string, string]> = {
  windows: ["brand-windows", "Windows"],
  win: ["brand-windows", "Windows"],
  macos: ["brand-apple", "macOS"],
  mac: ["brand-apple", "macOS"],
  darwin: ["brand-apple", "macOS"],
  ios: ["brand-apple", "iOS"],
  ubuntu: ["brand-ubuntu", "Ubuntu"],
  debian: ["brand-debian", "Debian"],
  linux: ["brand-ubuntu", "Linux"],
  android: ["brand-android", "Android"],
};

/**
 * The platform's NAME, for a label or a table cell.
 *
 * `OS_ICON` already holds the name each glyph is announced by, and the
 * dashboard had grown a second copy of "Windows" / "macOS" / "Linux" to title
 * a donut and fill an OS column -- two tables that can disagree about what a
 * raw `os` value is called. The raw value is the fallback, never a guess:
 * `humaniseEnum` would render `win32` as "Win32", which is not this platform's
 * name either, and inventing one would be worse than repeating what the
 * server said. `/devices` and `/inventory` render the same field.
 */
export function osLabel(os: string): string {
  const key = os.toLowerCase().split(/[\s_-]/)[0] ?? "";
  return OS_ICON[key]?.[1] ?? os;
}

/**
 * v3 `os(k)`: the platform's brand glyph, with the platform's name for
 * assistive technology. `linux` takes the Ubuntu glyph as the design does;
 * anything unrecognised is a generic desktop.
 */
export function OsIcon({ os, size = 18 }: { os: string; size?: number }) {
  const t = useT();
  const key = os.toLowerCase().split(/[\s_-]/)[0] ?? "";
  const [icon, name] = OS_ICON[key] ?? ["device-desktop", os || t("shell.viz.unknownPlatform")];
  return (
    <span className="os" role="img" aria-label={name} style={{ display: "inline-flex" }}>
      <Icon name={icon} size={size} />
    </span>
  );
}

/* ------------------------------------------------------------------- chips */

/**
 * v3 `chips(list, on)`: a single-select filter row, each chip a real button.
 *
 * `stacked` MAKES IT A CHOICE GROUP RATHER THAN A FILTER STRIP. Wrapping is
 * right across the width of a page and wrong inside a table cell: the autonomy
 * matrix draws three options in each of nine cells, and every one of them
 * wrapped 2 + 1 at 1440px, so `Needs approval` sat alone on a second line and
 * no option lined up with the same option in the column beside it. Stacked,
 * each option is a row, the rows line up across the columns, and the cell is
 * as wide as the widest label instead of as wide as two of them.
 */
export function Chips({
  items,
  active,
  onSelect,
  label,
  stacked = false,
  disabled = false,
  labelledBy,
}: {
  items: readonly string[];
  active: number;
  onSelect: (index: number) => void;
  label?: string;
  /** One option per row, all the same width. For a choice group in a cell. */
  stacked?: boolean;
  /**
   * Genuinely inert, not merely dimmed. Each chip gets the real `disabled`
   * attribute, so a disabled group is dropped from the tab order (not just
   * unresponsive to a click that still lands) and is announced as
   * unavailable -- the same two properties `.btn:disabled` already carries,
   * transcribed onto `.chip` below. `aria-pressed` is left exactly as it
   * is: the state stays true, only unreachable.
   */
  disabled?: boolean;
  /**
   * Id of an element that already names this group on screen, in place of
   * `label`. `ChipsField` passes this rather than also passing `label`
   * itself, because its caller already prints the caption as a visible
   * `<span>` right beside the group -- naming the group with BOTH that span
   * and `label` announces the caption twice, once as the preceding text and
   * once again as the group's own `aria-label`. `label` remains this
   * component's own name for every caller with no such span to point at,
   * which is most of them.
   */
  labelledBy?: string;
}) {
  const t = useT();
  const naming = labelledBy ? { "aria-labelledby": labelledBy } : { "aria-label": label ?? t("shell.viz.filter") };
  return (
    <div className={stacked ? "chips stacked" : "chips"} role="group" {...naming}>
      {items.map((item, at) => (
        <button
          type="button"
          key={item}
          className={`chip${at === active ? " on" : ""}`}
          aria-pressed={at === active}
          disabled={disabled}
          onClick={() => onSelect(at)}
        >
          {at === active ? <Icon name="circle-check" size={13} /> : null}
          {item}
        </button>
      ))}
    </div>
  );
}

/**
 * v3's `field(caption, chips(...))`: a caption and a chip group, wired so the
 * caption names the group exactly once.
 *
 * THE THIRD CALL SITE FOR THE SAME ~40 LINES, NOW ONE. `EmailTab`'s
 * Encryption, `BudgetTab`'s "Fiscal year starts in" and `OrganizationTab`'s
 * per-key choice fields each hand-rolled
 * `<div className="field"><span>{caption}</span><Chips label={caption}
 * .../></div>` -- the caption announced twice (see `Chips`' `labelledBy`
 * doc), and, before `Chips` carried `disabled`, an entire parallel
 * `<Field><select disabled>...</select></Field>` arm standing in for a
 * control that had no inert state of its own.
 *
 * NOT `Field`. `Field` wraps its control in a `<label>`, which is right for
 * an `<input>` and wrong for `Chips`: a chip button nested inside a `<label>`
 * inherits the label's own text as ITS OWN accessible name, silently
 * renaming every chip to the caption (`Toggle`'s own comment documents the
 * same "button nested in a `<label>` is two things claiming the same click"
 * conflict). A plain `.field` `div` is the shape the design's own
 * `field(...)` draws, and is what all three sites used already.
 */
export function ChipsField({
  caption,
  items,
  active,
  onSelect,
  disabled = false,
  full = false,
  help,
  stacked = false,
}: {
  caption: string;
  items: readonly string[];
  active: number;
  onSelect: (index: number) => void;
  disabled?: boolean;
  /** Spans the `.form-grid` it sits in -- the same modifier `Field`'s own `full` draws. */
  full?: boolean;
  /** A line under the group, in the same mono `.help` style `OrganizationTab` prints its raw key in. */
  help?: ReactNode;
  /** One option per row. See `Chips`' own doc: right for a choice group in a cell, wrong for a page-width strip. */
  stacked?: boolean;
}) {
  const captionId = useId();
  return (
    <div className={full ? "field full" : "field"}>
      <span id={captionId}>{caption}</span>
      <Chips
        items={items}
        active={active}
        onSelect={onSelect}
        disabled={disabled}
        stacked={stacked}
        labelledBy={captionId}
      />
      {help != null ? <span className="help mono">{help}</span> : null}
    </div>
  );
}

/* ----------------------------------------------------------------- toolbar */

/** v3 `toolbar(left, right)`: filters on the left, the count on the right. */
export function Toolbar({ left, right }: { left: ReactNode; right?: ReactNode }) {
  return (
    <div className="tb">
      <div className="tb-l">{left}</div>
      {right ? <div className="tb-r">{right}</div> : null}
    </div>
  );
}
