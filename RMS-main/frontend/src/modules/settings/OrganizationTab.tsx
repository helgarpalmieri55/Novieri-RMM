// Organization profile — `GET/PATCH /api/v1/orgs/current`, permission `orgs.manage`.
//
// WHAT IS EDITED HERE, AND WHAT DECIDES THAT. The org NAME, plus one control
// per key the server returns in `settings`. This console holds no copy of the
// key allowlist: the allowlist is enforced server-side, and a second list here
// would drift the first time a key is added or removed — the failure mode
// being a field that looks editable and is silently dropped on write.
// Rendering exactly what `GET` returned means what is on screen is what the
// server admits exists.
//
// THE ROUTE MAY NOT BE DEPLOYED YET. It is being written by a concurrent
// workstream. A 404 renders the form DISABLED with an explicit "not available"
// state — never an empty-but-editable form, which would take an operator's
// typing and drop it. The same state is what an operator sees if the route is
// ever removed, so it is tested rather than assumed.
//
// OMITTED FROM THE DESIGN'S "Organization" TAB, each with what it would need:
//   * Organization slug — no field on any org endpoint. Needs `slug` in the
//     `orgs/current` response and in its allowlist.
//   * Base currency — NOT omitted, but it is not here: it is a real, writable
//     field on `PUT /budget/settings` and is edited on the Budget tab. One
//     server-side setting must not have two forms.
//   * Default timezone / data residency — no endpoint carries either. If the
//     org `settings` allowlist gains them, they appear here automatically,
//     which is the point of deriving the fields from the response.
//   * "Tenant isolation" panel (RLS policy counts, cross-tenant test results)
//     — a claim about the database and the backend suite. Nothing serves those
//     numbers, and a console asserting its own tenant isolation is decoration
//     on a security property, not evidence of it.

// **SAVE EXPLAINS ITSELF INSTEAD OF FAILING QUIETLY.** Auditor (MEDIUM):
// "Organization settings save has no field-level validation feedback." Two
// silences behind that. Emptying the name left Save clickable and PATCHed
// `{"name": ""}`, and the server's 422 came back as the only account of what
// happened; a number key with something non-numeric in it was sent as the raw
// STRING (`Number.isFinite` was false, so the old code fell through to `raw`),
// which is a silent type change on a key the server allowlisted. Both are now
// said on submit, in the words on the screen, through the form kernel.

import { useEffect, useState } from "react";

import { ConfirmDialog } from "../../components/confirm";
import { attemptProblems, Form } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Field, inputClass } from "../../components/ui";
import { KIND_ICON, Panel, Toggle } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { ChipsField } from "../../shell/viz";
import { useCurrentOrg, useUpdateCurrentOrgMutation } from "./api";
import { FOLDING_FORM_GRID } from "./formGrid";
import { LoadFailure } from "./LoadFailure";
import { SaveBar } from "./SaveBar";
import { isDirty, isForbidden, isMissingRoute, permissionReason, refusal } from "./settingsState";
import { orgSettingFields, orgSettingLabel, unrenderableOrgSettingKeys } from "./types";
import type { CurrentOrgUpdate, OrgSettingValue } from "./types";

export const ORGS_MANAGE = "orgs.manage";

/** Form state is all-strings; the stored value's type decides how it is sent back. */
type OrgForm = { name: string; settings: Record<string, string> };

/**
 * The rules, built from the keys THE SERVER RETURNED — the same reason the
 * fields are (see the header): a list written here drifts the first time the
 * allowlist changes.
 *
 * Settings rules are namespaced `settings.<key>` so a server that ever
 * allowlists a key called `name` cannot shadow the org name. The name is never
 * shown; only the message is, and that carries `label`.
 */
