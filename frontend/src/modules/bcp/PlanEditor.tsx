import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { Button, Dialog, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { internalClientId, requireKnown } from "../../lib/internalClient";
import {
  useCreatePlanMutation,
  useProcedureArticles,
  useScopeDevices,
  useScopeGroups,
  useUpdatePlanMutation,
  useUserTimezone,
} from "./api";
import {
  labelForOption,
  procedurePickerEmptyHint,
  procedurePickerOptions,
  scopeDevicePickerEmptyHint,
  scopeDevicePickerOptions,
  scopeGroupPickerEmptyHint,
  scopeGroupPickerOptions,
} from "./pickerOptions";
import type { PlanOut, PlanScopeEntry } from "./types";
import { Icon } from "../../shell/icons";
import { ADD_COMPUTER_TO } from "../../shell/nav";
import { KIND_ICON } from "../../shell/viz";

// Creates or edits a BCP/DR plan (name/description, scope, RTO/RPO,
// procedure refs, review interval). Mirrors modules/change's
// ChangeRequestForm shape: `plan` is either "new", an existing PlanOut to
// edit, or null (closed).

// Auditor (HIGH), /bcp → New plan → Save: "Empty submit is a silent no-op."
// The cause was `disabled={mutation.isPending || !name}` -- the one field the
// server insists on was the one the dead button refused to name. Save stays
// enabled now and answers through this list; the labels are the words ON
// SCREEN, so "Name is required" reads back to the field being looked at.
//
// `review_interval_days` is here because it is required in a second, quieter
// way: `submit` sends `Number(reviewIntervalDays)`, and `Number("") === 0`, so
// clearing the field used to enrol the plan as permanently overdue for review
// rather than refusing the save.
const planBaseRules = (): readonly FieldRule[] => [
  { name: "name", label: translate("bcp.editor.name") },
  {
    name: "review_interval_days",
    label: translate("bcp.editor.reviewInterval"),
    validate: (value) => {
      if (!/^\d+$/.test(value)) return translate("bcp.editor.intervalWhole");
      if (Number(value) === 0) return translate("bcp.editor.intervalMin");
      return null;
    },
  },
];

// Auditor (HIGH), /bcp → plan → RTO / RPO: "Negative RTO and RPO are accepted
// and displayed as recovery objectives." Nothing else stops them --
// `bcp/schema.py` types both as a bare `int | None` with no bound, so a plan
// promising recovery -90 minutes after the outage persisted and then read back
// out onto the plan list as the org's stated objective.
//
// The label is baked into each message because `FieldRule.validate` sees only
// the value, and "cannot be negative" under a dialog with three number fields
// does not say which one.
//
// Zero is allowed on purpose: an RPO of 0 is "lose nothing", which is a real
// objective an org can hold, so this rule may not demand a minimum of 1.
function recoveryObjectiveRule(name: string, labelKey: MessageKey): FieldRule {
  const label = translate(labelKey);
  return {
    name,
    label,
    validate: (value) => {
      if (/^-/.test(value)) return translate("bcp.editor.negative", { label });
      if (!/^\d+$/.test(value)) return translate("bcp.editor.wholeMinutes", { label });
      return null;
    },
  };
}

// One mechanism for "missing" and for "malformed" (form.tsx's
// `requiredProblems` runs both), so RTO/RPO -- genuinely optional server-side
// (`rto_minutes: int | None = None`) -- are expressed as rules that only apply
// once the operator has typed something, never as a second check bolted on
// beside the first. Same shape as modules/licenses' `licenseRules`.
function planRules(rto: string, rpo: string): readonly FieldRule[] {
  return [
    ...planBaseRules(),
    ...(rto.trim() ? [recoveryObjectiveRule("rto_minutes", "bcp.editor.rto")] : []),
    ...(rpo.trim() ? [recoveryObjectiveRule("rpo_minutes", "bcp.editor.rpo")] : []),
  ];
}

/** A scope kind in the words on screen; a kind nobody catalogued is shown as it came. */
function scopeKindWord(kind: string): string {
  return kind === "device" || kind === "group"
    ? translate(`bcp.scopeKind.${kind}` as MessageKey)
    : kind;
}

// bcp_plan.scope entry kinds exactly as the backend accepts them
// (backend/app/modules/bcp/service.py's `_SCOPE_KIND_TO_MODEL`:
// {"device": Device, "group": DeviceGroup}; anything else raises
// `BcpValidationError` -> HTTP 400). This dialog seeded every new scope row
// with the kind "device_group", which is not one of them -- so adding a scope
// entry and pressing Save could only ever produce a refusal, and the free-text
// "kind" box invited a dozen more spellings that would do the same.
const SCOPE_KINDS = ["device", "group"] as const;
type ScopeKind = (typeof SCOPE_KINDS)[number];

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// A scope row with no id resolves to nothing and 400s ("scope ref not found"),
// which used to be the only way to discover the row was incomplete. Both halves
// answer through the one problem list, in the words on screen.
function scopeRules(scope: readonly PlanScopeEntry[]): FieldRule[] {
  return scope.map((row, index) => ({
    name: `scope_${index}`,
    label: translate("bcp.editor.scopeRuleLabel", { n: index + 1, kind: scopeKindWord(row.kind) }),
    validate: (value) =>
      UUID_PATTERN.test(value)
        ? null
        : translate("bcp.editor.scopeInvalid", { n: index + 1, value }),
  }));
}

function scopeValues(scope: readonly PlanScopeEntry[]): Record<string, string> {
  return Object.fromEntries(scope.map((row, index) => [`scope_${index}`, row.id]));
}

export function PlanEditor({
  plan,
  onClose,
}: {
  plan: PlanOut | "new" | null;
  onClose: () => void;
}) {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const create = useCreatePlanMutation();
  const update = useUpdatePlanMutation();

  const existing = plan === "new" || plan == null ? null : plan;

  const [name, setName] = useState(existing?.name ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [scope, setScope] = useState<PlanScopeEntry[]>(existing?.scope ?? []);
  const [rtoMinutes, setRtoMinutes] = useState(existing?.rto_minutes?.toString() ?? "");
  const [rpoMinutes, setRpoMinutes] = useState(existing?.rpo_minutes?.toString() ?? "");
  // A LIST, not a comma-separated string. Every entry has to be a
  // `doc_articles.id` (see pickerOptions.ts's header), so the string form could
  // not be typed correctly by hand and the plan could not be saved at all.
  const [procedureRefs, setProcedureRefs] = useState<string[]>(existing?.procedure_refs ?? []);
  const [procedurePick, setProcedurePick] = useState("");
  const [reviewIntervalDays, setReviewIntervalDays] = useState(
    existing?.review_interval_days.toString() ?? "365",
  );
  // Filled ON SUBMIT, never while typing: an "RTO (minutes) must be a whole
  // number of minutes" that appears mid-keystroke is its own kind of unhelpful.
  // Held as data (the rules and the values they judged), so a language switch
  // re-words the sentences; the rules read only that snapshot.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const mutation = existing ? update : create;

  // Both lists are fetched only while the dialog is open -- see ./api. The
  // device list is what makes a scope row saveable and the article list is what
  // makes a procedure reference possible at all.
  const open = plan != null;
  const zone = useUserTimezone();
  const devices = useScopeDevices(open);
  const groups = useScopeGroups(open);
  const articles = useProcedureArticles(open);
  // Built at render, never memoised: the sublabels hold translated words.
  const deviceOptions = scopeDevicePickerOptions(devices.data ?? []);
  const groupOptions = scopeGroupPickerOptions(groups.data ?? []);
  const articleOptions = procedurePickerOptions(articles.data ?? [], zone);
  const noArticles = articles.data != null && articles.data.length === 0;
  // The same "the list answered and is empty" test for the two scope lists,
  // shown only for a kind a scope row is actually using: the link is the way
  // to fill the picker the operator is looking at, not a checklist.
  const noDevices =
    devices.data != null && deviceOptions.length === 0 && scope.some((row) => row.kind === "device");
  const noGroups =
    groups.data != null && groupOptions.length === 0 && scope.some((row) => row.kind === "group");

  // A recovery objective that restores service more slowly than it loses data
  // is incoherent: RPO is how much data may be lost, RTO how long recovery may
  // take, and RTO < RPO says the system is back before the data it lost. A
  // WARNING and not a refusal -- both numbers are legal server-side and an org
  // may be mid-way through revising them; the audit asked for "a sanity
  // warning", and refusing the save would be inventing a rule the platform
  // does not hold.
  const objectivesIncoherent =
    /^\d+$/.test(rtoMinutes.trim()) &&
    /^\d+$/.test(rpoMinutes.trim()) &&
    Number(rtoMinutes) < Number(rpoMinutes);

  function reset() {
    setName("");
    setDescription("");
    setScope([]);
    setRtoMinutes("");
    setRpoMinutes("");
    setProcedureRefs([]);
    setProcedurePick("");
    setReviewIntervalDays("365");
    setAttempt(null);
  }

  // PlansPage renders this component unconditionally, so it stays mounted with
  // the dialog shut and mutation state OUTLIVES the dialog: without the resets
  // a failed Save leaves `mutation.error` set and the next open announces the
  // refusal of a request nobody has made yet. Same for a stale problem list.
  function close() {
    setAttempt(null);
    create.reset();
    update.reset();
    onClose();
  }

  function submit() {
    const rto = rtoMinutes;
    const rpo = rpoMinutes;
    const scopeSnapshot = scope;
    const next: Attempt = {
      rules: () => [...planRules(rto, rpo), ...scopeRules(scopeSnapshot)],
      values: {
        name,
        rto_minutes: rtoMinutes,
        rpo_minutes: rpoMinutes,
        review_interval_days: reviewIntervalDays,
        ...scopeValues(scope),
      },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    const body = {
      // Trimmed to match what was validated -- `requiredProblems` trims before
      // testing emptiness, so an untrimmed send could carry a name the checks
      // never saw.
      name: name.trim(),
      description,
      scope,
      rto_minutes: rtoMinutes.trim() === "" ? null : Number(rtoMinutes),
      rpo_minutes: rpoMinutes.trim() === "" ? null : Number(rpoMinutes),
      procedure_refs: procedureRefs,
      review_interval_days: Number(reviewIntervalDays),
    };

    const onSuccess = () => {
      reset();
      onClose();
    };

    if (existing) {
      update.mutate({ id: existing.id, body }, { onSuccess });
    } else {
      // A plan is one client's (MSP clients phase 2), and that client must use
      // continuity: the server's 404 says so in a sentence, which
      // `FormActions` shows as it is.
      // PHASE 3: the client/site selector. Until it exists that is the
      // organisation's internal client, whose id is the organisation's --
      // `/me`'s `org_id` (`lib/internalClient.ts`).
      const internalClient = internalClientId(me.data);
      create.mutate(() => ({ ...body, client_id: requireKnown(internalClient) }), { onSuccess });
    }
  }

  function addScopeRow() {
    // "device", not "device_group": `_SCOPE_KIND_TO_MODEL` has never had a key
    // by that name, so every row this dialog created started out unsaveable.
    setScope([...scope, { kind: "device", id: "" }]);
  }

  function updateScopeRow(index: number, patch: Partial<PlanScopeEntry>) {
    setScope(scope.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  }

  function removeScopeRow(index: number) {
    setScope(scope.filter((_, i) => i !== index));
  }

  return (
    <Dialog
      open={plan != null}
      onClose={close}
      title={plan === "new" ? t("bcp.editor.titleNew") : t("bcp.editor.titleEdit")}
    >
      {/* A real <form>, so Enter in the Name field saves instead of doing
          nothing -- the other half of the same finding. */}
      <Form onSubmit={submit}>
        {/* Auditor B (MEDIUM, B87): "All six controls report `required ===
            false`." Two of the six actually are, and only those two are marked
            -- RTO and RPO are genuinely optional server-side (`rto_minutes:
            int | None = None`, bcp/schema.py) and marking a field the API
            accepts empty is the same lie pointing the other way. The sentence
            is the one the change module uses, so a reader who learns the
            convention once has learned it everywhere. */}
        <p className="note mb-3">{t("bcp.editor.requiredNote")}</p>
        <div className="form-grid">
          <Field label={t("bcp.editor.nameRequired")} full>
            {/* `required` states the same fact for assistive tech. It blocks
                nothing: <Form> is `noValidate`, because a native bubble that
                vanishes on the next click is the silent no-op wearing a hat.
                `PLAN_RULES` is what the user reads. */}
            <input
              className={inputClass}
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
          <Field label={t("bcp.editor.description")} full>
            <textarea
              className={inputClass}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
            />
          </Field>

          {/* The kit's `.field > span` caption, over a group the `Field`
              component cannot wrap because it is a list, not one control. */}
          <div className="field full">
            <span>{t("bcp.editor.scope")}</span>
            {scope.map((row, index) => (
              <div key={index} className="mb-2 flex items-end gap-2">
                <Field label={t("bcp.editor.scopeKind", { n: index + 1 })}>
                  {/* A SELECT, because the server accepts exactly two values and
                      the free-text box offered infinitely many -- including the
                      "device_group" this dialog used to seed. Switching kind
                      drops the id: a device id is not a group id, and carrying it
                      across submits an id of the wrong kind against a type the
                      server resolves in a different table. */}
                  <select
                    className={inputClass}
                    value={row.kind}
                    onChange={(e) =>
                      updateScopeRow(index, { kind: e.target.value as ScopeKind, id: "" })
                    }
                  >
                    {SCOPE_KINDS.map((kind) => (
                      <option key={kind} value={kind}>
                        {scopeKindWord(kind)}
                      </option>
                    ))}
                  </select>
                </Field>
                {row.kind === "device" ? (
                  <EntityPicker
                    label={t("bcp.editor.scopeDevice", { n: index + 1 })}
                    options={deviceOptions}
                    loading={devices.isLoading}
                    emptyHint={scopeDevicePickerEmptyHint()}
                    placeholder={t("bcp.editor.searchDevices")}
                    value={row.id}
                    onChange={(id) => updateScopeRow(index, { id })}
                  />
                ) : (
                  <EntityPicker
                    label={t("bcp.editor.scopeGroup", { n: index + 1 })}
                    options={groupOptions}
                    loading={groups.isLoading}
                    emptyHint={scopeGroupPickerEmptyHint()}
                    placeholder={t("bcp.editor.searchGroups")}
                    value={row.id}
                    onChange={(id) => updateScopeRow(index, { id })}
                  />
                )}
                {/* `type="button"` is load-bearing now that there is a form
                    around these: a button with no type inside a <form> IS a
                    submit button, so editing the scope would save the plan. */}
                <Button type="button" variant="ghost" onClick={() => removeScopeRow(index)}>
                  {t("bcp.editor.remove")}
                </Button>
              </div>
            ))}
            <Button type="button" variant="outline" onClick={addScopeRow}>
              {t("bcp.editor.addScope")}
            </Button>
            {noDevices && (
              <p className="mt-1">
                <Link className="underline" to={ADD_COMPUTER_TO}>
                  {t("bcp.editor.addComputer")}
                </Link>
              </p>
            )}
            {noGroups && (
              <p className="mt-1">
                <Link className="underline" to="/device-groups">
                  {t("bcp.editor.createGroup")}
                </Link>
              </p>
            )}
          </div>

          {/* `min={0}` is the same bound stated to the spinner and to the mobile
              keypad; the refusal a typed or pasted "-90" gets is RTO_RULE's. */}
          <Field label={t("bcp.editor.rto")}>
            <input
              type="number"
              min={0}
              className={inputClass}
              value={rtoMinutes}
              onChange={(e) => setRtoMinutes(e.target.value)}
            />
          </Field>
          {/* Auditor B (MEDIUM): "RTO and RPO are routinely confused even by
              practitioners; a form that offers neither definition nor example
              invites incorrect data into a compliance record." These two numbers
              are quoted into audit evidence and customer contracts. */}
          <p className="note">{t("bcp.editor.rtoNote")}</p>
          <Field label={t("bcp.editor.rpo")}>
            <input
              type="number"
              min={0}
              className={inputClass}
              value={rpoMinutes}
              onChange={(e) => setRpoMinutes(e.target.value)}
            />
          </Field>
          <p className="note">{t("bcp.editor.rpoNote")}</p>
          {objectivesIncoherent && (
            <div role="status" className="notice gold">
              <Icon name={KIND_ICON.warn} size={17} />
              <div>{t("bcp.editor.incoherent")}</div>
            </div>
          )}

          <div className="field full">
            <span>{t("bcp.editor.procedures")}</span>
            {/* CHOSEN, NOT TYPED. `_validate_procedure_refs` resolves every entry
                against `doc_articles.id` org-scoped, so the old
                "Procedure refs (comma-separated)" text box could not produce a
                value the server would accept -- a plan with anything typed in it
                could not be saved. */}
            {procedureRefs.length > 0 ? (
              <ul className="mb-2">
                {procedureRefs.map((ref) => (
                  <li key={ref} className="mb-1 flex items-center gap-2">
                    <span>{labelForOption(articleOptions, ref)}</span>
                    <Button
                      type="button"
                      variant="ghost"
                      onClick={() => setProcedureRefs(procedureRefs.filter((r) => r !== ref))}
                    >
                      {t("bcp.editor.remove")}
                    </Button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="note mb-2">{t("bcp.editor.noProcedures")}</p>
            )}
            <EntityPicker
              label={t("bcp.editor.addProcedure")}
              options={articleOptions.filter((option) => !procedureRefs.includes(option.id))}
              loading={articles.isLoading}
              emptyHint={procedurePickerEmptyHint()}
              placeholder={t("bcp.editor.searchDocs")}
              value={procedurePick}
              onChange={(id) => {
                // Chosen IS added: a picker that only stages a value needs a
                // second control to commit it, and the one this replaced had
                // nothing of the kind.
                setProcedureRefs([...procedureRefs, id]);
                setProcedurePick("");
              }}
            />
            {noArticles && (
              <p className="mt-1">
                <Link className="underline" to="/docs">
                  {t("bcp.editor.writeProcedure")}
                </Link>
              </p>
            )}
          </div>
          <Field label={t("bcp.editor.reviewIntervalRequired")}>
            <input
              type="number"
              min={1}
              className={inputClass}
              required
              value={reviewIntervalDays}
              onChange={(e) => setReviewIntervalDays(e.target.value)}
            />
          </Field>
        </div>

        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={close}>
            {t("common.actions.cancel")}
          </Button>
          {/* Enabled with the fields empty ON PURPOSE -- the refusal is what
              the user gets to read, and a control that will not move is not.
              The server failure comes from here too, so a bodyless 503 still
              produces a sentence and `role="alert"` announces it, replacing
              the `{String(mutation.error)}` line that rendered a raw object. */}
          <FormActions
            label={t("bcp.editor.save")}
            pending={mutation.isPending}
            error={mutation.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
