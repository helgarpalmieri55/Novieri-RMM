import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { describeSubmitError } from "../../components/form";
import { EmptyState, NoValue } from "../../components/emptystate";
import { useT } from "../../i18n/context";
import { lazyLabels } from "../../i18n/lazy";
import { Badge, Table, Td, Th } from "../../components/ui";
import { ApiError } from "../../lib/api";
import { RelTime } from "../../components/reltime";
import { formatDateTime, intlLocale } from "../../lib/datetime";
import { Btn, FilterBar, Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KIND_ICON, kindOf } from "../../shell/viz";
import { useHealthAlerts, useUserTimezone } from "./api";
import {
  ALERT_STATE_PRESENTATION,
  SEVERITY_PRESENTATION,
  checkGlyph,
  checkLabel,
  countsAsResolved,
  describeAlertStatus,
  describeObservation,
  isAlertOpen,
  isMachineResolved,
  severityVariant,
} from "./healthState";
import type { AlertFilters, AlertOut, AlertState, HealthSeverity } from "./types";

// The live alert list (spec §13), filterable by state, severity and device, and
// sortable by flapping. Drawn in v3's row shape (`P['health']`): the check's
// glyph leads, the severity edge marks the row, the severity and the state
// are pills, and the way in is a button at the end of the row.
//
// THE ROW IS WHERE THE THREE STATES MUST NOT COLLAPSE. `state` and
// `cleared_reason` are rendered as SEPARATE cells, always:
//
//   * an ACKNOWLEDGED alert shows "Acknowledged" and an EMPTY clear cell,
//     because it is owned and still open;
//   * a MUTED alert shows "Muted" plus the instant the mute expires, because it
//     is silenced and still open — a mute is not a resolution;
//   * only a CLEARED alert shows a clear reason, and the reason is shown rather
//     than the word "cleared" alone, because "recovered" and "someone deleted
//     the check" are different outcomes.
//
// `healthState.ts` owns those three decisions; this file only lays them out.

const STATES: ReadonlyArray<AlertState> = ["active", "acknowledged", "muted", "cleared"];
const SEVERITIES: ReadonlyArray<HealthSeverity> = ["critical", "warning", "info"];

// THE FILTERS LIVE IN THE URL, AND THAT IS WHAT MAKES THE FIGURES ABOVE THIS
// LIST CLICKABLE. They were `useState`, so "Machine-resolved: 12" on the
// report panel had nowhere to send anybody — the twelve rows it counted were
// reachable only by an operator re-deriving the filter by hand. A filter in
// the address is also one an operator can paste into an incident channel,
// which is the other half of the same property.
//
// `state=open` and every `resolution` value are applied HERE, over the rows,
// not sent to the server: `GET /health/alerts` takes one concrete state and
// has no notion of "open (any of the three)" or of a clear reason. The
// predicates are `healthState.ts`'s own — `isAlertOpen`, `countsAsResolved`,
// `isMachineResolved` — so the list a figure links to is filtered by the SAME
// judgement that produced the figure. A second copy of "what counts as
// resolved" here is how the tile and the list come to disagree.
const OPEN_STATE = "open";

/** The `state` parameter: a real alert state, the synthetic "open", or none. */
type StateParam = AlertState | typeof OPEN_STATE | "";

const RESOLUTIONS = ["resolved", "machine", "human", "excluded"] as const;
type Resolution = (typeof RESOLUTIONS)[number] | "";

const RESOLUTION_LABEL: Readonly<Record<(typeof RESOLUTIONS)[number], string>> = lazyLabels<
  (typeof RESOLUTIONS)[number]
>({
  resolved: "health.console.resolved",
  machine: "health.console.machine",
  human: "health.console.human",
  excluded: "health.console.excluded",
});

// A URL IS UNTRUSTED INPUT. An unrecognised value reads as "no filter" rather
// than as a filter matching nothing — a page that renders "No alerts match
// this filter" because somebody mistyped a query string is the console
// telling an operator the fleet is quiet.
function readState(raw: string | null): StateParam {
  if (raw === OPEN_STATE) return OPEN_STATE;
  return STATES.includes(raw as AlertState) ? (raw as AlertState) : "";
}

