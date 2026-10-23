import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { NoValue } from "../../components/emptystate";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime, toInstant } from "../../lib/datetime";
import {
  useCreateTestMutation,
  usePlan,
  usePlanStatus,
  usePlanTests,
  useProcedureArticles,
  useReviewPlanMutation,
  useScopeDevices,
  useScopeGroups,
  useUpdatePlanMutation,
  useUserTimezone,
} from "./api";
import {
  labelForOption,
  procedurePickerOptions,
  scopeDevicePickerOptions,
  scopeGroupPickerOptions,
} from "./pickerOptions";
import { outcomeKind, outcomeLabel, planStateLabel, testTypeLabel } from "./bcpState";
import { reviewStanding, reviewStandingKind, reviewStandingLabel } from "./reviewState";
import { PlanEditor } from "./PlanEditor";
import type { PlanOut, TestType } from "./types";
import { PageHead, Panel, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { Facts, KV } from "../../shell/viz";

const TEST_TYPES: TestType[] = ["restore_drill", "failover", "tabletop", "other"];

export function PlanDetail() {
  const t = useT();
  const { planId } = useParams<{ planId: string }>();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canManage = (me.data?.permissions ?? []).includes("bcp.manage");
  // "Record review" is HIDDEN entirely without bcp.approve (server-side RBAC
  // is authoritative; hiding here is purely cosmetic).
  const canApprove = (me.data?.permissions ?? []).includes("bcp.approve");
  const zone = useUserTimezone();

  const plan = usePlan(planId);
  const tests = usePlanTests(planId);
  const reviewPlan = useReviewPlanMutation();
  const updatePlan = useUpdatePlanMutation();
  const isArchived = plan.data?.state === "archived";

  // Same shared query key the plan list uses, so this costs no extra request
  // when the reader arrived from there. `review_overdue` is computed by the
  // backend and lives only on this response -- PlanOut carries the raw
  // `last_reviewed_at` and nothing about whether it is late.
  const status = usePlanStatus();
  const standing = plan.data
    ? reviewStanding(
        plan.data.last_reviewed_at,
        status.data?.find((row) => row.id === plan.data?.id)?.review_overdue,
      )
    : null;

  // The scope and the procedure list are stored as ids, and this page printed
  // them: "Scope: device:9f1f4a1e-0000-4000-8000-000000000001" and
  // "Procedure refs: 3c2b1a09-…". The same two lists the editor picks from name
  // them here (cycle 6's recognition-over-recall, on the read side) and fall
  // back to the id when a row cannot be read rather than to blank.
  const scopeDevices = useScopeDevices(true);
  const scopeGroups = useScopeGroups(true);
  const articles = useProcedureArticles(true);
  const scopeDeviceOptions = scopeDevicePickerOptions(scopeDevices.data ?? []);
  const scopeGroupOptions = scopeGroupPickerOptions(scopeGroups.data ?? []);
  const articleOptions = procedurePickerOptions(articles.data ?? [], zone);

  const [editing, setEditing] = useState<PlanOut | null>(null);
  const [creatingTest, setCreatingTest] = useState(false);
  // Cycle 8. Both of these write to the immutable audit log and neither can be
  // withdrawn, so neither may fire on a single click from a row of three
  // identically styled buttons -- Auditor B: "The three header buttons are
  // visually identical... this matches the unconfirmed Delete actions I
  // verified on /sla and the unconfirmed Resolve on /error-monitor."
  const [confirming, setConfirming] = useState<"review" | "archive" | null>(null);

  if (!planId) return null;

  return (
    <div>
      {/* THE ACTIONS BELONG TO THE HEAD, so they are passed to it. They were
          in a hand-built flex row wrapping `PageHead`, which already IS a
          `space-between` flex row with an actions slot and an 18px bottom
          margin — so the page carried two rows of layout doing one row's job,
          with its own `gap-2` beside `.head-actions`'s 9px. That is why the
          buttons on this page do not line up with the buttons on the plans
          list, which passes `actions` properly. */}
      <PageHead
        title={plan.data?.name ?? t("bcp.detail.planFallback")}
        description={t("bcp.detail.description")}
        actions={
          <>
            {canApprove && (
              <Button disabled={reviewPlan.isPending} onClick={() => setConfirming("review")}>
                <Icon name="circle-check" size={14} />
                {reviewPlan.isPending ? t("bcp.detail.recording") : t("bcp.detail.recordReview")}
              </Button>
            )}
            {canManage && plan.data && (
              <Button variant="outline" onClick={() => setEditing(plan.data as PlanOut)}>
                <Icon name="edit" size={14} />
                {t("bcp.detail.editPlan")}
              </Button>
            )}
            {canManage && plan.data && (
              <Button
                variant="outline"
                disabled={updatePlan.isPending}
                onClick={() => {
                  // ARCHIVING ASKS, UNARCHIVING DOES NOT. Archiving retires the
                  // plan from active DR coverage -- it drops out of the
                  // continuity posture a compliance reader is looking at.
                  // Restoring it takes nothing away, so a dialog there would be
                  // friction that buys nothing.
                  if (isArchived) {
                    updatePlan.mutate({ id: planId, body: { state: "active" } });
                    return;
                  }
                  setConfirming("archive");
                }}
              >
                {isArchived ? t("bcp.detail.unarchive") : t("bcp.detail.archive")}
              </Button>
            )}
          </>
        }
      />
      {/* `{String(mutation.error)}` rendered "[object Object]" at best, and for
          a bodyless 503 -- what a proxy in front of this API returns -- an
          EMPTY STRING: Archive appeared to work and the plan stayed active.
          `describeSubmitError` is driven off the status first so the sentence
          exists either way, and `role="alert"` is what announces it. */}
      {updatePlan.isError && (
        <p role="alert" className="form-error mb-2">
          {describeSubmitError(updatePlan.error)}
        </p>
      )}
      {reviewPlan.isError && (
        <p role="alert" className="form-error mb-2">
          {describeSubmitError(reviewPlan.error)}
        </p>
      )}

      <ConfirmDialog
        open={confirming === "review"}
        title={t("bcp.detail.reviewTitle")}
        body={t("bcp.detail.reviewBody")}
        // WHICH plan, by the name on the screen — what lets somebody notice
        // they have the wrong plan open before signing off on it.
        subject={plan.data?.name ?? t("bcp.detail.thisPlan")}
        confirmLabel={t("bcp.detail.recordReview")}
        onConfirm={() => {
          setConfirming(null);
          reviewPlan.mutate(planId as string);
        }}
        onCancel={() => setConfirming(null)}
      />

      <ConfirmDialog
        open={confirming === "archive"}
        title={t("bcp.detail.archiveTitle")}
        body={t("bcp.detail.archiveBody")}
        subject={plan.data?.name ?? t("bcp.detail.thisPlan")}
        confirmLabel={t("bcp.detail.archiveConfirm")}
        onConfirm={() => {
          setConfirming(null);
          updatePlan.mutate({ id: planId as string, body: { state: "archived" } });
        }}
        onCancel={() => setConfirming(null)}
      />

      {plan.data && (
        <>
          {/* v3's facts strip: the state, the two objectives and the review
              standing as caption/value cells. An unset objective is drawn as
              absent, never as a zero. */}
          <h2 className="sr-only">{t("bcp.detail.summary")}</h2>
          <Facts
            items={[
              {
                k: t("bcp.detail.state"),
                v: (
                  <StatusPill
                    value={plan.data.state}
                    kind={plan.data.state === "active" ? "ok" : "mut"}
                    label={planStateLabel(plan.data.state)}
                  />
                ),
              },
              {
                k: t("bcp.detail.rto"),
                v:
                  plan.data.rto_minutes != null
                    ? t("bcp.detail.minutes", { minutes: plan.data.rto_minutes })
                    : t("bcp.detail.na"),
              },
              {
                k: t("bcp.detail.rpo"),
                v:
                  plan.data.rpo_minutes != null
                    ? t("bcp.detail.minutes", { minutes: plan.data.rpo_minutes })
                    : t("bcp.detail.na"),
              },
              {
                k: t("bcp.detail.lastReviewed"),
                /* "never" rather than `NO_DATE`, because an unreviewed plan is a
                   fact about the plan and not a missing value -- and it is the
                   fact the review standing is computed from. */
                v: plan.data.last_reviewed_at ? (
                  <span>{formatDateTime(plan.data.last_reviewed_at, zone)}</span>
                ) : (
                  t("bcp.detail.never")
                ),
              },
              {
                k: t("bcp.detail.reviewInterval"),
                v: t("bcp.detail.days", { count: plan.data.review_interval_days }),
              },
              {
                k: t("bcp.detail.review"),
                /* The detail page stated the raw fact and left the reader to do
                   the arithmetic, while the list said "up to date" for the same
                   plan. One expression now decides both. */
                v: standing ? (
                  <StatusPill
                    value={standing}
                    kind={reviewStandingKind(standing)}
                    label={reviewStandingLabel(standing)}
                  />
                ) : (
                  t("bcp.detail.na")
                ),
              },
            ]}
          />
          <Panel title={t("bcp.detail.planPanel")}>
            <p style={{ whiteSpace: "pre-wrap" }}>{plan.data.description}</p>
            <div className="mt-3">
              <KV
                items={[
                  [
                    t("bcp.detail.scope"),
                    plan.data.scope.length > 0
                      ? plan.data.scope
                          .map((entry) =>
                            entry.kind === "device"
                              ? labelForOption(scopeDeviceOptions, entry.id)
                              : labelForOption(scopeGroupOptions, entry.id),
                          )
                          .join(", ")
                      : t("bcp.detail.noScope"),
                  ],
                  [
                    t("bcp.detail.procedures"),
                    plan.data.procedure_refs.length > 0
                      ? plan.data.procedure_refs
                          .map((ref) => labelForOption(articleOptions, ref))
                          .join(", ")
                      : t("bcp.detail.noProcedures"),
                  ],
                ]}
              />
            </div>
          </Panel>
        </>
      )}

      <Panel
        title={t("bcp.detail.testsPanel")}
        actions={
          canManage && (
            <Button variant="outline" onClick={() => setCreatingTest(true)}>
              <Icon name="plus" size={14} />
              {t("bcp.detail.newTest")}
            </Button>
          )
        }
        bare
      >
        <Table>
          <thead>
            <tr>
              <Th>{t("bcp.detail.colTest")}</Th>
              <Th>{t("bcp.detail.colScheduled")}</Th>
              <Th>{t("bcp.detail.colExecuted")}</Th>
              <Th>{t("bcp.detail.colOutcome")}</Th>
              <Th>{""}</Th>
            </tr>
          </thead>
          <tbody>
            {tests.data?.map((test) => (
              <tr key={test.id}>
                {/* A FLOOR UNDER THE TEST COLUMN, the same 18ch measure the
                    plans list puts under its name cell. Four nowrap cells sit
                    beside this one (two timestamps, a pill, a button) and take
                    their intrinsic width first, so at 390px the notes wrapped
                    at one or two words per line over ten lines while SCHEDULED
                    kept its full width. The table wrapper is a scroll
                    container, so a floor here scrolls the table rather than
                    the page. */}
                <Td lead={<Icon name="player-play" size={18} />}>
                  <div className="cell-title" style={{ minWidth: "18ch" }}>
                    {testTypeLabel(test.type)}
                  </div>
                  {test.notes ? (
                    <div className="cell-sub" style={{ minWidth: "18ch" }}>
                      {test.notes}
                    </div>
                  ) : null}
                </Td>
                {/* THE ABSENCE IS LABELLED. `formatDateTime` returns a bare
                    em dash for a null (`lib/datetime.ts` NO_DATE), which a
                    screen reader announces as "em dash" or skips -- the very
                    bug `NoValue` was written for, and a pending test's
                    Executed cell is the console's most common null. */}
                <Td>
                  {test.scheduled_for ? (
                    <span className="mono" style={{ whiteSpace: "nowrap" }}>
                      {formatDateTime(test.scheduled_for, zone)}
                    </span>
                  ) : (
                    <NoValue label={t("bcp.detail.notScheduled")} />
                  )}
                </Td>
                <Td>
                  {test.executed_at ? (
                    <span className="mono" style={{ whiteSpace: "nowrap" }}>
                      {formatDateTime(test.executed_at, zone)}
                    </span>
                  ) : (
                    <NoValue label={t("bcp.detail.notExecuted")} />
                  )}
                </Td>
                <Td>
                  <StatusPill
                    value={test.outcome}
                    kind={outcomeKind(test.outcome)}
                    label={outcomeLabel(test.outcome)}
                  />
                </Td>
                <Td>
                  <Link className="btn small" to={`/bcp/tests/${test.id}`}>
                    {t("bcp.detail.recordView")}
                  </Link>
                </Td>
              </tr>
            ))}
            {tests.data && tests.data.length === 0 && (
              <tr>
                <Td className="cell-sub" colSpan={5}>
                  {t("bcp.detail.noTests")}
                </Td>
              </tr>
            )}
          </tbody>
        </Table>
      </Panel>

      {editing && <PlanEditor plan={editing} onClose={() => setEditing(null)} />}
      {creatingTest && (
        <NewTestDialog planId={planId} onClose={() => setCreatingTest(false)} />
      )}
    </div>
  );
}

function NewTestDialog({ planId, onClose }: { planId: string; onClose: () => void }) {
  const t = useT();
  const navigate = useNavigate();
  const zone = useUserTimezone();
  const createTest = useCreateTestMutation();
  const [type, setType] = useState<TestType>("restore_drill");
  const [scheduledFor, setScheduledFor] = useState("");
  const [notes, setNotes] = useState("");

  function submit() {
    createTest.mutate(
      // `<input type="datetime-local">` hands back a WALL CLOCK with no zone
      // ("2026-08-20T14:00"). Sent unqualified to an API that reads it as UTC
      // it schedules the drill five hours out for an operator in New York, and
      // the list then reports that wrong time confidently. `toInstant`
      // attaches the zone; it returns "" for an untouched field, so the `||`
      // still sends null rather than the epoch.
      {
        planId,
        body: { type, scheduled_for: toInstant(scheduledFor, zone) || null, notes },
      },
      {
        onSuccess: (test) => {
          onClose();
          navigate(`/bcp/tests/${test.id}`);
        },
      },
    );
  }

  return (
    <Dialog open onClose={onClose} title={t("bcp.detail.newTestTitle")}>
      <div className="form-grid">
        <Field label={t("bcp.detail.type")}>
          <select
            className={inputClass}
            value={type}
            onChange={(e) => setType(e.target.value as TestType)}
          >
            {TEST_TYPES.map((kind) => (
              <option key={kind} value={kind}>
                {testTypeLabel(kind)}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t("bcp.detail.scheduledFor")}>
          <input
            type="datetime-local"
            className={inputClass}
            value={scheduledFor}
            onChange={(e) => setScheduledFor(e.target.value)}
          />
        </Field>
        {/* Auditor B (BLOCKER, B84): "No timezone is stated on the input, on the
            output, or anywhere on either page." Cycle 5 made the round trip
            correct -- `toInstant` above attaches the zone -- and this says WHICH
            clock it is correct in, on a field whose whole output is telling a
            continuity team when to be somewhere. `null` on the profile means the
            browser's, which is what `lib/datetime` falls back to, so the sentence
            names that same fallback rather than a preference nobody set. */}
        <p className="note">
          {t("bcp.detail.timesIn", { zone: zone ?? Intl.DateTimeFormat().resolvedOptions().timeZone })}
        </p>
        <Field label={t("bcp.detail.notes")} full>
          <textarea
            className={inputClass}
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            rows={2}
          />
        </Field>
      </div>
      {createTest.isError && (
        <p role="alert" className="form-error mb-2">
          {describeSubmitError(createTest.error)}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose}>
          {t("common.actions.cancel")}
        </Button>
        <Button onClick={submit} disabled={createTest.isPending}>
          {createTest.isPending ? t("bcp.detail.creating") : t("bcp.detail.create")}
        </Button>
      </div>
    </Dialog>
  );
}