function orgRules(fields: ReturnType<typeof orgSettingFields>, form: OrgForm): FieldRule[] {
  const rules: FieldRule[] = [{ name: "name", label: translate("settings.organization.name") }];
  for (const field of fields) {
    // An EMPTY number box means "clear this key" — `submit` sends `null` for
    // it — so it is not missing and gets no rule. A filled one has to be a
    // number the server can store.
    if (field.kind !== "number") continue;
    if ((form.settings[field.key] ?? "").trim() === "") continue;
    rules.push({ name: `settings.${field.key}`, label: field.key, validate: invalidNumber(field.key) });
  }
  return rules;
}

function invalidNumber(key: string): (value: string) => string | null {
  return (value) => {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return translate("settings.organization.numberInvalid", { key, value });
    // Every numeric key this server allowlists so far is a count or a window
    // (`evidence_retention_days`); a negative one is not a smaller setting, it
    // is a value the server will refuse. Stated here rather than as a 422.
    if (parsed < 0) return translate("settings.organization.numberNegative", { key, value });
    return null;
  };
}

function seedFrom(name: string, fields: ReturnType<typeof orgSettingFields>): OrgForm {
  const settings: Record<string, string> = {};
  for (const field of fields) {
    settings[field.key] = field.kind === "boolean" ? String(field.value === true) : String(field.value ?? "");
  }
  return { name, settings };
}

