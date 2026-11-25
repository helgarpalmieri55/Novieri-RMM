import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { Form, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Field, Table, Td, Th, inputClass } from "../../components/ui";
import { WeekPainter } from "../../components/weekpainter";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { CREATE_GROUP_CTA, useDeviceGroupsQuery } from "../../lib/detectors";
import { humaniseEnum } from "../../lib/labels";
import { usePatchPolicies, usePatchPolicyMutation } from "./api";
import { CRON_ZONE_NOTE, cronProblem, describeCron } from "./cron";
import type { PatchPolicy, PatchPolicyInput } from "./types";
import {
  Btn,
  FormPage,
  FormSection,
  Panel,
  QueryState,
  SaveBar,
  StatusPill,
  Toggle,
  useDraft,
  useUnsavedGuard,
  useUrlFilters,
} from "../../shell/components";
import { Icon } from "../../shell/icons";

// The wire vocabulary is never|if_required|forced; "always" was offered here
// and made every apply for the policy fail validation.
const REBOOT_OPTIONS = ["never", "if_required", "forced"] as const;


/**
 * `if_required` IS A WIRE VALUE AND WAS REACHING THE SCREEN AS ONE — in the
 * table's Reboot column and in the dialog's own select, so the operator picked
 * an identifier and then read it back. This module already has a test named
 * "maps every backend state to a label with no underscore in it" for
 * `device_patch_status`; the policy's reboot mode was the enum that never got
 * the same treatment. `humaniseEnum` is the shared answer (`lib/labels.ts`),
 * written after the audits filed this same defect in seven modules under seven
 * headings.
 */
const rebootLabel = (value: string): string => humaniseEnum(value, "PatchReboot");

/**
 * A whole number in `[lo, hi]`, as a `FieldRule.validate`.
 *
 * `Number("")` is 0 and `Number("abc")` is NaN, and the old form fed both
 * straight into `setForm` — a scan interval the operator half-typed became 0,
 * which the server stores and the scheduler reads as "scan continuously".
 */
function wholeNumber(lo: number, hi: number) {
  return (value: string): string | null => {
    if (!/^\d+$/.test(value)) return translate("patch.policy.wholeNumber");
    const parsed = Number(value);
    if (parsed < lo || parsed > hi) return translate("patch.policy.betweenNumbers", { lo, hi });
    return null;
  };
}

// Labels are what the dialog prints beside the control, never the wire name.
// Every message `requiredProblems` builds reads "<label> is required" or
// "<label> must be …", so the rule labels ARE the error copy.
// A function, not a constant: the labels and messages are worded when the rules
// run, which is at render (see `Attempt`), so they follow the language.
const policyRules = (): readonly FieldRule[] => [
  { name: "ring_name", label: translate("patch.policy.ringName") },
  { name: "ring_order", label: translate("patch.policy.ringOrder"), validate: wholeNumber(0, 10_000) },
  {
    name: "reboot_deferral_hours",
    label: translate("patch.policy.warnHours"),
    validate: wholeNumber(0, 168),
  },
  { name: "scan_interval_hours", label: translate("patch.policy.scanHours"), validate: wholeNumber(1, 8_760) },
];

/**
 * The form's own state, held as STRINGS.
 *
 * `PatchPolicyInput` types the numbers as numbers, and binding a number input
 * straight to it means every keystroke passes through `Number()`: clearing the
 * field yields 0, and a partially-typed value yields NaN, both of which then
 * render back into the input. Strings until submit, converted once, after the
 * rules have run.
 */
interface PolicyForm {
  /** The device group the policy governs, `""` for the organisation default. */
  group: string;
  ring_name: string;
  ring_order: string;
  maintenance_window: string;
  auto_approve_security: boolean;
  reboot: string;
  reboot_deferral_hours: string;
  scan_interval_hours: string;
}

function toForm(policy: PatchPolicy | null): PolicyForm {
  return policy
    ? {
        group: policy.device_group_id ?? "",
        ring_name: policy.ring_name,
        ring_order: String(policy.ring_order),
        maintenance_window: policy.maintenance_window ?? "",
        auto_approve_security: policy.auto_approve_security,
        reboot: policy.reboot,
        reboot_deferral_hours: String(policy.reboot_deferral_hours),
        scan_interval_hours: String(policy.scan_interval_hours),
      }
    : {
        group: "",
        ring_name: "broad",
        ring_order: "100",
        maintenance_window: "",
        auto_approve_security: false,
        reboot: "if_required",
        reboot_deferral_hours: "24",
        scan_interval_hours: "24",
      };
}

