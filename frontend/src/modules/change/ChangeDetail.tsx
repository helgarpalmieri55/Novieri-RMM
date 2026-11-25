import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { Button, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useClientScope } from "../../lib/clientScope";
import { formatDateTime } from "../../lib/datetime";
import {
  useAddChangeLinkMutation,
  useApproveChangeMutation,
  useChange,
  useChangeApprovals,
  useChangeLinks,
  useLinkableDeployments,
  useLinkablePackages,
  useLinkableProvisionProfiles,
  useLinkableProvisionRuns,
  useRejectChangeMutation,
  useRemoveChangeLinkMutation,
  useSubmitChangeMutation,
  useTransitionChangeMutation,
  useUserTimezone,
} from "./api";
import {
  availableChangeActions,
  hiddenActionsReason,
  noActionsReason,
} from "./changeActions";
import type { ChangeActionSpec } from "./changeActions";
import {
  changeStateKind,
  changeStateLabel,
  changeTypeLabel,
  decisionLabel,
  linkActionLabel,
  riskKind,
  riskLabel,
} from "./changeState";
import {
  deploymentPickerEmptyHint,
  deploymentPickerOptions,
  provisionRunPickerEmptyHint,
  provisionRunPickerOptions,
} from "./pickerOptions";
import type { LinkActionType } from "./types";
import { ClientContext, DetailHead, Panel, QueryState, StatusPill, UrlTabs, useUrlTab } from "../../shell/components";
import { useRecordTitle } from "../../shell/title";
import { Icon } from "../../shell/icons";
import { Facts, KV, kindOf } from "../../shell/viz";

const LINK_ACTION_TYPES: LinkActionType[] = [
  "patch_run",
  "deployment",
  "provision_run",
  "patch_approval",
];

// The two types that have a picker. `patch_run` and `patch_approval` keep the
// hand-typed field. Routes DO list both now (`GET /patch/runs`,
// `GET /patch/approvals`), so each could become a picker; until one is built
// the field stays and the note under it says where the console shows them --
// see pickerOptions.ts's header.
const PICKABLE: Record<string, { label: MessageKey; hint: () => string; search: MessageKey } | undefined> = {
  deployment: { label: "change.detail.deployment", hint: deploymentPickerEmptyHint, search: "change.detail.searchDeployments" },
  provision_run: {
    label: "change.detail.provisionRun",
    hint: provisionRunPickerEmptyHint,
    search: "change.detail.searchRuns",
  },
};

// `action_id` is a `UUID` in change_mgmt/schema.py's LinkCreateRequest, so
// anything else is a 422 discovered after the round trip. Auditor B (HIGH),
// /changes/:id → Link action: "Clicking 'Link' with the field empty does
// nothing at all — no error, table still reads 'No linked actions.'" —
// `disabled={addLink.isPending || !actionId}` refused the click and named no
// field. Absence and shape answer through the one list, because a second
// mechanism for "wrong" beside the mechanism for "missing" is how a product
// ends up with four error styles.
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const EXAMPLE_ACTION_ID = "06a85c19-55e2-7f3c-8000-831c7bdba357";

function invalidActionId(value: string): string | null {
  if (!UUID_PATTERN.test(value)) {
    return translate("change.detail.invalidActionId", { example: EXAMPLE_ACTION_ID, value });
  }
  return null;
}

// The label follows the control on screen: a picker names what was not chosen
// ("Deployment is required"), the raw field names the id it still wants.
function linkRules(actionType: LinkActionType): readonly FieldRule[] {
  const pickable = PICKABLE[actionType];
  if (pickable) return [{ name: "actionId", label: translate(pickable.label) }];
  return [{ name: "actionId", label: translate("change.detail.actionId"), validate: invalidActionId }];
}

/**
 * ONE CHANGE AS A RECORD (redesign Task 11, 6.2; Receta F): the kit's head
 * (the change's title, its state, its risk and its client; Export evidence),
 * and its three sections in `?tab=` tabs, so a tab reloads and links: the
 * summary with the lifecycle decision, the approval history, and the linked
 * actions with the form that links one.
 */
