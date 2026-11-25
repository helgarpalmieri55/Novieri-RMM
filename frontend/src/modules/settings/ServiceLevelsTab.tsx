// Service levels — `GET /sla/settings` (`sla.read`), `PUT /sla/settings`
// (`sla.admin`).
//
// ONE WRITABLE FIELD, AND TWO NUMBERS THAT ARE NOT SETTINGS. `SettingsUpdate`
// carries `auto_close_after_days` and nothing else, so that is the only input
// here. `tickets_without_sla` and `stale_ticket_sla` ride on the same response
// and are shown as CONSEQUENCE measures, not controls: they are counts of
// tickets the current configuration is failing, and they are the reason to
// change the configuration rather than something to type into.
//
// Calendars and policies are the SLA module's own screens (`/sla`), not
// omitted from here by accident: they are collections with per-row editors —
// business days, holidays, per-priority targets — and duplicating that editor
// would put two writers on one resource. The setting that applies everywhere
// is what belongs on a settings page, and it is here in full.

import { useState } from "react";

import { Badge, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { FormSection, KIND_ICON, SaveBar, UnsavedGuard, useDraft } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { DEFAULT_AUTO_CLOSE_AFTER_DAYS, MAX_AUTO_CLOSE_AFTER_DAYS } from "../sla/types";
import { useSlaSettings, useUpdateSlaSettingsMutation, useGlobalWriteReason } from "./api";
import { LoadFailure } from "./LoadFailure";
import { ReadOnlyNotice } from "./ReadOnlyNotice";
import { isForbidden, parseIntegerField, sentenceProblem } from "./settingsState";

export const SLA_READ = "sla.read";
export const SLA_ADMIN = "sla.admin";

export function ServiceLevelsTab({ canRead, canAdmin }: { canRead: boolean; canAdmin: boolean }) {
  const t = useT();
  const reason = useGlobalWriteReason(SLA_ADMIN);
  const settings = useSlaSettings(canRead);
  const update = useUpdateSlaSettingsMutation();

  // The kit's draft (redesign, Receta FO): the server's value is the saved
  // state, and a save's answer becomes it (`markSent`).
  const { draft, setDraft, dirty, discard, markSent } = useDraft<string>(
    settings.data ? String(settings.data.auto_close_after_days) : undefined,
  );
  const days = draft ?? "";
  // A save refused for the value, said in the bar until the value is fixed.
  const [tried, setTried] = useState(false);

  const forbidden = isForbidden(settings.error);
  const failed = settings.error != null && !forbidden;
  const parsed = parseIntegerField(days);
  const valid = parsed != null && parsed >= 1 && parsed <= MAX_AUTO_CLOSE_AFTER_DAYS;

  function submit() {
    // Out of range keeps Save enabled and is said on submit (the kit's rule:
    // `blockedReason` is a permission, never input to correct).
    if (!valid || parsed == null) {
      setTried(true);
      return;
    }
    setTried(false);
    markSent(days);
    update.mutate({ auto_close_after_days: parsed }, { onError: () => markSent(null) });
  }

  if (settings.isLoading) return <p className="note">{t("settings.sla.loading")}</p>;
  if (forbidden) return <p className="note">{t("settings.sla.forbidden", { code: SLA_READ })}</p>;
  if (failed) return <LoadFailure error={settings.error} />;

  return (
    <>
      <UnsavedGuard dirty={dirty} search={["tab"]} />
      {!canAdmin ? <ReadOnlyNotice reason={reason} /> : null}
      <FormSection title={t("settings.sla.title")} icon="clock">
        <p className="note" style={{ marginBottom: 12 }}>
          {t("settings.sla.subtitle")}
        </p>
        {settings.data ? (
          <div className="flex flex-wrap items-center gap-3" style={{ marginBottom: 14 }}>
            <span className="note">{t("settings.sla.withoutSla")}</span>
            <Badge variant={settings.data.tickets_without_sla > 0 ? "destructive" : "success"}>
              {String(settings.data.tickets_without_sla)}
            </Badge>
            <span className="note">{t("settings.sla.staleDeadlines")}</span>
            <Badge variant={settings.data.stale_ticket_sla > 0 ? "destructive" : "success"}>
              {String(settings.data.stale_ticket_sla)}
            </Badge>
            <Badge variant={settings.data.configured ? "muted" : "warning"}>
              {settings.data.configured
                ? t("settings.sla.configured")
                : t("settings.sla.neverConfigured", { count: DEFAULT_AUTO_CLOSE_AFTER_DAYS })}
            </Badge>
          </div>
        ) : null}

        <Field label={t("settings.sla.autoClose")} help={t("settings.sla.days")}>
          <input
            className={inputClass}
            type="number"
            min={1}
            max={MAX_AUTO_CLOSE_AFTER_DAYS}
            value={days}
            disabled={!canAdmin || settings.data == null}
            onChange={(event) => setDraft(event.target.value)}
          />
        </Field>
        {/* Said as the value is typed; once a save was refused, the bar says it. */}
        {!valid && days !== "" && !tried ? (
          <p className="form-error">
            <Icon name={KIND_ICON.crit} size={13} />{" "}
            {t("settings.sla.invalid", { max: MAX_AUTO_CLOSE_AFTER_DAYS })}
          </p>
        ) : null}
      </FormSection>

      <SaveBar
        dirty={dirty}
        saving={update.isPending}
        error={update.error}
        problems={
          tried && !valid
            ? [sentenceProblem("days", t("settings.sla.invalid", { max: MAX_AUTO_CLOSE_AFTER_DAYS }))]
            : []
        }
        saved={update.isSuccess && !dirty}
        onDiscard={() => {
          discard();
          setTried(false);
          update.reset();
        }}
        onSave={submit}
      />
    </>
  );
}
