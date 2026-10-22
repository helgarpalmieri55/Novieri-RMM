/**
 * The page-level component set -- TRANSCRIBED from the v3 design's own kit
 * (`head`, `btn`, `tabs`, `tiles`, `panel`, `state`, `bar`, `rows`, `empty`,
 * `toolbar` in `docs/design/itms-prototype-v3.html`).
 *
 * EVERY EXPORT KEEPS ITS NAME AND PROPS. 165 files import from here, and the
 * v3 port (spec §4) changes how they look without changing their code: a
 * `StatCard` is now a v3 `Tile`, a `StatusPill` a v3 `.pill` with its kind's
 * glyph, a `Btn` the v3 `.btn`. What each adds over the design's string
 * templates is the three states a mock never has -- loading, empty, error --
 * and the accessibility a static page never needed.
 *
 * The pieces v3 introduced (`Tile`, `Sparkline`, `Donut`, `Heat`, `Timeline`,
 * `Facts`, `KV`, `Steps`, `Avatar`, `Person`, `OsIcon`, `Chips`, `Toolbar`)
 * live in `./viz.tsx`; `Kind` and `kindOf` are re-exported from there so a
 * caller needs one import.
 */

import type {
  CSSProperties,
  InputHTMLAttributes,
  KeyboardEvent,
  MouseEvent,
  ReactNode,
  Ref,
} from "react";

import { describeSubmitError } from "../components/form";
import { useT } from "../i18n/context";
import { humaniseStatus } from "../lib/labels";

import { Icon, type IconProps } from "./icons";
import { KIND_ICON, Pill, Tile, kindOf, type Kind } from "./viz";
import "./components.css";

export { kindOf, KIND_ICON, type Kind } from "./viz";

/* ---------------------------------------------------------------- page head */

/**
 * v3 `head(title, sub, actions)`: the title, the sentence under it and the
 * action row, which wraps below a long title on a narrow viewport.
 *
 * NO EYEBROW. The previous design let a page draw its own qualifier above the
 * `h1`; v3 draws nothing between the crumb and the title, and the crumb is
 * the shell's -- `AppShell` renders it as the first line of the content area,
 * as a real `<nav aria-label="Breadcrumb">` with a real link. A page-drawn
 * eyebrow restated the section the crumb had just named, one line above it.
 * The prop and its `.eyebrow` rule are gone; four module tests already assert
 * that nothing renders one.
 */
export function PageHead({
  title,
  description,
  actions,
}: {
  title: string;
  /**
   * OPTIONAL, deliberately. The design shows a sentence under every title, and
   * a migration that needed one for all thirty screens would have invented
   * sixty-six of them. An absent description renders nothing; a wrong one
   * describes the product incorrectly in the operator's console for ever.
   */
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className="ph">
      <div className="ph-row">
        <div>
          <h1>{title}</h1>
          {description ? <p className="sub">{description}</p> : null}
        </div>
        {actions ? <div className="ph-act">{actions}</div> : null}
      </div>
    </section>
  );
}

/* ----------------------------------------------------------------- controls */

/**
 * v3 `btn(label, icon, pri, ghost)`, plus the states a mock never draws.
 *
 * FOUR VARIANTS, AND `gold` IS NOT ONE OF THEM. v3 has one accent and no
 * second commit colour, so the retired gold button maps to the plain
 * secondary one; the prop stays accepted so no caller breaks, and a module is
 * finished migrating when it stops passing it.
 */
