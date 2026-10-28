// Budget — `GET /budget/settings` (`budget.read`), `PUT /budget/settings`
// (`budget.manage`).
//
// THIS IS THE DESIGN'S "Base currency" FIELD, and it is on this tab rather
// than on Organization because this is the endpoint that owns it. The mock put
// currency under the org profile; the server keeps it in budget settings,
// where changing it re-bases every expense. One setting, one form.
//
// `acknowledge_mixed_basis` IS OFFERED ONLY WHEN IT MEANS SOMETHING. Once any
// non-void expense carries an `amount_base`, the server REFUSES a currency
// change unless the request acknowledges that historic figures were converted
// on the old basis. The checkbox appears when the currency field is actually
// being changed, and the acknowledgement is sent only when it is ticked — a
// permanently-on flag would be an acknowledgement nobody made.

import { useEffect, useState } from "react";

import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { formatDateTime } from "../../lib/datetime";
import { internalClientId } from "../../lib/internalClient";
import { KIND_ICON, Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { ChipsField } from "../../shell/viz";
import type { SettingsOut, SettingsUpdate } from "../budget/types";
import { useBudgetSettings, useMe, useUpdateBudgetSettingsMutation, useUsers } from "./api";
import { FOLDING_FORM_GRID } from "./formGrid";
import { LoadFailure } from "./LoadFailure";
import { SaveBar } from "./SaveBar";
import { isDirty, isForbidden, parseIntegerField, permissionReason } from "./settingsState";

export const BUDGET_READ = "budget.read";
export const BUDGET_MANAGE = "budget.manage";

/**
 * The account behind `SettingsOut.updated_by`, as a person reads it.
 *
 * `updated_by` IS A USER ID, NEVER A NAME (`budget/api.py:_settings_out` puts
 * `row.updated_by` -- a uuid FK, `models.py:37` -- straight on the wire; there
 * is no join). Design review, 2026-09-11: "usr-helgar is rendered as a
 * person" -- the id was printed after "by " as if it were one. The fix is the
 * one this console already has for exactly this shape
 * (`patch/api.ts:useApprovalActorDirectory` + `patch/ApprovalsPanel.tsx`'s
 * `ActorName`): resolve the id against the org's user directory when it can
 * be read, and when it cannot -- or the id is not in it -- print the id
 * honestly AS an id (`.mono`, the treatment every other raw wire handle in
 * this console gets) rather than dressing it up as a name.
 */
function actorName(
  users: readonly { id: string; display_name: string; email: string }[] | undefined,
  actorId: string,
): string | null {
  const user = users?.find((candidate) => candidate.id === actorId);
  if (!user) return null;
  return user.display_name || user.email || null;
}

// Catalogue keys, worded when the chips are drawn (never remembered).
const MONTH_KEYS: readonly MessageKey[] = [
  "settings.budget.months.january",
  "settings.budget.months.february",
  "settings.budget.months.march",
  "settings.budget.months.april",
  "settings.budget.months.may",
  "settings.budget.months.june",
  "settings.budget.months.july",
  "settings.budget.months.august",
  "settings.budget.months.september",
  "settings.budget.months.october",
  "settings.budget.months.november",
  "settings.budget.months.december",
];

interface BudgetForm {
  baseCurrency: string;
  fiscalYearStartMonth: string;
}

function seedFrom(data: SettingsOut | undefined): BudgetForm {
  return {
    baseCurrency: data?.base_currency ?? "",
    fiscalYearStartMonth: data ? String(data.fiscal_year_start_month) : "",
  };
}

export function BudgetTab({
  canRead,
  canManage,
  canReadUsers,
}: {
  canRead: boolean;
  canManage: boolean;
  /**
   * `users.read`, ASKED FOR ONLY TO NAME `updated_by` -- not implied by
   * `budget.read`/`budget.manage`, so for many operators this call would
   * only 403. `enabled` below is gated on it rather than the request firing
   * hopefully on every visit (the same shape `patch/api.ts`'s
   * `useApprovalActorDirectory` already documents for the identical case).
   */
  canReadUsers: boolean;
}) {
  const t = useT();
  const me = useMe();
  // Budget settings are one client's (base currency, fiscal year).
  // PHASE 3: the client/site selector. Until it exists that is the
  // organisation's internal client, whose id is the organisation's --
  // `/me`'s `org_id` (`lib/internalClient.ts`).
  const clientId = internalClientId(me.data);
  const settings = useBudgetSettings(clientId, canRead);
  const update = useUpdateBudgetSettingsMutation(clientId);
  // Only when there is actually somebody to name, and only when this session
  // can read the directory at all -- a stamp with no `updated_by` (a server
  // that has never had this setting written) asks for nothing.
  const directory = useUsers(canReadUsers && settings.data?.updated_by != null);
  const resolvedActor =
    settings.data?.updated_by != null ? actorName(directory.data?.users, settings.data.updated_by) : null;

  const seed = seedFrom(settings.data);
  const [form, setForm] = useState<BudgetForm>(seed);
  const [acknowledged, setAcknowledged] = useState(false);

  const seedSignature = JSON.stringify(seed);
  useEffect(() => {
    setForm(JSON.parse(seedSignature) as BudgetForm);
    setAcknowledged(false);
  }, [seedSignature]);

  const forbidden = isForbidden(settings.error);
  const failed = settings.error != null && !forbidden;
  const dirty = isDirty(form, seed);
  const editable = canManage && settings.data != null;
  const currencyChanged = settings.data != null && form.baseCurrency !== settings.data.base_currency;
  const month = parseIntegerField(form.fiscalYearStartMonth);
  const monthValid = month != null && month >= 1 && month <= 12;

  function submit() {
    const body: SettingsUpdate = {
      base_currency: form.baseCurrency === "" ? null : form.baseCurrency.toUpperCase(),
      fiscal_year_start_month: month,
    };
    if (currencyChanged && acknowledged) body.acknowledge_mixed_basis = true;
    update.mutate(body);
  }

  return (
    <Panel
      title={t("settings.budget.title")}
      subtitle={t("settings.budget.subtitle")}
    >
      {settings.isLoading ? (
        <p className="note">{t("settings.budget.loading")}</p>
      ) : forbidden ? (
        <p className="note">
          {t("settings.budget.forbidden", { code: BUDGET_READ })}
        </p>
      ) : failed ? (
        <LoadFailure error={settings.error} />
      ) : (
        <>
          <div className="form-grid" style={FOLDING_FORM_GRID}>
            <Field label={t("settings.budget.baseCurrency")} help={t("settings.budget.baseCurrencyHelp")}>
              <input
                className={inputClass}
                value={form.baseCurrency}
                maxLength={3}
                disabled={!editable}
                onChange={(event) =>
                  setForm((current) => ({ ...current, baseCurrency: event.target.value }))
                }
              />
            </Field>
            {/* A CHOICE OF TWELVE, NOT A MENU. `<select>` is the browser's
                own widget painted through the kit's box; the design draws a
                short, fixed enumeration as `Chips`, now through the shared
                `ChipsField` (`viz.tsx`) that closed this call site,
                `EmailTab`'s and `OrganizationTab`'s into one -- a plain
                `.field` `div` rather than `Field`'s `<label>` (a chip
                button nested inside a `<label>` inherits that label's text
                as ITS OWN accessible name; see `EmailTab`'s Encryption for
                the same note), its caption named once through
                `aria-labelledby`, and `disabled={!editable}` in place of
                the `Field` + `<select disabled>` twin this used to need. */}
            <ChipsField
              caption={t("settings.budget.fiscalYear")}
              items={MONTH_KEYS.map((key) => t(key))}
              active={month != null ? month - 1 : -1}
              disabled={!editable}
              onSelect={(at) =>
                setForm((current) => ({
                  ...current,
                  fiscalYearStartMonth: String(at + 1),
                }))
              }
            />
          </div>

          {/* THE ONLY TIMESTAMP ON THE SETTINGS PAGE, and it used to be printed
              raw: `Last changed 2026-08-01T00:00:00Z by admin.` — the wire
              value, in UTC, in a format nobody reads at a glance. Now it goes
              through the one formatter, in the zone the signed-in user set on
              their profile, so "last changed" here agrees with every other time
              in the product. `me` is read with the module's own `useMe` hook
              rather than threaded down as a prop: the query is already cached
              by `SettingsPage`, so this costs no extra request and no shared
              file changes. An absent or unloaded zone falls back to the
              browser's, and an absent date renders `NO_DATE` rather than
              "Invalid Date" — though the guard above means it renders nothing
              at all.

              AND `by admin` STOPPED BEING THE ACTOR'S NAME. It was the raw
              id, unstyled, in the one sentence on this panel that names a
              human — design review, 2026-09-11: "usr-helgar is rendered as a
              person". `actorName` above resolves it against the directory
              when this session can read one and the fixture actually holds
              the id; failing either, the id still prints, but as `.mono` —
              the same mark every other raw wire handle in this console
              carries — so it is never mistaken for a name again. */}
          {settings.data?.updated_at ? (
            <p className="note" style={{ marginTop: 10 }}>
              {t("settings.budget.lastChanged", { when: formatDateTime(settings.data.updated_at, me.data?.timezone) })}
              {settings.data.updated_by != null ? (
                <>
                  {` ${t("settings.budget.by")} `}
                  {resolvedActor != null ? (
                    resolvedActor
                  ) : (
                    // Not a name -- an id, and it looks like one: `.mono`,
                    // the same treatment every other raw wire handle in this
                    // console gets, so it is never mistaken for a person's
                    // name the way it was before this fix.
                    <span className="mono" title={settings.data.updated_by}>
                      {settings.data.updated_by}
                    </span>
                  )}
                </>
              ) : null}
              {"."}
            </p>
          ) : null}

          {currencyChanged ? (
            <label className="flex items-start gap-2" style={{ marginTop: 12 }}>
              <input
                type="checkbox"
                checked={acknowledged}
                disabled={!editable}
                onChange={(event) => setAcknowledged(event.target.checked)}
              />
              <span>
                {t("settings.budget.acknowledge")}
              </span>
            </label>
          ) : null}

          {!monthValid && form.fiscalYearStartMonth !== "" ? (
            <p className="form-error" style={{ marginTop: 10 }}>
              <Icon name={KIND_ICON.crit} size={13} /> {t("settings.budget.monthInvalid")}
            </p>
          ) : null}

          <SaveBar
            label={t("settings.budget.save")}
            canEdit={editable && monthValid}
            disabledReason={
              canManage
                ? t("settings.budget.correct")
                : permissionReason(BUDGET_MANAGE)
            }
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
}
