import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { formatDate, formatDateTime, toInstant } from "../../lib/datetime";
import { useT } from "../../i18n/context";
import { useChangeCalendar, useUserTimezone } from "./api";
import { changeStateKind, changeStateLabel, changeTypeLabel, riskKind, riskLabel } from "./changeState";
import { PageHead, Panel, QueryState, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KIND_ICON } from "../../shell/viz";

// Today's date in `zone`, as the "YYYY-MM-DD" an `<input type="date">` holds.
// NOT A DISPLAY LOCALE, so it deliberately does not follow `intlLocale()`:
// these are machine keys, and `en-CA` is the locale whose numeric date IS that format; the alternative --
// `new Date().toISOString().slice(0, 10)`, which is what this file used to do
// twice -- is the date in UTC, so for anyone west of Greenwich the calendar
// opened on NEXT month for the last few hours of every month.
function dayInZone(at: Date, zone: string | null): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: zone || undefined,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(at);
}

function defaultFrom(zone: string | null): string {
  return `${dayInZone(new Date(), zone).slice(0, 8)}01`;
}

function defaultTo(zone: string | null): string {
  const [year, month] = dayInZone(new Date(), zone).split("-").map(Number);
  // Day 0 of the NEXT month is the last day of this one. Computed in UTC so
  // the arithmetic cannot drift back across a boundary the way the old
  // `d.setMonth(d.getMonth() + 1, 0)` on a local Date could.
  const lastDay = new Date(Date.UTC(year as number, month as number, 0)).getUTCDate();
  return `${year}-${String(month).padStart(2, "0")}-${String(lastDay).padStart(2, "0")}`;
}