export function Btn({
  label,
  icon,
  variant = "secondary",
  small = false,
  onClick,
  type = "button",
  disabled = false,
  ariaLabel,
  iconOnly = false,
  pressed,
  title,
}: {
  label: string;
  icon?: string;
  /** `ghost` paints no edge and the accent ink; `danger` the critical edge and ink. */
  variant?: "secondary" | "primary" | "danger" | "gold" | "ghost";
  small?: boolean;
  /** THE EVENT IS PASSED, and that is not a convenience. A button inside a
   *  row that opens on click has to stop the click propagating, and with no
   *  event there was nothing to stop -- so three controls on the error
   *  monitor dropped out of the kit to a raw `button.btn.small` and their
   *  comment named the missing prop. A caller that ignores it still
   *  type-checks. */
  onClick?: (event: MouseEvent<HTMLButtonElement>) => void;
  type?: "button" | "submit";
  /** A page action with pending state needs this, or every caller drops out of
   *  the design system to get it -- which is what was happening. */
  disabled?: boolean;
  /** The name assistive technology hears, when the visible label cannot carry
   *  it: two hundred rows whose every action reads "Copy" is two hundred
   *  identical buttons to a screen reader. Falls back to the visible label. */
  ariaLabel?: string;
  /** Hides the label visually and keeps it for assistive technology. `label`
   *  stays REQUIRED so an icon-only button cannot ship nameless. */
  iconOnly?: boolean;
  /** A TOGGLE, not a command: emits `aria-pressed` and takes v3's `.on`.
   *  Without it a page that needs an on/off control drops out of the kit and
   *  writes a raw `button.btn.small`, which is what the error monitor's
   *  Events toggle was doing -- and a raw button carries no `aria-pressed`,
   *  so its state was invisible to anything not looking at pixels. */
  pressed?: boolean;
  /** A REASON A DISABLED ACTION IS DISABLED, on hover -- a control that
   *  cannot be used because of what its own row is, not because of anything
   *  the operator can do about it, needs a place to say so. */
  title?: string;
}) {
  const tone = variant === "secondary" || variant === "gold" ? "" : variant;
  const cls = ["btn", tone, small ? "small" : "", iconOnly ? "icon-only" : "", pressed ? "on" : ""]
    .filter(Boolean)
    .join(" ");
  return (
    <button
      className={cls}
      onClick={onClick}
      type={type}
      disabled={disabled}
      title={title}
      aria-label={ariaLabel ?? (iconOnly ? label : undefined)}
      aria-pressed={pressed}
    >
      {icon ? <Icon name={icon} size={14} /> : null}
      {iconOnly ? null : <span>{label}</span>}
    </button>
  );
}

/* -------------------------------------------------------------------- stats */

/**
 * The tile row -- and the column count is an ATTRIBUTE, not an inline style.
 *
 * An inline `gridTemplateColumns` outranks every stylesheet rule short of
 * `!important`, so `.stats-grid`'s three folding steps (three columns at
 * 1300px, two at 1024px, one at 640px, `components.css`) never applied while
 * it was one. `data-columns` puts the count in the same cascade layer as the
 * media queries that override it; a count the CSS does not enumerate falls
 * back to the base rule rather than rendering unstyled.
 */
export function StatsGrid({
  columns = 4,
  keepTrackWidth = false,
  children,
}: {
  columns?: number;
  /**
   * Keep the declared track width when the grid holds exactly two tiles,
   * instead of stretching them to two halves.
   *
   * WHY THIS ESCAPE HATCH EXISTS, and why it is not a style preference. A
   * design review found `/discovered` leaving the right half of its tile row
   * empty at 1440 while `/service-desk/my-work` stretched its two across, and
   * the kit answered by stretching every two-tile row. That rule cannot tell
   * an author who asked for four tracks from one who said nothing, because
   * `columns` DEFAULTS to 4 -- so it would have silently reversed a decision
   * `/discovered` had already made and written down: the design draws three
   * tiles there, the console deliberately ships two (the third's only
   * destination was the page it sits on, and the operator asked us to stop
   * shipping dead tiles), and the quarter width is what keeps two ordinary
   * counters from reading as the page's headline.
   *
   * So the stretch is the default and this is the opt-out, rather than the
   * page silently losing an argument to a CSS rule.
   */
  keepTrackWidth?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="stats-grid" data-columns={columns} data-keep-tracks={keepTrackWidth ? "" : undefined}>
      {children}
    </div>
  );
}

/** The hue a metric owned in the previous design. Kept so no caller breaks. */
export type StatTone =
  | "primary"
  | "action"
  | "violet"
  | "gold"
  | "orange"
  | "green"
  | "red"
  | "blue"
  | "neutral";

export interface StatTrend {
  direction: "up" | "down";
  /** "+4 since yesterday" -- the period is not optional; a delta without one
   *  is not a fact. */
  label: string;
}

