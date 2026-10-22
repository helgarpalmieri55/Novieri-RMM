import { useEffect, useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { attemptProblems, describeSubmitError, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Badge, Button, Dialog, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { Icon } from "../../shell/icons";
import { Btn, Panel, StatusPill } from "../../shell/components";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { ApiError } from "../../lib/api";
import { formatDateTime } from "../../lib/datetime";
import {
  useCreateSlaCalendarMutation,
  useDeleteSlaCalendarMutation,
  useSlaCalendars,
  useUpdateSlaCalendarMutation,
  useUserTimezone,
} from "./api";
import { CalendarDetailPanel } from "./CalendarDetailPanel";
import {
  calendarHealthExplanation,
  calendarHealthLabel,
  calendarHealthVariant,
} from "./slaState";
import type { CalendarOut } from "./types";

// Business calendars (spec §2.1, §9.1). The timezone is shown on every row
// because a working window is meaningless without it, and the health flag is
// shown because a misconfigured calendar is otherwise INVISIBLE: the ticket
// hooks tolerate `UnsatisfiableCalendar` and degrade to "no SLA row" plus a
// server-side log line, which nobody in the org can see.
//
// `canAdmin` decides what renders, never what is permitted -- every mutation
// route is gated `sla.admin` server-side (invariant #6).

// The timezone the calendar is PREFILLED with. Named once so the field, the
// reset in `openCreate` and the sentence under the field cannot drift apart --
// the audit found the same string doing duty as a placeholder AND as the
// silently-applied value, which is exactly how those three drift.
const DEFAULT_TIMEZONE = "Europe/London";

// The browser's own tz database, so the field autocompletes rather than
// inviting a guess. Auditor: the timezone field is "free-text with no
// validation or autocomplete for IANA names" -- and a typo here does not fail
// loudly, it produces a calendar with `health: invalid_timezone` whose tickets
// quietly get no SLA at all.
//
// Computed once at module load: `supportedValuesOf` walks ICU, and this list
// does not change while the tab is open.
const TIMEZONE_OPTIONS: readonly string[] = ianaTimezones();

function ianaTimezones(): readonly string[] {
  const supported = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] })
    .supportedValuesOf;
  if (supported) {
    try {
      return supported("timeZone");
    } catch {
      // An engine carrying the method but not the `timeZone` key. Fall through
      // rather than taking the whole panel down over an autocomplete list.
    }
  }
  return [
    DEFAULT_TIMEZONE,
    "Europe/Dublin",
    "Europe/Paris",
    "Europe/Berlin",
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Australia/Sydney",
  ];
}

// The server checks the name against `zoneinfo` and 422s -- before this rule
// the user found that out only by pressing Save, which is the same silence the
// disabled button produced. `Intl.DateTimeFormat` throws `RangeError` for a
// name its tz database does not carry, so this is the same class of check
// rather than a regex approximation of one.
//
// The shape test runs first because it catches what people actually type:
// "London", "GMT", "BST", "UTC+1" are all rejected by `zoneinfo` too, but some
// of them are accepted by ICU as offsets and would otherwise pass here and
// fail at the API.
function invalidTimezone(value: string): string | null {
  if (value !== "UTC" && !/^[A-Za-z][A-Za-z0-9_+-]*(?:\/[A-Za-z0-9_+-]+)+$/.test(value)) {
    return translate("sla.calendars.timezoneShape", { example: DEFAULT_TIMEZONE, value });
  }
  try {
    Intl.DateTimeFormat("en-GB", { timeZone: value });
  } catch {
    return translate("sla.calendars.timezoneUnknown", { value });
  }
  return null;
}

// Auditor: "same silent-no-op failure repeats on every form in the module
// (Add calendar, ...)". `disabled={create.isPending || !name || !timezone}`
// refused the click and named no field. Both dialogs answer through this one
// list instead, and the labels are the words ON SCREEN so the sentence reads
// back to what the user is looking at.
// A function so the words are looked up when the refusal is rendered.
const calendarRules = (): readonly FieldRule[] => [
  { name: "name", label: translate("sla.common.name") },
  { name: "timezone", label: translate("sla.calendars.timezoneLabel"), validate: invalidTimezone },
];

