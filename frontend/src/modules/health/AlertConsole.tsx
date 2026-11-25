import { useState } from "react";
import { Link } from "react-router-dom";
import { describeSubmitError } from "../../components/form";
import { NoValue } from "../../components/emptystate";
import { useT } from "../../i18n/context";
import { lazyLabels } from "../../i18n/lazy";
import { Badge, Table, Td, Th } from "../../components/ui";
import { ApiError } from "../../lib/api";
import { RelTime } from "../../components/reltime";
import { formatDateTime, intlLocale } from "../../lib/datetime";
import { ADD_COMPUTER_CTA, useFleetQuery } from "../../lib/detectors";
import {
  Btn,
  ClientFilter,
  FilterChips,
  ListEmpty,
  ListToolbar,
  Panel,
  QueryState,
  ResultCount,
  useUrlFilters,
} from "../../shell/components";
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
import type { AlertFilters, AlertOut, AlertState, AlertSummary, HealthSeverity } from "./types";

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
/** The three states an open alert wears, which the server's summary counts. */
const OPEN_STATES = ["active", "acknowledged", "muted"] as const;
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
function readState(raw: string): StateParam {
  if (raw === OPEN_STATE) return OPEN_STATE;
  return STATES.includes(raw as AlertState) ? (raw as AlertState) : "";
}

function readSeverity(raw: string): HealthSeverity | "" {
  return SEVERITIES.includes(raw as HealthSeverity) ? (raw as HealthSeverity) : "";
}