/**
 * The tone-and-urgency vocabulary of the previous design, as v3 kinds. v3 is
 * explicit that only crit and warn colour a whole tile, so the wash is
 * reserved for the urgent red and gold tiles; green colours the figure; every
 * other tone is the plain white card, with the label and the icon carrying the
 * category.
 */
function tileKind(tone: StatTone, loud: boolean): Kind | undefined {
  if (loud && tone === "red") return "crit";
  if (loud && (tone === "gold" || tone === "orange")) return "warn";
  if (tone === "green") return "ok";
  return undefined;
}

/**
 * A metric tile. KEEPS ITS PROPS AND RENDERS A v3 `Tile`.
 *
 * `to` IS REQUIRED, AND THAT IS THE FEATURE. A number on a dashboard is a
 * promise that somewhere there is a list behind it; this type refuses the
 * tile until somebody says where. It renders as a LINK, so it is keyboard
 * reachable, middle-clickable and pasteable into an incident channel.
 *
 * ZERO IS A MEASUREMENT, NOT AN ABSENCE. A zero tile drops out of the urgent
 * treatment on purpose: "0 breached" wearing an alarm wash reads as a perfect
 * score, and the audit found nine of those on one page while nothing had been
 * measured at all. Pass `zeroLabel` to say which it is.
 *
 * THE `.stat-card` / `.stat-label` / `.stat-value` / `.stat-foot` / `.urgent`
 * / `.tone-*` CLASS NAMES STAY, as hooks with no rules behind them: thirty
 * module tests locate tiles by them, and a hook that costs nothing is cheaper
 * than thirty tests rewritten twice (once now, once when each page migrates to
 * `Tile` directly).
 */
export function StatCard({
  label,
  value,
  icon,
  to,
  tone = "neutral",
  urgent = false,
  trend,
  zeroLabel,
  foot,
  spark,
  pending = false,
}: {
  label: string;
  value: string;
  icon: string;
  /** Where the number lives. Required: see above. */
  to: string;
  tone?: StatTone;
  /** Needs a person. The crit or warn wash, and the trend slot is honoured. */
  urgent?: boolean;
  trend?: StatTrend;
  /** What a zero means here -- "nothing breached" vs "nothing measured". */
  zeroLabel?: string;
  foot?: ReactNode;
  /** The trend behind the figure, as v3's corner sparkline. Forwarded to
   *  `Tile`; a page wanting one no longer has to render `Tile` directly and
   *  lose `StatCard`'s zero handling and its tone vocabulary. */
  spark?: readonly number[];
  /**
   * NOT MEASURED YET, WHICH IS NOT THE SAME FACT AS ZERO.
   *
   * A tile above a `QueryState` renders while its list is still loading, and
   * with nothing to count the caller passes `String(items.length)` -- so
   * every stat row in the console flashed a confident `0` before the data
   * arrived, and kept it if the request failed. The spec's rule is explicit:
   * unmeasured renders `n/a`, never `0`. This is that rule, in the one place
   * every page inherits it from; the label, the tone and the destination are
   * unchanged, because the tile is still about the same thing.
   */
  pending?: boolean;
}) {
  // A measured zero is not urgent, whatever the caller said. Neither is an
  // unmeasured one, and an unmeasured one is not a zero at all.
  const t = useT();
  const isZero = !pending && value.trim() === "0";
  const loud = urgent && !isZero && !pending;
  const kind = pending ? "mut" : tileKind(tone, loud);

  const sub =
    (isZero && zeroLabel) || (trend && !isZero && !pending) || foot ? (
      <>
        {isZero && zeroLabel ? <span className="stat-zero">{zeroLabel}</span> : null}
        {trend && !isZero && !pending ? (
          <span className={`trend stat-trend ${trend.direction}`}>
            <Icon name={trend.direction === "up" ? "arrow-up" : "arrow-down"} size={12} />
            <span className="sr-only">{trend.direction === "up" ? t("shell.ui.statUp") : t("shell.ui.statDown")}</span>
            {trend.label}
          </span>
        ) : null}
        {foot ? <span className="stat-foot">{foot}</span> : null}
      </>
    ) : undefined;

  return (
    <Tile
      className={`stat-card tone-${tone}${loud ? " urgent" : ""}`}
      parts={{ label: "stat-label", value: "stat-value" }}
      label={label}
      value={pending ? t("shell.viz.na") : value}
      icon={icon}
      to={to}
      kind={kind}
      spark={pending ? undefined : spark}
      sub={sub}
    />
  );
}