export function OrganizationTab({ canManage }: { canManage: boolean }) {
  const t = useT();
  const org = useCurrentOrg();
  const update = useUpdateCurrentOrgMutation();

  const fields = orgSettingFields(org.data?.settings);
  const unrenderable = unrenderableOrgSettingKeys(org.data?.settings);
  const seed = seedFrom(org.data?.name ?? "", fields);
  const [form, setForm] = useState<OrgForm>(seed);
  // On submit only. A "required" that fires while the org request is still in
  // flight would be complaining about the loading state. Held as DATA (the
  // rules and the values they were checked against) and worded at render, so a
  // language switch re-words it.
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const problems = attemptProblems(attempt);
  /**
   * A rename waiting to be confirmed.
   *
   * AUDIT [MEDIUM], /settings → Organization: "Renaming the organization is a
   * tenant-wide change with no confirmation ... Add a dirty-state indicator, a
   * confirmation for tenant-level identity changes, and a success message in a
   * live region." The dirty state and the announced success came with the form
   * kernel; this is the third part.
   *
   * ONLY THE NAME CONFIRMS. It is the tenant's identity — it appears on every
   * screen, every export and every notification this platform sends. The
   * allowlisted settings keys beside it are ordinary configuration, and putting
   * a modal in front of `evidence_retention_days` would teach operators to
   * click through the one that matters.
   */
  const [renameTo, setRenameTo] = useState<string | null>(null);

  // Re-seed only when the server's values change — after a save, after a
  // refetch. Typing is never overwritten by a background refetch of the same
  // values, because the seed is only compared when it actually differs.
  const seedSignature = JSON.stringify(seed);
  useEffect(() => {
    setForm(JSON.parse(seedSignature) as OrgForm);
    // A complaint about values the server has since replaced is not a
    // complaint about anything on screen.
    setAttempt(null);
  }, [seedSignature]);

  const missing = isMissingRoute(org.error);
  const forbidden = isForbidden(org.error);
  const failed = org.error != null && !missing && !forbidden;
  const dirty = isDirty(form, seed);
  const editable = canManage && org.data != null && !missing;

  // "You may not save at all", with the reason rendered beside the control.
  // The third case is the one the old `canEdit={editable}` swallowed: while the
  // org request is in flight the form is seeded EMPTY, so a save then would
  // PATCH the org name to "".
  const blockedReason = missing
    ? t("settings.organization.blockedMissing")
    : !canManage
      ? permissionReason(ORGS_MANAGE)
      : org.data == null
        ? t("settings.organization.blockedWaiting")
        : undefined;

  function submit() {
    // The rules read only `fields` (the server's keys) and this submit's own
    // snapshot of the form, so the attempt can be re-worded later without
    // reading anything live.
    const snapshot = form;
    const found: Attempt = {
      rules: () => orgRules(fields, snapshot),
      values: {
        name: snapshot.name,
        ...Object.fromEntries(
          Object.entries(snapshot.settings).map(([key, value]) => [`settings.${key}`, value]),
        ),
      },
    };
    setAttempt(found);
    if (attemptProblems(found).length > 0) return;
    // A save with nothing to save is the other silent no-op: SaveBar disabled
    // the button for it and said nothing. It writes an audit entry for a change
    // that is not one, so it is still refused — out loud.
    if (!dirty) {
      setAttempt(refusal("name", "settings.saveBar.noChanges"));
      return;
    }

    // The dialog holds the WHOLE write, not just the name: a submit that also
    // changed a settings key must not apply half of itself while the operator
    // is still deciding about the other half.
    if (form.name.trim() !== seed.name.trim()) {
      setRenameTo(form.name);
      return;
    }
    update.mutate(currentBody());
  }

  /** The PATCH body for what is on screen, built once and used by both paths. */
  function currentBody(): CurrentOrgUpdate {
    const settings: Record<string, OrgSettingValue> = {};
    for (const field of fields) {
      const raw = form.settings[field.key];
      if (raw === undefined) continue;
      // Sent back in the TYPE the server stored it as. A number field that
      // round-trips as a string is a silent schema change on the server's side
      // of an allowlisted key.
      if (field.kind === "number") {
        const value = Number(raw);
        settings[field.key] = raw.trim() === "" ? null : Number.isFinite(value) ? value : raw;
      } else if (field.kind === "boolean") {
        settings[field.key] = raw === "true";
      } else {
        settings[field.key] = raw;
      }
    }
    const body: CurrentOrgUpdate = { name: form.name };
    if (fields.length > 0) body.settings = settings;
    return body;
  }

  return (
    <Panel
      title={t("settings.organization.title")}
      subtitle={t("settings.organization.subtitle")}
    >
      {org.isLoading ? (
        <p className="note">{t("settings.organization.loading")}</p>
      ) : missing ? (
        <div className="notice gold">
          <Icon name={KIND_ICON.warn} size={16} />
          <div>
            <strong>{t("settings.organization.missingTitle")}</strong>
            <br />
            {t("settings.organization.missingBody")}
          </div>
        </div>
      ) : forbidden ? (
        <p className="note">
          {t("settings.organization.forbidden", { code: ORGS_MANAGE })}
        </p>
      ) : failed ? (
        <LoadFailure error={org.error} />
      ) : null}

      {/* A REAL `<form>`, so Enter in the name box saves. There was none: the
          tab was a `<div>` of inputs and a button, which is why every settings
          tab in the audit "does nothing on Enter". */}
      {!forbidden && !failed ? (
        <Form onSubmit={submit}>
          <div className="form-grid" style={{ ...FOLDING_FORM_GRID, marginTop: missing ? 14 : 0 }}>
            <Field label={t("settings.organization.name")}>
              <input
                className={inputClass}
                value={form.name}
                disabled={!editable}
                onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
              />
            </Field>
            {/* THE CAPTION IS THE KEY IN WORDS, AND THAT IS ALL THAT IS ON
                SCREEN NOW. Six of the seven captions on this panel used to be
                the server's raw `snake_case` -- `date_format`,
                `support_email` -- beside the one human label, "Organization
                name"; a later pass gave every control a human caption AND
                kept the raw key underneath it in `mono`, which is what the
                design review (`docs/design/settings-language.md`) caught:
                this was the only tab left printing wire keys in a code font,
                on the very first screen Settings opens to. Deriving the
                fields from the allowlist (which is the point of this tab, and
                does not change) never required printing its identifiers at
                all -- the caption already says which server-admitted setting
                each control is. */}
            {fields.map((field) =>
              field.kind === "choice" ? (
                // NEITHER `choice` NOR `boolean` BELOW GOES THROUGH `Field`,
                // AND THE REASON IS NO LONGER THE ACCESSIBLE NAME -- `Field`
                // has carried a `help` slot outside its `<label>` since
                // `4c7d22e`, and review finding 4 moved the plain-text arm
                // below onto it. `Chips` and `Toggle` stay off `Field`
                // because each is its OWN focusable control with its own
                // accessible name and its own click target; nested inside
                // `Field`'s wrapping `<label>`, a click on the control would
                // also fire the label's click, which is the same "button
                // nested in a <label> is two things claiming the same click"
                // conflict `Toggle`'s own comment below documents (the rule
                // `patch/PatchPolicyEditor` already follows).
                //
                // A CLOSED VOCABULARY IS PICKED FROM, NOT TYPED. `date_format`
                // was a text box holding `dmy`: the one value on this panel a
                // person could neither read nor guess the alternatives to,
                // beside six they could. The server declares the set
                // (`SettingSpec.choices`), so the console names it in words and
                // sends the token back unchanged.
                //
                // CHIPS, WHICH IS WHAT v3 DRAWS FOR A SHORT ENUMERATION ON THIS
                // VERY SCREEN: `field('Reboot',chips(['If required','Never',
                // 'Forced'],0))` on `P['settings']`. A `<select>` here would be
                // the browser's widget painted through the kit's box, which is
                // the defect this panel already lost once when its boolean
                // stopped being a "true"/"false" dropdown.
                //
                // `ChipsField` (`viz.tsx`) IS THIS CALL SITE, `EmailTab`'s
                // AND `BudgetTab`'s, CLOSED INTO ONE. The `id={`org-setting-
                // ${field.key}`}` this used to carry on its own `<span>` had
                // no `aria-labelledby` pointed at it -- an id with nothing
                // reading it, which is what `ChipsField` now actually does
                // with the id it generates. And `disabled={!editable}` is a
                // genuine fix, not just a refactor: `onSelect` used to
                // silently no-op on `!editable` while every chip stayed a
                // live, focusable, clickable button -- the read-only render
                // looked editable and swallowed the click. `Chips` carries a
                // real disabled state now, so this field is disabled the way
                // every input two cases below already is.
                <ChipsField
                  key={field.key}
                  caption={orgSettingLabel(field.key)}
                  full
                  items={(field.choices ?? []).map((choice) => choice.label)}
                  active={(field.choices ?? []).findIndex(
                    (choice) => choice.value === (form.settings[field.key] ?? ""),
                  )}
                  disabled={!editable}
                  onSelect={(at) => {
                    const choice = (field.choices ?? [])[at];
                    if (choice === undefined) return;
                    setForm((current) => ({
                      ...current,
                      settings: { ...current.settings, [field.key]: choice.value },
                    }));
                  }}
                  // NO `help={field.key}` ANY MORE (design review, 2026-09-11:
                  // "the tab the operator lands on prints six database keys in
                  // a code font"). The caption already names the decision in
                  // words; the wire key underneath told the operator nothing
                  // they could act on and read as exactly the "looks like db
                  // tables" complaint that started this pass. Nobody outside
                  // this codebase needs the key from this screen -- an
                  // integration reads the API, not a settings tab's help
                  // text -- so this is removal, the same call the notification
                  // event cell already made, not a disclosure.
                />
              ) : field.kind === "boolean" ? (
                // A SWITCH, NOT A DROPDOWN OF "true" / "false". The only two
                // options were the literal wire enums, which is the server's
                // vocabulary again; v3 draws a boolean with `.tog` and the kit
                // ships it. `Toggle` carries its own label and its own
                // `aria-checked`, so it replaces the `Field` rather than
                // sitting inside one -- a button nested in a `<label>` is two
                // things claiming the same click (the rule
                // `patch/PatchPolicyEditor` already follows). `.full` so the
                // row spans the grid instead of leaving a half-empty cell.
                <div className="field full" key={field.key}>
                  <Toggle
                    label={orgSettingLabel(field.key)}
                    checked={(form.settings[field.key] ?? "false") === "true"}
                    disabled={!editable}
                    onChange={(next) =>
                      setForm((current) => ({
                        ...current,
                        settings: { ...current.settings, [field.key]: String(next) },
                      }))
                    }
                  />
                  {/* No `<span className="help mono">{field.key}</span>` any
                      more -- see the ChipsField branch above for the reason;
                      this is the same removal, not a second decision. */}
                </div>
              ) : (
                // `Field`, WITH NO `help` ANY MORE. It used to carry the raw
                // key as a monospace description (`4c7d22e` gave `Field` the
                // slot for exactly that); the design review that removed
                // `mono` from every other tab's raw wire values named this
                // panel as the last one still printing them, and the six keys
                // here are ordinary settings an operator never needs to match
                // against anything. `Field` renders a plain `.field` with no
                // description when `help` is omitted -- the ~300 other call
                // sites that pass none, unchanged.
                <Field key={field.key} label={orgSettingLabel(field.key)}>
                  <input
                    className={inputClass}
                    type={field.kind === "number" ? "number" : "text"}
                    value={form.settings[field.key] ?? ""}
                    disabled={!editable}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        settings: { ...current.settings, [field.key]: event.target.value },
                      }))
                    }
                  />
                </Field>
              ),
            )}
          </div>

          {org.data && fields.length === 0 ? (
            <p className="note" style={{ marginTop: 10 }}>
              {t("settings.organization.noKeys")}
            </p>
          ) : null}

          {unrenderable.length > 0 ? (
            <p className="note" style={{ marginTop: 10 }}>
              {t("settings.organization.unrenderable", { keys: unrenderable.join(", ") })}
            </p>
          ) : null}

          {/* THE DIRTY-STATE INDICATOR, audit [MEDIUM]'s first ask on this tab.
              NOT a live region: it is the state of the form as the operator
              arrives at it, not an interruption — the same distinction
              `ai-automation/AutonomyDialPage` draws, and the reason the
              announced roles here stay reserved for the outcome of a save. */}
          {dirty && editable ? (
            <p className="form-hint" style={{ marginTop: 12 }}>
              {t("settings.organization.unsaved")}
            </p>
          ) : null}

          {/* THE SAME COMMIT CONTROL AS EVERY OTHER SECTION ON THIS PAGE.
              This was `FormActions` with the label "Save" and no glyph, while
              the eight sections below it saved through `SaveBar`: "Save
              changes" with the check, which is what v3 draws. `submit` keeps
              Enter-in-the-name-box saving, and `requireDirty={false}` keeps
              this tab's own rule that a clean save is refused OUT LOUD rather
              than by a dead button.

              THE ERROR GOES IN RAW: `describeSubmitError` unwraps FastAPI's
              `detail`, names the field on each validation problem, and answers
              a bodyless 5xx with a sentence instead of a status. Wrapping it in
              `new Error(...)` would downgrade the `ApiError` to a plain `Error`
              and throw away the status the kernel reads. */}
          <SaveBar
            submit
            canEdit={blockedReason === undefined}
            disabledReason={blockedReason}
            dirty={dirty}
            requireDirty={false}
            pending={update.isPending}
            error={update.error}
            problems={problems}
            // Unchanged rule: `saved` is the server's 2xx and nothing else, and
            // it disappears the moment the form is edited again.
            saved={update.isSuccess && !dirty}
          />
        </Form>
      ) : null}

      <ConfirmDialog
        open={renameTo != null}
        title={t("settings.organization.rename.title")}
        subject={`${seed.name} → ${renameTo ?? ""}`}
        body={t("settings.organization.rename.body")}
        confirmLabel={t("settings.organization.rename.confirm")}
        onConfirm={() => {
          setRenameTo(null);
          update.mutate(currentBody());
        }}
        onCancel={() => setRenameTo(null)}
      />
    </Panel>
  );
}
