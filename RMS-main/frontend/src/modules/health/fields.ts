// The Health settings field table -- one definition, both surfaces.
//
// `docs/design/settings-language.md`, the Health table: label and help text
// are used verbatim from there. Two screens render this table --
// `../settings/HealthTab.tsx` (Settings → Health) and `./HealthSettingsPanel.tsx`
// (the operational Health page's own copy) -- and until this file existed
// each carried its own byte-identical copy of all eight rows. That is the
// exact failure mode `docs/design/settings-language.md` names and that was
// fixed for the notification event-type labels BY DELETION
// (`../notification/labels.ts` is the precedent this follows): one
// definition, imported twice, so the two screens cannot drift apart the way
// they already had once (`HealthSettingsPanel.tsx` used to hand-roll its own
// older copy -- suffixed labels, no duration control -- before the language
// pass).
//
// `kind: "duration"` renders through `DurationInput` -- a number beside an
// editable minutes/hours/days unit, seconds unchanged on the wire. `kind:
// "count"` stays the plain number input both pages have always used for a
// value that is not a number of seconds at all.
//
// `flap_threshold` IS A COUNT OF ALERTS, NOT A DURATION -- its unit word is
// "times", and forcing it through the seconds-based control would be simply
// wrong. Its own help ("Past this many alerts...") already names what it
// counts, so it carries no `unit` below.
//
// `alert_retention_days` STAYS A PLAIN NUMBER FOR A DIFFERENT REASON: it
// reads like a duration, but it is NOT stored in seconds --
// `backend/app/modules/health/sweep.py`'s cutoff is
// `now - timedelta(days=settings.alert_retention_days)`. Wrapping it in a
// control that "stores seconds unchanged on the wire" would silently
// multiply whatever the operator types by up to 86400. It is already in the
// one unit it has (days), so there is no unit to PICK -- but that unit still
// has to be SAID, or an operator typing into a bare number box has no way to
// know what "400" means. The language pass's own rewrite dropped it: the old
// label was `Alert retention (days)`, the parenthetical carrying the unit;
// the new label is `Delete closed alerts after`, and nothing replaced what
// the parenthetical said. `unit: "days"` below puts it back in the HELP
// ("removed after this many days", not the mechanical "(days)" suffix the
// pass exists to remove from labels) and in the out-of-range message
// (`HealthSettingsPanel.tsx`'s `SETTING_RULES`, which reads `field.unit` to
// name what a bound counts).
import { lazyFields } from "../../i18n/lazy";

export type NumericKey =
  | "flap_window_s"
  | "flap_threshold"
  | "flap_cooldown_s"
  | "unavailable_clear_after_s"
  | "alert_retention_days"
  | "escalate_after_s"
  | "offline_after_s"
  | "approval_ttl_s";

export interface NumericFieldDef {
  key: NumericKey;
  label: string;
  help: string;
  kind: "duration" | "count";
  /**
   * The operator-facing unit word for a `kind: "count"` field that has one --
   * e.g. "days" for `alert_retention_days`. A `kind: "duration"` field never
   * needs this: `DurationInput` shows its own unit beside the value, so
   * naming one here would be a second, redundant answer to the same
   * question.
   */
  unit?: string;
}

// Each field's `label`, `help` and `unit` are accessors over the catalogue
// (`lazyFields`), read when the field is drawn, so a language switch shows on
// the next render and Settings → Health, which imports this table, follows too.
export const NUMERIC_FIELDS: readonly NumericFieldDef[] = [
  lazyFields(
    { key: "offline_after_s" as const, kind: "duration" as const },
    { label: "health.settings.fields.offline_after_s.label", help: "health.settings.fields.offline_after_s.help" },
  ),
  lazyFields(
    { key: "escalate_after_s" as const, kind: "duration" as const },
    { label: "health.settings.fields.escalate_after_s.label", help: "health.settings.fields.escalate_after_s.help" },
  ),
  lazyFields(
    { key: "unavailable_clear_after_s" as const, kind: "duration" as const },
    {
      label: "health.settings.fields.unavailable_clear_after_s.label",
      help: "health.settings.fields.unavailable_clear_after_s.help",
    },
  ),
  lazyFields(
    { key: "flap_window_s" as const, kind: "duration" as const },
    { label: "health.settings.fields.flap_window_s.label", help: "health.settings.fields.flap_window_s.help" },
  ),
  lazyFields(
    { key: "flap_threshold" as const, kind: "count" as const },
    { label: "health.settings.fields.flap_threshold.label", help: "health.settings.fields.flap_threshold.help" },
  ),
  lazyFields(
    { key: "flap_cooldown_s" as const, kind: "duration" as const },
    { label: "health.settings.fields.flap_cooldown_s.label", help: "health.settings.fields.flap_cooldown_s.help" },
  ),
  lazyFields(
    { key: "approval_ttl_s" as const, kind: "duration" as const },
    { label: "health.settings.fields.approval_ttl_s.label", help: "health.settings.fields.approval_ttl_s.help" },
  ),
  lazyFields(
    { key: "alert_retention_days" as const, kind: "count" as const },
    {
      label: "health.settings.fields.alert_retention_days.label",
      help: "health.settings.fields.alert_retention_days.help",
      unit: "health.settings.fields.alert_retention_days.unit",
    },
  ),
];
