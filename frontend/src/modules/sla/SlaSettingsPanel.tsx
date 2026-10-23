import { useState } from "react";
import { attemptProblems, describeSubmitError, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { Panel } from "../../shell/components";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { ApiError } from "../../lib/api";
import { useSlaSettings, useUpdateSlaSettingsMutation } from "./api";
import { DEFAULT_AUTO_CLOSE_AFTER_DAYS, MAX_AUTO_CLOSE_AFTER_DAYS } from "./types";

// Org-wide SLA settings (spec §2.6): the auto-close window.
//
// `tickets_without_sla` and `stale_ticket_sla` ride on this response rather
// than on routes of their own (api.py). They are the CONSEQUENCE measures: 0
// in a healthy org, growing in one whose calendar or policy configuration is
// broken — including causes no per-calendar health flag enumerates, such as
// "no policy matched at all". They are given the most prominent treatment on
// this page for that reason, which is the STAT ROW (SlaAdminPage), not this
// panel: both were rendered in both places, so the page stated each number
// twice within 200px. The tiles carry the figure, the zero-state sentence and
// the diagnosis; this panel is the setting it is titled after.
// The one numeric setting on this panel, checked here so the bound arrives
// before the round trip rather than as a 422 after it. The server re-checks it
// and remains the authority.
//
// THE DISABLED BUTTON WAS THE SILENT NO-OP. `disabled={update.isPending ||
// !valid}` meant an admin who cleared the field pressed Save and watched
// nothing happen; the red sentence above it explained the value, never the
// dead button. Cycle 3's rule: refuse out loud, disable only for "in flight"
// and "you may not do this at all".
//
// A function, not a constant: the label and the sentences are worded when the
// refusal is rendered (`attemptProblems`), so a language switch re-words them.
const settingRules = (): readonly FieldRule[] => [
  {
    name: "days",
    label: translate("sla.settings.daysLabel"),
    validate: (value) => {
      if (!/^\d+$/.test(value)) return translate("sla.settings.daysWhole");
      const days = Number(value);
      if (days < 1 || days > MAX_AUTO_CLOSE_AFTER_DAYS) {
        return translate("sla.settings.daysRange", { max: MAX_AUTO_CLOSE_AFTER_DAYS });
      }
      return null;
    },
  },
];

export function SlaSettingsPanel({ canAdmin }: { canAdmin: boolean }) {
  const t = useT();
  const settings = useSlaSettings();
  const update = useUpdateSlaSettingsMutation();
  // `null` means "untouched, show what the server has" -- distinct from "",
  // which is a field the admin has deliberately cleared and which must stay
  // cleared rather than snapping back to the stored value mid-edit.
  const [days, setDays] = useState<string | null>(null);
  // On submit only. Held as the rules and the value they were checked against,
  // so the sentences are worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const forbidden = settings.error instanceof ApiError && settings.error.status === 403;
  const failed = settings.error != null && !forbidden;
  const value = days ?? String(settings.data?.auto_close_after_days ?? "");

  function submit() {
    const next: Attempt = { rules: settingRules, values: { days: value } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    update.mutate({ auto_close_after_days: Number(value) });
  }

  return (
    <Panel title={t("sla.settings.title")}>
      {settings.isLoading ? (
        <p className="note">{t("sla.common.loading")}</p>
      ) : forbidden ? (
        <p className="note">{t("sla.settings.forbidden")}</p>
      ) : failed ? (
        <p role="alert" className="notice red">
          {t("sla.settings.failed", { reason: describeSubmitError(settings.error) })}
        </p>
      ) : (
        <>
          {/* THE TWO CONSEQUENCE COUNTS ARE STATED ONCE, ON THE TILES.
              "Tickets with no SLA" and "Deadlines out of date" were printed
              here as label-plus-pill rows sitting directly under the tile row
              that already carried the identical figures and the identical
              zero-state sentences -- the same two numbers twice within 200px.
              The tiles keep the numbers AND the diagnosis (SlaAdminPage's
              `foot`), which is what these rows existed to carry; the one
              sentence that is NOT on a tile is the stale rows' residual
              meaning, so it stays here, attached to the setting whose edits
              produce them and shown only when there are any. */}
          {settings.data && settings.data.stale_ticket_sla > 0 && (
            <p className="note" style={{ marginBottom: 12 }}>
              {t("sla.settings.staleNote")}
            </p>
          )}

          {/* A real <form>, so Enter in the field saves rather than doing
              nothing -- the same finding the create dialogs on this page
              carried. */}
          {/* HIDDEN WITHOUT `sla.admin`, matching Add calendar and Add policy on
              this same page. Three controls with two treatments -- two hidden,
              one shown-and-disabled -- is the consistency defect the audits
              file over and over, and the console's established convention is
              the sidebar's: do not offer what the operator cannot use. The
              gate is cosmetic either way; every mutation here is enforced
              server-side by `require_permission`. */}
          {/* OUTSIDE THE GATE. Whether the org has ever configured this is a
              fact about the org, not a control -- a technician reading the
              page needs it as much as an administrator, and hiding it with the
              form would make a read-only view quietly less informative. */}
          {settings.data && !settings.data.configured && (
            <p className="form-hint" style={{ marginBottom: 8 }}>
              {t("sla.settings.neverConfigured", { count: DEFAULT_AUTO_CLOSE_AFTER_DAYS })}
            </p>
          )}

          {!canAdmin ? (
            <p className="note">
              {t("sla.settings.needsAdmin")}
            </p>
          ) : (
          <Form onSubmit={submit}>
            <Field label={t("sla.settings.daysLabel")}>
              {/* `type="text"`, NOT `type="number"`: a number input reports
                  `value === ""` for "abc", so the keystrokes vanish and the
                  field then looks merely untouched. Text lets the rule name
                  what is wrong with it. */}
              <input
                type="text"
                inputMode="numeric"
                className={inputClass}
                value={value}
                disabled={!canAdmin}
                onChange={(e) => setDays(e.target.value)}
              />
            </Field>
            {/* `error` and `problems` both land in ONE announced sentence here;
                the standalone red paragraph this replaces was neither
                announced nor beside the control, and `describeError` made it
                empty whenever the server sent no body. */}
            <FormActions
              label={t("sla.settings.save")}
              variant="outline"
              pendingLabel={t("sla.settings.saving")}
              pending={update.isPending}
              error={update.error}
              saved={update.isSuccess}
              problems={attemptProblems(attempt)}
              savedMessage={t("sla.settings.saved")}
              failedMessage={t("sla.settings.notSaved")}
            />
          </Form>
          )}
        </>
      )}
    </Panel>
  );
}