function toInput(form: PolicyForm, deviceGroupId: string | null): PatchPolicyInput {
  return {
    device_group_id: deviceGroupId,
    ring_name: form.ring_name.trim(),
    ring_order: Number(form.ring_order),
    maintenance_window: form.maintenance_window.trim() || null,
    auto_approve_security: form.auto_approve_security,
    reboot: form.reboot,
    reboot_deferral_hours: Number(form.reboot_deferral_hours),
    scan_interval_hours: Number(form.scan_interval_hours),
  };
}

/** `?policy=` names the policy being edited, `new` a new one; absent, none is open. */
const NEW_POLICY = "new";

/**
 * PATCH POLICIES ON THE FORM PATTERN (redesign plan, Receta FO; O3 3.2): the
 * policies are a list, and the one being edited is a form below it, in
 * sections (the ring, the maintenance window, approval and restart), with the
 * console's one save bar. It was a dialog over the list.
 *
 * THE POLICY OPEN IS IN THE URL (`?policy=<id>`, `?policy=new`), so a link
 * opens it and Back closes it; with a change in it, leaving the policy (Back,
 * another row's Edit, another page) asks first (`search: ["policy"]`).
 *
 * WHAT A SAVE SENDS IS UNCHANGED: the same `PUT /patch/policies` body, built by
 * `toInput` after the same rules ran, and the same confirmation before "New"
 * replaces a group's or the organisation's policy (the route upserts on
 * `device_group_id`).
 */
