import { useEffect, useState } from "react";
import { describeSubmitError } from "../../components/form";
import { Badge, Button, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { useReplaceSlaWindowsMutation } from "./api";
import {
  formatMinutes,
  formatTimeOfDay,
  formatWindow,
  isMidnightEnd,
  totalWindowMinutes,
  validateWindows,
  weekdayInlineLabel,
  weekdayLabel,
  WEEKDAYS,
  windowDurationMinutes,
} from "./slaState";
import type { WindowIn, WindowOut } from "./types";
import { intlLocale } from "../../lib/datetime";

// The weekly working-window grid (spec §9.1). Windows are replaced as a SET
// (`PUT .../windows`), never individually -- a weekday's windows only make
// sense together and a partial edit can leave the calendar overlapping or
// inverted -- so this editor holds the whole week locally and submits it in
// one call.
//
// THE MIDNIGHT SENTINEL IS THE FIDDLY PART (spec §2.2, migration 0035).
// `end_time = 00:00` means midnight at the END of the day, worth 1440
// minutes: it is the only legal encoding for an evening shift's second half
// (`22:00–00:00`) and for a genuine 24-hour day (`00:00–00:00`, which
// `00:00–23:59` would silently shorten by a minute a day). Rendering it as
// "00:00" would make both look like empty or inverted windows, so every read
// of an end boundary goes through `slaState.formatWindowEnd`, which prints it
// as 24:00, and every duration through `windowDurationMinutes`, which counts
// it as 1440.
//
// Validation here mirrors `calendars._validate_windows` for immediate
// feedback only. The server validates the whole set again before touching a
// row, and it is the authority; this never suppresses a submission the server
// would have accepted, it only explains one it would reject.

// An "HH:MM" input value; Pydantic accepts both this and "HH:MM:SS", but the
// column is a `time` and the API echoes "HH:MM:SS", so outgoing values are
// normalised to match what comes back.
function toWireTime(value: string): string {
  return value.length === 5 ? `${value}:00` : value;
}

function toInputTime(value: string): string {
  return formatTimeOfDay(value);
}

function fromRows(windows: ReadonlyArray<WindowOut>): WindowIn[] {
  return windows.map((window) => ({
    weekday: window.weekday,
    start_time: toInputTime(window.start_time),
    end_time: toInputTime(window.end_time),
  }));
}

export function WorkingWindowsEditor({
  calendarId,
  timezone,
  windows,
  canAdmin,
}: {
  calendarId: string;
  timezone: string;
  windows: ReadonlyArray<WindowOut>;
  canAdmin: boolean;
}) {
  const t = useT();
  const replace = useReplaceSlaWindowsMutation();
  const [draft, setDraft] = useState<WindowIn[]>(() => fromRows(windows));
  const [dirty, setDirty] = useState(false);

  // Adopt the server's week whenever it changes underneath an untouched
  // editor (another admin's save, or a refetch after this one's). A dirty
  // draft is left alone -- silently discarding half-typed hours would be
  // worse than showing a stale grid the Revert button can clear.
  useEffect(() => {
    if (!dirty) setDraft(fromRows(windows));
  }, [windows, dirty]);

  function mutate(next: WindowIn[]) {
    setDraft(next);
    setDirty(true);
  }

  function addWindow(weekday: number, start: string, end: string) {
    mutate([...draft, { weekday, start_time: start, end_time: end }]);
  }

  function editWindow(index: number, patch: Partial<WindowIn>) {
    mutate(draft.map((window, i) => (i === index ? { ...window, ...patch } : window)));
  }

  function removeWindow(index: number) {
    mutate(draft.filter((_, i) => i !== index));
  }

  const problems = validateWindows(draft);
  const weeklyMinutes = totalWindowMinutes(draft);

  function save() {
    replace.mutate(
      {
        id: calendarId,
        windows: draft.map((window) => ({
          weekday: window.weekday,
          start_time: toWireTime(window.start_time),
          end_time: toWireTime(window.end_time),
        })),
      },
      { onSuccess: () => setDirty(false) },
    );
  }

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h3 className="cell-title">{t("sla.windows.title")}</h3>
        {/* The timezone is on every view of a working window (spec §9.1): a
            reader who assumes their own timezone misreads every row. */}
        <Badge variant="info">{timezone}</Badge>
        <span className="note">
          {t("sla.windows.perWeek", { duration: formatMinutes(weeklyMinutes) })}
        </span>
      </div>

      {draft.length === 0 && (
        <p className="notice gold">
          {t("sla.windows.none")}
        </p>
      )}

      <div className="space-y-2">
        {WEEKDAYS.map((weekday) => {
          const label = weekdayLabel(weekday);
          const indexed = draft
            .map((window, index) => ({ window, index }))
            .filter((entry) => entry.window.weekday === weekday)
            .sort((a, b) => a.window.start_time.localeCompare(b.window.start_time, intlLocale()));
          return (
            <div
              key={weekday}
              className="flex flex-wrap items-start gap-3 pb-2" style={{ borderBottom: "1px solid var(--line)" }}
            >
              <span className="w-24 shrink-0 cell-title">{label}</span>
              <div className="flex-1 space-y-1">
                {indexed.length === 0 && (
                  <span className="note">{t("sla.windows.nonWorkingDay")}</span>
                )}
                {indexed.map(({ window, index }, position) => (
                  <div key={index} className="flex flex-wrap items-center gap-2">
                    {/* `w-36`, NOT `w-28`. Auditor B: the meridiem character
                        was clipped by the input's right edge -- "reads 12:00
                        AN" -- with the clock affordance overlapping the text.
                        AM vs PM is the one distinction a business calendar
                        cannot afford to get wrong: it moves every deadline on
                        it by twelve hours. 112px is not enough for
                        "12:00 AM" plus the picker glyph in a 12-hour locale;
                        144px is. */}
                    <input
                      type="time"
                      className={`${inputClass} w-36`}
                      aria-label={t("sla.windows.startAria", { day: label, position: position + 1 })}
                      value={window.start_time}
                      disabled={!canAdmin}
                      onChange={(e) => editWindow(index, { start_time: e.target.value })}
                    />
                    <span className="cell-sub">–</span>
                    <input
                      type="time"
                      className={`${inputClass} w-36`}
                      aria-label={t("sla.windows.endAria", { day: label, position: position + 1 })}
                      value={window.end_time}
                      disabled={!canAdmin}
                      onChange={(e) => editWindow(index, { end_time: e.target.value })}
                    />
                    <span className="note">
                      {`${formatWindow(window)} · ${formatMinutes(windowDurationMinutes(window) ?? 0)}`}
                    </span>
                    {isMidnightEnd(window.end_time) && (
                      <Badge variant="info">{t("sla.windows.endsAtMidnight")}</Badge>
                    )}
                    {canAdmin && (
                      <Button variant="ghost" onClick={() => removeWindow(index)}>
                        {t("sla.common.remove")}
                      </Button>
                    )}
                  </div>
                ))}
              </div>
              {canAdmin && (
                /* A FIXED COLUMN, AND THE DAY NAME OUT OF THE VISIBLE LABEL.
                   Auditor B: each row's buttons embedded the weekday, so the
                   pair's width varied per row and, right-aligned, their left
                   edge stepped in and out by ~40px down the list -- no single
                   scan column. The day is already the row header, so on screen
                   it is redundant; it stays in the ACCESSIBLE NAME, where a
                   screen-reader user has no row header in view and "Add
                   window" seven times would name nothing. */
                <div className="flex w-64 shrink-0 justify-end gap-2">
                  <Button
                    variant="ghost"
                    aria-label={t("sla.windows.addWindowAria", { day: weekdayInlineLabel(weekday) })}
                    onClick={() => addWindow(weekday, "09:00", "17:00")}
                  >
                    {t("sla.windows.addWindow")}
                  </Button>
                  {/* Makes the sentinel discoverable: a true 24-hour day is
                      00:00–00:00 (1440 minutes), not 00:00–23:59. */}
                  <Button
                    variant="ghost"
                    aria-label={t("sla.windows.day24Aria", { day: weekdayInlineLabel(weekday) })}
                    onClick={() => addWindow(weekday, "00:00", "00:00")}
                  >
                    {t("sla.windows.day24")}
                  </Button>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {problems.length > 0 && (
        <ul className="notice red" style={{ marginTop: 8, listStyle: "disc", paddingLeft: 28, display: "block" }}>
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}
      {/* Saving the week replaces the WHOLE set and recomputes every running
          deadline on this calendar, so a refusal that renders as an empty
          paragraph (which is what `describeError` did with `ApiError(503, "")`)
          leaves an admin believing the week they are looking at is the week
          the server holds. The draft is still dirty and the Revert button is
          still live, and this sentence is what says so. */}
      {replace.isError && (
        <p role="alert" className="notice red">
          {t("sla.windows.notSaved", { reason: describeSubmitError(replace.error) })}
        </p>
      )}

      {canAdmin && (
        <div className="mt-3 flex items-center gap-2">
          <Button onClick={save} disabled={replace.isPending || problems.length > 0 || !dirty}>
            {replace.isPending ? t("sla.windows.saving") : t("sla.windows.saveWeek")}
          </Button>
          <Button
            variant="outline"
            disabled={!dirty}
            onClick={() => {
              setDraft(fromRows(windows));
              setDirty(false);
            }}
          >
            {t("sla.windows.revert")}
          </Button>
          {dirty && <span className="note">{t("sla.windows.unsaved")}</span>}
          <span className="note">
            {t("sla.windows.replaceNote")}
          </span>
        </div>
      )}
    </div>
  );
}