/* --------------------------------------------------------------------- tabs */

export interface TabItem {
  key: string;
  label: string;
  count?: number;
}

/**
 * v3 `tabs(items, on)`, as a real tablist.
 *
 * ROVING TABINDEX: a tablist is ONE tab stop, and the arrow keys move within
 * it. `aria-controls` names the panel when the page has one and is omitted
 * otherwise -- an `aria-controls` pointing at nothing is worse than none.
 */
export function Tabs({
  items,
  active,
  onSelect,
  panelId,
  scroll = false,
}: {
  items: readonly TabItem[];
  active: string;
  onSelect: (key: string) => void;
  /** The id of the `role="tabpanel"` element these tabs switch, once the page
   *  has one. Omitted rather than guessed. */
  panelId?: string;
  /** One row that scrolls sideways instead of folding into several, for a
   *  strip too long for a phone. */
  scroll?: boolean;
}) {
  const move = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    const jump = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : -1;
    if (step === 0 && jump < 0) return;
    event.preventDefault();

    const current = items.findIndex((item) => item.key === active);
    // Wrapping, because a tablist is a ring.
    const next = jump >= 0 ? jump : (current + step + items.length) % items.length;
    const target = items[next];
    if (!target) return;
    onSelect(target.key);
    // Selection follows focus, so focus has to follow selection.
    event.currentTarget.querySelector<HTMLButtonElement>(`[data-tab="${target.key}"]`)?.focus();
  };

  return (
    <div className={scroll ? "tabs scroll" : "tabs"} role="tablist" onKeyDown={move}>
      {items.map((item) => (
        <button
          type="button"
          className={`tab${item.key === active ? " active" : ""}`}
          key={item.key}
          data-tab={item.key}
          role="tab"
          aria-selected={item.key === active}
          aria-controls={panelId}
          tabIndex={item.key === active ? 0 : -1}
          onClick={() => onSelect(item.key)}
        >
          {item.label}
          {item.count === undefined ? null : <span className="tab-count"> {item.count}</span>}
        </button>
      ))}
    </div>
  );
}

/* -------------------------------------------------------------------- panel */

/** v3 `panel(title, sub, body, actions, bare)`. */
export function Panel({
  title,
  subtitle,
  actions,
  children,
  bare = false,
  headingLevel = 2,
}: {
  title?: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
  /** A panel wrapping a table has no padded body -- the design's lists sit flush. */
  bare?: boolean;
  /** The rung this panel's title sits on. `2` is right for a panel at page
   *  level; a panel NESTED inside another panel is a level deeper, and
   *  repeating `h2` inside `h2` is what axe `heading-order` reports (WCAG
   *  1.3.1). AlertDetail's per-action panels and DeviceHealthPanel's second
   *  panel are the nested case. */
  headingLevel?: 2 | 3;
}) {
  const Heading = headingLevel === 3 ? "h3" : "h2";
  return (
    <section className="panel">
      {title ? (
        <div className="panel-h">
          <div>
            {/* AN `h2`, NOT A STYLED `div`: the rung between the page's `h1` and
                the `h3`s empty states render. Without it the outline jumps
                h1 -> h3 on every page that carries a panel (axe
                `heading-order`, WCAG 1.3.1). `.panel-title` stays as the
                class its test and three modules know it by. */}
            <Heading className="panel-title">{title}</Heading>
            {subtitle ? <p>{subtitle}</p> : null}
          </div>
          {actions ? <div className="panel-a">{actions}</div> : null}
        </div>
      ) : null}
      <div className={bare ? "panel-b bare" : "panel-b"}>{children}</div>
    </section>
  );
}