const TABS = ["overview", "approvals", "links"] as const;
type Tab = (typeof TABS)[number];
const TAB_LABEL: Record<Tab, MessageKey> = {
  overview: "change.detail.summary",
  approvals: "change.detail.approvalHistory",
  links: "change.detail.linkedActions",
};

// Triggers a browser download of a Blob. No shared download helper exists
// elsewhere in src/ under modules/change; mirrors
// modules/patch/FleetPatchPage.tsx's downloadBlob.
function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function ChangeDetail() {
  const { changeId } = useParams<{ changeId: string }>();
  // ONE CHANGE, ONE PAGE (general ruling 12). Another change under the same
  // route keeps the element mounted, and with it the decision comment and the
  // link being typed, which the next approval would then send with the change
  // now shown. Keyed by the change, the page starts again.
  return <ChangeRecord key={changeId} />;
}

function ChangeRecord() {
  const t = useT();
  const { changeId } = useParams<{ changeId: string }>();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canManage = permissions.includes("change.manage");
  const canRead = permissions.includes("change.read");
  const zone = useUserTimezone();

  const change = useChange(changeId);
  const links = useChangeLinks(changeId);
  // `GET /changes/{id}/approvals` is a plain, non-auditing read — see
  // backend/app/modules/change_mgmt/api.py's list_approvals_route. Passive
  // viewing of this timeline must never write a `change.evidence_exported`
  // audit event; only the explicit "Export evidence" button below does
  // that, via the separate `/evidence` route.
  const approvals = useChangeApprovals(changeId);

  const addLink = useAddChangeLinkMutation();
  const removeLink = useRemoveChangeLinkMutation();
  const [actionType, setActionType] = useState<LinkActionType>("patch_run");
  const [actionId, setActionId] = useState("");
  // Filled ON SUBMIT, never while typing: an "Action ID is required" that
  // appears before the first keystroke has landed is its own kind of unhelpful.
  // Held as rules and values, worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [exporting, setExporting] = useState(false);
  // The caught value, not a sentence: it is worded at render so a language
  // switch re-words it.
  const [exportError, setExportError] = useState<{ error: unknown } | null>(null);

  // THE LIFECYCLE, which is the BLOCKER this page was filed for. Every one of
  // these mutations already existed in ./api.ts; nothing on the page called
  // them, so a change could be created and never left `draft`.
  const submitChange = useSubmitChangeMutation();
  const approveChange = useApproveChangeMutation();
  const rejectChange = useRejectChangeMutation();
  const transitionChange = useTransitionChangeMutation();
  const [comment, setComment] = useState("");
  const [confirming, setConfirming] = useState<ChangeActionSpec | null>(null);

  const wantDeployments = canManage && actionType === "deployment";
  const wantRuns = canManage && actionType === "provision_run";
  const deployments = useLinkableDeployments(wantDeployments);
  const packages = useLinkablePackages(wantDeployments);
  const runs = useLinkableProvisionRuns(wantRuns);
  const profiles = useLinkableProvisionProfiles(wantRuns);

  // Built at render, never memoised: the options hold worded state and
  // formatted dates, which a language switch must re-word.
  const deploymentOptions = deploymentPickerOptions(deployments.data ?? [], packages.data ?? [], zone);
  const runOptions = provisionRunPickerOptions(runs.data ?? [], profiles.data ?? [], zone);

  const specs = change.data ? availableChangeActions(change.data.state, change.data.type) : [];
  const allowed = specs.filter((spec) => permissions.includes(spec.permission));
  const hidden = specs.filter((spec) => !permissions.includes(spec.permission));
  // A cosmetic nod to the segregation-of-duties guard the backend enforces
  // regardless (state.py's next_state: "approver cannot be the change
  // requester"). Stated in words, not only as a disabled control, because a
  // control that will not move and does not say why is the finding.
  const isOwnChange = change.data != null && change.data.created_by === me.data?.user_id;
  const decidingOwn = isOwnChange && allowed.some((spec) => spec.action === "approve");

  // THE TAB HAD NO NAME (cycle 12, auditor B, LOW): with several change tabs
  // open they could not be told apart. The change's own title is the only
  // stable identity this record has (`ChangeOut` carries no change number);
  // the shell composes "<title> · Changes · Novieri RMM" from it and puts the
  // previous title back when the page goes (`useRecordTitle`, redesign Task 2).
  useRecordTitle(change.data?.title);
  const [tab, setTab] = useUrlTab<Tab>(TABS, "overview");
  // The client's name is in the head when the session knows it (a link to its 360 page).
  const { clients } = useClientScope();
  const clientShown = change.data != null && clients.some((client) => client.id === change.data?.client_id);

  const lifecyclePending =
    submitChange.isPending ||
    approveChange.isPending ||
    rejectChange.isPending ||
    transitionChange.isPending;
  const lifecycleError =
    submitChange.error ?? approveChange.error ?? rejectChange.error ?? transitionChange.error;

  if (!changeId) return null;

  function runAction(spec: ChangeActionSpec) {
    const id = changeId as string;
    const body = { comment: comment.trim() || null };
    // Only the last refusal should be on screen, so each press clears the
    // others: a 503 from a Reject left standing beside a successful Approve
    // reads as the approval having failed.
    submitChange.reset();
    approveChange.reset();
    rejectChange.reset();
    transitionChange.reset();
    if (spec.action === "approve") {
      approveChange.mutate({ id, body }, { onSuccess: () => setComment("") });
    } else if (spec.action === "reject") {
      rejectChange.mutate({ id, body }, { onSuccess: () => setComment("") });
    } else if (spec.action === "submit") {
      submitChange.mutate(id);
    } else {
      transitionChange.mutate({ id, body: { action: spec.action } });
    }
  }

  function press(spec: ChangeActionSpec) {
    if (spec.confirm) {
      setConfirming(spec);
      return;
    }
    runAction(spec);
  }

  function submitLink() {
    // `type` is this render's value, so the rules do not read live state.
    const type = actionType;
    const next: Attempt = { rules: () => linkRules(type), values: { actionId } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    addLink.mutate(
      // Trimmed to match what was validated — `requiredProblems` trims before
      // testing, so an untrimmed send could carry an id the checks never saw.
      { id: changeId as string, body: { action_type: actionType, action_id: actionId.trim() } },
      { onSuccess: () => setActionId("") },
    );
  }

  async function exportEvidence() {
    setExportError(null);
    setExporting(true);
    try {
      const data = await api.getChangeEvidence(changeId as string);
      downloadBlob(
        new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
        `change-${changeId}-evidence.json`,
      );
    } catch (err) {
      // `String(err)` produced "Error:" with nothing after it for a bodyless
      // 503 -- the evidence export failed, the button went back to "Export
      // evidence", and the page said nothing a reader could act on.
      setExportError({ error: err });
    } finally {
      setExporting(false);
    }
  }

  const pickable = PICKABLE[actionType];

  return (
    <div>
      <DetailHead
        back={{ to: "/changes", label: t("change.common.allChanges") }}
        title={change.data?.title ?? t("change.detail.fallbackTitle")}
        status={
          change.data
            ? { kind: changeStateKind(change.data.state), label: changeStateLabel(change.data.state) }
            : undefined
        }
        context={
          change.data ? (
            <span className="change-head-context">
              <span>{t("change.common.risk")}</span>{" "}
              <StatusPill value={change.data.risk} label={riskLabel(change.data.risk)} kind={riskKind(change.data.risk)} />
              {clientShown ? " · " : null}
              <ClientContext clientId={change.data.client_id} />
            </span>
          ) : undefined
        }
        actions={
          canRead ? (
            /* Secondary, not primary: the page's one commit is the lifecycle
               decision on the summary, and an export is a read. */
            <Button variant="outline" onClick={() => void exportEvidence()} disabled={exporting}>
              <Icon name="file-download" size={14} />
              {exporting ? t("change.detail.exporting") : t("change.detail.exportEvidence")}
            </Button>
          ) : undefined
        }
      />
      {exportError && (
        <p role="alert" className="form-error mb-2">
          {describeSubmitError(exportError.error)}
        </p>
      )}

      <UrlTabs
        label={t("change.detail.tabs")}
        items={TABS.map((key) => ({ key, label: t(TAB_LABEL[key]) }))}
        active={tab}
        onSelect={setTab}
      >
        {tab === "overview" ? (
          <>
            {change.data && (
              <>
                {/* v3's facts strip: the state, the type, the risk and the window
                    as caption/value cells, then the prose in its own panel. */}
                <h2 className="sr-only">{t("change.detail.summary")}</h2>
                {/* The state and the risk are the head's now; the strip keeps what
                    the head does not say (the Task 5 ruling: no record says a fact
                    twice on one screen). */}
                <Facts
                  items={[
                    { k: t("change.detail.type"), v: changeTypeLabel(change.data.type) },
                    {
                      k: t("change.detail.window"),
                      v: (
                        <span className="mono">
                          {formatDateTime(change.data.window_start, zone)}
                          {" – "}
                          {formatDateTime(change.data.window_end, zone)}
                        </span>
                      ),
                    },
                    { k: t("change.detail.raised"), v: <span>{formatDateTime(change.data.created_at, zone)}</span> },
                  ]}
                />
                <Panel title={t("change.detail.descriptionPanel")}>
                  <p style={{ whiteSpace: "pre-wrap" }}>{change.data.description}</p>
                  {change.data.rollback_plan && (
                    <div className="mt-3">
                      <KV items={[[t("change.detail.rollbackPlan"), change.data.rollback_plan]]} />
                    </div>
                  )}
                </Panel>
              </>
            )}

            {/* THE BLOCKER FIX. Auditor B: "there is no Submit for approval, Approve,
                Reject, Schedule, Implement, Close or Cancel control, and no state
                dropdown... Records accumulate in `draft` indefinitely." The
                transitions offered are exactly the ones state.py will accept from
                this state and type, so no button here can 400. */}
            {change.data && (
              <div className="mb-4">
                <Panel title={t("change.detail.lifecycle")}>
                  {allowed.length > 0 ? (
                    <>
                      {allowed.some((spec) => spec.action === "approve" || spec.action === "reject") && (
                        <Field label={t("change.detail.decisionComment")}>
                          <input
                            className={inputClass}
                            value={comment}
                            onChange={(e) => setComment(e.target.value)}
                          />
                        </Field>
                      )}
                      <div className="flex flex-wrap items-center gap-2">
                        {allowed.map((spec) => {
                          // Approve and Reject differ here: the SoD guard bars the
                          // requester from DECIDING, and rejecting your own change is
                          // the same decision. state.py checks `actor_id ==
                          // created_by` for both, so both are refused.
                          const barred =
                            isOwnChange && (spec.action === "approve" || spec.action === "reject");
                          return (
                            <Button
                              key={spec.action}
                              /* One primary in the row: the forward step. Reject,
                                 cancel and revise are the secondary shape. */
                              variant={
                                spec.confirm?.destructive ||
                                spec.action === "reject" ||
                                spec.action === "revise" ||
                                spec.action === "cancel"
                                  ? "outline"
                                  : "default"
                              }
                              disabled={lifecyclePending || barred}
                              onClick={() => press(spec)}
                            >
                              {spec.label}
                            </Button>
                          );
                        })}
                      </div>
                      {decidingOwn && (
                        <p className="note mt-2">
                          {t("change.detail.ownChange")}
                        </p>
                      )}
                    </>
                  ) : (
                    <p className="note">{noActionsReason(change.data.state)}</p>
                  )}
                  {hidden.length > 0 && (
                    <p className="note mt-2">{hiddenActionsReason(hidden)}</p>
                  )}
                  {/* A refused transition used to have nowhere to land at all, because
                      no control existed to refuse. `describeSubmitError` renders the
                      server's own sentence for the ones it explains -- "approver
                      cannot be the change requester", "emergency change requires
                      retrospective approval before completion" -- and a status-based
                      one for the bodyless 503. */}
                  {lifecycleError != null && (
                    <p role="alert" className="form-error mt-2">
                      {describeSubmitError(lifecycleError)}
                    </p>
                  )}
                </Panel>
              </div>
            )}

          </>
        ) : tab === "approvals" ? (
          <div className="mb-4">
            <Panel title={t("change.detail.approvalHistory")} bare>
              {/* THE KIT'S EMPTY, NOT A SENTENCE IN A SPANNED CELL. A column header
                  row over one line of prose is the shape the empty state exists to
                  replace, and /changes/approvals — the sibling this table's rows
                  come from — already uses it. */}
              <QueryState
                loading={approvals.isLoading}
                error={approvals.error}
                empty={(approvals.data?.length ?? 0) === 0}
                emptyLabel={t("change.detail.noApprovals")}
                emptyIcon="circle-check"
              >
                <Table>
                  <thead>
                    <tr>
                      <Th>{t("change.detail.approver")}</Th>
                      <Th>{t("change.detail.decision")}</Th>
                      <Th>{t("change.detail.comment")}</Th>
                      <Th>{t("change.detail.retrospective")}</Th>
                      <Th>{t("change.detail.decided")}</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {approvals.data?.map((approval) => (
                      <tr key={approval.id}>
                        <Td lead={<Icon name="user" size={18} />}>
                          <span className="mono">{approval.approver_id}</span>
                        </Td>
                        <Td>
                          <StatusPill value={approval.decision} label={decisionLabel(approval.decision)} kind={kindOf(approval.decision)} />
                        </Td>
                        <Td>{approval.comment ?? ""}</Td>
                        <Td>{approval.is_retrospective ? t("change.detail.yes") : t("change.detail.no")}</Td>
                        <Td>
                          <span className="mono" style={{ whiteSpace: "nowrap" }}>
                            {formatDateTime(approval.decided_at, zone)}
                          </span>
                        </Td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              </QueryState>
            </Panel>
          </div>

        ) : (
          <Panel title={t("change.detail.linkedActions")}>
            <QueryState
              loading={links.isLoading}
              error={links.error}
              empty={(links.data?.length ?? 0) === 0}
              emptyLabel={t("change.detail.noLinks")}
              emptyIcon="link"
            >
              <Table>
                <thead>
                  <tr>
                    <Th>{t("change.detail.actionType")}</Th>
                    <Th>{t("change.detail.actionId")}</Th>
                    <Th>{t("change.detail.linkedAt")}</Th>
                    {canManage && <Th>{""}</Th>}
                  </tr>
                </thead>
                <tbody>
                  {links.data?.map((link) => (
                    <tr key={link.id}>
                      <Td lead={<Icon name="link" size={18} />}>{linkActionLabel(link.action_type)}</Td>
                      <Td>
                        <span className="mono">{link.action_id}</span>
                      </Td>
                      <Td>
                        <span className="mono" style={{ whiteSpace: "nowrap" }}>
                          {formatDateTime(link.linked_at, zone)}
                        </span>
                      </Td>
                      {canManage && (
                        <Td>
                          <Button
                            variant="outline"
                            className="small"
                            disabled={removeLink.isPending}
                            onClick={() => removeLink.mutate({ id: changeId, linkId: link.id })}
                          >
                            {t("change.detail.remove")}
                          </Button>
                        </Td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </Table>
            </QueryState>

            {/* SWALLOWED ENTIRELY BEFORE THIS. `removeLink` had no error surface
                anywhere on the page: unlinking a patch run that the server refused
                (403, or the 503 a proxy returns) left the row in the table with no
                word about why, and the operator's own reading of the audit trail
                for this change was wrong by one link. */}
            {removeLink.isError && (
              <p role="alert" className="form-error mt-2">
                {describeSubmitError(removeLink.error)}
              </p>
            )}

            {canManage && (
              // A real <form>, so Enter in the Action ID field links instead of
              // doing nothing — the other half of the same finding.
              <Form onSubmit={submitLink}>
                <div className="mt-4 flex flex-wrap items-end gap-2">
                  <Field label={t("change.detail.actionType")}>
                    <select
                      className={inputClass}
                      value={actionType}
                      onChange={(e) => {
                        // The chosen action does not survive the switch: a
                        // deployment id is not a provisioning run id, and carrying
                        // it across would link an id of the wrong kind to a type
                        // the server existence-checks against a different table.
                        setActionId("");
                        setAttempt(null);
                        setActionType(e.target.value as LinkActionType);
                      }}
                    >
                      {LINK_ACTION_TYPES.map((kind) => (
                        <option key={kind} value={kind}>
                          {linkActionLabel(kind)}
                        </option>
                      ))}
                    </select>
                  </Field>
                  {pickable ? (
                    // THE UUID FIELD, REPLACED. Auditor B (HIGH): "the ID format is
                    // a 36-character UUID which the user must find in another
                    // module and transcribe."
                    <EntityPicker
                      label={t(pickable.label)}
                      options={actionType === "deployment" ? deploymentOptions : runOptions}
                      loading={
                        actionType === "deployment"
                          ? deployments.isLoading || packages.isLoading
                          : runs.isLoading || profiles.isLoading
                      }
                      emptyHint={pickable.hint()}
                      placeholder={t(pickable.search)}
                      value={actionId}
                      onChange={setActionId}
                    />
                  ) : (
                    <Field label={t("change.detail.actionIdRequired")}>
                      {/* NO PICKER FOR THESE TWO YET, and saying so beats a
                          control that pretends otherwise. `GET /patch/runs` and
                          `GET /patch/approvals` exist, so a picker is possible and
                          is the real fix; until it is built the id is pasted.
                          `required` states for
                          assistive tech what the asterisk states on screen. It
                          blocks nothing — <Form> is `noValidate`, because a native
                          bubble that vanishes on the next click is the silent
                          no-op wearing a hat. `linkRules` is what the user
                          reads. */}
                      <input
                        className={inputClass}
                        required
                        // i18n-exempt: an example UUID, an identifier written the same in every language
                        placeholder="06a85c19-55e2-7f3c-8000-831c7bdba357"
                        value={actionId}
                        onChange={(e) => setActionId(e.target.value)}
                      />
                    </Field>
                  )}
                  {/* Enabled with the field empty ON PURPOSE — the refusal is what
                      the user gets to read, and a control that will not move is
                      not. The failure comes from here too, replacing the
                      `{String(addLink.error)}` line below it that rendered a raw
                      object and rendered NOTHING at all for a bodyless 503. */}
                  <FormActions
                    label={t("change.detail.link")}
                    pending={addLink.isPending}
                    error={addLink.error}
                    problems={attemptProblems(attempt)}
                  />
                </div>
                {!pickable && (
                  <p className="note mt-1">
                    {t(actionType === "patch_run" ? "change.detail.patchRunNote" : "change.detail.patchApprovalNote")}
                  </p>
                )}
              </Form>
            )}
          </Panel>
        )}
      </UrlTabs>

      <ConfirmDialog
        open={confirming != null}
        title={confirming?.confirm?.title ?? ""}
        body={confirming?.confirm?.body ?? ""}
        // WHICH thing, in the words on the screen: the change's own title, so
        // a reader with three change tabs open can see they are about to
        // approve the wrong one.
        subject={change.data?.title ?? t("change.detail.thisChange")}
        confirmLabel={confirming?.confirm?.confirmLabel ?? t("change.detail.confirm")}
        destructive={confirming?.confirm?.destructive ?? false}
        onConfirm={() => {
          const spec = confirming;
          setConfirming(null);
          if (spec) runAction(spec);
        }}
        onCancel={() => setConfirming(null)}
      />
    </div>
  );
}
