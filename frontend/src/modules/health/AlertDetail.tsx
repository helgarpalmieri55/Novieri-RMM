import { useState } from "react";
import { Link } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { Badge, Button, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { ApiError } from "../../lib/api";
import { formatDateTime } from "../../lib/datetime";
import { Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { Facts, KIND_ICON } from "../../shell/viz";
import {
  useAcknowledgeAlertMutation,
  useApproveAlertActionMutation,
  useCloseAlertMutation,
  useHealthAlert,
  useMuteAlertMutation,
  useRefuseAlertActionMutation,
  useUserTimezone,
} from "./api";
import {
  ACTION_KIND_LABEL,
  ACTION_STATE_PRESENTATION,
  ALERT_STATE_PRESENTATION,
  AUTONOMY_PRESENTATION,
  CLEARED_REASON_PRESENTATION,
  SEVERITY_PRESENTATION,
  blockedReason,
  checkLabel,
  describeObservation,
  describeRepeatPause,
  severityVariant,
} from "./healthState";
import type { AlertActionOut, AlertDetailOut } from "./types";

// The full alert context (spec §13): the action rows and their states
// (including a blocked one WITH ITS REASON), the linked ticket, the
// predecessor's ticket number, and the monitoring deep link.
//
// THE HEADER STATES THE ALERT'S MEANING, NOT ONLY ITS NAME. `acknowledged`,
// `muted` and `cleared` each carry a sentence from `healthState.ts` saying what
// the state does and does not imply, because the whole content of
// "acknowledged" is what it does NOT do -- it does not stop the check, does not
// prevent auto-clear, and does not cancel a remediation in flight.
//
// THE APPROVE/REFUSE CONTROLS ARE AN AFFORDANCE, NOT A GATE. `health.remediate`
// is checked server-side on every one of these routes (invariant #6); hiding
// the buttons prevents nothing and is done only so a technician is not offered
// a control that will 403.
//
// AND `canRemediate` IS NOT A PROMISE THAT APPROVE WILL SUCCEED (spec §20).
// Approving dispatches a job whose type carries its own module gate --
// `patch.apply` for a `patch.apply` remediation -- and the server requires that
// too. Which permission applies depends on the approved action behind the row,
// so the frontend cannot know it from `permissions` alone and deliberately does
// not try: the button stays visible and the server's 403 is rendered verbatim
// below, naming the missing code. Guessing here would either hide a control the
// user may in fact use or invent a second authorization model to disagree with
// the real one. `refuse` is not gated that way at all -- it dispatches nothing.

export function AlertDetail({
  alertId,
  canManageAlerts,
  canRemediate,
}: {
  alertId: string;
  canManageAlerts: boolean;
  canRemediate: boolean;
}) {
  const t = useT();
  const alert = useHealthAlert(alertId);
  const zone = useUserTimezone();
  const acknowledge = useAcknowledgeAlertMutation();
  const mute = useMuteAlertMutation();
  const close = useCloseAlertMutation();
  const [muteHours, setMuteHours] = useState("4");
  // CLOSING AN ALERT BY HAND IS A ONE-WAY DOOR (UX remediation cycle 8). The
  // API carries acknowledge, mute and close and NO reopen -- there is no
  // `POST /health/alerts/{id}/reopen` -- so a mis-clicked Close is a decision
  // the console cannot take back: the alert leaves the open list, the check
  // that raised it has to breach again to raise a new one, and the closure is
  // recorded as human-resolved in the reporting split. That is precisely the
  // "'resolve' where nothing can reopen it" case a confirmation is for.
  //
  // Acknowledge and Mute are deliberately NOT confirmed. Both are reversible
  // and neither leaves the open list; confirming them too would make this
  // dialog the thing people click through on their way to the one that
  // matters.
  const [confirmingClose, setConfirmingClose] = useState(false);

  // `alert.error` UNQUALIFIED, not `instanceof ApiError`. A dropped connection
  // rejects with a TypeError, which is not an `ApiError`, so it fell past this
  // branch into `!alert.data` and the panel sat on "Loading…" for as long as
  // the operator was willing to wait for an alert that was never coming.
  if (alert.error) {
    const status = alert.error instanceof ApiError ? alert.error.status : null;
    return (
      <Panel title={t("health.detail.title")}>
        <p role="alert" className="note">
          {status === 403
            ? t("health.detail.forbidden")
            : status === 404
              ? t("health.detail.notFound")
              : describeSubmitError(alert.error)}
        </p>
      </Panel>
    );
  }
  if (!alert.data) {
    return (
      <Panel title={t("health.detail.title")}>
        <div className="empty">
          <Icon name="clock" size={26} />
          <b>{t("health.detail.loading")}</b>
        </div>
      </Panel>
    );
  }

  const data: AlertDetailOut = alert.data;
  const alertMutations: ReadonlyArray<{ label: string; mutation: { error: unknown } }> = [
    { label: t("health.detail.failedAcknowledge"), mutation: acknowledge },
    { label: t("health.detail.failedMute"), mutation: mute },
    { label: t("health.detail.failedClose"), mutation: close },
  ];
  const presentation = ALERT_STATE_PRESENTATION[data.state];
  const reason = data.cleared_reason ? CLEARED_REASON_PRESENTATION[data.cleared_reason] : null;
  const open = presentation.open;
  /* THE DRAWER IS HEADED WHAT THE ROW IS HEADED. The console row now reads
     "Disk free"; a drawer headed `disk.free_pct` would make a reader match a
     sentence against an identifier to know they opened the right alert. The
     key itself is a fact below, verbatim, because it is what an operator
     greps a log or the API for. */
  const host = data.device_hostname || data.device_id;
  const title = data.selector
    ? t("health.detail.titleLineSelector", { check: checkLabel(data.check_type), selector: data.selector, host })
    : t("health.detail.titleLine", { check: checkLabel(data.check_type), host });

  return (
    <Panel
      title={title}
      // The state's MEANING, spelled out. This is the sentence that stops an
      // acknowledged alert reading as a handled one and a muted alert reading
      // as a resolved one.
      subtitle={presentation.meaning}
      actions={
        <>
          <Badge variant={severityVariant(data.severity)}>
            {SEVERITY_PRESENTATION[data.severity].label}
          </Badge>
          <Badge variant={presentation.variant}>{presentation.label}</Badge>
          {data.flapping && (
            <Badge variant="warning">{t("health.console.repeating", { count: data.flap_count })}</Badge>
          )}
        </>
      }
    >
      {reason && (
        <p className="note mb-3">
          <strong>{reason.label}.</strong> {reason.meaning}
          {!reason.countsAsResolution && (
            <>
              {" "}
              <Badge variant="warning">{t("health.detail.notFix")}</Badge>
            </>
          )}
        </p>
      )}

      <Facts
        items={[
          {
            k: t("health.detail.factDevice"),
            v: <Link to={`/devices/${data.device_id}`}>{data.device_hostname || data.device_id}</Link>,
          },
          { k: t("health.detail.factProfile"), v: data.profile_name || "—" },
          {
            // The label leads; the key stays beneath it because it is what
            // an operator searches a log or the API for.
            k: t("health.detail.factCheck"),
            v: (
              <>
                <span>{checkLabel(data.check_type)}</span>
                <span className="cell-sub mono" style={{ display: "block" }}>
                  {data.check_type}
                </span>
              </>
            ),
          },
          { k: t("health.detail.factObserved"), v: describeObservation(data) },
          { k: t("health.detail.factOpened"), v: formatDateTime(data.opened_at, zone) },
          { k: t("health.detail.factLastSeen"), v: formatDateTime(data.last_seen_at, zone) },
          { k: t("health.detail.factOccurrences"), v: String(data.occurrence_count) },
          { k: t("health.detail.factEscalated"), v: formatDateTime(data.escalated_at, zone) },
          { k: t("health.detail.factTicket"), v: data.ticket_number ?? "—" },
          { k: t("health.detail.factMutedUntil"), v: formatDateTime(data.muted_until, zone) },
          { k: t("health.detail.factAcknowledged"), v: formatDateTime(data.acknowledged_at, zone) },
        ]}
      />

      {data.previous_alert_id && (
        <p className="note mb-3">
          {/* The predecessor's TICKET NUMBER, not just its id: a chronic
              flapper is only legible if the earlier incident is reachable by
              the number a human already has. Four whole sentences, not
              fragments glued together, so each reads right in either language. */}
          {reopenedSentence(data, zone)}.
        </p>
      )}

      {data.monitoring_deep_link && (
        <p className="mb-3">
          <a
            className="underline"
            href={data.monitoring_deep_link}
            rel="noreferrer noopener"
            target="_blank"
          >
            {t("health.detail.monitoringLink")}
          </a>
        </p>
      )}

      {canManageAlerts && open && (
        <div className="mb-4 flex flex-wrap items-end gap-2">
          {data.state === "active" && (
            <Button
              onClick={() => acknowledge.mutate(alertId)}
              disabled={acknowledge.isPending}
            >
              {t("health.detail.acknowledge")}
            </Button>
          )}
          {(data.state === "active" || data.state === "acknowledged") && (
            <>
              <Field label={t("health.detail.muteFor")}>
                <input
                  className={inputClass}
                  type="number"
                  min={1}
                  value={muteHours}
                  onChange={(e) => setMuteHours(e.target.value)}
                />
              </Field>
              <Button
                variant="outline"
                disabled={mute.isPending}
                onClick={() =>
                  mute.mutate({
                    id: alertId,
                    mutedUntil: new Date(
                      Date.now() + Number(muteHours || "0") * 3600_000,
                    ).toISOString(),
                  })
                }
              >
                {t("health.detail.mute")}
              </Button>
            </>
          )}
          <Button
            variant="outline"
            className="danger"
            disabled={close.isPending}
            onClick={() => {
              close.reset();
              setConfirmingClose(true);
            }}
          >
            {t("health.detail.closeManually")}
          </Button>
        </div>
      )}
      <ConfirmDialog
        open={confirmingClose}
        title={t("health.detail.closeTitle")}
        subject={title}
        body={t("health.detail.closeBody")}
        confirmLabel={t("health.detail.closeLabel")}
        destructive
        onCancel={() => setConfirmingClose(false)}
        onConfirm={() => {
          setConfirmingClose(false);
          close.mutate(alertId);
        }}
      />

      {/* LABELLED, and announced. Three controls sit above this line, so an
          unlabelled red sentence does not say which of them refused; and a
          bodyless 503 used to make the sentence itself empty, so the operator
          saw a Close that appeared to work. */}
      {alertMutations.map(({ label, mutation }) =>
        mutation.error ? (
          <div key={label} role="alert" className="notice red mb-2">
            <Icon name={KIND_ICON.crit} size={17} />
            <div>
              {t("health.detail.mutationFailed", { label, detail: describeSubmitError(mutation.error) })}
            </div>
          </div>
        ) : null,
      )}

      {data.actions.length === 0 ? (
        // `headingLevel={3}`: this panel is INSIDE the "Alert" panel above,
        // and an h2 inside an h2 skips a rung of the outline (axe
        // `heading-order`, WCAG 1.3.1).
        <Panel title={t("health.detail.noActionsTitle")} headingLevel={3}>
          <p className="note">{t("health.detail.noActions")}</p>
        </Panel>
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {data.actions.map((action) => (
            <ActionRow
              key={action.id}
              alertId={alertId}
              action={action}
              flapCount={data.flap_count}
              canRemediate={canRemediate}
            />
          ))}
        </ul>
      )}
    </Panel>
  );
}

/**
 * WHICH remediation is being approved, in the words the registry uses.
 *
 * The pinned script VERSION is part of the subject rather than a detail
 * underneath it: an action whose pin no longer matches the live script refuses
 * to dispatch, and "nobody approves a script they cannot identify" (spec §13)
 * means identifying the exact version, not the name it shares with every
 * later edit.
 */
export function describeApprovalSubject(
  action: Pick<
    AlertActionOut,
    "kind" | "approved_action_key" | "approved_action_name" | "script_name" | "script_version"
  >,
): string {
  const parts: string[] = [action.approved_action_key || action.kind];
  if (action.approved_action_name) parts.push(action.approved_action_name);
  if (action.script_name) parts.push(`${action.script_name} v${action.script_version}`);
  return parts.join(" — ");
}

/** "Re-opened after a previous alert from <when> (ticket <number>)", without the full stop. */
function reopenedSentence(
  data: Pick<AlertDetailOut, "previous_alert_opened_at" | "previous_alert_ticket_number">,
  zone: string | null | undefined,
): string {
  const when = data.previous_alert_opened_at ? formatDateTime(data.previous_alert_opened_at, zone) : null;
  const number = data.previous_alert_ticket_number;
  if (when !== null && number) return translate("health.detail.reopenedBoth", { when, number });
  if (when !== null) return translate("health.detail.reopenedFrom", { when });
  if (number) return translate("health.detail.reopenedTicket", { number });
  return translate("health.detail.reopened");
}

function ActionRow({
  alertId,
  action,
  flapCount,
  canRemediate,
}: {
  alertId: string;
  action: AlertActionOut;
  /** The alert's repeat count, for the plain-words pause explanation. */
  flapCount: number;
  canRemediate: boolean;
}) {
  const t = useT();
  const approve = useApproveAlertActionMutation();
  const refuse = useRefuseAlertActionMutation();
  const zone = useUserTimezone();
  const [reason, setReason] = useState("");
  // Approving DISPATCHES A SIGNED JOB that runs as SYSTEM/root on a real
  // endpoint, and for a `destructive` action the endpoint cannot undo it
  // either. Cycle 8's rule is "every irreversible or privileged action gets a
  // confirmation naming the subject": the subject here is the approved-action
  // key plus the pinned script version, because that pair -- not the row's
  // position in a list -- is what the operator is actually authorising.
  //
  // Refuse is not confirmed: it dispatches nothing, and the action expires on
  // its own anyway.
  const [confirmingApprove, setConfirmingApprove] = useState(false);
  const state = ACTION_STATE_PRESENTATION[action.state];
  const blocked = blockedReason(action);

  return (
    <li>
      {/* One panel per action: the kind as the title, the state and its
          governance as pills, and the state's MEANING as the sub-line. A rung
          below the "Alert" panel that contains this list, so `h3`. */}
      <Panel
        headingLevel={3}
        title={ACTION_KIND_LABEL[action.kind] ?? action.kind}
        subtitle={state.meaning}
        actions={
          <>
            <Badge variant={state.variant}>{state.label}</Badge>
            {action.autonomy && (
              <Badge variant={AUTONOMY_PRESENTATION[action.autonomy].variant}>
                {AUTONOMY_PRESENTATION[action.autonomy].label}
              </Badge>
            )}
            {action.destructive && <Badge variant="destructive">{t("health.detail.destructive")}</Badge>}
          </>
        }
      >
        {action.approved_action_key && (
          <p className="mb-1">
            {t("health.detail.approvedAction", {
              name: action.approved_action_name || action.approved_action_key,
            })}
            {action.approved_action_name ? (
              <>
                {" "}
                <code>{action.approved_action_key}</code>
              </>
            ) : null}
          </p>
        )}
        {/* Spec §13: "nobody approves a script they cannot identify". The pinned
            NAME and VERSION, both, because a version-less name approves whatever
            that script says tomorrow. */}
        {action.script_name && (
          <p className="mb-1">
            {t("health.detail.pinnedScript")} <strong>{action.script_name}</strong> v{action.script_version}
          </p>
        )}
        {action.job_id && (
          <p className="note mb-1">
            {t("health.detail.job")} <span className="mono">{action.job_id}</span>
          </p>
        )}
        {action.expires_at && action.state === "awaiting_approval" && (
          <p className="note mb-1">
            {t("health.detail.approvalExpires", { when: formatDateTime(action.expires_at, zone) })}
          </p>
        )}

        {/* WHY it was blocked, verbatim: the fuse, its window, and the job ids
            counted against it. Two approved actions sharing a job type share one
            fuse, and a manual run consumes the same budget — so nothing else on
            this row can answer the question. */}
        {/* A repeat pause is explained in words; the server's sentence is
            the engine's, so it sits behind a disclosure. A rate-limit block
            stays verbatim, because only it names the limit and the jobs. */}
        {action.state === "blocked_flapping" ? (
          <div className="notice gold mb-2">
            <Icon name={KIND_ICON.warn} size={17} />
            <div>
              <strong>{t("health.detail.paused")}</strong> {describeRepeatPause(flapCount)}
              {blocked ? (
                <details className="mt-1">
                  <summary className="cell-sub">{t("health.detail.whatServerRecorded")}</summary>
                  <span className="mono cell-sub">{blocked}</span>
                </details>
              ) : null}
            </div>
          </div>
        ) : blocked !== null ? (
          <div className="notice red mb-2">
            <Icon name={KIND_ICON.crit} size={17} />
            <div>
              <strong>{t("health.detail.blocked")}</strong> {blocked}
            </div>
          </div>
        ) : null}
        {blocked === null && action.last_error && (
          <p className="note mb-1">{action.last_error}</p>
        )}

        {canRemediate && action.state === "awaiting_approval" && (
          <div className="mt-2 flex flex-wrap items-end gap-2">
            <Button
              disabled={approve.isPending}
              onClick={() => {
                approve.reset();
                setConfirmingApprove(true);
              }}
            >
              {t("health.detail.approve")}
            </Button>
            <Field label={t("health.detail.refusalReason")}>
              <input
                className={inputClass}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </Field>
            <Button
              variant="outline"
              disabled={refuse.isPending}
              onClick={() => refuse.mutate({ alertId, actionId: action.id, reason })}
            >
              {t("health.detail.refuse")}
            </Button>
          </div>
        )}
        <ConfirmDialog
          open={confirmingApprove}
          title={action.destructive ? t("health.detail.approveDestructiveTitle") : t("health.detail.approve")}
          subject={describeApprovalSubject(action)}
          body={action.destructive ? t("health.detail.approveBodyDestructive") : t("health.detail.approveBody")}
          confirmLabel={t("health.detail.approve")}
          // `destructive` is nullable on the wire; ConfirmDialog takes a
          // boolean, and a null there is "not marked" rather than "unknown".
          destructive={action.destructive ?? false}
          onCancel={() => setConfirmingApprove(false)}
          onConfirm={() => {
            setConfirmingApprove(false);
            approve.mutate({ alertId, actionId: action.id });
          }}
        />

        {approve.error && (
          <div role="alert" className="notice red mt-2">
            <Icon name={KIND_ICON.crit} size={17} />
            <div>{t("health.detail.approveFailed", { detail: describeSubmitError(approve.error) })}</div>
          </div>
        )}
        {refuse.error && (
          <div role="alert" className="notice red mt-2">
            <Icon name={KIND_ICON.crit} size={17} />
            <div>{t("health.detail.refuseFailed", { detail: describeSubmitError(refuse.error) })}</div>
          </div>
        )}
      </Panel>
    </li>
  );
}
