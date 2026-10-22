import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
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
import { Btn, PageHead, Panel, QueryState, StatusPill, Toggle } from "../../shell/components";
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
        ring_name: policy.ring_name,
        ring_order: String(policy.ring_order),
        maintenance_window: policy.maintenance_window ?? "",
        auto_approve_security: policy.auto_approve_security,
        reboot: policy.reboot,
        reboot_deferral_hours: String(policy.reboot_deferral_hours),
        scan_interval_hours: String(policy.scan_interval_hours),
      }
    : {
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

export function PatchPolicyEditor() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canManage = (me.data?.permissions ?? []).includes("patch.policy.manage");

  const policies = usePatchPolicies();
  const upsert = usePatchPolicyMutation();
  // The groups a policy can be aimed at (`GET /device-groups`, devices.read).
  // Archived groups are not offered: nothing new should be aimed at one.
  const groups = useDeviceGroupsQuery();
  const liveGroups = (groups.data?.items ?? []).filter((group) => group.archived_at === null);
  const groupName = (id: string | null): string =>
    id == null
      ? t("patch.policy.orgDefault")
      : (groups.data?.items.find((group) => group.id === id)?.name ?? t("patch.policy.deviceGroup"));
  // "" is the organisation default (`device_group_id: null`).
  const [groupId, setGroupId] = useState("");

  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<PolicyForm>(toForm(null));
  // The refused submission as rules + values, worded at render (see `Attempt`).
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  // Holds the validated input while the operator confirms an overwrite. Null
  // means nothing is waiting on an answer.
  const [pendingOverwrite, setPendingOverwrite] = useState<PatchPolicyInput | null>(null);

  const editing = editingId != null;
  const creating = editingId === "new";
  const editingPolicyGroup =
    policies.data?.find((policy) => policy.id === editingId)?.device_group_id ?? null;

  // The policy this save would land on. `PUT /patch/policies` is an UPSERT
  // keyed on `device_group_id` (api.py:408-425). "New policy" for a group --
  // or for the organisation default -- that already has one does not create a
  // second row: it REPLACES that one, and the operator is told before it
  // happens rather than after. (Until 2026-09-24 this console could only
  // address the default, on the stale belief that no route lists device
  // groups; `GET /device-groups` does.)
  const existingPolicy =
    policies.data?.find((policy) => (policy.device_group_id ?? "") === groupId) ?? null;

  function startEdit(policy: PatchPolicy | null) {
    setAttempt(null);
    upsert.reset();
    setEditingId(policy?.id ?? "new");
    setGroupId(policy?.device_group_id ?? "");
    setForm(toForm(policy));
  }

  function close() {
    setAttempt(null);
    setPendingOverwrite(null);
    upsert.reset();
    setEditingId(null);
  }

  function save(input: PatchPolicyInput) {
    setPendingOverwrite(null);
    upsert.mutate(input, { onSuccess: () => setEditingId(null) });
  }

  // ESCAPE CLOSES IT, and the page behind it stops scrolling. Auditor A filed
  // both against the deployment dialog next door; this is the same `Dialog`
  // with the same two gaps. Held OFF while the overwrite confirmation is
  // stacked on top: `ConfirmDialog` has its own document-level Escape handler
  // and does not stop propagation, so one keypress would otherwise cancel the
  // confirmation AND throw the half-typed policy away behind it.

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

    const input = toInput(form, groupId === "" ? null : groupId);
    if (creating && existingPolicy) {
      setPendingOverwrite(input);
      return;
    }
    save(input);
  }

  return (
    <div>
      <PageHead
        title={t("patch.policy.title")}
        description={t("patch.policy.description")}
        actions={
          canManage ? (
            <Btn label={t("patch.policy.newPolicy")} icon="plus" variant="primary" onClick={() => startEdit(null)} />
          ) : undefined
        }
      />

      {/* `Panel bare` -- the same `.panel` the rest of this module's tables
          sit in, and `bare` is what makes a table sit flush inside one. */}
      <div className="mb-4">
        <Panel bare>
          <QueryState
            loading={policies.isLoading}
            error={policies.error}
            empty={policies.data != null && policies.data.length === 0}
            emptyIcon="patch"
            emptyLabel={t("patch.policy.empty")}
          >
          <Table>
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
                <tr key={policy.id}>
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
                      This cell used to print `0 2-5 * * 6` and nothing else,
                      one row above another saying "always open" in words --
                      the same fact in two languages, and the machine's was
                      winning. `describeCron` reads it with the painter's own
                      parser, so the list and the editor behind Edit cannot
                      disagree about what a stored expression means; an
                      expression the painter cannot draw says so here rather
                      than being given an invented reading. The raw value
                      stays, in the sub line, because it is what an operator
                      pastes into a ticket and checks against the matcher. */}
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
                        {/* `Btn small`, the density every other row control in
                            this module uses, and NAMED with the row it belongs
                            to: a policy list whose every control reads "Edit"
                            is a list of identical buttons to a screen reader. */}
                        <Btn
                          label={t("patch.policy.edit")}
                          icon="edit"
                          ariaLabel={t("patch.policy.editAria", { ring: policy.ring_name })}
                          small
                          onClick={() => startEdit(policy)}
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

      {/* THE BLOCKER, Auditor A: "'New policy' is completely non-functional —
          patch policies cannot be created at all ... no form, no dialog, no
          navigation, no POST."

          The editor was a `<Card>` rendered BELOW the table and below the
          empty state, outside the viewport on the page the audit measured, so
          a click that did open it looked exactly like a click that did
          nothing. A dialog cannot be missed, and it is what the finding's own
          fix asks for ("wire the button to the policy-creation form/modal").
          The controls sit in a real `<form>` too, so Enter submits — the
          `type="submit"`-outside-a-form pattern the audit called out on this
          very button. */}
      <Dialog
        open={canManage && editing}
        onClose={close}
        // OFF while the overwrite confirmation is stacked on top: ConfirmDialog
        // binds its own document-level Escape and does not stop propagation,
        // so one keypress would answer the confirmation AND discard the
        // half-typed policy behind it.
        dismissible={pendingOverwrite == null}
        title={creating ? t("patch.policy.dialogNew") : t("patch.policy.dialogEdit")}
      >
        <Form onSubmit={submit}>
          {creating ? null : (
            <p className="note mb-3">{t("patch.policy.appliesTo", { group: groupName(editingPolicyGroup) })}</p>
          )}
          <div className="form-grid">
            {creating ? (
              // WHICH DEVICES THIS POLICY GOVERNS, chosen before anything else:
              // the upsert is keyed on it, so it is also what decides whether
              // Save adds a ring or replaces one. An operator who believes they
              // are adding a `pilot` ring alongside `broad` sees here whether
              // they are, not from an unchanged table after.
              <>
                <Field label={t("patch.policy.appliesToField")} full>
                  <select
                    className={inputClass}
                    value={groupId}
                    onChange={(event) => setGroupId(event.target.value)}
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
                  {groupId === "" ? t("patch.policy.hintOrg") : t("patch.policy.hintGroup")}
                  {groups.isSuccess && liveGroups.length === 0 ? (
                    <>
                      {" "}
                      {t("patch.policy.noGroups")}{" "}
                      <Link to={CREATE_GROUP_CTA.to}>{CREATE_GROUP_CTA.label}</Link>
                    </>
                  ) : groups.isError ? (
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
            {/* THE WINDOW IS PAINTED, NOT TYPED. This field used to be a text
                box whose own placeholder (`e.g. Sat 02:00-04:00`) was not
                something `window_open` could parse, so a policy saved by
                following the field's example gated nothing. v3 draws the week
                as a grid and says "drag to paint"; `WeekPainter` is that grid,
                and it emits the five-field expression the matcher reads. The
                raw expression stays reachable under Advanced, and a stored
                value the grid cannot draw opens there. `cronProblem` still
                runs on submit, whichever way the value arrived.

                NOT A `<label>`: the grid is twenty-four times seven buttons,
                and a label wrapping it would name only the first. The caption
                is the design's `.field > span`; the grid names itself. The
                zone is stated here as it always was: the window is matched
                against UTC (`patch/apply.py`). */}
            <div className="field full">
              <span>{t("patch.policy.windowCaption", { zone: CRON_ZONE_NOTE })}</span>
              <WeekPainter
                label={t("patch.policy.window")}
                value={form.maintenance_window || null}
                onChange={(cron) => setForm({ ...form, maintenance_window: cron ?? "" })}
                emptyHint={t("patch.policy.emptyHint")}
              />
            </div>
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
            <Field
              label={t("patch.policy.warnHours")}
              help={t("patch.policy.warnHelp")}
            >
              <input
                type="number"
                className={inputClass}
                value={form.reboot_deferral_hours}
                onChange={(event) =>
                  setForm({ ...form, reboot_deferral_hours: event.target.value })
                }
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
            {/* A `Toggle`, NOT a `Field` around a checkbox. This is a
                standing setting that is on or off, which is what v3's `.tog`
                draws; it stayed a bare checkbox only because the kit had no
                switch. `Toggle` carries its own label and its own
                `aria-checked`, so it replaces the `Field` rather than sitting
                inside one -- a button nested in a `<label>` is two things
                claiming the same click. `.field.full` so it spans the grid
                like the other full-width rows. */}
            <div className="field full">
              <Toggle
                label={t("patch.policy.autoApproveToggle")}
                checked={form.auto_approve_security}
                onChange={(next) => setForm({ ...form, auto_approve_security: next })}
              />
            </div>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <Button type="button" variant="ghost" onClick={close}>
              {t("patch.policy.cancel")}
            </Button>
            {/* `{String(upsert.error)}` printed "Error: " for the bodyless 503
                a proxy returns, in a `<p>` no screen reader announced.
                `FormActions` renders the sentence, the field problems, and the
                server's own 422 translated rather than dumped. */}
            <FormActions
              label={t("patch.policy.save")}
              pending={upsert.isPending}
              error={upsert.error}
              problems={attemptProblems(attempt)}
            />
          </div>
        </Form>
      </Dialog>

      {/* CYCLE 8, and this one is not deletion — it is a REPLACEMENT the
          operator did not ask for. "New policy" for a group (or the org
          default) that already has one overwrites the policy governing those
          devices: its ring, its maintenance window, and whether security
          patches auto-approve. The old values are not recoverable from this
          screen. The subject is the ring being replaced, because that is the
          name on the row about to change. */}
      <ConfirmDialog
        open={pendingOverwrite != null}
        title={
          groupId === ""
            ? t("patch.policy.replaceOrgTitle")
            : t("patch.policy.replaceGroupTitle", { group: groupName(groupId) })
        }
        subject={existingPolicy ? existingPolicy.ring_name : ""}
        body={
          groupId === ""
            ? t("patch.policy.replaceOrgBody", {
                ring: form.ring_name.trim(),
                reboot: rebootLabel(form.reboot).toLowerCase(),
              })
            : t("patch.policy.replaceGroupBody", {
                group: groupName(groupId),
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
    </div>
  );
}
