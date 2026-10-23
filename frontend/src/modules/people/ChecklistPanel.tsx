import { useState } from "react";
import { Link } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { Button, Dialog, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { Panel } from "../../shell/components";
import { Steps } from "../../shell/viz";
import type { StepState as BarState } from "../../shell/viz";
import { useCloseChecklist, useUpdateStep } from "./api";
import type { ChecklistOut, StepOut, StepState } from "./types";

/**
 * One open checklist. It records work; it never does any. Each step links to
 * the page that does the work and ticks itself when that page's data says so.
 */
export function ChecklistPanel({
  personId,
  personName,
  checklist,
  canEdit,
}: {
  personId: string;
  personName: string;
  checklist: ChecklistOut;
  canEdit: boolean;
}) {
  const t = useT();
  const update = useUpdateStep(personId, checklist.id);
  const close = useCloseChecklist(personId, checklist.id);
  // A step being marked by hand: a note is asked for when the data disagrees.
  const [marking, setMarking] = useState<{ step: StepOut; state: StepState } | null>(null);
  const [closing, setClosing] = useState<"complete" | "cancel" | null>(null);
  const firstPending = checklist.steps.find((step) => step.state === "pending")?.key;
  const remaining = checklist.steps.filter((step) => step.state === "pending").length;
  const leaver = checklist.kind === "leaver";

  const barState = (step: StepOut): BarState =>
    step.state === "pending" ? (step.key === firstPending ? "now" : "todo") : "done";

  return (
    <Panel
      title={t(leaver ? "people.checklist.titleLeaver" : "people.checklist.titleStarter")}
      subtitle={
        remaining === 0
          ? t("people.checklist.allDone")
          : t("people.checklist.stepsLeft", { remaining, count: checklist.steps.length })
      }
      actions={
        canEdit ? (
          <>
            <Button variant="ghost" onClick={() => setClosing("cancel")} disabled={close.isPending}>
              {t("people.checklist.cancelChecklist")}
            </Button>
            <Button
              onClick={() => setClosing("complete")}
              disabled={!checklist.can_complete || close.isPending}
              title={checklist.can_complete ? undefined : t("people.checklist.finishFirst")}
            >
              {t("people.checklist.finish")}
            </Button>
          </>
        ) : undefined
      }
    >
      <Steps
        items={checklist.steps.map((step) => ({
          label: step.title,
          state: barState(step),
          kind: step.state === "skipped" ? ("mut" as const) : undefined,
          detail: <StepDetail step={step} />,
          action: canEdit ? (
            <StepActions
              step={step}
              busy={update.isPending}
              onMark={(state) => {
                // Pending needs no reason; skipping, or forcing a tick the
                // data disagrees with, does.
                if (state === "pending" || (state === "done" && step.checked !== false && !step.destructive)) {
                  update.mutate({ key: step.key, state, note: "" });
                } else {
                  setMarking({ step, state });
                }
              }}
            />
          ) : undefined,
        }))}
      />

      {update.isError && (
        <p role="alert" className="form-error">
          {describeSubmitError(update.error)}
        </p>
      )}
      {close.isError && (
        <p role="alert" className="form-error">
          {describeSubmitError(close.error)}
        </p>
      )}

      <MarkStepDialog
        marking={marking}
        pending={update.isPending}
        onCancel={() => setMarking(null)}
        onConfirm={(note) => {
          if (!marking) return;
          update.mutate(
            { key: marking.step.key, state: marking.state, note },
            { onSuccess: () => setMarking(null) },
          );
        }}
      />

      <ConfirmDialog
        open={closing === "complete"}
        title={t(leaver ? "people.checklist.finishLeaverTitle" : "people.checklist.finishStarterTitle")}
        subject={personName}
        body={t(leaver ? "people.checklist.finishLeaverBody" : "people.checklist.finishStarterBody")}
        confirmLabel={t("people.checklist.finish")}
        onCancel={() => setClosing(null)}
        onConfirm={() => {
          setClosing(null);
          close.mutate("complete");
        }}
      />
      <ConfirmDialog
        open={closing === "cancel"}
        title={t(leaver ? "people.checklist.cancelLeaverTitle" : "people.checklist.cancelStarterTitle")}
        subject={personName}
        body={t("people.checklist.cancelBody")}
        confirmLabel={t("people.checklist.cancelChecklist")}
        onCancel={() => setClosing(null)}
        onConfirm={() => {
          setClosing(null);
          close.mutate("cancel");
        }}
      />
    </Panel>
  );
}

function StepDetail({ step }: { step: StepOut }) {
  const t = useT();
  return (
    <>
      {step.help} <span className="text-[var(--muted)]">{step.evidence}.</span>{" "}
      <Link className="underline" to={step.link}>{t("people.checklist.goToPage")}</Link>
      {step.state === "skipped" ? (
        <> · {step.note ? t("people.checklist.skippedWithNote", { note: step.note }) : t("people.checklist.skipped")}</>
      ) : null}
      {step.state === "done" && step.marked === "done" && step.note ? (
        <> · {t("people.checklist.markedDone", { note: step.note })}</>
      ) : null}
    </>
  );
}

function StepActions({
  step,
  busy,
  onMark,
}: {
  step: StepOut;
  busy: boolean;
  onMark: (state: StepState) => void;
}) {
  const t = useT();
  if (step.marked !== "pending") {
    return (
      <Button variant="ghost" disabled={busy} onClick={() => onMark("pending")}>
        {t("people.checklist.undo")}
      </Button>
    );
  }
  if (step.state === "done") return <span className="text-[var(--muted)]">{t("people.checklist.done")}</span>;
  return (
    <span className="flex gap-2">
      <Button variant="outline" disabled={busy} onClick={() => onMark("done")}>
        {t("people.checklist.markDone")}
      </Button>
      <Button variant="ghost" disabled={busy} onClick={() => onMark("skipped")}>
        {t("people.checklist.skip")}
      </Button>
    </span>
  );
}

function MarkStepDialog({
  marking,
  pending,
  onCancel,
  onConfirm,
}: {
  marking: { step: StepOut; state: StepState } | null;
  pending: boolean;
  onCancel: () => void;
  onConfirm: (note: string) => void;
}) {
  const t = useT();
  const [note, setNote] = useState("");
  if (!marking) return null;
  const skipping = marking.state === "skipped";
  const disagrees = !skipping && marking.step.checked === false;
  const needsNote = skipping || disagrees;
  return (
    <Dialog
      open
      onClose={() => {
        setNote("");
        onCancel();
      }}
      title={t(skipping ? "people.checklist.skipTitle" : "people.checklist.markDoneTitle", {
        title: marking.step.title,
      })}
    >
      {disagrees ? (
        <p className="notice gold">
          <span>{t("people.checklist.stillSees", { evidence: marking.step.evidence })}</span>
        </p>
      ) : null}
      {!disagrees && marking.step.destructive && !skipping ? (
        <p className="note">{t("people.checklist.confirmDone")}</p>
      ) : null}
      <Field label={needsNote ? t("people.checklist.why") : t("people.checklist.noteOptional")}>
        <textarea
          className={inputClass}
          rows={3}
          maxLength={500}
          value={note}
          onChange={(event) => setNote(event.target.value)}
        />
      </Field>
      <div className="flex justify-end gap-2" style={{ marginTop: 12 }}>
        <Button
          variant="ghost"
          onClick={() => {
            setNote("");
            onCancel();
          }}
        >
          {t("people.checklist.back")}
        </Button>
        <Button
          disabled={pending || (needsNote && !note.trim())}
          onClick={() => {
            onConfirm(note.trim());
            setNote("");
          }}
        >
          {skipping ? t("people.checklist.skipStep") : t("people.checklist.markDone")}
        </Button>
      </div>
    </Dialog>
  );
}
