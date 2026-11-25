// One push run (design 2026-09-30 §1.5, §1.7): its progress, what happened to
// each confirmed machine and why, and the controls its state allows. Pause,
// Resume and Cancel are a person's acts; the server checks the permission on
// each, and Cancel is final.
//
// ON THE RECORD KIT (redesign plan Task 6, 1.8; Receta F without tabs): a head
// with the way back to every run, the run's state, its client and the acts its
// state allows; four counts of its machines; the paged machines under them.
// The browser tab names the run (`useRecordTitle`), under Devices. None of the
// acts is offered to the platform's pacer: each is a person's (2a(e)).

import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Td, Th } from "../../components/ui";
import {
  Btn,
  ClientContext,
  DetailHead,
  Pager,
  Panel,
  Progress,
  QueryState,
  StatsGrid,
  StatusPill,
  TableWrap,
} from "../../shell/components";
import { useRecordTitle } from "../../shell/title";
import { Tile } from "../../shell/viz";
import { usePushPermissions, usePushRun, usePushRunControl, usePushRunTargets } from "./api";
import {
  RUN_STATE_KIND,
  RUN_STATE_LABELS,
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
  const title = data ? t("agentPush.run.title", { prefix: data.prefix }) : t("agentPush.run.titleBare");
  useRecordTitle(data ? title : undefined);

  const actions = (
    <>
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
      <DetailHead
        back={{ to: "/devices/push-runs", label: t("agentPush.run.all") }}
        title={title}
        status={data ? { kind: RUN_STATE_KIND[data.state], label: RUN_STATE_LABELS[data.state] } : undefined}
        context={data ? <ClientContext clientId={data.client_id} /> : undefined}
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
            {/* WHERE ITS MACHINES STAND, counted by the server (`counts`). Not
                links: the machines are the table below, which has no filter
                to open at one state. */}
            <StatsGrid columns={4}>
              <Tile label={t("agentPush.run.tileTotal")} value={String(data.target_count)} icon="devices" />
              <Tile
                label={t("agentPush.run.tileInFlight")}
                value={String(data.counts.in_flight)}
                icon="activity"
                kind={data.counts.in_flight > 0 ? "run" : undefined}
              />
              <Tile
                label={t("agentPush.run.tileInstalled")}
                value={String(data.counts.installed)}
                icon="circle-check"
                kind={data.counts.installed > 0 ? "ok" : undefined}
              />
              <Tile
                label={t("agentPush.run.tileFailed")}
                value={String(data.counts.failed)}
                icon="circle-x"
                kind={data.counts.failed > 0 ? "crit" : undefined}
              />
            </StatsGrid>
            <Panel
              title={t("agentPush.run.progressTitle")}
              // The page's sentence follows the confirmed list's (the wave's
              // critique: on top of the bar it crowded the bar and its counts).
              subtitle={`${t("agentPush.run.progressSubtitle", {
                count: data.target_count,
                name: data.credential_name || t("agentPush.run.noCredential"),
              })} ${t("agentPush.run.description", { count: data.concurrency })}`}
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