function readSeverity(raw: string | null): HealthSeverity | "" {
  return SEVERITIES.includes(raw as HealthSeverity) ? (raw as HealthSeverity) : "";
}

function readResolution(raw: string | null): Resolution {
  return RESOLUTIONS.includes(raw as (typeof RESOLUTIONS)[number])
    ? (raw as (typeof RESOLUTIONS)[number])
    : "";
}

/**
 * Does this alert belong in the resolution bucket asked for?
 *
 * Every branch delegates to `healthState.ts`, deliberately: `countsAsResolved`
 * is where spec §7.4's exclusions live, and re-stating them here would put a
 * second definition of "resolved" in the product.
 */
export function matchesResolution(alert: AlertOut, resolution: Resolution): boolean {
  if (resolution === "") return true;
  if (alert.state !== "cleared") return false;
  const counted = countsAsResolved(alert);
  if (resolution === "excluded") return !counted;
  if (!counted) return false;
  if (resolution === "resolved") return true;
  return resolution === "machine" ? isMachineResolved(alert) : !isMachineResolved(alert);
}

export function AlertConsole({
  onSelect,
  selectedId,
}: {
  onSelect: (alertId: string) => void;
  selectedId?: string;
}) {
  const t = useT();
  const [params, setParams] = useSearchParams();
  const stateParam = readState(params.get("state"));
  const severityParam = readSeverity(params.get("severity"));
  const resolution = readResolution(params.get("resolution"));
  // One device's alerts, as the device page links here. The route filters on
  // `device_id` itself.
  const deviceParam = params.get("device") ?? "";

  // What the SERVER can narrow. "open" is not one of its states, so it is
  // dropped here and applied to the rows below.
  const filters: AlertFilters = {
    state: stateParam === OPEN_STATE ? "" : stateParam,
    severity: severityParam,
    ...(deviceParam ? { device_id: deviceParam } : {}),
  };

  // `replace`, because a filter is not a place: an operator who changed the
  // state select four times should press Back once and be where they came
  // from, not walk out through four intermediate filters.
  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value === "") next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  };

  const [flappingFirst, setFlappingFirst] = useState(false);
  const alerts = useHealthAlerts(filters);
  const zone = useUserTimezone();
  const forbidden = alerts.error instanceof ApiError && alerts.error.status === 403;
  // EVERY OTHER FAILURE USED TO VANISH HERE. `alerts.data ?? []` turns a 500,
  // a 503 and a dropped connection into an empty array, and the empty array
  // renders "No alerts match this filter." — the console stating, in its own
  // voice, that a fleet it could not reach has nothing wrong with it. That is
  // the single most dangerous sentence this module can produce, and it was
  // produced by a `??`.
  const loadFailed = Boolean(alerts.error) && !forbidden;

  const matched = (alerts.data ?? []).filter(
    (alert) =>
      (stateParam === OPEN_STATE ? isAlertOpen(alert) : true) &&
      matchesResolution(alert, resolution),
  );
  const rows = sortAlerts(matched, flappingFirst);
  const openCount = rows.filter(isAlertOpen).length;

  if (forbidden) {
    return (
      <Panel title={t("health.console.title")}>
        <p role="alert" className="note">
          {t("health.console.forbidden")}
        </p>
      </Panel>
    );
  }
  if (loadFailed) {
    return (
      <Panel title={t("health.console.title")}>
        <div role="alert" className="notice red">
          <Icon name={KIND_ICON.crit} size={17} />
          <div>{t("health.console.loadFailed", { detail: describeSubmitError(alerts.error) })}</div>
        </div>
      </Panel>
    );
  }

  return (
    <Panel
      title={t("health.console.title")}
      subtitle={alerts.data ? t("health.console.subtitle", { open: openCount, total: rows.length }) : undefined}
      bare
    >
      <FilterBar>
        <select
          aria-label={t("health.console.filterState")}
          className="select-field"
          value={stateParam}
          onChange={(e) => setParam("state", e.target.value)}
        >
          <option value="">{t("health.console.anyState")}</option>
          {/* "Open (any state)" is a REAL option and not a convenience:
              acknowledged and muted alerts are open, so a person looking
              for what is still wrong cannot get there by picking one
              state. It is also the destination of the report panel's
              "Still open" figure, which counts exactly this set. */}
          <option value={OPEN_STATE}>{t("health.console.openAny")}</option>
          {STATES.map((state) => (
            <option key={state} value={state}>
              {ALERT_STATE_PRESENTATION[state].label}
            </option>
          ))}
        </select>
        <select
          aria-label={t("health.console.filterSeverity")}
          className="select-field"
          value={severityParam}
          onChange={(e) => setParam("severity", e.target.value)}
        >
          <option value="">{t("health.console.anySeverity")}</option>
          {/* THE SAME LABEL THE ROWS CARRY. This list read `critical` /
              `warning` / `info` -- the wire words -- while the two selects
              beside it offered presentation labels, so one filter bar spoke
              two alphabets. */}
          {SEVERITIES.map((severity) => (
            <option key={severity} value={severity}>
              {SEVERITY_PRESENTATION[severity].label}
            </option>
          ))}
        </select>
        <select
          aria-label={t("health.console.filterResolution")}
          className="select-field"
          value={resolution}
          onChange={(e) => setParam("resolution", e.target.value)}
        >
          <option value="">{t("health.console.anyResolution")}</option>
          {RESOLUTIONS.map((value) => (
            <option key={value} value={value}>
              {RESOLUTION_LABEL[value]}
            </option>
          ))}
        </select>
        {deviceParam ? (
          <span className="flex flex-wrap items-center gap-2">
            <span className="note">
              {t("health.console.oneDevice")}{" "}
              <Link to={`/devices/${deviceParam}`}>
                {alerts.data?.[0]?.device_hostname || t("health.console.thisDevice")}
              </Link>
            </span>
            <Btn label={t("health.console.showEvery")} small onClick={() => setParam("device", "")} />
          </span>
        ) : null}
        <span className="filter-spacer" />
        <Btn
          label={flappingFirst ? t("health.console.sortSeverity") : t("health.console.sortRepeating")}
          icon="sort-descending"
          small
          onClick={() => setFlappingFirst((v) => !v)}
        />
      </FilterBar>

      {rows.length === 0 ? (
        <EmptyState
          icon="health"
          title={t("health.console.emptyTitle")}
          body={t("health.console.emptyBody")}
        />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>{t("health.console.colDevice")}</Th>
              <Th>{t("health.console.colState")}</Th>
              <Th>{t("health.console.colSeverity")}</Th>
              <Th>{t("health.console.colCheck")}</Th>
              <Th>{t("health.console.colCleared")}</Th>
              <Th>{t("health.console.colObserved")}</Th>
              <Th>{t("health.console.colSeen")}</Th>
              <Th> </Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((alert) => (
              <tr key={alert.id} aria-current={alert.id === selectedId ? "true" : undefined}>
                {/* The check's glyph leads and the severity edge marks the
                    row (only crit and warn draw one, as in v3); a cleared
                    alert carries no edge, whatever its severity was. */}
                <Td
                  lead={<Icon name={checkGlyph(alert.check_type)} size={18} />}
                  gutter={alert.state === "cleared" ? undefined : kindOf(alert.severity)}
                >
                  {/* A HOSTNAME IS ONE WORD, whatever the column is worth.
                      `lis-ws-041` broke at its own hyphens into three lines at
                      390px, and the hostname is the one identifier in the row
                      a person scans for. The table outgrows its scroller
                      instead, which is what `.table-wrap`'s horizontal scroll
                      (and its tab stop) is for. */}
                  <div className="cell-title">
                    <Link to={`/devices/${alert.device_id}`} style={{ whiteSpace: "nowrap" }}>
                      {alert.device_hostname || alert.device_id}
                    </Link>
                  </div>
                </Td>
                {/* NAME AND STATUS FIRST: on a phone the table scrolls
                    sideways, and these two are what fit before it does. */}
                <Td>
                  <span className="flex flex-wrap items-center gap-1">
                    <Badge variant={ALERT_STATE_PRESENTATION[alert.state].variant}>
                      {ALERT_STATE_PRESENTATION[alert.state].label}
                    </Badge>
                    {/* A muted alert is STILL OPEN, and the expiry is what
                        makes that legible rather than a silent hole. */}
                    {alert.state === "muted" && alert.muted_until && (
                      <span className="cell-sub">
                        {t("health.console.until")} <RelTime iso={alert.muted_until} zone={zone} />
                      </span>
                    )}
                    {alert.flapping && (
                      <Badge variant="warning">{t("health.console.repeating", { count: alert.flap_count })}</Badge>
                    )}
                  </span>
                </Td>
                <Td>
                  <Badge variant={severityVariant(alert.severity)}>
                    {SEVERITY_PRESENTATION[alert.severity].label}
                  </Badge>
                </Td>
                <Td>
                  {/* THE CHECK, IN WORDS. `checkLabel` is the one place that
                      translation is made; the selector stays beneath it,
                      because "Disk free" on two mounts of the same machine is
                      two different facts. */}
                  <div className="cell-title">{checkLabel(alert.check_type)}</div>
                  {alert.selector ? <div className="cell-sub mono">{alert.selector}</div> : null}
                </Td>
                <Td>
                  {/* EMPTY for every open alert, including acknowledged and
                      muted ones. Only a cleared alert has an outcome. */}
                  {alert.state === "cleared" ? (
                    <span className="cell-sub">{describeAlertStatus(alert)}</span>
                  ) : (
                    <NoValue label={t("health.console.stillOpen")} />
                  )}
                </Td>
                <Td>
                  {/* THE COMPARATOR IS THE AGENT'S ALPHABET, NOT A READER'S.
                      This cell read "96 vs gt 95", and on an alert with no
                      observed value it read "vs is true" -- a sentence
                      beginning with its own operator. `describeObservation`
                      is the one place that translation is made, so this cell,
                      the detail drawer and the dashboard cannot drift apart. */}
                  {/* NOT `.mono`: this is a sentence ("3.1% under 5"), not a
                      payload fragment, and a monospaced ribbon is what made it
                      read as one. */}
                  <span>{describeObservation(alert)}</span>
                </Td>
                <Td className="whitespace-nowrap">
                  <RelTime iso={alert.last_seen_at} zone={zone} />
                </Td>
                <Td>
                  <Btn label={t("health.console.open")} icon="arrow-right" small onClick={() => onSelect(alert.id)} />
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Panel>
  );
}

const SEVERITY_RANK: Readonly<Record<HealthSeverity, number>> = { critical: 0, warning: 1, info: 2 };

/**
 * Open before closed, then critical, warning, info, then newest first -- or
 * repeating first when asked (spec §13's sort). Newest-first alone put the
 * one critical alert on row nine. Exported for the tests: the ordering is a
 * claim about which rows an operator sees, not a detail.
 */
export function sortAlerts(alerts: readonly AlertOut[], flappingFirst: boolean): AlertOut[] {
  const rows = [...alerts];
  rows.sort((a, b) => {
    if (flappingFirst && a.flapping !== b.flapping) return a.flapping ? -1 : 1;
    if (flappingFirst && a.flap_count !== b.flap_count) return b.flap_count - a.flap_count;
    const aOpen = isAlertOpen(a);
    if (aOpen !== isAlertOpen(b)) return aOpen ? -1 : 1;
    if (aOpen && a.severity !== b.severity) {
      return SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity];
    }
    return b.opened_at.localeCompare(a.opened_at, intlLocale());
  });
  return rows;
}

/**
 * DEPRECATED, and kept only because `modules/sla/CalendarsPanel.tsx` imports
 * it from here. Health itself no longer calls it: every instant in this module
 * now goes through `lib/datetime#formatDateTime` with the reader's zone.
 *
 * WHAT IT USED TO DO, and why that was a defect: it was
 * `new Date(value).toISOString().replace("T", " ").slice(0, 16)` -- which
 * renders the instant in UTC, always, with no indication that it has done so.
 * An operator in Bogota reading "Last contact 2026-08-20 19:00" was looking at
 * a moment their own clock called 14:00. Auditor B's BLOCKER is the entry half
 * of the same mechanism; this was the exit half, in a second format
 * (`2026-08-20 19:00`) that agreed with nothing else on the page.
 *
 * The body is now a delegation, so the SLA row it still feeds is corrected
 * too rather than left on the old renderer while health moves off it.
 */
export function formatInstant(value: string, zone?: string | null): string {
  return formatDateTime(value, zone);
}
