import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { Button, Dialog, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { Icon } from "../../shell/icons";
import {
  useAuthorScriptMutation,
  useDeviceGroups,
  useEditScriptMutation,
  useRunScriptMutation,
  useScript,
  useScripts,
  useTargetDevices,
} from "./api";
import { OsIcon } from "../../shell/viz";
import { DeployPageNav } from "./deployNav";
import { SchedulesPanel } from "./SchedulesPanel";
import {
  devicePickerEmptyHint,
  devicePickerOptions,
  groupPickerEmptyHint,
  groupPickerOptions,
  interpreterLabel,
  nameForOption,
  osFamilyLabel,
} from "./pickerOptions";
import type { DeployTargetType, ScriptListItem } from "./types";
import { Btn, PageHead, Panel, QueryState } from "../../shell/components";
import { useEverywherePermissions } from "../../lib/mePermissions";

// The design's `P['deploy/scripts']`: one bare panel of rows led by the
// terminal glyph, the interpreter beside the name, a version in mono. The
// design's rows also carry a "Signed" pill and the owner as a person;
// signing happens per dispatch (the body travels inside the signed job
// envelope, so a stored script is not itself signed), and `created_by` is
// an id with no display name, so both are OMITTED rather than asserted.

const TARGET_TYPES: DeployTargetType[] = ["device", "group"];

// Both are required by the server, and the old guard (`!name || !body`) named
// neither of them when it refused.
// Functions, so the labels are worded when the problems are shown (see `Attempt`).
const authorRules = (): readonly FieldRule[] => [
  { name: "name", label: translate("deploy.scripts.ruleName") },
  { name: "osFamily", label: translate("deploy.scripts.ruleOs") },
  { name: "interpreter", label: translate("deploy.scripts.ruleInterpreter") },
  { name: "body", label: translate("deploy.scripts.ruleBody") },
];

const runRules = (): readonly FieldRule[] => [{ name: "targetId", label: translate("deploy.scripts.ruleTarget") }];

const editRules = (): readonly FieldRule[] => [{ name: "body", label: translate("deploy.scripts.ruleBody") }];

export function ScriptsPage() {
  const t = useT();
  const api = useApi();
  const [authorOpen, setAuthorOpen] = useState(false);
  const [editing, setEditing] = useState<ScriptListItem | null>(null);
  const [running, setRunning] = useState<ScriptListItem | null>(null);

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("deploy.read");
  // Configuration every client shares (GLOBAL_WRITES): the code on EVERY client (phase 3 design §2).
  const canManage = useEverywherePermissions().has("deploy.script.manage");
  const canRun = permissions.includes("deploy.script.run");
  const canSchedule = permissions.includes("deploy.script.schedule");

  const scripts = useScripts();

  return (
    <div>
      <PageHead
        title={t("deploy.scripts.title")}
        description={t("deploy.scripts.description")}
        actions={
          canManage ? (
            <Btn label={t("deploy.scripts.newScript")} icon="plus" variant="primary" onClick={() => setAuthorOpen(true)} />
          ) : undefined
        }
      />

      {/* REVIEW WAVE, round 2 (function): this page drew no sub-nav while
          both sibling groups did, and nothing in the console linked to it --
          it was reachable only by typing the URL. */}
      <DeployPageNav canRead={canRead} />

      <Panel bare>
        <QueryState
          loading={scripts.isLoading}
          error={scripts.error}
          empty={scripts.data != null && scripts.data.length === 0}
          emptyIcon="terminal"
          emptyLabel={t("deploy.scripts.empty")}
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("deploy.scripts.colName")}</Th>
                <Th>{t("deploy.scripts.colPlatform")}</Th>
                <Th>{t("deploy.scripts.colInterpreter")}</Th>
                <Th>{t("deploy.scripts.colVersion")}</Th>
                <Th>{""}</Th>
              </tr>
            </thead>
            <tbody>
              {scripts.data?.map((script) => (
                <tr key={script.id}>
                  {/* REVIEW WAVE, round 3 (composition): this cell carried a
                      constant sub-line, "runs as SYSTEM or root" -- the same
                      string on every row, one line below a page description
                      that already says it. The design's sub-line here is
                      "PowerShell · runs as SYSTEM": its per-row half is the
                      interpreter, which this table gives a column of its own,
                      and its constant half is the sentence above. Nothing
                      per-row was left to say, so the sub-line is gone rather
                      than refilled -- at 390px the constant wrapped to three
                      lines in every row and pushed the four columns after it
                      further out of reach inside the table's scroller. */}
                  <Td lead={<Icon name="terminal-2" size={18} />}>
                    <div className="cell-title">{script.name}</div>
                  </Td>
                  {/* Platform is a property, not a severity: the brand
                      glyph and the platform's name, as /deploy's Target
                      column draws it. */}
                  <Td lead={<OsIcon os={script.os_family} />}>{osFamilyLabel(script.os_family)}</Td>
                  {/* "powershell" is how the value is stored, not how the
                      product it names is spelled. */}
                  <Td>{interpreterLabel(script.interpreter)}</Td>
                  <Td>
                    <span className="mono">{script.version}</span>
                  </Td>
                  <Td>
                    <div className="flex justify-end gap-2">
                      {canManage && <Btn label={t("deploy.scripts.edit")} icon="edit" small onClick={() => setEditing(script)} />}
                      {canRun && (
                        <Btn label={t("deploy.scripts.run")} icon="player-play" small onClick={() => setRunning(script)} />
                      )}
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      <SchedulesPanel scripts={scripts.data ?? []} canRead={canRead} canSchedule={canSchedule} />

      <AuthorScriptDialog open={authorOpen} onClose={() => setAuthorOpen(false)} />
      <EditScriptDialog script={editing} onClose={() => setEditing(null)} />
      <RunScriptDialog script={running} onClose={() => setRunning(null)} />
    </div>
  );
}

function AuthorScriptDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const author = useAuthorScriptMutation();
  const [name, setName] = useState("");
  const [osFamily, setOsFamily] = useState("windows");
  const [interpreter, setInterpreter] = useState("powershell");
  const [body, setBody] = useState("");
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function submit() {
    const next: Attempt = { rules: authorRules, values: { name, osFamily, interpreter, body } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    author.mutate(
      // The BODY IS NOT TRIMMED. Everything else is: a script's leading blank
      // line or trailing newline is part of the text an interpreter will run,
      // and rewriting what somebody wrote before signing it is not this
      // form's business.
      { name: name.trim(), os_family: osFamily.trim(), interpreter: interpreter.trim(), body },
      {
        onSuccess: () => {
          setName("");
          setBody("");
          setAttempt(null);
          onClose();
        },
      },
    );
  }

  function dismiss() {
    setAttempt(null);
    author.reset();
    onClose();
  }

  // Escape closes it and the page behind it stops scrolling. Both now live in
  // the shared `Dialog` (components/ui.tsx), which is where this module's own
  // `useModalChrome` said they belonged — "fixing them there fixes every
  // dialog in the console at once". Cycle 12 made that change and retired the
  // local copy.

  return (
    <Dialog open={open} onClose={dismiss} title={t("deploy.scripts.authorTitle")}>
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("deploy.scripts.ruleName")}>
            <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label={t("deploy.scripts.ruleOs")}>
            <input
              className={inputClass}
              value={osFamily}
              onChange={(e) => setOsFamily(e.target.value)}
            />
          </Field>
          <Field label={t("deploy.scripts.ruleInterpreter")}>
            <input
              className={inputClass}
              value={interpreter}
              onChange={(e) => setInterpreter(e.target.value)}
            />
          </Field>
          <Field label={t("deploy.scripts.ruleBody")} full>
            <textarea
              className={inputClass}
              rows={6}
              value={body}
              onChange={(e) => setBody(e.target.value)}
            />
          </Field>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={dismiss}>
            {t("common.actions.cancel")}
          </Button>
          {/* WAS `disabled={author.isPending || !name || !body}`: the click
              moved nothing and named no field, and `String(author.error)` was
              "ApiError: " for a bodyless 503 and the raw
              `{"detail":[{"type":"string_too_short",…}]}` payload for a 422.
              `FormActions` closes all three. */}
          <FormActions
            label={t("deploy.scripts.save")}
            pending={author.isPending}
            error={author.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}

function EditScriptDialog({
  script,
  onClose,
}: {
  script: ScriptListItem | null;
  onClose: () => void;
}) {
  const t = useT();
  const edit = useEditScriptMutation();
  const [body, setBody] = useState("");
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  // Fetch the current body when the dialog is open for a script — the row
  // from useScripts() omits it, and PUT /deploy/scripts/{id} is a full-body
  // replace, so an unprefilled textarea would silently wipe the script.
  const detail = useScript(script?.id);

  useEffect(() => {
    if (script == null) {
      setBody("");
      setAttempt(null);
    }
  }, [script]);

  useEffect(() => {
    if (detail.data) {
      setBody(detail.data.body);
    }
  }, [detail.data]);

  const bodyReady = script != null && detail.data != null;

  function submit() {
    if (!script) return;
    // NOT A VALIDATION MESSAGE — the body genuinely is not loaded yet, and
    // saving now would PUT an empty string over a script that runs as
    // SYSTEM. The control says so rather than refusing silently.
    if (!bodyReady) return;
    const next: Attempt = { rules: editRules, values: { body } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    edit.mutate(
      { id: script.id, body: { body } },
      {
        onSuccess: () => {
          setBody("");
          setAttempt(null);
          onClose();
        },
      },
    );
  }

  function dismiss() {
    setAttempt(null);
    edit.reset();
    onClose();
  }

  // Escape closes it and the page behind it stops scrolling. Both now live in
  // the shared `Dialog` (components/ui.tsx), which is where this module's own
  // `useModalChrome` said they belonged — "fixing them there fixes every
  // dialog in the console at once". Cycle 12 made that change and retired the
  // local copy.

  return (
    <Dialog open={script != null} onClose={dismiss} title={t("deploy.scripts.editTitle", { name: script?.name ?? "" })}>
      <Form onSubmit={submit}>
        <Field label={t("deploy.scripts.ruleBody")}>
          <textarea
            className={inputClass}
            rows={6}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            disabled={!bodyReady}
          />
        </Field>
        {script != null && !bodyReady && !detail.isError && (
          <p className="note">{t("deploy.scripts.loadingBody")}</p>
        )}
        {/* This one already had a sentence, but not the REASON — "Failed to
            load" is the same text whether the operator lacks the permission,
            the script was deleted under them, or the service is down, and only
            one of those is worth retrying. The fixed half stays because it says
            what could not be done; `describeSubmitError` supplies the why. */}
        {detail.error ? (
          <p role="alert" className="form-error">
            {t("deploy.scripts.loadFailed", { detail: describeSubmitError(detail.error) })}
          </p>
        ) : null}
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={dismiss}>
            {t("common.actions.cancel")}
          </Button>
          {/* A save that fails here silently is a script the operator believes
              they have edited — and this body travels inside a signed job to
              run as SYSTEM/root. `disabledReason` is the one legitimate use of
              a disabled submit: not "you have not filled this in", but "the
              value you would be overwriting is not here yet", said out loud. */}
          <FormActions
            label={t("deploy.scripts.save")}
            pending={edit.isPending}
            error={edit.error}
            problems={attemptProblems(attempt)}
            disabledReason={script != null && !bodyReady ? t("deploy.scripts.waitingBody") : undefined}
          />
        </div>
      </Form>
    </Dialog>
  );
}

function RunScriptDialog({
  script,
  onClose,
}: {
  script: ScriptListItem | null;
  onClose: () => void;
}) {
  const t = useT();
  const run = useRunScriptMutation();
  const devices = useTargetDevices();
  const groups = useDeviceGroups();
  const [targetType, setTargetType] = useState<DeployTargetType>("device");
  const [targetId, setTargetId] = useState("");
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  // Set when the form has passed its own checks and the operator has yet to
  // confirm. Nothing is dispatched until this clears.
  const [confirming, setConfirming] = useState(false);

  // Not memoised: the sublabels are worded, and must follow a language switch.
  const deviceOptions = devicePickerOptions(devices.data ?? []);
  const groupOptions = groupPickerOptions(groups.data ?? []);

  const targetLabel =
    targetType === "device"
      ? (nameForOption(deviceOptions, targetId) ?? targetId)
      : (nameForOption(groupOptions, targetId) ?? targetId);

  function submit() {
    const next: Attempt = { rules: runRules, values: { targetId } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    setConfirming(true);
  }

  function dispatch() {
    setConfirming(false);
    if (!script) return;
    run.mutate(
      { id: script.id, body: { target_type: targetType, target_id: targetId.trim() } },
      {
        onSuccess: () => {
          setTargetId("");
          setAttempt(null);
          onClose();
        },
      },
    );
  }

  function dismiss() {
    setAttempt(null);
    setConfirming(false);
    run.reset();
    onClose();
  }

  // Escape closes it and the page behind it stops scrolling. Both now live in
  // the shared `Dialog` (components/ui.tsx), which is where this module's own
  // `useModalChrome` said they belonged — "fixing them there fixes every
  // dialog in the console at once". Cycle 12 made that change and retired the
  // local copy.
  // `dismissible={false}` while the "Run script as SYSTEM" confirmation is
  // stacked on top: `ConfirmDialog` has its own document-level Escape handler
  // and does not stop propagation, so one keypress would cancel the
  // confirmation AND throw away the target behind it.

  return (
    <>
      <Dialog
        open={script != null}
        onClose={dismiss}
        dismissible={!confirming}
        title={t("deploy.scripts.runTitle", { name: script?.name ?? "" })}
      >
        <Form onSubmit={submit}>
          <div className="form-grid">
            <Field label={t("deploy.scripts.targetType")}>
              <select
                className={inputClass}
                value={targetType}
                onChange={(e) => {
                  // A device id is not a group id: carrying the old value across
                  // the switch submits an id of the wrong kind under a type the
                  // server trusts.
                  setTargetId("");
                  setTargetType(e.target.value as DeployTargetType);
                }}
              >
                {TARGET_TYPES.map((option) => (
                  <option key={option} value={option}>
                    {t(option === "device" ? "deploy.targetType.deviceLower" : "deploy.targetType.groupLower")}
                  </option>
                ))}
              </select>
            </Field>
            {targetType === "device" ? (
              // CYCLE 6, and the stakes here are the highest in the module: this
              // field decides which machine runs an arbitrary script as
              // SYSTEM/root. A hand-typed UUID that is one character wrong is
              // not a validation error, it is the right script on the wrong
              // host. The sublabel carries `os_family · agent_status` so a
              // PowerShell script is not aimed at a Linux box, and an offline
              // agent is visible before the job is queued against it.
              <EntityPicker
                label={t("deploy.scripts.target")}
                options={deviceOptions}
                loading={devices.isLoading}
                emptyHint={devicePickerEmptyHint()}
                placeholder={t("deploy.scripts.devicePlaceholder")}
                value={targetId}
                onChange={setTargetId}
              />
            ) : (
              <EntityPicker
                label={t("deploy.scripts.target")}
                options={groupOptions}
                loading={groups.isLoading}
                emptyHint={groupPickerEmptyHint()}
                placeholder={t("deploy.scripts.groupPlaceholder")}
                value={targetId}
                onChange={setTargetId}
              />
            )}
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <Button type="button" variant="ghost" onClick={dismiss}>
              {t("common.actions.cancel")}
            </Button>
            {/* Run dispatches a signed job to a device or a whole group. A
                refusal that renders as an empty red line leaves the operator
                to assume it went out — `FormActions` renders the sentence and
                announces it. */}
            <FormActions
              label={t("deploy.scripts.runSubmit")}
              pendingLabel={t("deploy.scripts.runPending")}
              pending={run.isPending}
              error={run.error}
              problems={attemptProblems(attempt)}
            />
          </div>
        </Form>
      </Dialog>

      {/* CYCLE 8. This is the most privileged control in the module: the body
          travels inside a signed job envelope and executes with the agent's
          SYSTEM/root privilege. There is no cancel path for a job already
          dispatched (no `DELETE /api/v1/jobs/{id}` exists — Auditor B filed
          "Jobs cannot be cancelled, retried or requeued" as its own BLOCKER),
          so the last moment anybody can stop this is here.

          The subject names the script AND the machine, because with a group
          selected the blast radius is every device in it and the operator
          should read that back before agreeing to it. `requireTyping` stays
          off: this is a targeted, repeatable action, and teaching people to
          copy a word to proceed devalues the one place that control belongs. */}
      <ConfirmDialog
        open={confirming}
        title={t("deploy.scripts.confirmTitle")}
        subject={t("deploy.scripts.confirmSubject", { script: script?.name ?? "", target: targetLabel })}
        body={t("deploy.scripts.confirmBody")}
        confirmLabel={t("deploy.scripts.confirmLabel")}
        destructive
        onCancel={() => setConfirming(false)}
        onConfirm={dispatch}
      />
    </>
  );
}
