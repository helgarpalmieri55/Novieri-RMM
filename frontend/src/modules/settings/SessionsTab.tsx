// Sessions -- `GET /settings/sessions` (`orgs.manage`), `PUT /settings/sessions`
// (`orgs.manage` on EVERY client): how long a sign-in lasts (own
// authentication design §2.4, §2.6, §3). One setting for the whole
// organization, so the write needs the code on every client, like Email.
//
// SEVEN VALUES, AND THE BOUNDS ARE THE SERVER'S. The table's CHECK constraints
// (migration 0245) are restated in `SESSION_BOUNDS` only so a value out of
// range is refused before the round trip, with the range under its field;
// the server refuses it anyway. A save sends all seven.
//
// AFTER THE INACTIVITY TIME ONLY THE PASSWORD IS ASKED AGAIN; after the longest
// session, the second factor too (§2.4, what NIST allows at AAL2). The note
// under the technicians' two values says so in the operator's words.
//
// "REMEMBER THIS DEVICE" IS A TRADE THE OPERATOR MADE KNOWINGLY (design §0,
// S2b): on a remembered device the second factor is not asked until it
// expires, which is below AAL2. The warning under the switch says what that
// means, and the limits that bound it -- the person or an administrator can
// forget a device; a password change, a factor reset or a disable forgets
// them all -- are the server's.

import { useEffect, useState } from "react";

import { useStepUp } from "../../components/stepUp";
import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { FieldGroup, KIND_ICON, Panel, Toggle } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useGlobalWriteReason, useSessionSettings, useUpdateSessionSettingsMutation } from "./api";
import { FOLDING_FORM_GRID } from "./formGrid";
import { LoadFailure } from "./LoadFailure";
import { SaveBar } from "./SaveBar";
import { isDirty, isForbidden, parseIntegerField } from "./settingsState";
import {
  SESSION_BOUNDS,
  type SessionNumberKey,
  type SessionSettingsOut,
  type SessionSettingsUpdate,
} from "./types";

/** Read and write are the same code, as on the Email tab. */
export const SESSIONS_MANAGE = "orgs.manage";

type SessionsForm = Record<SessionNumberKey, string> & { remember_device_enabled: boolean };

const NUMBER_KEYS: readonly SessionNumberKey[] = [
  "staff_idle_minutes",
  "staff_max_hours",
  "remember_device_days",
  "remembered_max_hours",
  "contact_idle_minutes",
  "contact_max_days",
];

/** The two values that mean something only while remembering a device is on. */
const REMEMBER_KEYS: ReadonlySet<SessionNumberKey> = new Set(["remember_device_days", "remembered_max_hours"]);

/** Each value's caption, and the unit its range is said in. */
const FIELD: Readonly<Record<SessionNumberKey, { label: MessageKey; range: MessageKey }>> = {
  staff_idle_minutes: { label: "settings.sessions.staffIdle", range: "settings.sessions.rangeMinutes" },
  staff_max_hours: { label: "settings.sessions.staffMax", range: "settings.sessions.rangeHours" },
  remember_device_days: { label: "settings.sessions.rememberDays", range: "settings.sessions.rangeDays" },
  remembered_max_hours: { label: "settings.sessions.rememberedMax", range: "settings.sessions.rangeHours" },
  contact_idle_minutes: { label: "settings.sessions.contactIdle", range: "settings.sessions.rangeMinutes" },
  contact_max_days: { label: "settings.sessions.contactMax", range: "settings.sessions.rangeDays" },
};

function seedFrom(data: SessionSettingsOut | undefined): SessionsForm {
  const text = (key: SessionNumberKey): string => (data ? String(data[key]) : "");
  return {
    staff_idle_minutes: text("staff_idle_minutes"),
    staff_max_hours: text("staff_max_hours"),
    remember_device_days: text("remember_device_days"),
    remembered_max_hours: text("remembered_max_hours"),
    contact_idle_minutes: text("contact_idle_minutes"),
    contact_max_days: text("contact_max_days"),
    remember_device_enabled: data?.remember_device_enabled ?? true,
  };
}

function inRange(key: SessionNumberKey, raw: string): boolean {
  const value = parseIntegerField(raw);
  const [min, max] = SESSION_BOUNDS[key];
  return value != null && value >= min && value <= max;
}

