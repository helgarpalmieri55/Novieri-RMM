// Endpoint health — `GET /health/settings` (`health.read`), `PUT
// /health/settings` (`health.admin`).
//
// EVERY FIELD `SettingsUpdate` ACCEPTS IS HERE. Eight bounded integers, the
// two id fields that decide whether an alert can become a ticket, the
// monitoring deep-link template, and the auto-resolve flag. A settings page
// that edited six of the twelve would leave the other six configurable only by
// editing the server, which is the thing this page exists to stop.
//
// THE BOUNDS ARE AN AFFORDANCE, NOT A VALIDATION. `SETTING_BOUNDS` mirrors the
// server's `SETTING_BOUNDS`, the service re-checks every one with a typed 400
// naming the bound it broke, and a database CHECK constraint owns the fact.
// The `min`/`max` attributes here save a round trip; they decide nothing.
//
// `alert_tickets_enabled` IS SHOWN BECAUSE IT IS A SILENT FAILURE. The alert
// pipeline only declares its `open_ticket` action when BOTH the default queue
// and the default ticket category are set; an org missing either gets alerts
// that never become tickets, and nothing anywhere errors.

import { useEffect, useId, useState } from "react";
import { Link } from "react-router-dom";

import { DurationInput } from "../../components/duration";
import { EntityPicker } from "../../components/picker";
import type { PickerOption } from "../../components/picker";
import { Badge, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { FieldGroup, KIND_ICON, Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { NUMERIC_FIELDS } from "../health/fields";
import { SETTING_BOUNDS } from "../health/types";
import type { SettingsOut, SettingsUpdate } from "../health/types";
import { useHealthSettings, useTicketCategories, useTicketQueues, useUpdateHealthSettingsMutation, useGlobalWriteReason } from "./api";
import { FOLDING_FORM_GRID } from "./formGrid";
import { LoadFailure } from "./LoadFailure";
import { SaveBar } from "./SaveBar";
import { isDirty, isForbidden, parseIntegerField } from "./settingsState";

export const HEALTH_READ = "health.read";
export const HEALTH_ADMIN = "health.admin";

// `NUMERIC_FIELDS` (label, help, `kind`, and a count field's `unit`) lives in
// `../health/fields.ts` -- ONE definition, imported here AND by
// `../health/HealthSettingsPanel.tsx`, which renders the identical table on
// the operational Health page. The two screens holding byte-identical copies
// of all eight rows was the exact failure mode
// `docs/design/settings-language.md` names, fixed here the same way the
// notification event-type labels were fixed: by deletion, not by comment.
interface HealthForm {
  numbers: Record<string, string>;
  defaultAlertQueueId: string;
  defaultTicketCategoryId: string;
  monitoringDeepLinkTemplate: string;
  autoResolveAlertTickets: boolean;
}

function seedFrom(data: SettingsOut | undefined): HealthForm {
  const numbers: Record<string, string> = {};
  for (const field of NUMERIC_FIELDS) {
    numbers[field.key] = data ? String(data[field.key]) : "";
  }
  return {
    numbers,
    defaultAlertQueueId: data?.default_alert_queue_id ?? "",
    defaultTicketCategoryId: data?.default_ticket_category_id ?? "",
    monitoringDeepLinkTemplate: data?.monitoring_deep_link_template ?? "",
    autoResolveAlertTickets: data?.auto_resolve_alert_tickets ?? false,
  };
}

/**
 * An id the form is holding that the fetched list does not contain.
 *
 * `listUnavailable` must mean "the list did not load" — ANY error, not only a
 * 403 — because the only thing this function is allowed to declare orphaned
 * is an id that a list which actually arrived does not contain. A 500, a
 * gateway timeout or a dropped connection is not "forbidden", and treating it
 * as resolved-and-absent would print "this queue no longer exists" about a
 * queue nobody has looked up yet (review finding 1).
 */
function unresolvedId(
  id: string,
  listUnavailable: boolean,
  loading: boolean,
  options: readonly PickerOption[],
): boolean {
  if (id === "" || listUnavailable || loading) return false;
  return !options.some((option) => option.id === id);
}

export function HealthTab({ canRead, canAdmin }: { canRead: boolean; canAdmin: boolean }) {
  const t = useT();
  const reason = useGlobalWriteReason(HEALTH_ADMIN);
  const settings = useHealthSettings(canRead);
  const update = useUpdateHealthSettingsMutation();
  // CROSS-MODULE READS (see `useTicketQueues`/`useTicketCategories` in
  // `./api.ts`): both can 403 for a `health.admin` holder who lacks
  // `ticket.read`, and that must not block editing the eight fields that do
  // not depend on either list. `includeInactive: true` — a queue or category
  // the org DEACTIVATED is still a real row, and a stored default pointing at
  // one must resolve, not read as deleted (review finding 4; the same reason
  // `ServiceDeskAdminPage.tsx` fetches the same way).
  const queues = useTicketQueues(canRead, true);
  const categories = useTicketCategories(canRead, true);

  const seed = seedFrom(settings.data);
  const [form, setForm] = useState<HealthForm>(seed);

  const seedSignature = JSON.stringify(seed);
  useEffect(() => {
    setForm(JSON.parse(seedSignature) as HealthForm);
  }, [seedSignature]);

  const forbidden = isForbidden(settings.error);
  const failed = settings.error != null && !forbidden;
  const dirty = isDirty(form, seed);
  const editable = canAdmin && settings.data != null;

  // A box the operator emptied or filled with something that is not a whole
  // number blocks the save with a named reason. It is never silently coerced
  // to zero, and it is never dropped from the body so that the field keeps its
  // old value while the save reports success.
  const invalidKeys = NUMERIC_FIELDS.filter(
    (field) => parseIntegerField(form.numbers[field.key] ?? "") == null,
  ).map((field) => field.key);

  function submit() {
    const body: SettingsUpdate = {};
    for (const field of NUMERIC_FIELDS) {
      const value = parseIntegerField(form.numbers[field.key] ?? "");
      if (value == null) return;
      body[field.key] = value;
    }
    // "" is a cleared id, which is `null` — "no default queue" — not the
    // absence of an opinion.
    body.default_alert_queue_id =
      form.defaultAlertQueueId === "" ? null : form.defaultAlertQueueId;
    body.default_ticket_category_id =
      form.defaultTicketCategoryId === "" ? null : form.defaultTicketCategoryId;
    body.monitoring_deep_link_template =
      form.monitoringDeepLinkTemplate === "" ? null : form.monitoringDeepLinkTemplate;
    body.auto_resolve_alert_tickets = form.autoResolveAlertTickets;
    update.mutate(body);
  }

  // `queuesForbidden`/`categoriesForbidden` decide the WORDING of the
  // degrade box's help text (a permission problem reads differently from an
  // outage). `queuesUnavailable`/`categoriesUnavailable` decide whether to
  // degrade AT ALL, and whether an id can be declared orphaned: the list
  // failed to load, full stop, whatever the status code (review finding 1).
  //
  // `&& queues.data === undefined` (review finding 3): a REFETCH that fails
  // with a good list already cached must not tear a working picker down and
  // replace it with a raw id box. `queues.error` stays set on a failed
  // background refetch even though `queues.data` still holds the last good
  // result, so degrading on the error alone would throw away a perfectly
  // usable cached list over one transient failure. It never falsely claims
  // the queue is gone either way -- `queuesUnavailable` gates the raw-id
  // branch, not `unresolvedId()` below -- but costing the operator their
  // picker over a transient 500 is a real degradation of the control.
  const queuesForbidden = isForbidden(queues.error);
  const categoriesForbidden = isForbidden(categories.error);
  const queuesUnavailable = queues.error != null && queues.data === undefined;
  const categoriesUnavailable = categories.error != null && categories.data === undefined;

  // "No default" is a value, not an absence of one -- `""` means exactly
  // that server-side (see `submit()` above), so it gets an explicit option
  // rather than being what an empty search box happens to mean.
  // Built at render, not remembered: the first option and the "inactive"
  // sub-label are worded, and a memo would keep them in the language they were
  // built in.
  const queueOptions: PickerOption[] = [
    { id: "", label: t("settings.health.noQueue") },
    ...(queues.data ?? []).map((queue) => ({
      id: queue.id,
      label: queue.name,
      sublabel: queue.active ? queue.key : t("settings.health.inactiveSub", { key: queue.key }),
    })),
  ];
  const categoryOptions: PickerOption[] = [
    { id: "", label: t("settings.health.noCategory") },
    ...(categories.data ?? []).map((category) => ({
      id: category.id,
      label: category.name,
      sublabel: category.active ? category.key : t("settings.health.inactiveSub", { key: category.key }),
    })),
  ];

  // Fetched-and-empty, as distinct from "not fetched yet" and from "could not
  // be fetched" (that last one degrades to the raw-id box above and never
  // reaches here) — the one state in which "no queues/categories exist yet"
  // is a claim this tab actually knows to be true (review finding 2).
  const queuesEmpty = !queuesUnavailable && !queues.isLoading && (queues.data ?? []).length === 0;
  const categoriesEmpty =
    !categoriesUnavailable && !categories.isLoading && (categories.data ?? []).length === 0;

  // A SETTINGS PAGE MUST NOT DISCARD A VALUE IT CANNOT RENDER: a deleted
  // queue or category is a configuration to correct, not a field to
  // silently blank. `form.defaultAlertQueueId`/`defaultTicketCategoryId`
  // keep whatever id the org has stored even when it matches no option below
  // -- these two booleans only decide whether the orphan note renders.
  const alertQueueOrphaned = unresolvedId(
    form.defaultAlertQueueId,
    queuesUnavailable,
    queues.isLoading,
    queueOptions,
  );
  const ticketCategoryOrphaned = unresolvedId(
    form.defaultTicketCategoryId,
    categoriesUnavailable,
    categories.isLoading,
    categoryOptions,
  );

  // Review finding 3: a screen-reader user on the picker's own combobox
  // heard nothing about an id the list could not resolve -- the note was a
  // sighted-only sibling. `picker.tsx`'s `describedBy` points the input at
  // whichever of these actually renders below.
  const alertQueueOrphanId = useId();
  const ticketCategoryOrphanId = useId();

  return (
    <Panel
      title={t("settings.health.title")}
      subtitle={t("settings.health.subtitle")}
    >
      {settings.isLoading ? (
        <p className="note">{t("settings.health.loading")}</p>
      ) : forbidden ? (
        <p className="note">
          {t("settings.health.forbidden", { code: HEALTH_READ })}
        </p>
      ) : failed ? (
        <LoadFailure error={settings.error} />
      ) : (
        <>
          {settings.data ? (
            <div className="flex flex-wrap items-center gap-2" style={{ marginBottom: 14 }}>
              <Badge variant={settings.data.configured ? "muted" : "warning"}>
                {settings.data.configured ? t("settings.health.configured") : t("settings.health.defaults")}
              </Badge>
              <Badge variant={settings.data.alert_tickets_enabled ? "success" : "warning"}>
                {settings.data.alert_tickets_enabled ? t("settings.health.ticketsOn") : t("settings.health.ticketsOff")}
              </Badge>
              {/* The fix is on this tab, so the hint is a link to it rather
                  than "below" (docs/design/cross-module-guidance.md §2). */}
              {settings.data.alert_tickets_enabled ? null : (
                <a href="#alert-tickets">{t("settings.health.pickQueue")}</a>
              )}
            </div>
          ) : null}

          <FieldGroup title={t("settings.health.groups.timing")} icon="adjustments">
            <div className="form-grid" style={FOLDING_FORM_GRID}>
              {NUMERIC_FIELDS.map((field) => {
                const bounds = SETTING_BOUNDS[field.key];
                return (
                  <Field key={field.key} label={field.label} help={field.help}>
                    {field.kind === "duration" ? (
                      // Keyed on the SEED, not the live edit: this remounts
                      // (and re-derives its displayed unit from the stored
                      // seconds) only when a fresh value arrives from the
                      // server -- initial load, or a refetch after save --
                      // never on every keystroke. See `duration.tsx`'s own
                      // comment on why that distinction matters.
                      <DurationInput
                        key={seedSignature}
                        value={form.numbers[field.key] ?? ""}
                        bounds={bounds}
                        disabled={!editable}
                        label={field.label}
                        unitLabel={t("settings.health.unitLabel", { label: field.label })}
                        onChange={(next) =>
                          setForm((current) => ({
                            ...current,
                            numbers: { ...current.numbers, [field.key]: next },
                          }))
                        }
                      />
                    ) : (
                      <input
                        className={inputClass}
                        type="number"
                        min={bounds?.[0]}
                        max={bounds?.[1]}
                        value={form.numbers[field.key] ?? ""}
                        disabled={!editable}
                        onChange={(event) =>
                          setForm((current) => ({
                            ...current,
                            numbers: { ...current.numbers, [field.key]: event.target.value },
                          }))
                        }
                      />
                    )}
                  </Field>
                );
              })}
            </div>
          </FieldGroup>

          <div id="alert-tickets">
          <FieldGroup title={t("settings.health.groups.alerting")} icon="heartbeat">
            <div className="form-grid" style={FOLDING_FORM_GRID}>
              {queuesUnavailable ? (
                <Field
                  label={t("settings.health.queueIdLabel")}
                  help={
                    queuesForbidden
                      ? t("settings.health.queueIdForbidden")
                      : t("settings.health.queueIdFailed")
                  }
                >
                  <input
                    className={inputClass}
                    value={form.defaultAlertQueueId}
                    disabled={!editable}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, defaultAlertQueueId: event.target.value }))
                    }
                  />
                </Field>
              ) : (
                // ONE GRID CELL, NOT TWO. `EntityPicker` and its hint used to
                // be separate direct children of `.form-grid` -- two grid
                // items where Task 1's whole argument for `.field-wrap` was
                // that a control and its hint are one. Wrapped here, they are
                // (review finding 2).
                <div className="picker-group">
                  <EntityPicker
                    label={t("settings.health.queueLabel")}
                    options={queueOptions}
                    loading={queues.isLoading}
                    placeholder={t("settings.health.searchQueues")}
                    value={form.defaultAlertQueueId}
                    disabled={!editable}
                    // Review finding 8: `emptyHint` is unreachable here --
                    // the synthetic "No default queue" entry means
                    // `options.length` is never 0 -- so it is no longer
                    // passed at all.
                    describedBy={alertQueueOrphaned ? alertQueueOrphanId : undefined}
                    onChange={(id) =>
                      setForm((current) => ({ ...current, defaultAlertQueueId: id }))
                    }
                  />
                  {queuesEmpty ? (
                    <p className="form-hint">
                      <Trans
                        k="settings.queuePicker.none"
                        components={{ link: <Link to="/service-desk/admin" /> }}
                      />
                    </p>
                  ) : null}
                  {alertQueueOrphaned ? (
                    <p className="form-hint" id={alertQueueOrphanId}>
                      {t("settings.health.queueOrphan", { id: form.defaultAlertQueueId })}
                    </p>
                  ) : null}
                </div>
              )}

              {categoriesUnavailable ? (
                <Field
                  label={t("settings.health.categoryIdLabel")}
                  help={
                    categoriesForbidden
                      ? t("settings.health.categoryIdForbidden")
                      : t("settings.health.categoryIdFailed")
                  }
                >
                  <input
                    className={inputClass}
                    value={form.defaultTicketCategoryId}
                    disabled={!editable}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        defaultTicketCategoryId: event.target.value,
                      }))
                    }
                  />
                </Field>
              ) : (
                <div className="picker-group">
                  <EntityPicker
                    label={t("settings.health.categoryLabel")}
                    options={categoryOptions}
                    loading={categories.isLoading}
                    placeholder={t("settings.health.searchCategories")}
                    value={form.defaultTicketCategoryId}
                    disabled={!editable}
                    describedBy={ticketCategoryOrphaned ? ticketCategoryOrphanId : undefined}
                    onChange={(id) =>
                      setForm((current) => ({ ...current, defaultTicketCategoryId: id }))
                    }
                  />
                  {categoriesEmpty ? (
                    <p className="form-hint">
                      <Trans
                        k="settings.queuePicker.noCategories"
                        components={{ link: <Link to="/service-desk/admin" /> }}
                      />
                    </p>
                  ) : null}
                  {ticketCategoryOrphaned ? (
                    <p className="form-hint" id={ticketCategoryOrphanId}>
                      {t("settings.health.categoryOrphan", { id: form.defaultTicketCategoryId })}
                    </p>
                  ) : null}
                </div>
              )}
            </div>
          </FieldGroup>
          </div>
          <FieldGroup title={t("settings.health.groups.monitoring")} icon="activity">
            <div className="form-grid" style={FOLDING_FORM_GRID}>
              <Field label={t("settings.health.deepLink")} help={t("settings.health.blankForNone")}>
                <input
                  className={inputClass}
                  value={form.monitoringDeepLinkTemplate}
                  disabled={!editable}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      monitoringDeepLinkTemplate: event.target.value,
                    }))
                  }
                />
              </Field>
            </div>
          </FieldGroup>

          <label className="flex items-center gap-2" style={{ marginTop: 14 }}>
            <input
              type="checkbox"
              checked={form.autoResolveAlertTickets}
              disabled={!editable}
              onChange={(event) =>
                setForm((current) => ({
                  ...current,
                  autoResolveAlertTickets: event.target.checked,
                }))
              }
            />
            {t("settings.health.autoResolve")}
          </label>

          {invalidKeys.length > 0 ? (
            <p className="form-error" style={{ marginTop: 10 }}>
              <Icon name={KIND_ICON.crit} size={13} />{" "}
              {t("settings.health.invalid", { keys: invalidKeys.join(", ") })}
            </p>
          ) : null}

          <SaveBar
            label={t("settings.health.save")}
            canEdit={editable && invalidKeys.length === 0}
            disabledReason={
              canAdmin
                ? t("settings.health.correct")
                : reason
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
