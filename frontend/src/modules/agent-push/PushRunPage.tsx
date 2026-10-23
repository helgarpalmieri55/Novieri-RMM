// One push run (design 2026-09-30 §1.5, §1.7): its progress, what happened to
// each confirmed machine and why, and the controls its state allows. Pause,
// Resume and Cancel are a person's acts; the server checks the permission on
// each, and Cancel is final.

import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Td, Th } from "../../components/ui";
import {
  Btn,
  PageHead,
  Pager,
  Panel,
  Progress,
  QueryState,
  StatusPill,
  TableWrap,
} from "../../shell/components";
import { usePushPermissions, usePushRun, usePushRunControl, usePushRunTargets } from "./api";
import {
  TARGET_STATE_KIND,
  TARGET_STATE_LABELS,
  countsSentence,
  mayCancel,
  mayResume,
  outcomeLabel,
  runIsLive,
  runProgress,
  runReasonLabel,
} from "./runState";
import type { PushRunTargetOut } from "./types";

const PAGE_SIZE = 50;

function whatHappened(target: PushRunTargetOut, help: Record<string, string>): string {
  switch (target.state) {
    case "waiting":
      return translate("agentPush.run.waiting");
    case "in_flight":
      return translate("agentPush.run.installing");
    case "cancelled":
      return translate("agentPush.run.notPushed");
    case "refused":
      return target.reasons.map((r) => runReasonLabel(r, help[r])).join(" ");
    default:
      return target.detail
        ? translate("agentPush.run.outcomeDetail", { outcome: outcomeLabel(target.outcome), detail: target.detail })
        : outcomeLabel(target.outcome);
  }
}

export function PushRunPage() {
  const t = useT();
  const { runId = "" } = useParams<{ runId: string }>();
  const permissions = usePushPermissions();
  const run = usePushRun(runId);
  const [page, setPage] = useState(0);
  const data = run.data;
  const live = data ? runIsLive(data) : false;
  const targets = usePushRunTargets(runId, page * PAGE_SIZE, PAGE_SIZE, live);
  const control = usePushRunControl();
  const [confirmCancel, setConfirmCancel] = useState(false);
  const total = targets.data?.total ?? 0;

  const actions = (
    <>
      <Link className="btn" to="/devices/push-runs">
        {t("agentPush.run.all")}
      </Link>
      {data && permissions.run ? (
        <>
          {data.state === "running" ? (
            <Btn
              label={t("agentPush.run.pause")}
              disabled={control.isPending}
              onClick={() => control.mutate({ runId, action: "pause" })}
            />
          ) : null}
          {mayResume(data.state) ? (
            <Btn
              label={t("agentPush.run.resume")}
              variant="primary"
              disabled={control.isPending}
              onClick={() => control.mutate({ runId, action: "resume" })}
            />
          ) : null}
          {mayCancel(data.state) ? (
            <Btn label={t("agentPush.run.cancelRun")} variant="danger" onClick={() => setConfirmCancel(true)} />
          ) : null}
        </>
      ) : null}
    </>
  );

  return (
    <>
      <PageHead
        title={data ? t("agentPush.run.title", { prefix: data.prefix }) : t("agentPush.run.titleBare")}
        description={t("agentPush.run.description", { count: data?.concurrency ?? 50 })}
        actions={actions}
      />
      {control.isError ? (
        <p role="alert" className="form-error">
          {describeSubmitError(control.error)}
        </p>
      ) : null}
      <QueryState loading={run.isLoading} error={run.error} empty={false} emptyLabel="">
        {data ? (
          <>
            <Panel
              title={t("agentPush.run.progressTitle")}
              subtitle={t("agentPush.run.progressSubtitle", {
                count: data.target_count,
                name: data.credential_name || t("agentPush.run.noCredential"),
              })}
            >
              <Progress value={runProgress(data.counts, data.target_count)} label={t("agentPush.run.progressLabel")} />
              <p className="note mt-2">
                {t("agentPush.run.installingNow", { counts: countsSentence(data.counts), count: data.counts.in_flight })}
              </p>
              {data.state === "running" && !data.signed_in ? (
                <p className="note mt-2">{t("agentPush.run.oneAtATime")}</p>
              ) : null}
              {data.state === "paused" ? (
                <p className="note mt-2">{t("agentPush.run.paused")}</p>
              ) : null}
              {data.state === "stopped_credential" ? (
                <div className="notice gold mt-2" role="alert">
                  <p>{t("agentPush.run.stopped", { count: data.auth_failures })}</p>
                </div>
              ) : null}
              {data.state === "cancelled" ? (
                <p className="note mt-2">{t("agentPush.run.cancelled")}</p>
              ) : null}
            </Panel>
            <Panel title={t("agentPush.run.machines")} bare>
              <QueryState
                loading={targets.isLoading}
                error={targets.error}
                empty={total === 0}
                emptyLabel={t("agentPush.run.noMachines")}
              >
                <TableWrap>
                  <table aria-label={t("agentPush.run.tableLabel")}>
                    <thead>
                      <tr>
                        <Th>{t("agentPush.run.position")}</Th>
                        <Th>{t("agentPush.run.address")}</Th>
                        <Th>{t("agentPush.run.state")}</Th>
                        <Th>{t("agentPush.run.whatHappened")}</Th>
                        <Th label={t("agentPush.run.job")} />
                      </tr>
                    </thead>
                    <tbody>
                      {(targets.data?.items ?? []).map((row) => (
                        <tr key={row.position}>
                          <Td>{row.position + 1}</Td>
                          <Td>
                            <code>{row.address}</code>
                          </Td>
                          <Td>
                            <StatusPill value={row.state} label={TARGET_STATE_LABELS[row.state]} kind={TARGET_STATE_KIND[row.state]} />
                          </Td>
                          <Td>{whatHappened(row, targets.data?.reason_help ?? {})}</Td>
                          <Td>
                            {row.job_id ? (
                              <Link to={`/jobs/${row.job_id}`} aria-label={t("agentPush.run.jobFor", { address: row.address })}>
                                {t("agentPush.run.job")}
                              </Link>
                            ) : null}
                          </Td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </TableWrap>
                <Pager
                  page={page}
                  pageCount={Math.ceil(total / PAGE_SIZE)}
                  onPrevious={() => setPage((p) => Math.max(0, p - 1))}
                  onNext={() => setPage((p) => p + 1)}
                  scopeName={t("agentPush.run.pagerScope")}
                />
              </QueryState>
            </Panel>
            <ConfirmDialog
              open={confirmCancel}
              title={t("agentPush.run.confirmTitle")}
              body={t("agentPush.run.confirmBody")}
              subject={data.prefix}
              confirmLabel={t("agentPush.run.confirmLabel")}
              destructive
              onConfirm={() => {
                setConfirmCancel(false);
                control.mutate({ runId, action: "cancel" });
              }}
              onCancel={() => setConfirmCancel(false)}
            />
          </>
        ) : null}
      </QueryState>
    </>
  );
}
