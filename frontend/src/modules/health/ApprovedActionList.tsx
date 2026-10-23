import { useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { Badge, Button, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { ApiError } from "../../lib/api";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { humaniseEnum } from "../../lib/labels";
import { jobTypeLabel } from "../jobs/jobTypes";
import { formatDateTime } from "../../lib/datetime";
import { Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KIND_ICON } from "../../shell/viz";
import {
  useApprovedActions,
  useExemptScriptMutation,
  useRevokeScriptExemptionMutation,
  useUpdateApprovedActionMutation,
  useUserTimezone,
} from "./api";
import { AUTONOMY_PRESENTATION, formatSeconds, isAutonomous } from "./healthState";
import type { ApprovedActionOut } from "./types";

// The approved-action registry (spec §2.9, §13) — what a machine is permitted
// to run on an endpoint as SYSTEM/root, and under what governance.
//
// `autonomy='autonomous'` IS RENDERED DISTINCTLY, ALWAYS, and the row itself is
// marked, not only a badge inside it. Spec §13 states the reason plainly: "a
// page where 'runs itself on 400 machines' looks like every other row is a page
// that gets clicked past". The label is deliberately a sentence rather than a
// word.
//
// SCRIPT EXEMPTIONS ARE A FIRST-CLASS VIEW ON THIS PAGE (spec §2.10), not a
// hidden flag: an exemption OUTLIVES the action that motivated it, so a later
// action pinning the same `(script_id, script_version)` inherits a grant made
// for a different purpose. The mitigation §2.10 accepts is visibility — script
// name, pinned version, grantor, reason and grant time — because an invisible
// standing exemption is one nobody revokes.
//
// THE PIN-VERSUS-LIVE VERSION IS SHOWN WHENEVER THEY DIFFER. An action whose
// pinned `script_version` no longer matches the live script REFUSES to dispatch
// (`actions._refuse_reason`), and finding that out from a `last_error` at 03:00
// with an alert already open is the failure this column exists to prevent.

export function ApprovedActionList({ canActionAdmin }: { canActionAdmin: boolean }) {
  const t = useT();
  const actions = useApprovedActions();
  const zone = useUserTimezone();
  const update = useUpdateApprovedActionMutation();
  const exempt = useExemptScriptMutation();
  const revoke = useRevokeScriptExemptionMutation();
  const [reasons, setReasons] = useState<Record<string, string>>({});
  // WHAT THIS PAGE'S BUTTONS ACTUALLY DO (UX remediation cycle 8). Every
  // control below decides what a machine may run as SYSTEM/root on endpoints:
  //
  //   * Activate arms an action -- and for `autonomy: autonomous` that means
  //     it will run with no human in the loop on every device whose check
  //     declares it. That is the audit's "a security-relevant setting that
  //     changes on one click with no confirmation".
  //   * Grant exemption declares a script version NON-destructive. The grant
  //     is keyed to `(script_id, script_version)`, so it OUTLIVES the action
  //     that motivated it and any later action pinning that version inherits
  //     it (spec §2.10) -- a consequence no button label can carry.
  //   * Revoke can DEMOTE an autonomous action to require_approval, which
  //     silently stops remediation people believe is still running.
  //
  // One pending decision at a time, holding the row, so the dialog can name
  // the action rather than asking "are you sure?" about an unnamed one.
  const [pending, setPending] = useState<PendingDecision | null>(null);

  const forbidden = actions.error instanceof ApiError && actions.error.status === 403;
  // `actions.data ?? []` USED TO SAY SOMETHING FALSE. An empty registry renders
  // "No approved actions. No remediation can be dispatched by this
  // organisation." — a statement about this org's governance. A 500 produced
  // the same empty array and therefore the same sentence, telling an operator
  // that nothing can run automatically at the moment the page could not find
  // out what can.
  const loadFailed = Boolean(actions.error) && !forbidden;
  const rows = actions.data ?? [];
  const autonomous = rows.filter(isAutonomous);
  const exemptions = rows.filter((action) => action.script_exemption !== null);

  if (forbidden) {
    return (
      <Panel title={t("health.actions.title")}>
        <p role="alert" className="note">
          {t("health.actions.forbidden")}
        </p>
      </Panel>
    );
  }
  if (loadFailed) {
    return (
      <Panel title={t("health.actions.title")}>
        <div role="alert" className="notice red">
          <Icon name={KIND_ICON.crit} size={17} />
          <div>{t("health.actions.loadFailed", { detail: describeSubmitError(actions.error) })}</div>
        </div>
      </Panel>
    );
  }

  return (
    <>
      <Panel
        title={t("health.actions.title")}
        subtitle={t("health.actions.subtitle")}
      >
        <>
          {autonomous.length > 0 && (
            <div className="notice red mb-3">
              <Icon name={KIND_ICON.crit} size={17} />
              <span>
                {/* "1 action(s) run automatically" was the one parenthetical
                    plural left in the console, on the loudest sentence this
                    page says. Both halves agree with the count -- the noun and
                    the verb -- because "1 actions run" is the same defect
                    wearing a different hat. */}
                <strong>{t("health.actions.autoCount", { count: autonomous.length })}</strong>{" "}
                {t("health.actions.autoRest", { count: autonomous.length })}
              </span>
            </div>
          )}

          {rows.length === 0 ? (
            <p className="note">{t("health.actions.empty")}</p>
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>{t("health.actions.colAction")}</Th>
                  <Th>{t("health.actions.colWhatRuns")}</Th>
                  <Th>{t("health.actions.colRisk")}</Th>
                  <Th>{t("health.actions.colDestructive")}</Th>
                  <Th>{t("health.actions.colAutonomy")}</Th>
                  <Th>{t("health.actions.colLimits")}</Th>
                  <Th>{t("health.actions.colActive")}</Th>
                  <Th> </Th>
                </tr>
              </thead>
              <tbody>
                {rows.map((action) => (
                  <tr
                    key={action.id}
                    data-autonomy={action.autonomy}
                    // The ROW is marked, not only the badge in it: the
                    // critical edge and wash on an autonomous action are what
                    // stop "runs itself on 400 machines" looking like every
                    // other row.
                    className={isAutonomous(action) ? "wash-crit" : undefined}
                  >
                    <Td
                      lead={<Icon name={isAutonomous(action) ? "robot" : "bolt"} size={18} />}
                      gutter={isAutonomous(action) ? "crit" : undefined}
                    >
                      <div className="cell-title">{action.name || action.key}</div>
                      <div className="cell-sub mono">{action.key}</div>
                    </Td>
                    <Td>
                      <div>{jobTypeLabel(action.job_type)}</div>
                      <div className="cell-sub mono">{action.job_type}</div>
                      {action.script_name && (
                        <div className="cell-sub">
                          {action.script_name} v{action.script_version}
                        </div>
                      )}
                      {/* A SENTENCE IS NOT A PILL, AND THIS ONE WAS SETTING THE
                          TABLE'S WIDTH. Twelve words inside a `Badge` are
                          twelve words that cannot wrap, so this cell demanded
                          359px of a 1122px scroller and pushed the ACTIVE
                          column's "inactive" pill through the panel's right
                          edge -- the one row whose state is the interesting one
                          was the one nobody could read. Measured after: the
                          table is 1102px and every column ends inside the
                          panel.

                          Nothing is dropped or softened. The pill keeps the
                          fact that this is a destructive-class problem and
                          keeps its colour and icon; the consequence follows it
                          as prose, which is what it always was. Same remedy,
                          same reasoning, as the profile table's two badge
                          sentences in round 2. */}
                      {action.script_name &&
                        action.script_live_version !== null &&
                        action.script_live_version !== action.script_version && (
                          <div className="cell-sub">
                            <Badge variant="destructive">{t("health.actions.scriptEdited")}</Badge>{" "}
                            {t("health.actions.scriptEditedBody", { version: action.script_live_version })}
                          </div>
                        )}
                    </Td>
                    <Td>
                      <Badge
                        variant={
                          action.risk_class === "high"
                            ? "destructive"
                            : action.risk_class === "medium"
                              ? "warning"
                              : "muted"
                        }
                      >
                        {humaniseEnum(action.risk_class, "RiskClass")}
                      </Badge>
                    </Td>
                    <Td>
                      {action.destructive ? (
                        <Badge variant="destructive">{t("health.actions.destructive")}</Badge>
                      ) : (
                        <Badge variant="muted">{t("health.actions.nonDestructive")}</Badge>
                      )}
                    </Td>
                    <Td>
                      <Badge variant={AUTONOMY_PRESENTATION[action.autonomy].variant}>
                        {AUTONOMY_PRESENTATION[action.autonomy].label}
                      </Badge>
                      <div className="cell-sub">
                        {AUTONOMY_PRESENTATION[action.autonomy].meaning}
                      </div>
                    </Td>
                    <Td>
                      <span className="cell-sub mono">
                        {t("health.actions.limits", {
                          cooldown: formatSeconds(action.cooldown_s),
                          perDevice: action.max_per_device_per_day,
                          perOrg: action.max_per_org_per_hour,
                        })}
                      </span>
                    </Td>
                    <Td>
                      {action.active ? (
                        <Badge variant="success">{t("health.actions.active")}</Badge>
                      ) : (
                        <Badge variant="muted">{t("health.actions.inactive")}</Badge>
                      )}
                    </Td>
                    <Td>
                      {canActionAdmin && (
                        // Not red: switching an automatic fix off is the safe direction.
                        <Button
                          variant="outline"
                          onClick={() => {
                            update.reset();
                            setPending({ kind: "active", action });
                          }}
                        >
                          {action.active ? t("health.actions.deactivate") : t("health.actions.activate")}
                        </Button>
                      )}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}

        </>
      </Panel>

      <Panel
        title={t("health.actions.exemptTitle")}
        subtitle={t("health.actions.exemptSubtitle")}
      >
        <>
          {exemptions.length === 0 ? (
            <p className="note">{t("health.actions.noExemptions")}</p>
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>{t("health.actions.colScript")}</Th>
                  <Th>{t("health.actions.colVersion")}</Th>
                  <Th>{t("health.actions.colGrantedBy")}</Th>
                  <Th>{t("health.actions.colGranted")}</Th>
                  {/* The reason a person typed is a sentence, and at 390 it was
                      given ~80px to be a sentence in: the single exemption row
                      measured ~200px tall with a 70px empty band above and below
                      `Restart Spooler`, because the height came from a column
                      the reader could not see. It keeps wrapping -- a reason
                      clipped to one line is a reason nobody can act on -- and
                      gets a floor to wrap within. */}
                  <Th width={280}>{t("health.actions.colReason")}</Th>
                  <Th> </Th>
                </tr>
              </thead>
              <tbody>
                {exemptions.map((action) => (
                  <tr key={`exemption-${action.id}`}>
                    <Td lead={<Icon name="file-text" size={18} />} clip={200}>
                      <div className="cell-title">
                        {action.script_exemption?.script_name ?? action.script_id}
                      </div>
                    </Td>
                    <Td>
                      <span className="mono">v{action.script_exemption?.script_version}</span>
                    </Td>
                    <Td>{action.script_exemption?.granted_by}</Td>
                    <Td>
                      {formatDateTime(action.script_exemption?.granted_at, zone)}
                    </Td>
                    <Td>{action.script_exemption?.reason || t("health.actions.noReason")}</Td>
                    <Td>
                      {canActionAdmin && (
                        <Button
                          variant="outline"
                          className="danger"
                          onClick={() => {
                            revoke.reset();
                            setPending({ kind: "revoke", action });
                          }}
                        >
                          {t("health.actions.revoke")}
                        </Button>
                      )}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}

          {canActionAdmin && (
            <ExemptForm
              rows={rows}
              reasons={reasons}
              setReasons={setReasons}
              onGrant={(action) => {
                exempt.reset();
                setPending({ kind: "exempt", action });
              }}
            />
          )}

          {pending && (
            <ConfirmDialog
              open
              title={decisionTitle(pending)}
              subject={decisionSubject(pending)}
              body={decisionBody(pending)}
              confirmLabel={decisionConfirmLabel(pending)}
              /* `destructive` marks what cannot be undone by pressing the same
                 button again. Activating an autonomous action can put a signed
                 job on the fleet before anyone reaches the Deactivate button,
                 and an exemption re-derives `destructive` on every action
                 pinning that version. Deactivate and Revoke are reversible in
                 one click and are styled as ordinary confirmations. */
              destructive={isDestructiveDecision(pending)}
              onCancel={() => setPending(null)}
              onConfirm={() => {
                const decision = pending;
                setPending(null);
                if (decision.kind === "active") {
                  update.mutate({
                    id: decision.action.id,
                    body: { active: !decision.action.active },
                  });
                } else if (decision.kind === "revoke") {
                  revoke.mutate(decision.action.id);
                } else {
                  exempt.mutate({
                    id: decision.action.id,
                    reason: reasons[decision.action.id] ?? "",
                  });
                }
              }}
            />
          )}

          {/* LABELLED and ANNOUNCED. Three unrelated controls feed this line —
              Activate/Deactivate, Grant exemption, Revoke — so an unlabelled
              sentence names none of them; and Revoke is a governance change
              whose bodyless 503 rendered as an empty paragraph, which is the
              audit's swallowed-failure BLOCKER on a control that decides what
              may run as SYSTEM. */}
          {[
            { label: t("health.actions.failedActivate"), mutation: update },
            { label: t("health.actions.failedExempt"), mutation: exempt },
            { label: t("health.actions.failedRevoke"), mutation: revoke },
          ].map(({ label, mutation }) =>
            mutation.error ? (
              <div key={label} role="alert" className="notice red mt-2">
                <Icon name={KIND_ICON.crit} size={17} />
                <div>
                  {t("health.actions.mutationFailed", { label, detail: describeSubmitError(mutation.error) })}
                </div>
              </div>
            ) : null,
          )}
        </>
      </Panel>
    </>
  );
}

/** One privileged decision, held between the click and the confirmation. */
type PendingDecision =
  | { kind: "active"; action: ApprovedActionOut }
  | { kind: "revoke"; action: ApprovedActionOut }
  | { kind: "exempt"; action: ApprovedActionOut };

export function decisionTitle(pending: PendingDecision): string {
  if (pending.kind === "revoke") return translate("health.actions.revokeTitle");
  if (pending.kind === "exempt") return translate("health.actions.exemptDecisionTitle");
  return translate(pending.action.active ? "health.actions.deactivateTitle" : "health.actions.activateTitle");
}

export function decisionSubject(pending: PendingDecision): string {
  const action = pending.action;
  if (pending.kind === "active") return `${action.key} — ${action.name}`;
  return `${action.script_name ?? action.script_id ?? action.key} v${
    action.script_exemption?.script_version ?? action.script_version
  }`;
}

export function isDestructiveDecision(pending: PendingDecision): boolean {
  if (pending.kind === "exempt") return true;
  if (pending.kind === "revoke") return false;
  return !pending.action.active && isAutonomous(pending.action);
}

export function decisionConfirmLabel(pending: PendingDecision): string {
  if (pending.kind === "revoke") return translate("health.actions.revokeLabel");
  if (pending.kind === "exempt") return translate("health.actions.grantLabel");
  return translate(pending.action.active ? "health.actions.deactivate" : "health.actions.activate");
}

/**
 * The CONSEQUENCE, in the vocabulary this registry already uses.
 *
 * Each sentence states what changes about what a machine may do on an
 * endpoint. None of them says "are you sure" -- the subject line above already
 * says which row, and this says what happens to it.
 */
export function decisionBody(pending: PendingDecision): string {
  const action = pending.action;
  if (pending.kind === "revoke") {
    return translate("health.actions.revokeBody", { key: action.key });
  }
  if (pending.kind === "exempt") {
    return translate("health.actions.exemptBody", { key: action.key });
  }
  if (action.active) {
    return translate("health.actions.deactivateBody");
  }
  return isAutonomous(action)
    ? translate("health.actions.activateAutoBody", {
        perDevice: action.max_per_device_per_day,
        perOrg: action.max_per_org_per_hour,
      })
    : translate("health.actions.activateBody");
}

function ExemptForm({
  rows,
  reasons,
  setReasons,
  onGrant,
}: {
  rows: readonly ApprovedActionOut[];
  reasons: Record<string, string>;
  setReasons: (updater: (current: Record<string, string>) => Record<string, string>) => void;
  onGrant: (action: ApprovedActionOut) => void;
}) {
  const t = useT();
  // Only `script.run` actions with no exemption yet can be granted one: the
  // pair comes from the action's own pin, and the server refuses every other
  // shape with a 400.
  const candidates = rows.filter(
    (action) => action.job_type === "script.run" && action.script_exemption === null,
  );
  if (candidates.length === 0) return null;
  return (
    <div className="mt-4 space-y-2">
      {candidates.map((action) => (
        <div key={action.id} className="flex flex-wrap items-end gap-2">
          <Field
            label={t("health.actions.exemptField", {
              key: action.key,
              script: action.script_name ?? "",
              version: action.script_version ?? "",
            })}
          >
            <input
              className={inputClass}
              placeholder={t("health.actions.exemptPlaceholder")}
              value={reasons[action.id] ?? ""}
              onChange={(e) =>
                setReasons((current) => ({ ...current, [action.id]: e.target.value }))
              }
            />
          </Field>
          <Button variant="outline" onClick={() => onGrant(action)}>
            {t("health.actions.grantLabel")}
          </Button>
        </div>
      ))}
    </div>
  );
}