export function PatchPolicyEditor() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canManage = (me.data?.permissions ?? []).includes("patch.policy.manage");

  const policies = usePatchPolicies();
  // Held here, above the form, so "Saved." outlives the form a new policy
  // swaps for the stored one.
  const upsert = usePatchPolicyMutation();
  const groups = useDeviceGroupsQuery();
  const groupName = useGroupName(groups.data?.items);

  const url = useUrlFilters(["policy"] as const);
  const open = url.values.policy;
  const creating = open === NEW_POLICY;
  const editedPolicy = creating ? null : (policies.data?.find((policy) => policy.id === open) ?? null);
  // A policy is open once the list has named it; a stale id opens nothing.
  const editing = canManage && (creating || editedPolicy !== null);

  function openPolicy(policy: PatchPolicy | null) {
    upsert.reset();
    url.set("policy", policy?.id ?? NEW_POLICY);
  }

  return (
    <FormPage
      back={{ to: "/patch", label: t("patch.policy.back") }}
      title={t("patch.policy.title")}
      description={t("patch.policy.description")}
    >
      {/* `Panel bare` -- the same `.panel` the rest of this module's tables
          sit in, and `bare` is what makes a table sit flush inside one. The
          page's one primary action, New policy, is the list's. */}
      <div className="mb-4">
        <Panel
          title={t("patch.policy.listTitle")}
          bare
          actions={
            canManage ? (
              // Secondary: on a form page the save bar's Save is the one primary (DESIGN.md, "Buttons").
              <Btn label={t("patch.policy.newPolicy")} icon="plus" onClick={() => openPolicy(null)} />
            ) : undefined
          }
        >
          <QueryState
            loading={policies.isLoading}
            error={policies.error}
            empty={policies.data != null && policies.data.length === 0}
            emptyIcon="patch"
            emptyLabel={t("patch.policy.empty")}
          >
          <Table label={t("patch.policy.listTitle")}>
            <thead>
              <tr>
                <Th>{t("patch.policy.ring")}</Th>
                <Th>{t("patch.policy.order")}</Th>
                <Th>{t("patch.policy.windowCol", { zone: CRON_ZONE_NOTE })}</Th>
                <Th>{t("patch.policy.autoApproveSecurity")}</Th>
                <Th>{t("patch.policy.reboot")}</Th>
                <Th>{t("patch.policy.scanIntervalCol")}</Th>
                <Th>{""}</Th>
              </tr>
            </thead>
            <tbody>
              {policies.data?.map((policy) => (
                <tr key={policy.id} aria-current={policy.id === open ? "true" : undefined}>
                  <Td lead={<Icon name="adjustments" size={18} />}>
                    <div className="cell-title">{policy.ring_name}</div>
                    <div className="cell-sub">
                      {groupName(policy.device_group_id)}
                    </div>
                  </Td>
                  <Td>
                    <span className="mono">{policy.ring_order}</span>
                  </Td>
                  {/* THE WINDOW AS A SENTENCE, WITH THE EXPRESSION UNDER IT.
                      `describeCron` reads it with the painter's own parser, so
                      the list and the editor cannot disagree about what a
                      stored expression means; an expression the painter cannot
                      draw says so here rather than being given an invented
                      reading. The raw value stays, in the sub line, because it
                      is what an operator pastes into a ticket. */}
                  <Td>
                    {policy.maintenance_window ? (
                      <>
                        <div className="cell-title">
                          {describeCron(policy.maintenance_window) ?? t("patch.policy.customExpression")}
                        </div>
                        <div className="cell-sub mono">{policy.maintenance_window}</div>
                      </>
                    ) : (
                      <span className="note">{t("patch.policy.alwaysOpen")}</span>
                    )}
                  </Td>
                  <Td>
                    {policy.auto_approve_security ? (
                      <StatusPill value="enabled" kind="ok" label={t("patch.policy.yes")} />
                    ) : (
                      <StatusPill value="disabled" kind="mut" label={t("patch.policy.no")} />
                    )}
                  </Td>
                  <Td>{rebootLabel(policy.reboot)}</Td>
                  <Td>
                    <span className="mono">{policy.scan_interval_hours}</span>
                  </Td>
                  <Td>
                    {canManage && (
                      <div className="flex justify-end">
                        {/* NAMED with the row it belongs to: a policy list whose
                            every control reads "Edit" is a list of identical
                            buttons to a screen reader. */}
                        <Btn
                          label={t("patch.policy.edit")}
                          icon="edit"
                          ariaLabel={t("patch.policy.editAria", { ring: policy.ring_name })}
                          small
                          onClick={() => openPolicy(policy)}
                        />
                      </div>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
          </QueryState>
        </Panel>
      </div>

      {editing ? (
        // KEYED BY THE POLICY OPEN: another policy is another form, with its
        // own draft, never the last one's change carried over.
        <PolicyForm
          key={open}
          policy={editedPolicy}
          policies={policies.data ?? []}
          liveGroups={(groups.data?.items ?? []).filter((group) => group.archived_at === null)}
          groupsListed={groups.isSuccess}
          groupsFailed={groups.isError}
          groupName={groupName}
          upsert={upsert}
          onStored={(stored) => url.set("policy", stored.id, { replace: true })}
          onClose={() => url.set("policy", "", { replace: true })}
        />
      ) : null}
    </FormPage>
  );
}

/** A group's name for the policy list and the form, "Organisation default" for none. */
function useGroupName(items: readonly { id: string; name: string }[] | undefined): (id: string | null) => string {
  const t = useT();
  return (id) =>
    id == null
      ? t("patch.policy.orgDefault")
      : (items?.find((group) => group.id === id)?.name ?? t("patch.policy.deviceGroup"));
}

/**
 * THE POLICY OPEN, NAMED AS THE DIALOG WAS ("New patch policy", "Edit patch
 * policy"), in three sections and one save bar. A real `<form>`, so Enter in a
 * field saves, as it did.
 */
function PolicyForm({
  policy,
  policies,
  liveGroups,
  groupsListed,
  groupsFailed,
  groupName,
  upsert,
  onStored,
  onClose,
}: {
  /** The policy edited, `null` for a new one. */
  policy: PatchPolicy | null;
  policies: readonly PatchPolicy[];
  /** The groups a policy can be aimed at: archived ones are not offered. */
  liveGroups: readonly { id: string; name: string }[];
  groupsListed: boolean;
  groupsFailed: boolean;
  groupName: (id: string | null) => string;
  upsert: ReturnType<typeof usePatchPolicyMutation>;
  /** A new policy was stored: it becomes the one open. */
  onStored: (stored: PatchPolicy) => void;
  onClose: () => void;
}) {
  const t = useT();
  const creating = policy === null;
  // The refused submission as rules + values, worded at render (see `Attempt`).
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  // Holds the validated input while the operator confirms an overwrite. Null
  // means nothing is waiting on an answer.
  const [pendingOverwrite, setPendingOverwrite] = useState<PatchPolicyInput | null>(null);

  /* THE FORM AGAINST WHAT THE SERVER HAS (`useDraft`). A new policy starts
     from the defaults and is unsaved as it stands, so its bar offers Save at
     once; leaving asks only once something was changed. */
  const { draft, setDraft, dirty: changed, discard, markSent } = useDraft<PolicyForm>(toForm(policy));
  const form = draft ?? toForm(policy);
  const setForm = (next: PolicyForm) => setDraft(next);
  const { guard, release } = useUnsavedGuard(changed, { search: ["policy"] });

  // The policy this save would land on. `PUT /patch/policies` is an UPSERT
  // keyed on `device_group_id` (api.py:408-425). "New policy" for a group --
  // or for the organisation default -- that already has one does not create a
  // second row: it REPLACES that one, and the operator is told before it
  // happens rather than after.
  const existingPolicy = policies.find((item) => (item.device_group_id ?? "") === form.group) ?? null;

  function save(input: PatchPolicyInput) {
    setPendingOverwrite(null);
    markSent(form);
    upsert.mutate(input, {
      onSuccess: (stored) => {
        // A new policy becomes the one open, and its bar says "Saved.".
        // Released first: the guard reads the change from the render before
        // the save.
        if (creating) {
          release();
          onStored(stored);
        }
      },
      onError: () => markSent(null),
    });
  }

  function submit() {
    // The window is optional, so its rule exists only when something is typed;
    // the flag is read from the snapshot below, never from live state.
    const windowTyped = form.maintenance_window.trim() !== "";
    const next: Attempt = {
      rules: () => [
        ...policyRules(),
        ...(windowTyped
          ? [
              {
                name: "maintenance_window",
                label: translate("patch.policy.window"),
                validate: (value: string) => cronProblem(value),
              },
            ]
          : []),
      ],
      values: {
        ring_name: form.ring_name,
        ring_order: form.ring_order,
        reboot_deferral_hours: form.reboot_deferral_hours,
        scan_interval_hours: form.scan_interval_hours,
        maintenance_window: form.maintenance_window,
      },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    const input = toInput(form, form.group === "" ? null : form.group);
    if (creating && existingPolicy) {
      setPendingOverwrite(input);
      return;
    }
    save(input);
  }

  const title = creating ? t("patch.policy.dialogNew") : t("patch.policy.dialogEdit");

  // THE FORM COMES INTO VIEW WHEN IT OPENS. It opens below the list, and at
  // a phone's width (or under a long list) Edit would otherwise change
  // nothing on screen -- the very complaint that made it a dialog once
  // (Auditor A: "a click that did open it looked exactly like a click that
  // did nothing"). Guarded: jsdom has no `scrollIntoView`.
  const section = useRef<HTMLElement>(null);
  useEffect(() => {
    const node = section.current;
    if (node && typeof node.scrollIntoView === "function") node.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

  return (
    <section aria-label={title} ref={section}>
      {guard}
      <Form onSubmit={submit}>
        <FormSection title={title} icon="adjustments">
          {creating ? null : (
            <p className="note mb-3">{t("patch.policy.appliesTo", { group: groupName(policy.device_group_id) })}</p>
          )}
          <div className="form-grid">
            {creating ? (
              // WHICH DEVICES THIS POLICY GOVERNS, chosen before anything
              // else: the upsert is keyed on it, so it is also what decides
              // whether Save adds a ring or replaces one.
              <>
                <Field label={t("patch.policy.appliesToField")} full>
                  <select
                    className={inputClass}
                    value={form.group}
                    onChange={(event) => setForm({ ...form, group: event.target.value })}
                  >
                    <option value="">{t("patch.policy.everyDevice")}</option>
                    {liveGroups.map((group) => (
                      <option key={group.id} value={group.id}>
                        {group.name}
                      </option>
                    ))}
                  </select>
                </Field>
                <p className="form-hint">
                  {form.group === "" ? t("patch.policy.hintOrg") : t("patch.policy.hintGroup")}
                  {groupsListed && liveGroups.length === 0 ? (
                    <>
                      {" "}
                      {t("patch.policy.noGroups")}{" "}
                      <Link to={CREATE_GROUP_CTA.to}>{CREATE_GROUP_CTA.label}</Link>
                    </>
                  ) : groupsFailed ? (
                    ` ${t("patch.policy.groupsError")}`
                  ) : null}
                </p>
              </>
            ) : null}
            <Field label={t("patch.policy.ringName")}>
              <input
                className={inputClass}
                value={form.ring_name}
                onChange={(event) => setForm({ ...form, ring_name: event.target.value })}
              />
            </Field>
            <Field label={t("patch.policy.ringOrder")}>
              <input
                type="number"
                className={inputClass}
                value={form.ring_order}
                onChange={(event) => setForm({ ...form, ring_order: event.target.value })}
              />
            </Field>
          </div>
        </FormSection>

        <FormSection title={t("patch.policy.window")} icon="calendar-event">
          {/* THE WINDOW IS PAINTED, NOT TYPED. `WeekPainter` is v3's week
              grid, and it emits the five-field expression the matcher reads;
              the raw expression stays reachable under Advanced, and a stored
              value the grid cannot draw opens there. `cronProblem` still runs
              on submit. NOT A `<label>`: the grid is 168 buttons, and a label
              would name only the first. The zone is stated, as it always was:
              the window is matched against UTC (`patch/apply.py`). */}
          <div className="field full">
            <span>{t("patch.policy.windowCaption", { zone: CRON_ZONE_NOTE })}</span>
            <WeekPainter
              label={t("patch.policy.window")}
              value={form.maintenance_window || null}
              onChange={(cron) => setForm({ ...form, maintenance_window: cron ?? "" })}
              emptyHint={t("patch.policy.emptyHint")}
            />
          </div>
        </FormSection>

        <FormSection title={t("patch.policy.approvalSection")} icon="refresh">
          <div className="form-grid">
            <Field label={t("patch.policy.reboot")}>
              <select
                className={inputClass}
                value={form.reboot}
                onChange={(event) => setForm({ ...form, reboot: event.target.value })}
              >
                {REBOOT_OPTIONS.map((option) => (
                  <option key={option} value={option}>
                    {rebootLabel(option)}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t("patch.policy.warnHours")} help={t("patch.policy.warnHelp")}>
              <input
                type="number"
                className={inputClass}
                value={form.reboot_deferral_hours}
                onChange={(event) => setForm({ ...form, reboot_deferral_hours: event.target.value })}
              />
            </Field>
            <Field label={t("patch.policy.scanHours")}>
              <input
                type="number"
                className={inputClass}
                value={form.scan_interval_hours}
                onChange={(event) => setForm({ ...form, scan_interval_hours: event.target.value })}
              />
            </Field>
            {/* A `Toggle`, NOT a `Field` around a checkbox: a standing setting
                that is on or off, with its own label and `aria-checked`. */}
            <div className="field full">
              <Toggle
                label={t("patch.policy.autoApproveToggle")}
                checked={form.auto_approve_security}
                onChange={(next) => setForm({ ...form, auto_approve_security: next })}
              />
            </div>
          </div>
        </FormSection>

        {/* THE ONE SAVE BAR, inside the form so Enter saves. It says what the
            old buttons said: the server's refusal in a sentence (a bodyless
            503 included), the form's own problems, "Saved." after the 2xx.
            Discard closes a new policy and puts an edited one back. */}
        <SaveBar
          submit
          dirty={creating || changed}
          saving={upsert.isPending}
          error={upsert.error}
          problems={attemptProblems(attempt)}
          saved={upsert.isSuccess && !changed}
          onDiscard={() => {
            setAttempt(null);
            upsert.reset();
            discard();
            if (creating) {
              // Nothing to put back: the new policy goes, and so does its form.
              release();
              onClose();
            }
          }}
        />
      </Form>

      {/* A REPLACEMENT the operator did not ask for. "New policy" for a group
          (or the org default) that already has one overwrites the policy
          governing those devices. The subject is the ring being replaced,
          because that is the name on the row about to change. */}
      <ConfirmDialog
        open={pendingOverwrite != null}
        title={
          form.group === ""
            ? t("patch.policy.replaceOrgTitle")
            : t("patch.policy.replaceGroupTitle", { group: groupName(form.group) })
        }
        subject={existingPolicy ? existingPolicy.ring_name : ""}
        body={
          form.group === ""
            ? t("patch.policy.replaceOrgBody", {
                ring: form.ring_name.trim(),
                reboot: rebootLabel(form.reboot).toLowerCase(),
              })
            : t("patch.policy.replaceGroupBody", {
                group: groupName(form.group),
                ring: form.ring_name.trim(),
                reboot: rebootLabel(form.reboot).toLowerCase(),
              })
        }
        confirmLabel={t("patch.policy.replaceLabel")}
        destructive
        onCancel={() => setPendingOverwrite(null)}
        onConfirm={() => {
          if (pendingOverwrite) save(pendingOverwrite);
        }}
      />
    </section>
  );
}
