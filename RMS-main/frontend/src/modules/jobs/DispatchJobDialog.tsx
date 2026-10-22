/**
 * Dispatching a signed job at one endpoint.
 *
 * THREE AUDIT FINDINGS MEET IN THIS DIALOG, and they are not independent:
 *
 *   [HIGH] "Production job-type dropdown exposes a developer test job called
 *          `hello`, pre-filled with `ping`" — and `hello` was the DEFAULT, so
 *          the path of least resistance dispatched a meaningless job.
 *   [HIGH] "Jobs can be dispatched to an offline agent with no warning … the
 *          operator believes they have taken an action; in reality the job
 *          sits in `queued` indefinitely."
 *   [BLOCKER] "Jobs cannot be cancelled, retried or requeued — the detail
 *          panel has no actions at all."
 *
 * THE THIRD IS WHY THE FIRST TWO ARE SEVERE. There is no cancel route on this
 * API (`POST /api/v1/jobs` is the only write path in `app/api/v1/jobs.py`), so
 * a dispatch is final from the console's point of view: the only place an
 * operator can stop a wrong job is BEFORE it is signed. That is what the
 * confirmation below is for, and why it names the machine — cycle 8's rule is
 * that the subject is the load-bearing part, because it is what lets somebody
 * notice they have the wrong row.
 *
 * WHAT IS NOT HERE. No free-text device id: the target is the row the operator
 * clicked, which is the picker discipline of cycle 6 arriving by a different
 * road. No `hello`, and no message box — see `jobTypes.ts`; neither production
 * job type carries params, so there is no field to leave empty, which retires
 * "[HIGH] Job dispatch accepts an empty Message with no validation" by removing
 * the field rather than validating it.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import type { Device, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime } from "../../lib/datetime";
import { Button, Dialog, Field, inputClass } from "../../components/ui";
import { humaniseEnum } from "../../lib/labels";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { DISPATCHABLE_JOB_TYPES, jobTypeLabel } from "./jobTypes";

// Built at submit time: the label is translated text.
const typeRules = (): readonly FieldRule[] => [{ name: "type", label: translate("jobs.dispatch.jobType") }];

/** The name an operator recognises. An asset may have no hostname at all. */
function deviceLabel(device: Device | null): string {
  if (device == null) return "";
  return device.hostname || device.primary_ip || device.description || translate("jobs.dispatch.thisAsset");
}

