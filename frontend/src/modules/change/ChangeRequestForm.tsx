import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { attemptProblems, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Dialog, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime, toInstant } from "../../lib/datetime";
import { internalClientId, requireKnown } from "../../lib/internalClient";
import { useCreateChangeMutation, useUpdateChangeMutation, useUserTimezone } from "./api";
import { riskLabel } from "./changeState";
import type { ChangeRequest } from "./types";

// Creates or edits a draft change request (type, title, description, risk,
// maintenance window, rollback plan). Mirrors modules/provisioning's
// ProfileEditor shape: `change` is either "new", an existing ChangeRequest to
// edit, or null (closed).

// Auditor B (HIGH), /changes → New change → Save: "Clicking Save on the
// completely empty form is a silent no-op — the form stays open, `[role=alert]`
// count is 0, 'Open changes' stays at 0." The cause was
// `disabled={mutation.isPending || !title || !description}` — the two fields the
// server insists on were exactly the two the dead button refused to name. Save
// stays enabled now and answers through this list; the labels are the words ON
// SCREEN so "Title is required" reads back to the field being looked at.
// Built by a function, not held as a constant: the labels are worded when the
// rules are read, so a refusal shown on screen follows a language switch.
function baseRules(): readonly FieldRule[] {
  return [
    { name: "title", label: translate("change.form.title") },
    { name: "description", label: translate("change.form.description") },
  ];
}

// Auditor B (HIGH), /changes → New change: "I deliberately set Window start =
// 2026-08-25T09:00 and Window end = 2026-08-20T09:00 -- an end five days
// BEFORE the start. The record saved with no complaint... A negative-duration
// maintenance window is meaningless; anything downstream computing 'is this
// change in-window right now?' or 'how long is the outage?' will produce a
// negative or empty result."
//
// NOTHING ELSE STOPS IT. `change_mgmt/schema.py` types both as a bare
// `datetime | None` with no cross-field check, and the calendar's conflict
// detection reads the pair as an interval -- an inverted one silently matches
// nothing, so a change booked backwards never appears to collide with
// anything.
//
// COMPARED AS INSTANTS, not as the strings the fields hold. Both are wall
// clocks with no zone, and `"2026-11-01T01:30" < "2026-11-01T01:30"` is a
// string comparison that says nothing about which moment came first across a
// DST boundary -- `toInstant` is what makes the question answerable, and it is
// the same conversion `submit` sends.
function windowOrderRule(windowStart: string, zone: string | null): FieldRule {
  return {
    name: "window_end",
    label: translate("change.form.windowEnd"),
    validate: (value) => {
      const start = toInstant(windowStart, zone);
      if (!start) return null;
      const end = toInstant(value, zone);
      // An unparseable wall clock is not this rule's business; the field is a
      // `datetime-local` and cannot produce one.
      if (!end) return null;
      if (new Date(end).getTime() <= new Date(start).getTime()) {
        return translate("change.form.windowOrder", { start: formatDateTime(start, zone) });
      }
      return null;
    },
  };
}

// One mechanism for "missing" and for "malformed" (form.tsx's
// `requiredProblems` runs both). The window fields are genuinely optional
// server-side (`window_start: datetime | None`), so the ordering rule applies
// only once BOTH have been filled in -- expressed as a rule that appears, never
// as a second check bolted on beside the first.
function changeRules(
  windowStart: string,
  windowEnd: string,
  zone: string | null,
): readonly FieldRule[] {
  return [
    ...baseRules(),
    ...(windowStart.trim() && windowEnd.trim() ? [windowOrderRule(windowStart, zone)] : []),
  ];
}