export function CalendarsPanel({
  canAdmin,
  openCreateNonce = 0,
}: {
  canAdmin: boolean;
  /**
   * A counter the parent bumps to open this panel's create dialog (cycle 7).
   *
   * The policy form is dead without a calendar and says so; this is how its
   * "Add a business calendar" gets the user to the form that fixes it instead
   * of leaving them to find a card further down the page. A NONCE rather than
   * a boolean, so a second request after the dialog was dismissed reopens it
   * -- a boolean that is already `true` cannot ask twice.
   */
  openCreateNonce?: number;
}) {
  const t = useT();
  const calendars = useSlaCalendars();
  const create = useCreateSlaCalendarMutation();
  const update = useUpdateSlaCalendarMutation();
  const del = useDeleteSlaCalendarMutation();
  // `created_at` is an INSTANT and belongs in the reader's zone. The
  // calendar's OWN timezone -- which governs its working windows -- is a
  // different thing entirely and stays badged in its own column, unconverted.
  const zone = useUserTimezone();

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<CalendarOut | null>(null);
  const [name, setName] = useState("");
  const [timezone, setTimezone] = useState(DEFAULT_TIMEZONE);
  const [active, setActive] = useState(true);
  // Populated ON SUBMIT, never while typing: a "Name is required" that appears
  // before the first keystroke has landed is its own kind of unhelpful. Held as
  // the rules and the values they were checked against, worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  // The calendar a Delete click has named but not yet destroyed (cycle 8).
  // Auditor B filed the same HIGH against this table's Delete as against the
  // policy table's: "no confirm dialog, no toast, no Undo affordance".
  const [pendingDelete, setPendingDelete] = useState<CalendarOut | null>(null);

  const forbidden = calendars.error instanceof ApiError && calendars.error.status === 403;
  const failed = calendars.error != null && !forbidden;

  // `reset()` on open, because mutation state OUTLIVES the dialog: without it
  // a failed Save leaves `create.error` set, and the next time the dialog is
  // opened `FormActions` announces a refusal of a request nobody has made yet.
  function openCreate() {
    setName("");
    setTimezone(DEFAULT_TIMEZONE);
    setActive(true);
    setAttempt(null);
    create.reset();
    setCreating(true);
  }

  // Deliberately keyed on the nonce alone: `openCreate` is redefined every
  // render, and depending on it would reopen the dialog on any re-render.
  useEffect(() => {
    if (openCreateNonce > 0) openCreate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openCreateNonce]);

  function openEdit(calendar: CalendarOut) {
    setName(calendar.name);
    setTimezone(calendar.timezone);
    setActive(calendar.active);
    setAttempt(null);
    update.reset();
    setEditing(calendar);
  }

  function submitCreate() {
    const next: Attempt = { rules: calendarRules, values: { name, timezone } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    create.mutate(
      // Trimmed to match what was validated -- `requiredProblems` trims before
      // testing, so an untrimmed send could carry a name the checks never saw.
      { name: name.trim(), timezone: timezone.trim(), active },
      {
        onSuccess: (calendar) => {
          setCreating(false);
          // A brand-new calendar has no windows, so it is `no_working_windows`
          // by construction -- open it straight into the week editor rather
          // than leaving a calendar that cannot produce a deadline sitting in
          // the list.
          setSelectedId(calendar.id);
        },
      },
    );
  }

  function submitEdit() {
    if (!editing) return;
    const next: Attempt = { rules: calendarRules, values: { name, timezone } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    update.mutate(
      { id: editing.id, body: { name: name.trim(), timezone: timezone.trim(), active } },
      { onSuccess: () => setEditing(null) },
    );
  }

  return (
    <Panel
      title={t("sla.calendars.title")}
      subtitle={t("sla.calendars.subtitle")}
      bare
      actions={
        canAdmin ? <Btn label={t("sla.calendars.add")} icon="plus" onClick={openCreate} /> : undefined
      }
    >
      {calendars.isLoading ? (
        <p className="note" style={{ padding: "16px 18px" }}>{t("sla.common.loading")}</p>
      ) : forbidden ? (
        <p className="note" style={{ padding: "16px 18px" }}>{t("sla.calendars.forbidden")}</p>
      ) : failed ? (
        /* The generic half says WHICH surface is missing; `describeSubmitError`
           says why, which is the difference between "retry" and "you do not
           have permission" being the right next move. Announced, because the
           table it replaces is otherwise just absent. */
        <p role="alert" className="notice red" style={{ margin: 14 }}>
          {t("sla.calendars.failed", { reason: describeSubmitError(calendars.error) })}
        </p>
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>{t("sla.common.name")}</Th>
              <Th>{t("sla.calendars.colHealth")}</Th>
              <Th>{t("sla.common.active")}</Th>
              <Th>{t("sla.calendars.colCreated")}</Th>
              <Th>{""}</Th>
            </tr>
          </thead>
          <tbody>
            {calendars.data?.map((calendar) => (
              <tr key={calendar.id}>
                {/* The timezone under the name, in mono: a working window is
                    meaningless without it, and it is the one fact that tells
                    two calendars of the same name apart. */}
                <Td lead={<Icon name="calendar-event" size={18} />}>
                  <div className="cell-title">{calendar.name}</div>
                  <div className="cell-sub mono">{calendar.timezone}</div>
                </Td>
                <Td>
                  <Badge variant={calendarHealthVariant(calendar.health)}>
                    {calendarHealthLabel(calendar.health)}
                  </Badge>
                </Td>
                <Td>
                  {/* Humanised by the one table that humanises: the column
                      read "active"/"inactive" in the wire's own lower case,
                      beside a HEALTH column that read "ok". */}
                  <StatusPill value={calendar.active ? "active" : "inactive"} />
                </Td>
                <Td>
                  <span className="mono">{formatDateTime(calendar.created_at, zone)}</span>
                </Td>
                <Td>
                  {/* ONE WEIGHT FOR THE WHOLE CLUSTER: "Hours & holidays" and
                      "Edit" were borderless ghosts beside a bordered danger
                      "Delete", so the only action that looked like a control
                      was the one that destroys the row. All three are
                      `Btn small` now and Delete differs only in the ink and
                      edge its `danger` variant paints. */}
                  <div className="flex flex-wrap gap-2">
                    <Btn
                      small
                      label={selectedId === calendar.id ? t("sla.calendars.close") : t("sla.calendars.hoursAndHolidays")}
                      onClick={() =>
                        setSelectedId(selectedId === calendar.id ? null : calendar.id)
                      }
                    />
                    {canAdmin && (
                      <>
                        <Btn small label={t("sla.common.edit")} onClick={() => openEdit(calendar)} />
                        <Btn
                          small
                          variant="danger"
                          label={t("sla.common.delete")}
                          onClick={() => {
                            del.reset();
                            setPendingDelete(calendar);
                          }}
                        />
                      </>
                    )}
                  </div>
                </Td>
              </tr>
            ))}
            {calendars.data && calendars.data.length === 0 && (
              <tr>
                <Td colSpan={5}>
                  <span className="note">
                    {t("sla.calendars.empty")}
                  </span>
                </Td>
              </tr>
            )}
          </tbody>
        </Table>
      )}

      {/* Health is a per-calendar diagnosis of a fault that otherwise only
          shows up as tickets quietly having no SLA -- so the cause is spelled
          out, not left as a coloured word. */}
      {calendars.data
        ?.filter((calendar) => calendar.health !== "ok")
        .map((calendar) => (
          <p key={calendar.id} className="notice gold" style={{ margin: 14 }}>
            {`${calendar.name}: ${calendarHealthExplanation(calendar.health) ?? ""}`}
          </p>
        ))}

      {/* DELETE IS DESTRUCTIVE AND HAS NOWHERE ELSE TO SPEAK -- it is a row
          action, not a form, so this paragraph is the only thing between a
          failed delete and an operator who believes the calendar is gone.
          `describeError` returned "" for a bodyless 503, this paragraph
          rendered empty, and the UI looked exactly like a success. That is
          auditor A's BLOCKER, at this call site. */}
      {del.isError && (
        <p role="alert" className="notice red" style={{ margin: 14 }}>
          {t("sla.common.deleteFailed", { reason: describeSubmitError(del.error) })}
        </p>
      )}

      {/* The consequence, not the record: a calendar is referenced BY policies,
          and deleting one takes the deadline computation out from under every
          policy pointing at it. */}
      <ConfirmDialog
        open={pendingDelete !== null}
        title={t("sla.calendars.deleteTitle")}
        subject={pendingDelete ? `${pendingDelete.name} (${pendingDelete.timezone})` : ""}
        body={t("sla.calendars.deleteBody")}
        confirmLabel={t("sla.calendars.deleteConfirm")}
        destructive
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          const calendar = pendingDelete;
          setPendingDelete(null);
          if (calendar) del.mutate(calendar.id);
        }}
      />

      {selectedId && (
        <div style={{ padding: "16px 18px", borderTop: "1px solid var(--line)" }}>
          <CalendarDetailPanel calendarId={selectedId} canAdmin={canAdmin} />
        </div>
      )}

      {/* A real <form>, so Enter in the name field saves instead of doing
          nothing -- the other half of the same finding. */}
      <Dialog open={creating} onClose={() => setCreating(false)} title={t("sla.calendars.addTitle")}>
        <Form onSubmit={submitCreate}>
          <CalendarFields
            name={name}
            timezone={timezone}
            active={active}
            onName={setName}
            onTimezone={setTimezone}
            onActive={setActive}
          />
          <div className="flex items-center justify-end gap-2">
            {/* `type="button"` is load-bearing now that there is a form around
                it: a button with no type inside a <form> IS a submit button,
                so Cancel would save. */}
            <Button type="button" variant="ghost" onClick={() => setCreating(false)}>
              {t("common.actions.cancel")}
            </Button>
            {/* Enabled with the fields empty ON PURPOSE. The refusal is what
                the user gets to read; a control that will not move is not. The
                error also comes from here rather than a separate red line, so
                a bodyless 503 still produces a sentence and `role="alert"`
                announces it. */}
            <FormActions
              label={t("common.actions.save")}
              pending={create.isPending}
              error={create.error}
              problems={attemptProblems(attempt)}
            />
          </div>
        </Form>
      </Dialog>

      <Dialog open={editing !== null} onClose={() => setEditing(null)} title={t("sla.calendars.editTitle")}>
        <Form onSubmit={submitEdit}>
          <CalendarFields
            name={name}
            timezone={timezone}
            active={active}
            onName={setName}
            onTimezone={setTimezone}
            onActive={setActive}
          />
          <p className="form-hint" style={{ marginBottom: 8 }}>
            {t("sla.calendars.editHint")}
          </p>
          <div className="flex items-center justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setEditing(null)}>
              {t("common.actions.cancel")}
            </Button>
            <FormActions
              label={t("common.actions.save")}
              pending={update.isPending}
              error={update.error}
              problems={attemptProblems(attempt)}
            />
          </div>
        </Form>
      </Dialog>
    </Panel>
  );
}