/**
 * v3's own settings panel names its sections:
 * `<div style="font-weight:500"><icon> Maintenance window</div>`. A heading
 * here rather than a styled div, because it is what a screen reader
 * navigates by, and an `h3` because a titled `Panel` has already rendered
 * the `h2` above it -- a second `h2` inside it, or a bare div, is the same
 * `heading-order` defect `panel.test.tsx` exists to catch one level up.
 *
 * The glyph is decorative, not informative -- it restates the words beside
 * it -- so it must not join the heading's accessible name; `Icon` renders
 * every glyph `aria-hidden="true"` unconditionally (`./icons.tsx`), which is
 * what keeps this correct without a prop here to get wrong.
 */
export function FieldGroup({
  title,
  icon,
  children,
}: {
  title: string;
  icon: IconProps["name"];
  children: ReactNode;
}) {
  return (
    <section className="field-group">
      <h3>
        <Icon name={icon} size={14} />
        {title}
      </h3>
      {children}
    </section>
  );
}

/* ------------------------------------------------------------------- status */

/**
 * v3 `state(kind, text)`: the pill, WITH ITS KIND'S GLYPH, because colour is
 * never the only carrier (crit circle-x, warn alert-triangle, ok circle-check,
 * run progress, mut minus, info info-circle).
 *
 * `kind` DEFAULTS TO `kindOf(value)`, the one table that turns a wire word
 * into a severity; a caller that knows better passes `kind`. `label` defaults
 * to the humanised value rather than the identifier -- `not_applicable`,
 * `refused_loop` and raw snake_case credential types were five separate
 * module bugs that were one bug, leaking from here.
 */
export function StatusPill({ value, label, kind }: { value: string; label?: string; kind?: Kind }) {
  useT();
  return <Pill kind={kind ?? kindOf(value)}>{label ?? humaniseStatus(value)}</Pill>;
}

const PROGRESS_KIND: Record<"" | "gold" | "green" | "red" | "mut", Kind> = {
  // `""` STAYS "run", DELIBERATELY. An untoned bar elsewhere in the console
  // genuinely is a running job, and changing the default would recolour every
  // one of them. `mut` is the NEUTRAL rung, asked for by name: a compliance
  // split that nobody is held to a target on is not progress, and painting it
  // in the running blue said it was.
  "": "run",
  gold: "warn",
  green: "ok",
  red: "crit",
  mut: "mut",
};

/**
 * v3 `bar(pct, kind)`: a percentage as a bar, and one a screen reader can
 * read. `aria-valuetext` AS WELL AS `aria-valuenow`, because the two say
 * different things; `label` defaults rather than being required because the
 * default is safe -- a bar with no accessible name is an axe failure, and one
 * labelled "Progress" that reads its value is strictly better than a silent
 * one.
 */
export function Progress({
  value,
  tone = "",
  label,
}: {
  value: number;
  /** The previous design's names; v3's kinds under them: gold warns, green is ok, red is critical, `mut` is the neutral rung for a measure with no target, and the default is the running blue. */
  tone?: "" | "gold" | "green" | "red" | "mut";
  label?: string;
}) {
  const t = useT();
  const clamped = Math.max(0, Math.min(100, value));
  const pct = Math.round(clamped);
  return (
    <span
      className={`bar progress ${tone}`.trim()}
      role="progressbar"
      aria-label={label ?? t("shell.ui.progress")}
      aria-valuenow={pct}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuetext={`${pct}%`}
    >
      {/* The unrounded value drives the pixels; the rounded one is what is said. */}
      <i className={PROGRESS_KIND[tone]} style={{ width: `${clamped}%` }} />
    </span>
  );
}

/* -------------------------------------------------------------------- table */

/** A scroll container, so a tab stop: see `Table` in `components/ui.tsx`. */
export function TableWrap({ children }: { children: ReactNode }) {
  return (
    <div className="table-wrap" tabIndex={0}>
      {children}
    </div>
  );
}

/**
 * The three states the prototype has no concept of, in v3's `.empty` shape.
 *
 * A mock is always loaded, always non-empty and never fails. A console is
 * regularly all three, and a screen that renders an empty table body for
 * "still loading", "nothing here" and "the request failed" alike is telling
 * the operator nothing -- which is how a broken integration reads as an empty
 * fleet. The failure is announced (`role="alert"`) and SAYS WHAT WENT WRONG:
 * a 403, a 404 and a 500 need different responses from the reader.
 */