export function ChangeRequestForm({
  change,
  onClose,
}: {
  change: ChangeRequest | "new" | null;
  onClose: () => void;
}) {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const create = useCreateChangeMutation();
  const update = useUpdateChangeMutation();
  const zone = useUserTimezone();

  const existing = change === "new" || change == null ? null : change;

  const [type, setType] = useState(existing?.type ?? "standard");
  const [title, setTitle] = useState(existing?.title ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [risk, setRisk] = useState(existing?.risk ?? "low");
  const [windowStart, setWindowStart] = useState(existing?.window_start ?? "");
  const [windowEnd, setWindowEnd] = useState(existing?.window_end ?? "");
  const [rollbackPlan, setRollbackPlan] = useState(existing?.rollback_plan ?? "");
  // Filled ON SUBMIT, never while typing: a "Title is required" that appears
  // before the first keystroke has landed is its own kind of unhelpful. Held as
  // the rules and the values they were checked against, worded at render, so a
  // language switch re-words the refusal.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const mutation = existing ? update : create;

  // The zone every instant on this form is read and written in. `null` on the
  // profile means the browser's, which is what `lib/datetime` falls back to --
  // so the sentence has to name the same fallback rather than saying "your
  // profile timezone" to somebody who has never set one.
  const windowZone = zone ?? Intl.DateTimeFormat().resolvedOptions().timeZone;

  function reset() {
    setType("standard");
    setTitle("");
    setDescription("");
    setRisk("low");
    setWindowStart("");
    setWindowEnd("");
    setRollbackPlan("");
    setAttempt(null);
  }

  // This component stays mounted with the dialog shut (ChangesPage renders it
  // unconditionally), so mutation state OUTLIVES the dialog: without the resets
  // a failed Save leaves `mutation.error` set and the next open announces the
  // refusal of a request nobody has made yet. Same for a stale problem list.
  function close() {
    setAttempt(null);
    create.reset();
    update.reset();
    onClose();
  }

  function submit() {
    // The rules close over the values of THIS submission (constants of this
    // render), never over live state.
    const next: Attempt = {
      rules: () => changeRules(windowStart, windowEnd, zone),
      values: { title, description, window_end: windowEnd },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    const body = {
      type,
      // Trimmed to match what was validated — `requiredProblems` trims before
      // testing emptiness, so an untrimmed send could carry a title the checks
      // never saw.
      title: title.trim(),
      description: description.trim(),
      risk,
      // THE MAINTENANCE WINDOW IS THE POINT OF THE FIELD. Both inputs are
      // `datetime-local`, which hands back a WALL CLOCK with no zone
      // ("2026-08-20T22:00"); sent unqualified to an API that reads it as UTC,
      // a window opened at 22:00 for an operator in New York was stored as
      // 22:00Z -- 18:00 their time, in the middle of the working day the
      // window exists to avoid -- and the calendar then displayed that wrong
      // time back with confidence. `toInstant` attaches the zone, and returns
      // "" for an untouched field so these stay genuinely optional
      // (`window_start: datetime | None` in change_mgmt/schema.py) rather than
      // becoming the epoch. A value that already carries a zone -- what an
      // existing change's `window_start` is -- passes through unchanged.
      window_start: toInstant(windowStart, zone) || null,
      window_end: toInstant(windowEnd, zone) || null,
      rollback_plan: rollbackPlan,
    };

    const onSuccess = () => {
      reset();
      onClose();
    };

    if (existing) {
      update.mutate({ id: existing.id, body }, { onSuccess });
    } else {
      // A change is one client's (MSP clients phase 2), and that client must
      // use change management: the server's 404 says so in a sentence, which
      // `FormActions` shows as it is.
      // PHASE 3: the client/site selector. Until it exists that is the
      // organisation's internal client, whose id is the organisation's --
      // `/me`'s `org_id` (`lib/internalClient.ts`).
      const internalClient = internalClientId(me.data);
      create.mutate(() => ({ ...body, client_id: requireKnown(internalClient) }), { onSuccess });
    }
  }

  return (
    <Dialog open={change != null} onClose={close} title={change === "new" ? t("change.form.newTitle") : t("change.form.editTitle")}>
      {/* A real <form>, so Enter in the Title field saves instead of doing
          nothing — the other half of the same finding. */}
      <Form onSubmit={submit}>
        {/* Auditor B (MEDIUM): "All seven controls report `required === false`
            and none has a placeholder or hint." Two of the seven actually are
            required, and only those two are marked: Type and Risk are selects
            that always hold a value, and the window fields are genuinely
            optional server-side (`window_start: datetime | None = None`, see
            change_mgmt/schema.py) — marking a field the API accepts empty is
            the same lie pointing the other way. */}
        <p className="note mb-3">{t("change.form.requiredNote")}</p>
        <div className="form-grid">
          <Field label={t("change.form.type")} full>
            <select className={inputClass} value={type} onChange={(e) => setType(e.target.value)}>
              <option value="standard">{t("change.form.typeStandard")}</option>
              <option value="normal">{t("change.form.typeNormal")}</option>
              <option value="emergency">{t("change.form.typeEmergency")}</option>
            </select>
          </Field>
          {/* `required` is the same fact stated for assistive tech, which is what
              the auditor actually measured (`input.required === false` on all
              seven). It blocks nothing: <Form> is `noValidate`, because a native
              bubble that vanishes on the next click is the silent no-op wearing a
              hat. `CHANGE_RULES` is what the user reads. */}
          <Field label={t("change.form.titleRequired")} full>
            <input
              className={inputClass}
              required
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </Field>
          <Field label={t("change.form.descriptionRequired")} full>
            <textarea
              className={inputClass}
              required
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
            />
          </Field>
          <Field label={t("change.form.risk")} full>
            <select className={inputClass} value={risk} onChange={(e) => setRisk(e.target.value)}>
              <option value="low">{riskLabel("low")}</option>
              <option value="medium">{riskLabel("medium")}</option>
              <option value="high">{riskLabel("high")}</option>
            </select>
          </Field>
          <Field label={t("change.form.windowStart")}>
            <input
              type="datetime-local"
              className={inputClass}
              value={windowStart}
              onChange={(e) => setWindowStart(e.target.value)}
            />
          </Field>
          <Field label={t("change.form.windowEnd")}>
            {/* `min` is the same bound stated to the picker widget, so the
                backwards window is awkward to enter as well as refused; the
                sentence a typed or pasted one gets is `windowOrderRule`'s. */}
            <input
              type="datetime-local"
              className={inputClass}
              min={windowStart || undefined}
              value={windowEnd}
              onChange={(e) => setWindowEnd(e.target.value)}
            />
          </Field>
          {/* Auditor B (HIGH): "Nothing in the UI states which timezone either
              the input or the output is in." Cycle 5 made the round trip
              correct; this says which clock it is correct in, on the one field
              whose entire output is coordinating people across teams and
              vendors. */}
          <p className="note">{t("change.form.windowZone", { zone: windowZone })}</p>
          <Field label={t("change.form.rollbackPlan")} full>
            <textarea
              className={inputClass}
              value={rollbackPlan}
              onChange={(e) => setRollbackPlan(e.target.value)}
              rows={2}
            />
          </Field>
        </div>

        <div className="flex items-center justify-end gap-2">
          {/* `type="button"` is load-bearing now that there is a form around it:
              a button with no type inside a <form> IS a submit button, so
              Cancel would save. */}
          <Button type="button" variant="ghost" onClick={close}>
            {t("change.form.cancel")}
          </Button>
          {/* Enabled with the fields empty ON PURPOSE — the refusal is what the
              user gets to read, and a control that will not move is not. The
              server failure comes from here too, so a bodyless 503 still
              produces a sentence and `role="alert"` announces it, replacing the
              `{String(mutation.error)}` line that rendered a raw object. */}
          <FormActions
            label={t("change.form.save")}
            pending={mutation.isPending}
            error={mutation.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