function readResolution(raw: string): Resolution {
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
  counts,
}: {
  onSelect: (alertId: string) => void;
  selectedId?: string;
  /**
   * The server's counts of open alerts for the same client as the list
   * (`GET /health/alerts/summary`, read by the page under the Client chip's
   * narrowing). The chips carry its numbers; without it they carry none,
   * never a count of the page in hand (The Absent Zero Rule).
   */
  counts?: AlertSummary;
}) {
  const t = useT();
  // THE KIT'S FILTERS (redesign plan, Receta L): the same keys this console
  // always read, plus `client`, which the Client chip writes itself. A chip
  // pushes a history entry, so Back undoes one.
  const filters = useUrlFilters(["state", "severity", "resolution", "device", "client"] as const);
  const stateParam = readState(filters.values.state);
  const severityParam = readSeverity(filters.values.severity);
  const resolution = readResolution(filters.values.resolution);
  // One device's alerts, as the device page links here. The route filters on
  // `device_id` itself.
  const deviceParam = filters.values.device;

  // What the SERVER can narrow. "open" is not one of its states, so it is
  // dropped here and applied to the rows below.
  const query: AlertFilters = {
    state: stateParam === OPEN_STATE ? "" : stateParam,
    severity: severityParam,
    ...(deviceParam ? { device_id: deviceParam } : {}),
  };

  const [flappingFirst, setFlappingFirst] = useState(false);
  // Narrowed to the Client chip's client (O3 3.1); writes keep `useApi()`.
  const alerts = useHealthAlerts(query, filters.values.client);
  const fleet = useFleetQuery();
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

  // THE STATE CHIPS ARE TODAY'S STATE SELECT, ONE CHIP PER OPTION. "Open"
  // is a REAL option and not a convenience: acknowledged and muted alerts are
  // open, so a person looking for what is still wrong cannot get there by
  // picking one state. It is also the destination of the report panel's
  // "Still open" figure, and "Cleared" that of its resolution figures. The
  // numbers are the server's (`counts`); `cleared` and "every state" have
  // none there, so their chips carry none.
  const stateOptions = [
    { value: "", label: t("health.console.anyState") },
    { value: OPEN_STATE, label: t("health.console.chipOpen"), count: counts?.open },
    ...OPEN_STATES.map((state) => ({
      value: state,
      label: ALERT_STATE_PRESENTATION[state].label,
      count: counts?.by_state[state],
    })),
    { value: "cleared", label: ALERT_STATE_PRESENTATION.cleared.label },
  ];
  // THE SAME LABEL THE ROWS CARRY, so one filter bar speaks one alphabet.
  // "Any state" and "Any severity", not two "All"s side by side: the two
  // groups share one row, and each first chip has to say which one it clears.
  const severityOptions = [
    { value: "", label: t("health.console.anySeverity") },
    ...SEVERITIES.map((severity) => ({
      value: severity,
      label: SEVERITY_PRESENTATION[severity].label,
      count: counts?.by_severity[severity],
    })),
  ];
  // THE CAUSE OF AN EMPTY LIST (redesign plan, O3 3.1; Atera's flaw: "try
  // adjusting the filters" on an account with no computers). With no filter
  // and no computer running the agent, nothing can have raised an alert yet:
  // say so, and lead to adding one. `fleet` is the readiness panel's own
  // read, so this costs no request.
  const noComputer = fleet.data !== undefined && fleet.data.managed === 0;

  return (
    <Panel title={t("health.console.title")} bare>
      <ListToolbar
        chips={
          <>
            <FilterChips
              label={t("health.console.stateLabel")}
              options={stateOptions}
              value={stateParam}
              onChange={(value) => filters.set("state", value)}
            />
            <FilterChips
              label={t("health.console.severityLabel")}
              options={severityOptions}
              value={severityParam}
              onChange={(value) => filters.set("severity", value)}
            />
            <span className={resolution === "" ? "chip chip-select" : "chip chip-select on"}>
              <span aria-hidden="true">{t("health.console.resolutionLabel")}</span>
              <select
                aria-label={t("health.console.filterResolution")}
                value={resolution}
                onChange={(e) => filters.set("resolution", e.target.value)}
              >
                <option value="">{t("health.console.anyResolution")}</option>
                {RESOLUTIONS.map((value) => (
                  <option key={value} value={value}>
                    {RESOLUTION_LABEL[value]}
                  </option>
                ))}
              </select>
            </span>
            <ClientFilter filters={filters} />
            {deviceParam ? (
              <span className="flex flex-wrap items-center gap-2">
                <span className="note">
                  {t("health.console.oneDevice")}{" "}
                  <Link to={`/devices/${deviceParam}`}>
                    {alerts.data?.[0]?.device_hostname || t("health.console.thisDevice")}
                  </Link>
                </span>
                <Btn label={t("health.console.showEvery")} small onClick={() => filters.set("device", "")} />
              </span>
            ) : null}
          </>
        }
        end={
          <>
            <Btn
              label={flappingFirst ? t("health.console.sortSeverity") : t("health.console.sortRepeating")}
              icon="sort-descending"
              small
              onClick={() => setFlappingFirst((v) => !v)}
            />
            {/* THE LIST'S COUNT, in the kit's place for it: how many rows are
                shown, and how many of them are open, since acknowledged and
                muted alerts are. No count before the list has answered. */}
            {alerts.data ? (
              <ResultCount>{t("health.console.subtitle", { open: openCount, total: rows.length })}</ResultCount>
            ) : null}
          </>
        }
      />

      {/* PENDING, not loading: under the Client chip the list waits for /me
          (`enabled: ready`), and a disabled query is not "loading". The
          errors are drawn above, so this only ever waits. */}
      <QueryState loading={alerts.isPending} error={null} empty={false} emptyLabel="">
      {rows.length === 0 ? (
        <ListEmpty
          filtered={filters.active}
          onClear={filters.clear}
          icon="health"
          nothingYet={noComputer ? t("health.console.noComputer") : t("health.console.emptyYet")}
          action={
            noComputer ? (
              <Link className="btn primary small" to={ADD_COMPUTER_CTA.to}>
                <Icon name="plus" size={14} />
                {ADD_COMPUTER_CTA.label}
              </Link>
            ) : undefined
          }
        />
      ) : (
        <Table label={t("health.console.title")}>
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
      </QueryState>
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