export function QueryState({
  loading,
  error,
  empty,
  emptyLabel,
  emptyIcon = "devices",
  children,
}: {
  loading: boolean;
  error: unknown;
  empty: boolean;
  emptyLabel: string;
  emptyIcon?: string;
  children: ReactNode;
}) {
  // THE GLYPH IS WRAPPED, IN ALL THREE BRANCHES, AND THAT IS THE FIX.
  // `.empty` centres with `text-align: center`, which reaches inline content
  // -- and Tailwind's preflight sets `svg { display: block }`, so the icon is
  // a block box at the left edge of a centred column. `EmptyState` never had
  // the bug because it already wrapped its icon; `.empty-icon`
  // (`components.css`, `display: inline-grid`) is that wrapper, and it exists
  // already. No module could reach this: `QueryState` exposes no render slot.
  const t = useT();
  const glyph = (name: string) => (
    <span className="empty-icon">
      <Icon name={name} size={26} />
    </span>
  );
  if (loading) {
    // No live-region role here. The loading placeholder is the one branch that
    // must stay silent: pages put their own `role="status"` line on the screen
    // once data lands (the groups sweep record, the capped-list notice), and a
    // placeholder announcing itself first is what those lines' tests find.
    return (
      <div className="empty">
        {glyph("clock")}
        <b>{t("shell.ui.loading")}</b>
      </div>
    );
  }
  if (error) {
    return (
      <div className="empty" role="alert">
        {glyph(KIND_ICON.crit)}
        <b>{describeSubmitError(error)}</b>
      </div>
    );
  }
  if (empty) {
    return (
      <div className="empty">
        {glyph(emptyIcon)}
        <b>{emptyLabel}</b>
      </div>
    );
  }
  return <>{children}</>;
}

/* --------------------------------------------------------------- cell parts */

/** v3's row lead and title: a muted glyph, a 600 title, a small muted sub-line. */
export function CellMain({
  glyph,
  title,
  sub,
}: {
  glyph: string;
  title: ReactNode;
  sub?: ReactNode;
}) {
  return (
    <div className="cell-main">
      <span className="device-glyph">
        <Icon name={glyph} size={18} />
      </span>
      <span style={{ minWidth: 0 }}>
        <div className="cell-title">{title}</div>
        {sub ? <div className="cell-sub">{sub}</div> : null}
      </span>
    </div>
  );
}

/** v3 `toolbar(left, right)` drawn flush inside a bare panel, above its table. */
export function FilterBar({ children }: { children: ReactNode }) {
  return <div className="filterbar">{children}</div>;
}

/**
 * A SEARCH BOX WITH THE MAGNIFIER v3 DRAWS INSIDE IT.
 *
 * The design's search control is `.inp.srch` -- the glyph within the box,
 * before the placeholder. The console rendered a bare `<input class=
 * "search-field">` in twenty modules and drew no glyph in any of them, so the
 * one control a person looks for first was the only one with no icon on it.
 * Decorating just the page that filed it would have forked the control, which
 * is why this is a kit piece and not a page's `<span>`.
 *
 * AN `<input>` CANNOT HOLD A CHILD, so the glyph is positioned over it and
 * the field padded clear -- the same three lines `.topbar-find` already uses
 * for the same reason. The wrapper takes the flex the bare field had inside
 * `.filterbar` / `.tb`, so a search box still grows and a select still does
 * not.
 *
 * `type="search"` IS NOT THE DEFAULT HERE, deliberately. It brings the user
 * agent's own clear button, which is a second control inside a box that now
 * has a glyph in it; the pages that want one ask for it.
 */
export function SearchField({
  wrapperStyle,
  className,
  inputRef,
  ...rest
}: InputHTMLAttributes<HTMLInputElement> & {
  /** Geometry belongs to the WRAPPER now: it is the flex child, not the field. */
  wrapperStyle?: CSSProperties;
  /** /docs focuses its box from a keyboard shortcut. */
  inputRef?: Ref<HTMLInputElement>;
}) {
  return (
    <span className="srch-wrap" style={wrapperStyle}>
      <Icon name="search" size={14} />
      <input
        {...rest}
        ref={inputRef}
        className={className ? `search-field ${className}` : "search-field"}
      />
    </span>
  );
}