function CalendarFields({
  name,
  timezone,
  active,
  onName,
  onTimezone,
  onActive,
}: {
  name: string;
  timezone: string;
  active: boolean;
  onName: (value: string) => void;
  onTimezone: (value: string) => void;
  onActive: (value: boolean) => void;
}) {
  const t = useT();
  return (
    <>
      <Field label={t("sla.common.name")}>
        <input className={inputClass} value={name} onChange={(e) => onName(e.target.value)} />
      </Field>
      {/* An IANA name, validated against `zoneinfo` server-side at write time
          -- rejected at the API, never discovered at sweep time. `invalidTimezone`
          asks the same question here so the answer arrives before the round trip.

          NO PLACEHOLDER. It read "Europe/London" while the field's VALUE was
          also "Europe/London", so the prefilled default was indistinguishable
          from grey hint text -- the audit's "placeholder doubles as the
          silently-applied default value". A default that will be saved is
          stated in words underneath instead. */}
      <Field label={t("sla.calendars.timezoneLabel")}>
        <input
          className={inputClass}
          list="sla-timezone-options"
          value={timezone}
          onChange={(e) => onTimezone(e.target.value)}
        />
      </Field>
      <datalist id="sla-timezone-options">
        {TIMEZONE_OPTIONS.map((zone) => (
          <option key={zone} value={zone} />
        ))}
      </datalist>
      <p className="form-hint" style={{ marginBottom: 12 }}>
        {t("sla.calendars.timezoneHint", { example: DEFAULT_TIMEZONE })}
      </p>
      <label className="mb-3 flex items-center gap-2">
        <input type="checkbox" checked={active} onChange={(e) => onActive(e.target.checked)} />
        {t("sla.common.active")}
      </label>
    </>
  );
}
