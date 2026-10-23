import { useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Dialog, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { formatDateTime } from "../../lib/datetime";
import { humaniseEnum } from "../../lib/labels";
import { Btn, Panel, QueryState, StatusPill, Toggle } from "../../shell/components";
import {
  useCreateScriptPolicyMutation,
  useDeleteScriptPolicyMutation,
  useOrgZone,
  useScheduleGroups,
  useScriptPolicies,
  useScriptPolicyRuns,
  useUpdateScriptPolicyMutation,
} from "./api";
import type { ScriptListItem, ScriptPolicy } from "./types";

/** The choices a schedule offers, in hours. Stored as hours; never shown so. */
export const INTERVAL_CHOICES: readonly number[] = [1, 4, 24, 168];

/** "Every day", "Every 4 hours": the interval as a person says it. */
export function intervalLabel(hours: number): string {
  if (hours === 1) return translate("deploy.schedules.everyHour");
  if (hours === 24) return translate("deploy.schedules.everyDay");
  if (hours === 168) return translate("deploy.schedules.everyWeek");
  if (hours % 24 === 0) return translate("deploy.schedules.everyDays", { count: hours / 24 });
  return translate("deploy.schedules.everyHours", { count: hours });
}

/** When a schedule may run, in words. */
export function whenLabel(policy: Pick<ScriptPolicy, "interval_hours" | "in_maintenance_window">): string {
  return translate(policy.in_maintenance_window ? "deploy.schedules.whenWindow" : "deploy.schedules.whenAny", {
    interval: intervalLabel(policy.interval_hours),
  });
}

/** How the latest runs went, in words. */
export function outcomeLabel(policy: Pick<ScriptPolicy, "devices_failing" | "devices_succeeding">): {
  label: string;
  kind: "ok" | "crit" | "mut";
} {
  if (policy.devices_failing > 0) {
    return { label: translate("deploy.schedules.failing", { count: policy.devices_failing }), kind: "crit" };
  }
  if (policy.devices_succeeding > 0) return { label: translate("deploy.schedules.succeeding"), kind: "ok" };
  return { label: translate("deploy.schedules.notRun"), kind: "mut" };
}

// A function, so the labels are worded when the problems are shown (see `Attempt`).
const rules = (): readonly FieldRule[] => [
  { name: "name", label: translate("deploy.schedules.ruleName") },
  { name: "scriptId", label: translate("deploy.schedules.ruleScript") },
  { name: "groupId", label: translate("deploy.schedules.ruleGroup") },
];

/**
 * Scheduled scripts: a script run on every machine in a group, on a schedule.
 * Each run is the same signed script job a person starts with Run.
 */
export function SchedulesPanel({
  scripts,
  canRead,
  canSchedule,
}: {
  scripts: readonly ScriptListItem[];
  canRead: boolean;
  canSchedule: boolean;
}) {
  const t = useT();
  const policies = useScriptPolicies(canRead);
  const update = useUpdateScriptPolicyMutation();
  const remove = useDeleteScriptPolicyMutation();
  const zone = useOrgZone();
  const [creating, setCreating] = useState(false);
  const [viewing, setViewing] = useState<ScriptPolicy | null>(null);
  const [deleting, setDeleting] = useState<ScriptPolicy | null>(null);

  if (!canRead) return null;
  const items = policies.data ?? [];

  return (
    <div className="mt-4">
      <Panel
        title={t("deploy.schedules.title")}
        subtitle={t("deploy.schedules.subtitle")}
        actions={
          canSchedule ? (
            <Btn label={t("deploy.schedules.newSchedule")} icon="plus" onClick={() => setCreating(true)} />
          ) : undefined
        }
        bare
      >
        <QueryState
          loading={policies.isLoading}
          error={policies.error}
          empty={policies.data != null && items.length === 0}
          emptyIcon="clock"
          emptyLabel={t("deploy.schedules.empty")}
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("deploy.schedules.colSchedule")}</Th>
                <Th>{t("deploy.schedules.colRuns")}</Th>
                <Th>{t("deploy.schedules.colLatest")}</Th>
                <Th>{t("deploy.schedules.colLastRun")}</Th>
                <Th>{""}</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((policy) => {
                const outcome = outcomeLabel(policy);
                return (
                  <tr key={policy.id}>
                    <Td>
                      <div className="cell-title">{policy.name}</div>
                      <div className="cell-sub">
                        {t("deploy.schedules.scriptOn", { script: policy.script_name, group: policy.device_group_name })}
                      </div>
                    </Td>
                    <Td>
                      {whenLabel(policy)}
                      {policy.alert_on_failure && <div className="cell-sub">{t("deploy.schedules.alerts")}</div>}
                    </Td>
                    <Td>
                      <StatusPill value={outcome.kind} kind={outcome.kind} label={outcome.label} />
                    </Td>
                    <Td>{policy.last_run_at ? formatDateTime(policy.last_run_at, zone) : t("deploy.schedules.never")}</Td>
                    <Td>
                      <div className="flex justify-end gap-2">
                        <Btn label={t("deploy.schedules.runs")} icon="list" small onClick={() => setViewing(policy)} />
                        {canSchedule && (
                          <>
                            <Toggle
                              checked={policy.enabled}
                              label={t(policy.enabled ? "deploy.schedules.pause" : "deploy.schedules.resume", { name: policy.name })}
                              onChange={(next) =>
                                update.mutate({ id: policy.id, body: { enabled: next } })
                              }
                            />
                            <Btn label={t("deploy.schedules.delete")} icon="trash" small onClick={() => setDeleting(policy)} />
                          </>
                        )}
                      </div>
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      <NewScheduleDialog open={creating} scripts={scripts} onClose={() => setCreating(false)} />
      <RunsDialog policy={viewing} zone={zone} onClose={() => setViewing(null)} />
      <ConfirmDialog
        open={deleting != null}
        title={t("deploy.schedules.deleteTitle")}
        body={t("deploy.schedules.deleteBody")}
        subject={deleting?.name ?? ""}
        confirmLabel={t("deploy.schedules.deleteLabel")}
        destructive
        onConfirm={() => {
          if (deleting) remove.mutate(deleting.id);
          setDeleting(null);
        }}
        onCancel={() => setDeleting(null)}
      />
    </div>
  );
}

function NewScheduleDialog({
  open,
  scripts,
  onClose,
}: {
  open: boolean;
  scripts: readonly ScriptListItem[];
  onClose: () => void;
}) {
  const t = useT();
  const create = useCreateScriptPolicyMutation();
  const groups = useScheduleGroups(open);
  const [name, setName] = useState("");
  const [scriptId, setScriptId] = useState("");
  const [groupId, setGroupId] = useState("");
  const [interval, setEvery] = useState(24);
  const [inWindow, setInWindow] = useState(true);
  const [alert, setAlert] = useState(false);
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function reset() {
    setName("");
    setScriptId("");
    setGroupId("");
    setEvery(24);
    setInWindow(true);
    setAlert(false);
    setAttempt(null);
    create.reset();
  }

  function submit() {
    const next: Attempt = { rules, values: { name, scriptId, groupId } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    create.mutate(
      {
        name: name.trim(),
        script_id: scriptId,
        device_group_id: groupId,
        interval_hours: interval,
        in_maintenance_window: inWindow,
        alert_on_failure: alert,
        keep_runs: 30,
        enabled: true,
      },
      {
        onSuccess: () => {
          reset();
          onClose();
        },
      },
    );
  }

  return (
    <Dialog
      open={open}
      onClose={() => {
        reset();
        onClose();
      }}
      title={t("deploy.schedules.createTitle")}
    >
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("deploy.schedules.fieldName")}>
            <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label={t("deploy.schedules.fieldScript")}>
            <select className={inputClass} value={scriptId} onChange={(e) => setScriptId(e.target.value)}>
              <option value="">{t("deploy.schedules.chooseScript")}</option>
              {scripts.map((script) => (
                <option key={script.id} value={script.id}>
                  {script.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("deploy.schedules.fieldGroup")} help={t("deploy.schedules.groupHelp")}>
            <select className={inputClass} value={groupId} onChange={(e) => setGroupId(e.target.value)}>
              <option value="">{t("deploy.schedules.chooseGroup")}</option>
              {(groups.data ?? []).map((group) => (
                <option key={group.id} value={group.id}>
                  {group.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("deploy.schedules.fieldInterval")}>
            <select
              className={inputClass}
              value={interval}
              onChange={(e) => setEvery(Number(e.target.value))}
            >
              {INTERVAL_CHOICES.map((hours) => (
                <option key={hours} value={hours}>
                  {intervalLabel(hours)}
                </option>
              ))}
            </select>
          </Field>
          <Toggle
            checked={inWindow}
            onChange={setInWindow}
            label={t("deploy.schedules.inWindow")}
          />
          <Toggle
            checked={alert}
            onChange={setAlert}
            label={t("deploy.schedules.raiseAlert")}
          />
          {alert && (
            <p className="note">{t("deploy.schedules.alertNote")}</p>
          )}
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button
            type="button"
            variant="ghost"
            onClick={() => {
              reset();
              onClose();
            }}
          >
            {t("common.actions.cancel")}
          </Button>
          <FormActions
            label={t("deploy.schedules.submit")}
            pending={create.isPending}
            error={create.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}

function RunsDialog({
  policy,
  zone,
  onClose,
}: {
  policy: ScriptPolicy | null;
  zone: string | null;
  onClose: () => void;
}) {
  const t = useT();
  const runs = useScriptPolicyRuns(policy?.id ?? null);
  const items = runs.data ?? [];
  return (
    <Dialog open={policy != null} onClose={onClose} title={t("deploy.schedules.runsTitle", { name: policy?.name ?? "" })}>
      <QueryState
        loading={runs.isLoading}
        error={runs.error}
        empty={runs.data != null && items.length === 0}
        emptyIcon="clock"
        emptyLabel={t("deploy.schedules.runsEmpty")}
      >
        <ul className="grid gap-3">
          {items.map((run) => (
            <li key={run.id}>
              <div className="cell-title">
                {run.hostname ?? t("deploy.schedules.unnamed")} · {formatDateTime(run.created_at, zone)}
              </div>
              <div className="cell-sub">
                {run.state === "running" || run.state === "succeeded"
                  ? humaniseEnum(run.state, "ScriptRunState")
                  : run.exit_code != null
                    ? t("deploy.schedules.failedExit", { code: run.exit_code })
                    : t("deploy.schedules.failedRun")}
              </div>
              {run.output != null ? (
                <pre className="mono note mt-1 max-h-48 overflow-auto whitespace-pre-wrap">{run.output}</pre>
              ) : (
                run.state !== "running" && <p className="note">{t("deploy.schedules.outputGone")}</p>
              )}
            </li>
          ))}
        </ul>
      </QueryState>
    </Dialog>
  );
}
