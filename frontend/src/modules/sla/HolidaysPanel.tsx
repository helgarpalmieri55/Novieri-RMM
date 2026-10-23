import { useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { attemptProblems, describeSubmitError, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { formatDate } from "../../lib/datetime";
import { useAddSlaHolidayMutation, useRemoveSlaHolidayMutation, useSlaHolidays } from "./api";
import type { HolidayOut } from "./types";

// Auditor B, HIGH: "the same silent-no-op failure repeats on every form in the
// module ... 'Add holiday' with an empty Holiday date -> nothing happens".
// `disabled={add.isPending || !date || !name}` was that no-op: the press moved
// nothing and named no field. Both fields answer through `requiredProblems`
// now, and the date carries a shape rule as well as a presence one -- a
// browser without a native date picker hands this field free text.
//
// A function so the words are looked up when the refusal is rendered, not when
// the module loads.
const holidayRules = (): readonly FieldRule[] => [
  {
    name: "date",
    label: translate("sla.holidays.dateLabel"),
    validate: (value) =>
      /^\d{4}-\d{2}-\d{2}$/.test(value) ? null : translate("sla.holidays.dateShape"),
  },
  { name: "name", label: translate("sla.holidays.nameLabel") },
];

// Holidays for one calendar (spec §2.3). A holiday removes that date's
// windows ENTIRELY -- there are no half-days in v1, which is stated on the
// panel so nobody configures one by adding a date and expecting the morning
// to survive.
export function HolidaysPanel({
  calendarId,
  canAdmin,
}: {
  calendarId: string;
  canAdmin: boolean;
}) {
  const t = useT();
  const holidays = useSlaHolidays(calendarId);
  const add = useAddSlaHolidayMutation();
  const remove = useRemoveSlaHolidayMutation();
  const [date, setDate] = useState("");
  const [name, setName] = useState("");
  // On submit only -- nagging about a date nobody has typed yet is its own
  // kind of unhelpful. Held as data (the rules and the values they were
  // checked against) and worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  // Removing a holiday PUTS THAT DATE BACK TO WORK: every deadline computed
  // over it moves. That is not recoverable by pressing the button again --
  // re-adding the holiday does not un-move the deadlines already recomputed --
  // so it is confirmed, naming the date (cycle 8).
  const [pendingRemove, setPendingRemove] = useState<HolidayOut | null>(null);

  function submit() {
    const next: Attempt = { rules: holidayRules, values: { date, name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    add.mutate(
      { id: calendarId, body: { holiday_date: date, name: name.trim() } },
      {
        onSuccess: () => {
          setDate("");
          setName("");
        },
      },
    );
  }

  return (
    <div>
      <h3 className="cell-title">{t("sla.holidays.title")}</h3>
      <p className="note">
        {t("sla.holidays.note")}
      </p>
      <Table>
        <thead>
          <tr>
            <Th>{t("sla.holidays.colDate")}</Th>
            <Th>{t("sla.holidays.colName")}</Th>
            {canAdmin && <Th>{""}</Th>}
          </tr>
        </thead>
        <tbody>
          {holidays.data?.map((holiday) => (
            <tr key={holiday.id}>
              <Td>{formatHolidayDate(holiday.holiday_date)}</Td>
              <Td>{holiday.name}</Td>
              {canAdmin && (
                <Td>
                  <Button
                    variant="outline"
                    className="danger"
                    onClick={() => {
                      remove.reset();
                      setPendingRemove(holiday);
                    }}
                  >
                    {t("sla.common.remove")}
                  </Button>
                </Td>
              )}
            </tr>
          ))}
          {holidays.data && holidays.data.length === 0 && (
            <tr>
              <Td className="note">{t("sla.holidays.empty")}</Td>
              <Td>{""}</Td>
              {canAdmin && <Td>{""}</Td>}
            </tr>
          )}
        </tbody>
      </Table>

      {/* Remove is DESTRUCTIVE and this is the only place it can report a
          failure -- and until cycle 4 it could not: `describeError` was the
          error's `.message`, `ApiError(503, "")` carries "", and this
          paragraph rendered empty while the row sat there looking removed.
          `role="alert"` because nothing else on the panel changes when a
          remove fails.

          ADD'S FAILURE IS NO LONGER PRINTED HERE. It belongs to the form, and
          `FormActions` renders it beside the button that was pressed; leaving
          it here as well announced one refusal twice, in two places, one of
          them nowhere near the control. */}
      {remove.isError && (
        <p role="alert" className="notice red">
          {t("sla.common.removeFailed", { reason: describeSubmitError(remove.error) })}
        </p>
      )}

      <ConfirmDialog
        open={pendingRemove !== null}
        title={t("sla.holidays.removeTitle")}
        subject={
          pendingRemove
            ? `${formatHolidayDate(pendingRemove.holiday_date)} — ${pendingRemove.name}`
            : ""
        }
        body={t("sla.holidays.removeBody")}
        confirmLabel={t("sla.holidays.removeConfirm")}
        destructive
        onCancel={() => setPendingRemove(null)}
        onConfirm={() => {
          const holiday = pendingRemove;
          setPendingRemove(null);
          if (holiday) remove.mutate({ id: calendarId, holidayId: holiday.id });
        }}
      />

      {canAdmin && (
        <Form className="mt-3" onSubmit={submit}>
          <div className="flex flex-wrap items-end gap-3">
            <div className="w-44">
              <Field label={t("sla.holidays.dateLabel")}>
                <input
                  type="date"
                  className={inputClass}
                  value={date}
                  onChange={(e) => setDate(e.target.value)}
                />
                {/* The date this holds is spelled out under the row, below
                    (design review M1): a native date input draws the USER
                    AGENT'S mask and no CSS reorders it, so this reads
                    `09/22/2026` to a US-locale browser on a panel whose
                    holiday table renders `22 Sept 2026`. It is not inside this
                    `<Field>` because `Field` wraps its control in the
                    `<label>` and text placed here becomes part of the field's
                    NAME; the accounting is at
                    `agent-push/PushAuthorizationsPage`. "UTC" for the reason
                    the note at the foot of this file gives at length. */}
              </Field>
            </div>
            <div className="w-56">
              <Field label={t("sla.holidays.nameLabel")}>
                <input
                  className={inputClass}
                  placeholder={t("sla.holidays.namePlaceholder")}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </Field>
            </div>
            {/* Pressable with both fields empty ON PURPOSE: the press is what
                produces "Holiday date is required. Holiday name is required."
                A control that will not move explains nothing. */}
            <FormActions
              className="mb-3"
              label={t("sla.holidays.add")}
              pendingLabel={t("sla.holidays.adding")}
              pending={add.isPending}
              error={add.error}
              problems={attemptProblems(attempt)}
            />
          </div>
          {date !== "" ? (
            <p className="note" style={{ marginBottom: 12 }}>
              {formatDate(`${date}T00:00:00Z`, "UTC")}.
            </p>
          ) : null}
        </Form>
      )}
    </div>
  );
}

// A HOLIDAY DATE IS A CALENDAR DATE, NOT AN INSTANT, and that is why this
// pins the zone to UTC instead of reaching for the reader's (cycle 5).
//
// The wire value is "2026-12-25" — a wall date belonging to this calendar,
// with no time and no zone. `new Date("2026-12-25")` reads it as midnight
// UTC, so rendering it in any zone west of Greenwich moves Christmas Day to
// the 24th, and any zone far enough east moves it to the 26th. That is the
// "off by a whole day, which is harder to notice than five hours" hazard
// `lib/datetime` names in its own header, and converting here would be the
// bug rather than the fix. UTC in, UTC out, so the date is the date — with
// the product's one date format rather than a raw ISO string.
function formatHolidayDate(value: string): string {
  return formatDate(value, "UTC");
}