/** `canRead` reads the tab (`orgs.manage`); `canEdit` writes it: `orgs.manage` on every client. */
export function SessionsTab({ canRead, canEdit }: { canRead: boolean; canEdit: boolean }) {
  const t = useT();
  const reason = useGlobalWriteReason(SESSIONS_MANAGE);
  const settings = useSessionSettings(canRead);
  // A save needs the second factor confirmed in the last 10 minutes (§2.5, S1).
  const stepUp = useStepUp();
  const update = useUpdateSessionSettingsMutation(stepUp.run);

  const seed = seedFrom(settings.data);
  const [form, setForm] = useState<SessionsForm>(seed);
  const seedSignature = JSON.stringify(seed);
  useEffect(() => {
    setForm(JSON.parse(seedSignature) as SessionsForm);
  }, [seedSignature]);

  const forbidden = isForbidden(settings.error);
  const failed = settings.error != null && !forbidden;
  const dirty = isDirty(form, seed);
  // A REMEMBER VALUE IS NOT CHECKED WHILE REMEMBERING IS OFF (final review
  // D2, Minor 6): its box is disabled, so a value typed out of range and then
  // switched off could neither be corrected nor saved past, and Save stayed
  // blocked. What is sent for it then is the value the server holds.
  const hidden = (key: SessionNumberKey): boolean => !form.remember_device_enabled && REMEMBER_KEYS.has(key);
  const invalid = NUMBER_KEYS.filter((key) => !hidden(key) && !inRange(key, form[key]));
  const editable = canEdit && settings.data != null;

  function set<K extends keyof SessionsForm>(key: K, value: SessionsForm[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function submit() {
    if (invalid.length > 0) return;
    const number = (key: SessionNumberKey): number => parseIntegerField(hidden(key) ? seed[key] : form[key]) as number;
    const body: SessionSettingsUpdate = {
      staff_idle_minutes: number("staff_idle_minutes"),
      staff_max_hours: number("staff_max_hours"),
      remember_device_enabled: form.remember_device_enabled,
      remember_device_days: number("remember_device_days"),
      remembered_max_hours: number("remembered_max_hours"),
      contact_idle_minutes: number("contact_idle_minutes"),
      contact_max_days: number("contact_max_days"),
    };
    update.mutate(body);
  }

  function numberField(key: SessionNumberKey, off = false) {
    const [min, max] = SESSION_BOUNDS[key];
    return (
      <Field key={key} label={t(FIELD[key].label)} help={t(FIELD[key].range, { min, max })}>
        <input
          className={inputClass}
          type="number"
          inputMode="numeric"
          min={min}
          max={max}
          value={form[key]}
          disabled={!editable || off}
          onChange={(event) => set(key, event.target.value)}
        />
      </Field>
    );
  }

  // The panel, then the confirmation outside it (`components/stepUp.tsx`).
  const panel = (
    <Panel title={t("settings.sessions.title")} subtitle={t("settings.sessions.subtitle")}>
      {settings.isLoading ? (
        <p className="note">{t("settings.sessions.loading")}</p>
      ) : forbidden ? (
        <p className="note">{t("settings.sessions.forbidden", { code: SESSIONS_MANAGE })}</p>
      ) : failed ? (
        <LoadFailure error={settings.error} />
      ) : (
        <>
          <FieldGroup title={t("settings.sessions.groups.staff")} icon="users">
            <div className="form-grid" style={FOLDING_FORM_GRID}>
              {numberField("staff_idle_minutes")}
              {numberField("staff_max_hours")}
            </div>
            <p className="note" style={{ marginTop: 8 }}>
              {t("settings.sessions.staffNote")}
            </p>
          </FieldGroup>
          <FieldGroup title={t("settings.sessions.groups.remember")} icon="devices">
            <Toggle
              checked={form.remember_device_enabled}
              disabled={!editable}
              label={t("settings.sessions.rememberEnabled")}
              onChange={(next) => set("remember_device_enabled", next)}
            />
            <div className="form-grid" style={{ ...FOLDING_FORM_GRID, marginTop: 10 }}>
              {numberField("remember_device_days", !form.remember_device_enabled)}
              {numberField("remembered_max_hours", !form.remember_device_enabled)}
            </div>
            <p className="notice gold" style={{ marginTop: 10 }}>
              <Icon name={KIND_ICON.warn} size={16} />
              <span>{t("settings.sessions.rememberRisk")}</span>
            </p>
          </FieldGroup>
          <FieldGroup title={t("settings.sessions.groups.contacts")} icon="portal">
            <div className="form-grid" style={FOLDING_FORM_GRID}>
              {numberField("contact_idle_minutes")}
              {numberField("contact_max_days")}
            </div>
            <p className="note" style={{ marginTop: 8 }}>
              {t("settings.sessions.contactsNote")}
            </p>
          </FieldGroup>

          {invalid.length > 0 ? (
            <p className="form-error" style={{ marginTop: 10 }}>
              <Icon name={KIND_ICON.crit} size={13} /> {t("settings.sessions.outOfRange")}
            </p>
          ) : null}

          <SaveBar
            canEdit={editable && invalid.length === 0}
            disabledReason={canEdit ? t("settings.sessions.correct") : reason}
            dirty={dirty}
            pending={update.isPending}
            error={update.error}
            saved={update.isSuccess && !dirty}
            onSave={submit}
          />
        </>
      )}
    </Panel>
  );
  return (
    <>
      {panel}
      {stepUp.dialog}
    </>
  );
}
