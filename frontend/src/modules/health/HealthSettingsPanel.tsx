import { useState } from "react";
import { Link } from "react-router-dom";
import { Form, FormActions, attemptProblems, describeSubmitError, requiredProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { DurationInput } from "../../components/duration";
import { Badge, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { ApiError } from "../../lib/api";
import { Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KIND_ICON } from "../../shell/viz";
import { settingsHref } from "../settings/tabs";
import { useHealthSettings, useUpdateHealthSettingsMutation } from "./api";
import { NUMERIC_FIELDS } from "./fields";
import { formatSeconds } from "./healthState";
import { SETTING_BOUNDS } from "./types";
import type { SettingsUpdate } from "./types";

// Org-wide health settings (spec §2.8) -- the operational Health page's own
// copy of the eight fields also edited on Settings → Health
// (`../settings/HealthTab.tsx`).
//
// LANGUAGE PASS, 2026-09-11 (`docs/design/settings-language.md`, the Health
// table and "Second surfaces carrying the old words"). This file used to
// render its own copy: labels suffixed with "(seconds)"/"(days)", help text
// that explained the ENGINE ("clears as unavailable_timeout") instead of the
// CONSEQUENCE, and eight bare number boxes with no unit control. Two screens
// disagreeing about what the same setting is called is the defect the pass
// exists to remove, so `NUMERIC_FIELDS` is the direction doc's Health table
// verbatim -- the same labels and help strings `HealthTab.tsx` renders, and
// literally the same values: both files import it from `./fields.ts` rather
// than each carrying its own copy, which is what let the two screens drift
// apart in the first place. Seven of the eight fields go through the shared
// `DurationInput` (`components/duration.tsx`) instead of a raw number box.
//
// WHY THIS IS NOT A COPY OF `HealthTab.tsx`'S RENDERING, deliberately, in two
// places:
//
// 1. `alert_retention_days` STAYS A PLAIN NUMBER. It is stored in DAYS, not
//    seconds -- `backend/app/modules/health/sweep.py:673` builds
//    `timedelta(days=settings.alert_retention_days)`, bounds 90-3650 -- so
//    running it through a control whose contract is "stores seconds
//    unchanged" would silently multiply whatever the operator types by up to
//    86400. `flap_threshold` (a COUNT of alerts, unit "times") is the other
//    field that stays a plain number for the same underlying reason: neither
//    is a duration. `kind: "count"` below marks both.
// 2. THE CLIENT-SIDE BOUND CHECK STAYS, where `HealthTab.tsx` leaves its own
//    min/max attributes decorative and lets the server's typed 400 carry the
//    refusal. It stays here because it is not a language concern -- it is a
//    real, already-tested defect fix ("min/max constraints ... are
//    decorative -- the panel is not a <form>, so nothing is validated
//    client-side") that this pass has no reason to revert. `SETTING_RULES`
//    below quotes the bound in the FIELD'S OWN UNIT -- a friendly duration
//    (`formatSeconds`) for the seven duration fields, the raw count for the
//    two that are not durations -- so a bound violation never reads the raw
//    seconds figure that using `DurationInput` for the *value* exists to
//    hide.
//
// THE KNOWN TRAP THIS MIGRATION HAD TO CLEAR: the previous render put a
// hand-rolled `<div className="field">` (carrying the help paragraph)
// AROUND a `<Field>` (which is ALSO `.field`) -- two nested `.field`s, each
// contributing shell/components.css's `.field` margin and caption rule, so
// every hint on the panel doubled its bottom margin and its caption rule.
// The fix is not "keep the wrapper and reuse it for `DurationInput`" -- it is
// deleting the outer div outright and passing `help` straight to `Field`,
// which owns the caption/aria-describedby wiring itself (see `Field`'s own
// header comment in `components/ui.tsx`). There is exactly one `.field` per
// row below.

// One rule per field, so absence and shape answer through the same channel.
//
// The bound is quoted in the field's OWN unit: `formatSeconds` for the seven
// duration fields (e.g. "5 min", "1 d"); for the two that are not durations,
// the raw count, with `field.unit` appended when the field has one --
// `alert_retention_days` reads "must be between 90 and 3650 days", not a
// bare "90 and 3650" that never says what it is 90 and 3650 OF.
// `flap_threshold` carries no `unit` (see `./fields.ts`), so its own message
// is unchanged. Quoting a duration field's bound in raw seconds would put
// exactly the number this pass exists to stop showing back on screen, one
// keystroke after the operator typed into a control that shows minutes.
// A function, so the labels and the sentences are worded when the problems are
// shown, in the language then active (see `Attempt`).
const settingRules = (): readonly FieldRule[] =>
  NUMERIC_FIELDS.map((field) => ({
    name: field.key,
    label: field.label,
    validate: (value: string): string | null => {
      if (!/^-?\d+$/.test(value)) return translate("health.settings.notWhole", { label: field.label });
      const bounds = SETTING_BOUNDS[field.key];
      if (!bounds) return null;
      const [min, max] = bounds;
      if (Number(value) < min || Number(value) > max) {
        if (field.kind === "duration") {
          return translate("health.settings.between", {
            label: field.label,
            min: formatSeconds(min),
            max: formatSeconds(max),
          });
        }
        return field.unit
          ? translate("health.settings.betweenUnit", { label: field.label, min, max, unit: field.unit })
          : translate("health.settings.between", { label: field.label, min, max });
      }
      return null;
    },
  }));

export function HealthSettingsPanel({ canAdmin }: { canAdmin: boolean }) {
  const t = useT();
  const settings = useHealthSettings();
  const update = useUpdateHealthSettingsMutation();
  // A key present here is a field the admin has touched — including one they
  // have deliberately cleared, which must stay cleared rather than snapping
  // back to the server's value mid-edit.
  const [edits, setEdits] = useState<Record<string, string>>({});
  // Populated ON SUBMIT only. Nagging while someone types the first digit of
  // "3600" is its own kind of unhelpful. Held as rules + values and worded at
  // render (see `Attempt`), so a language switch re-words it.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const forbidden = settings.error instanceof ApiError && settings.error.status === 403;
  const data = settings.data;

  // What is on the screen right now, for every field, whether edited or not —
  // the rules are checked against this and not against `edits`, because an
  // untouched field is still a value the operator is about to submit.
  const values: Record<string, string> = {};
  if (data) {
    for (const field of NUMERIC_FIELDS) {
      values[field.key] = edits[field.key] ?? String(data[field.key as keyof typeof data] ?? "");
    }
  }

  // PATCH-shaped: only what changed is sent, so a concurrent edit to a field
  // this admin never looked at is not silently overwritten.
  const body: SettingsUpdate = {};
  for (const [key, raw] of Object.entries(edits)) {
    const value = Number(raw);
    if (raw !== "" && Number.isInteger(value)) {
      (body as Record<string, number>)[key] = value;
    }
  }
  const dirty = Object.keys(body).length > 0;

  function submitSettings() {
    // "Nothing to save" was previously expressed by disabling the button,
    // which is exactly the silent no-op: the operator presses it, nothing
    // moves, and the panel offers no theory. It travels through the problems
    // like every other reason a submission cannot proceed, and only once the
    // values themselves are fine.
    const valuesFine = requiredProblems(settingRules(), values).length === 0;
    const nothingToSave = valuesFine && !dirty;
    const next: Attempt = {
      rules: () => [
        ...settingRules(),
        ...(nothingToSave
          ? [
              {
                name: "settings",
                label: translate("health.settings.settingsRule"),
                validate: () => translate("health.settings.nothingChanged"),
              },
            ]
          : []),
      ],
      values: { ...values, ...(nothingToSave ? { settings: "unchanged" } : {}) },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    // Cleared only on the server's 2xx. Clearing beside `mutate` reverted
    // every field the instant a 422 came back, so the operator could not see
    // — let alone correct — the value the error was complaining about.
    update.mutate(body, { onSuccess: () => setEdits({}) });
  }

  return (
    <Panel
      title={t("health.settings.title")}
      subtitle={t("health.settings.subtitle")}
    >
      {forbidden ? (
        <p role="alert" className="note">
          {t("health.settings.forbidden")}
        </p>
      ) : settings.error ? (
        /* WITHOUT THIS BRANCH a failed load fell to `!data` and sat on
           "Loading…" for ever: the panel that tells an operator whether alerts
           can open tickets at all, stuck mid-sentence with no way to tell a
           slow server from a dead one. */
        <div role="alert" className="notice red">
          <Icon name={KIND_ICON.crit} size={17} />
          <div>{t("health.settings.loadFailed", { detail: describeSubmitError(settings.error) })}</div>
        </div>
      ) : !data ? (
        <div className="empty">
          <Icon name="clock" size={26} />
          <b>{t("health.settings.loading")}</b>
        </div>
      ) : (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Badge variant={data.configured ? "muted" : "warning"}>
              {data.configured ? t("health.settings.configured") : t("health.settings.defaults")}
            </Badge>
            {/* The silent failure this panel exists to surface. There is no
                queue/category picker on THIS page (Settings → Health owns
                that), so the notice below links to Settings → Health rather
                than the badge claiming a control sits "below" that this panel
                does not have. */}
            <Badge variant={data.alert_tickets_enabled ? "success" : "warning"}>
              {data.alert_tickets_enabled ? t("health.settings.ticketsOn") : t("health.settings.ticketsOff")}
            </Badge>
            <Badge variant={data.auto_resolve_alert_tickets ? "muted" : "warning"}>
              {data.auto_resolve_alert_tickets
                ? t("health.settings.autoResolveOn")
                : t("health.settings.autoResolveOff")}
            </Badge>
          </div>

          {!data.alert_tickets_enabled && (
            <div className="notice gold mb-3">
              <Icon name={KIND_ICON.warn} size={17} />
              <span>
                {t("health.settings.noQueue")}{" "}
                {/* A LINK, NOT A SECOND MESSAGE (cross-module-guidance §2
                    Alerts #3): the queue and category are chosen on
                    Settings -> Health, which `?tab=health` opens directly. */}
                <Link to={settingsHref("health")}>{t("health.settings.chooseWhere")}</Link>
              </span>
            </div>
          )}

          <Form onSubmit={submitSettings}>
            {/* THE TWO COLUMNS BECOME ONE ON A HANDSET, AND THIS INLINE STYLE
                IS WHY IT ACTUALLY HAPPENS -- unchanged from before this pass;
                see the historical note this comment used to carry in git
                blame. `auto-fit`/`minmax` answers to THIS panel's own width
                rather than the viewport, which is what a `grid-column: span`
                child needs. */}
            <div
              className="form-grid"
              style={{ gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))" }}
            >
              {NUMERIC_FIELDS.map((field) => {
                const bounds = SETTING_BOUNDS[field.key];
                return (
                  <Field key={field.key} label={field.label} help={field.help}>
                    {field.kind === "duration" ? (
                      // Keyed on the FETCHED VALUE, not the live edit: this
                      // remounts (and re-derives its displayed unit from the
                      // stored seconds) only when a fresh value arrives from
                      // the server -- initial load, or a refetch after save
                      // -- never on every keystroke. Same discipline as
                      // `HealthTab.tsx`'s `seedSignature` key, adapted to
                      // this file's simpler `edits`-over-`data` state (there
                      // is no separate seeded form to derive a signature
                      // from). See `duration.tsx`'s own comment on why the
                      // distinction matters.
                      <DurationInput
                        key={`${field.key}:${data.updated_at ?? ""}`}
                        value={values[field.key] ?? ""}
                        bounds={bounds}
                        disabled={!canAdmin}
                        label={field.label}
                        unitLabel={t("health.settings.unit", { label: field.label })}
                        onChange={(next) =>
                          setEdits((current) => ({ ...current, [field.key]: next }))
                        }
                      />
                    ) : (
                      // `type="text"`, NOT `type="number"`, kept from before
                      // this pass: the audit finding it fixes ("typing
                      // letters into a numeric field silently erases it, and
                      // the empty result passes validation") is a real,
                      // already-tested defect and not a language concern --
                      // a number input reports `value === ""` for "abc", so
                      // the keystrokes would vanish and the field would look
                      // merely untouched. `inputMode` keeps the numeric
                      // keypad on mobile.
                      <input
                        className={inputClass}
                        type="text"
                        inputMode="numeric"
                        disabled={!canAdmin}
                        value={values[field.key] ?? ""}
                        onChange={(e) =>
                          setEdits((c) => ({ ...c, [field.key]: e.target.value }))
                        }
                      />
                    )}
                  </Field>
                );
              })}
            </div>

            {/* Not disabled for "nothing changed" or "that value is out of
                range" — both of those are now sentences the operator gets by
                pressing it. Disabled only while the request is in flight, and
                for the caller who may not do this at all, who is told so. */}
            <FormActions
              className="mt-2"
              label={t("health.settings.save")}
              pending={update.isPending}
              error={update.error}
              saved={update.isSuccess && !dirty}
              problems={attemptProblems(attempt)}
              savedMessage={t("health.settings.saved")}
              disabledReason={canAdmin ? undefined : t("health.settings.noPermission")}
            />
          </Form>
        </>
      )}
    </Panel>
  );
}