/**
 * The FOOT of a paged list, flush inside a bare panel.
 *
 * `FilterBar` IS THE HEAD OF A LIST, and three pages were using it as a foot
 * for want of this: it rules along its BOTTOM, which under a foot draws a
 * hairline beneath a row that has nothing below it. `.table-foot` rules along
 * its top, which is the edge a foot shares with the table.
 */
export function TableFoot({ children }: { children: ReactNode }) {
  return <div className="table-foot">{children}</div>;
}

/**
 * The result count in a list foot: "Showing 1-14 of 14 devices."
 *
 * NOT `.mono`, WHICH IS WHAT THIS EXISTS TO REPLACE. `Pager` set its count in
 * the monospace face and every foot copied it, so /devices' count broke across
 * THREE lines beside the pager buttons at 390px while the comparable counts
 * elsewhere ("7 of 7", "4 shown") sat inline. A count is a sentence, so it is
 * set in the page's face; `.count` keeps the one property the mono face was
 * actually buying -- tabular figures, so the number does not jitter as the
 * page changes under it.
 */
export function ResultCount({ children }: { children: ReactNode }) {
  return <span className="count">{children}</span>;
}

/**
 * The foot of a paged list: "Page 2 of 12" and the two ghost buttons.
 *
 * LIFTED FROM `modules/patch/Pager.tsx`, which is where it was written and
 * where its own doc comment reported it as a kit gap. Three lists on /patch,
 * /devices and /enrollment drew the same foot out of the same utilities.
 *
 * NAMED CONTROLS. A page with three of these has three "Previous" buttons.
 * A list that owns the page keeps the bare word; a second list changes the
 * label itself ("Next devices"), and a drill-down names its subject through
 * `scopeName`, so no two feet on one page read the same to a screen reader.
 */
export function Pager({
  page,
  pageCount,
  onPrevious,
  onNext,
  scopeName,
  previousLabel,
  nextLabel,
  children,
}: {
  /** Zero-based. */
  page: number;
  pageCount: number;
  onPrevious: () => void;
  onNext: () => void;
  /** What is being paged, when the label alone would be ambiguous on the
   *  page: "srv-app02's updates". Absent, the label is the name. */
  scopeName?: string;
  previousLabel?: string;
  nextLabel?: string;
  /** What to count, in the caller's own words, in place of "Page n of m":
   *  "Showing 1-50 of 214 devices." */
  children?: ReactNode;
}) {
  const t = useT();
  if (pageCount <= 1) return null;
  const previous = previousLabel ?? t("shell.ui.previous");
  const next = nextLabel ?? t("shell.ui.next");
  return (
    <TableFoot>
      <span className="filter-spacer" />
      <ResultCount>
        {children ?? (
          t("shell.ui.pageOf", { page: page + 1, count: pageCount })
        )}
      </ResultCount>
      <Btn
        label={previous}
        ariaLabel={scopeName ? t("shell.ui.pageOfScope", { label: previous, scope: scopeName }) : undefined}
        variant="ghost"
        small
        disabled={page === 0}
        onClick={onPrevious}
      />
      <Btn
        label={next}
        ariaLabel={scopeName ? t("shell.ui.pageOfScope", { label: next, scope: scopeName }) : undefined}
        variant="ghost"
        small
        disabled={page >= pageCount - 1}
        onClick={onNext}
      />
    </TableFoot>
  );
}

/**
 * v3 `.tog`: an on/off switch.
 *
 * A REAL `role="switch"`, not a repainted checkbox. The design draws the
 * track and the knob and nothing else, so the state would be carried by
 * position alone; `aria-checked` is what says it out loud, and the visible
 * `label` beside it is what a pointer aims at. The patch policy dialog's
 * auto-approve stayed a bare checkbox for want of this piece.
 */
export function Toggle({
  checked,
  onChange,
  label,
  disabled = false,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      className="toggle-row"
      onClick={() => onChange(!checked)}
    >
      <span className={checked ? "tog on" : "tog"} aria-hidden="true">
        <i />
      </span>
      <span>{label}</span>
    </button>
  );
}
