import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import {
  Form,
  FormActions,
  attemptProblems,
  describeSubmitError,
} from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime, toInstant } from "../../lib/datetime";
import {
  useCreateFindingMutation,
  useFindings,
  useRecordTestMutation,
  useTest,
  useUpdateFindingMutation,
  useUpsertStepMutation,
  useUserTimezone,
} from "./api";
import {
  outcomeKind,
  outcomeLabel,
  remediationKind,
  remediationLabel,
  severityKind,
  severityLabel,
  stepResultLabel,
  testTypeLabel,
} from "./bcpState";
import type {
  BcpEvidence,
  BcpEvidenceValue,
  FindingSeverity,
  RemediationState,
  StepResult,
} from "./types";
import { PageHead, Panel, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { Facts } from "../../shell/viz";

type TriState = "" | "true" | "false";

function triToBool(value: TriState): boolean | null {
  if (value === "") return null;
  return value === "true";
}

// `BcpTest.evidence` is `{}` until record_test captures the frozen snapshot
// (backend/app/modules/bcp/service.py) — this guard is how the UI tells the
// two shapes apart.
function hasEvidenceSummary(value: BcpEvidenceValue): value is BcpEvidence {
  return "summary" in value;
}

// Records the outcome of a DR test, then surfaces the FROZEN backup-evidence
// snapshot captured at that moment (backend/app/modules/bcp/service.py's
// record_test) — the per-device rows below are that snapshot, not a live
// recomputation.
export function TestRecorder() {
  const t = useT();
  const { testId } = useParams<{ testId: string }>();
  const api = useApi();
  const me = useQuery<Me>({
    queryKey: ["me"],
    queryFn: () => api.me(),
    staleTime: 60_000,
  });
  const canManage = (me.data?.permissions ?? []).includes("bcp.manage");
  const zone = useUserTimezone();

  const test = useTest(testId);
  const recordTest = useRecordTestMutation();

  const [executedAt, setExecutedAt] = useState("");
  const [rtoMet, setRtoMet] = useState<TriState>("");
  const [rpoMet, setRpoMet] = useState<TriState>("");
  const [outcome, setOutcome] = useState<"pass" | "partial" | "fail">("pass");
  const [notes, setNotes] = useState("");
  const [confirming, setConfirming] = useState(false);

  // Cycle 8. Recording a test the FIRST time creates the evidence; recording it
  // again REPLACES it. `bcp/service.py#record_test` overwrites `test.evidence`
  // with a fresh snapshot of backup coverage taken now -- the frozen
  // point-in-time proof of what coverage looked like when the drill actually
  // ran is gone, and nothing in the product keeps a copy. That is destroyed
  // data on a compliance artefact, which is exactly the case the dialog exists
  // for; the first recording is not, and asking then would be friction.
  const alreadyRecorded = test.data?.executed_at != null;

  if (!testId) return null;

  function submit() {
    recordTest.mutate({
      id: testId as string,
      body: {
        // The wall clock the field hands back carries no zone. Unqualified,
        // a 14:00 execution is recorded as 14:00Z and read back as 09:00 --
        // the five hours this evidence pack is supposed to be trusted about.
        executed_at: toInstant(executedAt, zone) || null,
        rto_met: triToBool(rtoMet),
        rpo_met: triToBool(rpoMet),
        outcome,
        notes,
      },
    });
  }

  const evidence = test.data?.evidence;

  function press() {
    if (alreadyRecorded) {
      setConfirming(true);
      return;
    }
    submit();
  }

  return (
    <div>
      {/* IN THE HEAD'S SLOT, not in a flex row built around the head. `PageHead`
          is already the row this page was reproducing, and the page beside this
          one in the same module passes `actions` — so the one link this screen
          offers sat at a different height and a different gap from every other
          page action in the console. */}
      <PageHead
        title={t("bcp.recorder.title")}
        description={t("bcp.recorder.description")}
        actions={
          <Link className="btn" to={`/bcp/tests/${testId}/report`}>
            <Icon name="file-text" size={14} />
            {t("bcp.recorder.viewReport")}
          </Link>
        }
      />

      {test.data && (
        <>
          {/* v3's facts strip. Wire vocabulary, read as words (cycle 12):
              `restore_drill` is a DR test type, not a string a continuity
              officer should have to translate. */}
          <h2 className="sr-only">{t("bcp.recorder.summary")}</h2>
          <Facts
            items={[
              { k: t("bcp.recorder.type"), v: testTypeLabel(test.data.type) },
              {
                k: t("bcp.recorder.outcome"),
                v: (
                  <StatusPill
                    value={test.data.outcome}
                    kind={outcomeKind(test.data.outcome)}
                    label={outcomeLabel(test.data.outcome)}
                  />
                ),
              },
              {
                k: t("bcp.recorder.scheduled"),
                v: <span>{formatDateTime(test.data.scheduled_for, zone)}</span>,
              },
              {
                k: t("bcp.recorder.executed"),
                v: <span>{formatDateTime(test.data.executed_at, zone)}</span>,
              },
            ]}
          />
        </>
      )}

      {canManage && (
        <div className="mb-4">
          <Panel title={t("bcp.recorder.recordOutcome")}>
            <div className="form-grid">
              {/* THE DATE FIELD SPANS THE ROW, and the control takes ONE
                  COLUMN'S MEASURE WHERE THERE IS ONE. `.form-grid` WAS two
                  columns at every width -- the reflow rule lived in
                  `shell.css` and was outranked by the unconditional one in
                  `components.css`, which the kit has since fixed by moving the
                  fold beside the rule it overrides -- so at 390px an ordinary
                  cell was about 150px and a `datetime-local` needs roughly 190px for
                  `mm/dd/yyyy, --:-- --` plus the browser's calendar-picker
                  button -- the button was clipped off, so the picker could not
                  be opened on a handset at all. Hence `full`.

                  The flat `18rem` cap that came with it applied at EVERY width,
                  so on a 1440 desktop this field rendered ~287px over three
                  553px selects and the form opened on a stub. `max()` keeps
                  both facts: at desktop `calc(50% - 7px)` is exactly one grid
                  column (14px gap), so the row lines up with the selects under
                  it; at 390 that expression collapses to ~158px and the 18rem
                  floor wins, holding the picker button on screen. Same measure
                  as the period fields on `/bcp/report`. */}
              <Field label={t("bcp.recorder.executedAt")} full>
                <input
                  type="datetime-local"
                  className={inputClass}
                  style={{ maxWidth: "max(18rem, calc(50% - 7px))" }}
                  value={executedAt}
                  onChange={(e) => setExecutedAt(e.target.value)}
                />
                {/* The moment this holds is spelled out under the grid,
                    below. It is not inside this `<Field>` because `Field`
                    wraps its control in the `<label>` and text placed here
                    becomes part of the field's NAME; the accounting is at
                    `agent-push/PushAuthorizationsPage`. */}
              </Field>
              <Field label={t("bcp.recorder.rtoMet")}>
                <select
                  className={inputClass}
                  value={rtoMet}
                  onChange={(e) => setRtoMet(e.target.value as TriState)}
                >
                  <option value="">{t("bcp.recorder.unknown")}</option>
                  <option value="true">{t("bcp.recorder.yes")}</option>
                  <option value="false">{t("bcp.recorder.no")}</option>
                </select>
              </Field>
              <Field label={t("bcp.recorder.rpoMet")}>
                <select
                  className={inputClass}
                  value={rpoMet}
                  onChange={(e) => setRpoMet(e.target.value as TriState)}
                >
                  <option value="">{t("bcp.recorder.unknown")}</option>
                  <option value="true">{t("bcp.recorder.yes")}</option>
                  <option value="false">{t("bcp.recorder.no")}</option>
                </select>
              </Field>
              <Field label={t("bcp.recorder.outcome")}>
                <select
                  className={inputClass}
                  value={outcome}
                  onChange={(e) =>
                    setOutcome(e.target.value as "pass" | "partial" | "fail")
                  }
                >
                  <option value="pass">{outcomeLabel("pass")}</option>
                  <option value="partial">{outcomeLabel("partial")}</option>
                  <option value="fail">{outcomeLabel("fail")}</option>
                </select>
              </Field>
              <Field label={t("bcp.recorder.notes")} full>
                <textarea
                  className={inputClass}
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                  rows={2}
                />
              </Field>
            </div>
            {/* THE MOMENT, READ BACK IN THE CONSOLE'S OWN FORMAT. The mask a
                native datetime input draws is the USER AGENT'S locale and no
                CSS reorders it, so the field above reads `09/22/2026` to a
                US-locale browser while the "Executed" row on this same page
                renders `22 Sept 2026` and /settings says day/month/year. The
                control stays -- it is the one datetime widget every keyboard,
                screen reader and locale already agrees on -- and the moment it
                is about to record is spelled out here (design review M1, the
                idiom `agent-push/PushAuthorizationsPage` already carried).
                This one is the DR test's recorded evidence date, so a month
                misread here is misread in the audit trail. */}
            {executedAt !== "" ? (
              <p className="note" style={{ marginTop: 14 }}>
                {t("bcp.recorder.executedAtEcho", { when: formatDateTime(toInstant(executedAt, zone), zone) })}
              </p>
            ) : null}
            {/* `{String(recordTest.error)}` printed "[object Object]", and for a
                bodyless 503 printed nothing at all -- the operator read the
                unchanged page as "recorded". */}
            {recordTest.isError && (
              <p role="alert" className="form-error mb-2">
                {describeSubmitError(recordTest.error)}
              </p>
            )}
            <div className="mt-3">
              <Button onClick={press} disabled={recordTest.isPending}>
                {recordTest.isPending ? t("bcp.recorder.recording") : t("bcp.recorder.recordTest")}
              </Button>
            </div>
          </Panel>
        </div>
      )}

      <ConfirmDialog
        open={confirming}
        title={t("bcp.recorder.reRecordTitle")}
        body={t("bcp.recorder.reRecordBody")}
        subject={
          test.data
            ? t("bcp.recorder.reRecordSubject", {
                type: testTypeLabel(test.data.type),
                when: formatDateTime(test.data.executed_at, zone),
              })
            : t("bcp.recorder.thisTest")
        }
        confirmLabel={t("bcp.recorder.replaceEvidence")}
        destructive
        onConfirm={() => {
          setConfirming(false);
          submit();
        }}
        onCancel={() => setConfirming(false)}
      />

      {evidence && hasEvidenceSummary(evidence) && (
        <div className="mb-4">
          <Panel
            title={t("bcp.recorder.snapshotTitle")}
            subtitle={t("bcp.recorder.snapshotSubtitle", { when: formatDateTime(evidence.captured_at, zone) })}
          >
            <Facts
              items={[
                { k: t("bcp.recorder.devices"), v: evidence.summary.total_n },
                { k: t("bcp.recorder.covered"), v: evidence.summary.covered_n },
                { k: t("bcp.recorder.restoreProven"), v: evidence.summary.restore_proven_n },
              ]}
            />
            <Table>
              <thead>
                <tr>
                  <Th>{t("bcp.recorder.colHostname")}</Th>
                  <Th>{t("bcp.recorder.colCovered")}</Th>
                  <Th>{t("bcp.recorder.colLastBackup")}</Th>
                  <Th>{t("bcp.recorder.colVerified")}</Th>
                  <Th>{t("bcp.recorder.colRestoreTested")}</Th>
                  <Th>{t("bcp.recorder.colRestoreProven")}</Th>
                </tr>
              </thead>
              <tbody>
                {evidence.devices.map((device) => (
                  <tr key={device.device_id}>
                    {/* The snapshot names the host and nothing about its
                        platform, so the lead is the generic desktop glyph
                        rather than a guessed OS. */}
                    <Td
                      lead={<Icon name="device-desktop" size={18} />}
                      gutter={device.covered ? undefined : "crit"}
                    >
                      <span className="cell-title">{device.hostname}</span>
                    </Td>
                    <Td>
                      {device.covered ? (
                        <StatusPill value="covered" kind="ok" label={t("bcp.recorder.rowCovered")} />
                      ) : (
                        <StatusPill value="uncovered" kind="crit" label={t("bcp.recorder.notCovered")} />
                      )}
                    </Td>
                    <Td>
                      <span className="mono" style={{ whiteSpace: "nowrap" }}>
                        {formatDateTime(device.last_backup_taken_at, zone)}
                      </span>
                    </Td>
                    <Td>
                      <span className="mono" style={{ whiteSpace: "nowrap" }}>
                        {formatDateTime(device.verified_at, zone)}
                      </span>
                    </Td>
                    <Td>
                      <span className="mono" style={{ whiteSpace: "nowrap" }}>
                        {formatDateTime(device.restore_tested_at, zone)}
                      </span>
                    </Td>
                    <Td>
                      {device.restore_proven ? (
                        <StatusPill value="proven" kind="ok" label={t("bcp.recorder.proven")} />
                      ) : (
                        <StatusPill value="unproven" kind="mut" label={t("bcp.recorder.notProven")} />
                      )}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </Panel>
        </div>
      )}

      {canManage && <StepForm testId={testId} />}
      <FindingsSection testId={testId} canManage={canManage} />
    </div>
  );
}

// `ordinal` is the step's position in the procedure and `upsert_step` keys on
// it (`Number("") === 0` would silently rewrite step 0), and a step with no
// description is a row of evidence that says nothing. Both were guarded by
// `disabled={upsertStep.isPending || !description}` -- a control that would not
// move and named no field, which is the silent no-op this kernel exists to
// remove.
const stepRules = (): readonly FieldRule[] => [
  // "Step description", the words ON SCREEN: the findings form below carries
  // a Description of its own, and two controls sharing one label leave both
  // the reader and the screen reader guessing which one a message is about.
  { name: "description", label: translate("bcp.recorder.stepDescription") },
  {
    name: "ordinal",
    label: translate("bcp.recorder.stepNumber"),
    validate: (value) => (/^\d+$/.test(value) ? null : translate("bcp.recorder.stepWhole")),
  },
];

function StepForm({ testId }: { testId: string }) {
  const t = useT();
  const upsertStep = useUpsertStepMutation();
  const [ordinal, setOrdinal] = useState("1");
  const [description, setDescription] = useState("");
  const [result, setResult] = useState<StepResult>("pending");
  const [evidenceRef, setEvidenceRef] = useState("");
  // Held as data (rules and values), so a language switch re-words the refusal.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function submit() {
    const next: Attempt = { rules: stepRules, values: { description, ordinal } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    upsertStep.mutate(
      {
        testId,
        body: {
          ordinal: Number(ordinal),
          // Trimmed to match what was validated -- `requiredProblems` trims
          // before testing emptiness, so an untrimmed send could carry a
          // description the checks never saw.
          description: description.trim(),
          result,
          evidence_ref: evidenceRef.trim() || null,
        },
      },
      { onSuccess: () => setDescription("") },
    );
  }

  return (
    <div className="mb-4">
      <Panel title={t("bcp.recorder.stepPanel")}>
        {/* A real <form>, so Enter in the Description field saves the step
            instead of doing nothing. */}
        {/* THE GRID, AND THE SUBMIT ON ITS OWN LINE. As a wrapping flex row the
            four controls sat on two different label lines and `.form-actions`
            (`margin-top: 16px`) dropped "Save step" 20px below the field row
            and 16px past its bottom edge. "Record outcome" above is a
            `.form-grid` with its button under it; this is the same form, so it
            is the same rhythm. */}
        <Form onSubmit={submit}>
          <div className="form-grid">
            <Field label={t("bcp.recorder.stepNumber")}>
              <input
                type="number"
                className={inputClass}
                value={ordinal}
                onChange={(e) => setOrdinal(e.target.value)}
              />
            </Field>
            <Field label={t("bcp.recorder.stepDescription")}>
              <input
                className={inputClass}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </Field>
            <Field label={t("bcp.recorder.result")}>
              <select
                className={inputClass}
                value={result}
                onChange={(e) => setResult(e.target.value as StepResult)}
              >
                <option value="pending">{stepResultLabel("pending")}</option>
                <option value="pass">{stepResultLabel("pass")}</option>
                <option value="fail">{stepResultLabel("fail")}</option>
              </select>
            </Field>
            <Field label={t("bcp.recorder.evidenceRef")}>
              <input
                className={inputClass}
                value={evidenceRef}
                onChange={(e) => setEvidenceRef(e.target.value)}
              />
            </Field>
          </div>
          {/* Enabled with the description empty ON PURPOSE -- the refusal is what
              the user gets to read, and a control that will not move is not. The
              server failure comes from here too, so a bodyless 503 produces a
              sentence instead of the empty string the old line rendered. */}
          <FormActions
            label={t("bcp.recorder.saveStep")}
            pendingLabel={t("bcp.recorder.saving")}
            pending={upsertStep.isPending}
            error={upsertStep.error}
            problems={attemptProblems(attempt)}
          />
        </Form>
      </Panel>
    </div>
  );
}

const findingRules = (): readonly FieldRule[] => [
  { name: "description", label: translate("bcp.recorder.findingDescription") },
];

function FindingsSection({
  testId,
  canManage,
}: {
  testId: string;
  canManage: boolean;
}) {
  const t = useT();
  const findings = useFindings(testId);
  const createFinding = useCreateFindingMutation();
  const updateFinding = useUpdateFindingMutation();
  const [description, setDescription] = useState("");
  const [severity, setSeverity] = useState<FindingSeverity>("low");
  // Held as data (rules and values), so a language switch re-words the refusal.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function submit() {
    const next: Attempt = { rules: findingRules, values: { description } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    createFinding.mutate(
      { testId, body: { description: description.trim(), severity } },
      { onSuccess: () => setDescription("") },
    );
  }

  return (
    <Panel title={t("bcp.recorder.findings")}>
        <Table>
          <thead>
            <tr>
              <Th>{t("bcp.recorder.colDescription")}</Th>
              <Th>{t("bcp.recorder.colSeverity")}</Th>
              <Th>{t("bcp.recorder.colRemediation")}</Th>
            </tr>
          </thead>
          <tbody>
            {findings.data?.map((finding) => (
              <tr key={finding.id}>
                <Td lead={<Icon name="flag" size={18} />} gutter={severityKind(finding.severity)}>
                  {finding.description}
                </Td>
                <Td>
                  <StatusPill
                    value={finding.severity}
                    kind={severityKind(finding.severity)}
                    label={severityLabel(finding.severity)}
                  />
                </Td>
                <Td>
                  {canManage ? (
                    <select
                      className="select-field"
                      aria-label={t("bcp.recorder.remediationAria", { description: finding.description })}
                      value={finding.remediation_state}
                      onChange={(e) =>
                        updateFinding.mutate({
                          id: finding.id,
                          testId,
                          body: {
                            remediation_state: e.target.value as RemediationState,
                          },
                        })
                      }
                    >
                      <option value="open">{remediationLabel("open")}</option>
                      <option value="in_progress">{remediationLabel("in_progress")}</option>
                      <option value="resolved">{remediationLabel("resolved")}</option>
                    </select>
                  ) : (
                    <StatusPill
                      value={finding.remediation_state}
                      kind={remediationKind(finding.remediation_state)}
                      label={remediationLabel(finding.remediation_state)}
                    />
                  )}
                </Td>
              </tr>
            ))}
            {findings.data && findings.data.length === 0 && (
              <tr>
                <Td className="cell-sub" colSpan={3}>
                  {t("bcp.recorder.noFindings")}
                </Td>
              </tr>
            )}
          </tbody>
        </Table>

        {canManage && (
          <Form onSubmit={submit}>
            {/* The same rhythm as the two forms above: a grid of fields, the
                submit on its own line under it. */}
            <div className="form-grid mt-4">
              <Field label={t("bcp.recorder.findingDescription")}>
                <input
                  className={inputClass}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                />
              </Field>
              <Field label={t("bcp.recorder.severity")}>
                <select
                  className={inputClass}
                  value={severity}
                  onChange={(e) => setSeverity(e.target.value as FindingSeverity)}
                >
                  <option value="low">{severityLabel("low")}</option>
                  <option value="medium">{severityLabel("medium")}</option>
                  <option value="high">{severityLabel("high")}</option>
                  <option value="critical">{severityLabel("critical")}</option>
                </select>
              </Field>
            </div>
            {/* Enabled with the description empty ON PURPOSE, and the server's
                refusal comes from here too. A DR finding that fails to save
                silently is a gap in the evidence pack nobody knows about. */}
            <FormActions
              label={t("bcp.recorder.addFinding")}
              pending={createFinding.isPending}
              error={createFinding.error}
              problems={attemptProblems(attempt)}
            />
          </Form>
        )}
        {/* SWALLOWED ENTIRELY BEFORE THIS. Changing a finding's remediation state
            is a `<select>` with no error surface anywhere: a refused
            "resolved" -- 403 from a caller without bcp.manage, or a 503 --
            left the dropdown reading "resolved" until the next refetch, so a
            finding that is still open reads as closed on a SOC 2 evidence page.
            The refetch that eventually corrects the value says nothing either. */}
        {updateFinding.isError && (
          <p role="alert" className="form-error mt-2">
            {describeSubmitError(updateFinding.error)}
          </p>
        )}
    </Panel>
  );
}