export function ChangeCalendar() {
  const t = useT();
  const zone = useUserTimezone();
  // Seeded once, before `/me` has answered on a cold load, so these defaults
  // are the browser's month. Retyping is the correction, and the boundaries
  // sent from them are qualified below either way.
  const [from, setFrom] = useState(() => defaultFrom(zone));
  const [to, setTo] = useState(() => defaultTo(zone));

  // A `type="date"` value is a wall-clock DAY with no zone, and
  // `new Date("2026-02-01").toISOString()` read it as UTC midnight -- so an
  // operator in New York asking for February got a window that began at 19:00
  // on 31 January and ended five hours short. `T00:00` makes the wall clock
  // explicit for `toInstant`, which then attaches the reader's zone.
  const calendar = useChangeCalendar(
    from ? toInstant(`${from}T00:00`, zone) || undefined : undefined,
    to ? toInstant(`${to}T00:00`, zone) || undefined : undefined,
  );

  const conflicting = new Set<string>();
  for (const conflict of calendar.data?.conflicts ?? []) {
    conflicting.add(conflict.change_a);
    conflicting.add(conflict.change_b);
  }
  const conflicts = calendar.data?.conflicts.length ?? 0;

  return (
    <div>
      <PageHead
        title={t("change.calendar.title")}
        description={t("change.calendar.description")}
        actions={
          <Link className="btn" to="/changes">
            <Icon name="git-branch" size={14} />
            {t("change.common.allChanges")}
          </Link>
        }
      />

      {/* THE WINDOW IS A PANEL, NOT TWO CONTROLS LOOSE ON THE PAGE.

          These two fields sat directly on the page background between the
          header and the card, which is a surface nothing else in the console
          uses: /bcp/report puts the same two ends of the same kind of range
          inside a "Period" panel, and /budget/expenses, /security and /ipam
          all keep their filters inside the card they filter (design review
          M10). A control on the bare `--bg` reads as chrome rather than as
          part of the page's content, and there was no edge saying which card
          it governed.

          THE SHAPE IS `/bcp/report`'s, DELIBERATELY -- same `form-grid`, same
          `18rem` cap, same panel title -- because it is the same job. A date
          is a fixed measure, so the inputs keep it rather than stretching to
          half a 1140px card. */}
      <Panel title={t("change.calendar.period")}>
        <div className="form-grid">
          <Field label={t("change.calendar.from")}>
            <input
              type="date"
              className={inputClass}
              style={{ maxWidth: "18rem" }}
              value={from}
              onChange={(e) => setFrom(e.target.value)}
            />
          </Field>
          <Field label={t("change.calendar.to")}>
            <input
              type="date"
              className={inputClass}
              style={{ maxWidth: "18rem" }}
              value={to}
              onChange={(e) => setTo(e.target.value)}
            />
          </Field>
        </div>
        {/* BOTH ENDS, READ BACK IN THE CONSOLE'S OWN FORMAT. The mask a native
            date input draws is the USER AGENT'S locale and no CSS reorders it,
            so these controls read `09/01/2026` to a US-locale browser on a
            page whose every other date reads `01 Sept 2026` and whose
            /settings says day/month/year. The control stays -- it is the one
            date widget every keyboard, screen reader and locale already agrees
            on -- and the window it describes is spelled out under it (design
            review M1, the idiom `agent-push/PushAuthorizationsPage` already
            carried, and the note there records why this sits under the group
            rather than inside a `<Field>`). */}
        {from !== "" || to !== "" ? (
          <p className="note" style={{ marginTop: 14 }}>
            {t(from !== "" ? (to !== "" ? "change.calendar.rangeBoth" : "change.calendar.rangeFrom") : "change.calendar.rangeTo", {
              from: from !== "" ? formatDate(toInstant(`${from}T00:00`, zone), zone) : "",
              to: to !== "" ? formatDate(toInstant(`${to}T00:00`, zone), zone) : "",
            })}
          </p>
        ) : null}
      </Panel>

      {/* The count is a notice, with the critical glyph: a red sentence on
          its own is colour as the only carrier. */}
      {conflicts > 0 && (
        <div role="status" className="notice red mb-3">
          <Icon name={KIND_ICON.crit} size={17} />
          <div>
            {t("change.calendar.conflicts", { count: conflicts })}
          </div>
        </div>
      )}

      <Panel bare>
        <QueryState
          loading={calendar.isLoading}
          error={calendar.error}
          empty={(calendar.data?.entries ?? []).length === 0}
          emptyLabel={t("change.calendar.empty")}
          emptyIcon="calendar-event"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("change.common.name")}</Th>
                <Th>{t("change.common.risk")}</Th>
                <Th>{t("change.common.state")}</Th>
                <Th>{t("change.calendar.window")}</Th>
              </tr>
            </thead>
            <tbody>
              {calendar.data?.entries.map((entry) => {
                const hasConflict = conflicting.has(entry.change_id);
                return (
                  <tr key={entry.change_id}>
                    {/* A conflicting row takes the critical gutter and the
                        pill; a row that merely carries high risk keeps the
                        risk's own gutter. */}
                    <Td
                      lead={<Icon name="calendar-event" size={18} />}
                      gutter={hasConflict ? "crit" : riskKind(entry.risk)}
                    >
                      <div className="cell-title">
                        <Link to={`/changes/${entry.change_id}`}>{entry.title}</Link>
                        {hasConflict && (
                          <Badge variant="destructive" className="ml-2">
                            {t("change.calendar.conflict")}
                          </Badge>
                        )}
                      </div>
                      <div className="cell-sub">{changeTypeLabel(entry.type)}</div>
                    </Td>
                    <Td>
                      <StatusPill value={entry.risk} label={riskLabel(entry.risk)} kind={riskKind(entry.risk)} />
                    </Td>
                    <Td>
                      <StatusPill value={entry.state} label={changeStateLabel(entry.state)} kind={changeStateKind(entry.state)} />
                    </Td>
                    <Td>
                      <span className="mono" style={{ whiteSpace: "nowrap" }}>
                        {formatDateTime(entry.window_start, zone)}
                        {" – "}
                        {formatDateTime(entry.window_end, zone)}
                      </span>
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </QueryState>
      </Panel>
    </div>
  );
}
