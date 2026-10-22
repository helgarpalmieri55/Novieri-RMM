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

import { useEffect, useState } from "react";

import { Badge, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { KIND_ICON, Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { DEFAULT_AUTO_CLOSE_AFTER_DAYS, MAX_AUTO_CLOSE_AFTER_DAYS } from "../sla/types";
import { useSlaSettings, useUpdateSlaSettingsMutation } from "./api";
import { LoadFailure } from "./LoadFailure";
import { SaveBar } from "./SaveBar";
import { isForbidden, parseIntegerField, permissionReason } from "./settingsState";

export const SLA_READ = "sla.read";
export const SLA_ADMIN = "sla.admin";

export function ServiceLevelsTab({ canRead, canAdmin }: { canRead: boolean; canAdmin: boolean }) {
  const t = useT();
  const settings = useSlaSettings(canRead);
  const update = useUpdateSlaSettingsMutation();

  const seed = settings.data ? String(settings.data.auto_close_after_days) : "";
  const [days, setDays] = useState(seed);

  useEffect(() => {
    setDays(seed);
  }, [seed]);

  const forbidden = isForbidden(settings.error);
  const failed = settings.error != null && !forbidden;
  const parsed = parseIntegerField(days);
  const valid = parsed != null && parsed >= 1 && parsed <= MAX_AUTO_CLOSE_AFTER_DAYS;
  const dirty = days !== seed;

  return (
    <Panel
      title={t("settings.sla.title")}
      subtitle={t("settings.sla.subtitle")}
    >
      {settings.isLoading ? (
        <p className="note">{t("settings.sla.loading")}</p>
      ) : forbidden ? (
        <p className="note">
          {t("settings.sla.forbidden", { code: SLA_READ })}
        </p>
      ) : failed ? (
        <LoadFailure error={settings.error} />
      ) : (
        <>
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
              onChange={(event) => setDays(event.target.value)}
            />
          </Field>
          {!valid && days !== "" ? (
            <p className="form-error">
              <Icon name={KIND_ICON.crit} size={13} />{" "}
              {t("settings.sla.invalid", { max: MAX_AUTO_CLOSE_AFTER_DAYS })}
            </p>
          ) : null}

          <SaveBar
            label={t("settings.sla.save")}
            canEdit={canAdmin && settings.data != null && valid}
            disabledReason={
              canAdmin ? t("settings.sla.correct") : permissionReason(SLA_ADMIN)
            }
            dirty={dirty}
            pending={update.isPending}
            error={update.error}
            saved={update.isSuccess && !dirty}
            onSave={() => {
              if (parsed == null) return;
              update.mutate({ auto_close_after_days: parsed });
            }}
          />
        </>
      )}
    </Panel>
  );
}