export function DispatchJobDialog({
  device,
  onClose,
}: {
  device: Device | null;
  onClose: () => void;
}) {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();
  // NO DEFAULT JOB TYPE. "Default to no selection so the user must make a
  // deliberate choice" is the audit's own remedy for a dropdown whose default
  // was a smoke test.
  const [type, setType] = useState("");
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [confirming, setConfirming] = useState(false);

  // Same `["me"]` key and staleTime as every other route, so the profile zone
  // costs no extra request. A null zone falls the helper back to the browser.
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const zone = me.data?.timezone;

  const chosen = DISPATCHABLE_JOB_TYPES.find((entry) => entry.type === type) ?? null;

  // `agent_status` is null exactly when no `agents` row exists — an asset
  // recorded by hand, warehouse stock, a switch. Nothing signed can reach it.
  const agentState = device?.agent_status ?? null;
  const unreachable = agentState === null;
  const offline = agentState === "offline";

  const dispatch = useMutation({
    mutationFn: () =>
      api.dispatchJob({ type, device_id: device?.id ?? "", params: {} }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      // DELIBERATELY NOT `onClose()`. Closing here was the audit's "Dispatching
      // a signed job gives no confirmation of any kind — the form just
      // disappears": a vanishing dialog looks identical whether the job was
      // queued or the request died, which on a command-and-control plane is
      // the worst possible ambiguity. The confirmation below is the server's
      // 201 and nothing else; the operator dismisses it themselves.
    },
  });

  // This dialog is never unmounted — the parents keep it rendered and toggle
  // `device` — so without this the "Job dispatched." from the last device sits
  // in the dialog for the next one, claiming a job that was never sent.
  useEffect(() => {
    dispatch.reset();
    setAttempt(null);
    setType("");
    setConfirming(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed on the
    // target device only; `dispatch` is a fresh object every render.
  }, [device?.id]);

  function requestDispatch() {
    // Held as rules + values, not as sentences, so a language switch rewords them.
    const next: Attempt = { rules: typeRules, values: { type } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    // The submit does not dispatch. It asks. See the header: there is no
    // cancel path once this job is signed.
    setConfirming(true);
  }

  const confirmBody = [
    t("jobs.dispatch.confirmRuns", { type: chosen?.label ?? type }),
    offline ? t("jobs.dispatch.confirmOffline", { lastSeen: formatDateTime(device?.last_seen_at, zone) }) : "",
    t("jobs.dispatch.confirmNoCancel"),
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <Dialog
      open={device != null}
      onClose={onClose}
      title={t("jobs.dispatch.title", { device: deviceLabel(device) })}
    >
      <Form onSubmit={requestDispatch}>
        <div className="form-grid">
          <Field label={t("jobs.dispatch.jobType")} full>
            <select
              className={inputClass}
              value={type}
              onChange={(event) => setType(event.target.value)}
            >
              <option value="">{t("jobs.dispatch.selectType")}</option>
              {DISPATCHABLE_JOB_TYPES.map((entry) => (
                <option key={entry.type} value={entry.type}>
                  {entry.label}
                </option>
              ))}
            </select>
          </Field>

          {/* WHAT THE JOB DOES, beside the choice rather than in documentation
              nobody opens. The audit: an operator "cannot tell what
              `inventory.collect` does, how long it takes, or whether it is
              disruptive". The wire type stays visible because this is an
              evidence console — it is the string that appears in the queue, in
              the audit log and in the agent's own logs. */}
          {chosen ? (
            <p className="form-hint">
              {chosen.description} <span className="mono">({chosen.type})</span>
            </p>
          ) : null}

          {/* THE OFFLINE WARNING IS NOT A BLOCK. An offline agent is a normal
              reason to queue work — a laptop in a bag catches up when it opens.
              What the audit found unacceptable was dispatching into that state
              believing it had happened. */}
          {offline ? (
            <p role="status" className="notice gold">
              {t("jobs.dispatch.offlineNotice", { lastSeen: formatDateTime(device?.last_seen_at, zone) })}
            </p>
          ) : null}
        </div>

        <div className="flex flex-wrap items-center justify-end gap-2">
          {/* `type="button"` is load-bearing: `ui.tsx`'s Button sets no type,
              and an untyped button inside a form submits it — Cancel would
              dispatch the job. */}
          <Button type="button" variant="ghost" onClick={onClose}>
            {dispatch.isSuccess ? t("common.actions.close") : t("common.actions.cancel")}
          </Button>
          {/* Enabled with no job type chosen ON PURPOSE: a disabled button
              that never says why IS the silent no-op. The one thing that does
              disable it is a target nothing signed can reach — and it says so
              beside the control. */}
          <FormActions
            label={t("jobs.dispatch.send")}
            pending={dispatch.isPending}
            error={dispatch.error}
            saved={dispatch.isSuccess}
            problems={attemptProblems(attempt)}
            disabledReason={
              unreachable
                ? t("jobs.dispatch.noAgent")
                : undefined
            }
            savedMessage={
              dispatch.data
                ? t("jobs.dispatch.sentWithStatus", {
                    type: jobTypeLabel(dispatch.data.type),
                    status: humaniseEnum(dispatch.data.status, "JobStatus").toLowerCase(),
                  })
                : t("jobs.dispatch.sent")
            }
          />
        </div>
      </Form>

      <ConfirmDialog
        open={confirming}
        title={t("jobs.dispatch.confirmTitle")}
        subject={deviceLabel(device)}
        body={confirmBody}
        confirmLabel={t("jobs.dispatch.confirmLabel")}
        destructive
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          dispatch.mutate();
        }}
      />
    </Dialog>
  );
}
