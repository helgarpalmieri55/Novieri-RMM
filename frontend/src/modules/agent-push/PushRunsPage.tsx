// Devices → Push runs (design 2026-09-30 §1.7): every subnet push a person
// confirmed, newest first, with where each stands.

import { Link } from "react-router-dom";

import { useT } from "../../i18n/context";
import { Td, Th } from "../../components/ui";
import { PageHead, Panel, Progress, QueryState, StatusPill, TableWrap } from "../../shell/components";
import { usePushRuns } from "./api";
import { RUN_STATE_KIND, RUN_STATE_LABELS, countsSentence, runProgress } from "./runState";
import { intlLocale } from "../../lib/datetime";

export function PushRunsPage() {
  const t = useT();
  const runs = usePushRuns();
  const items = runs.data ?? [];
  return (
    <>
      <PageHead
        title={t("agentPush.runs.title")}
        description={t("agentPush.runs.description")}
        actions={
          <Link className="btn" to="/devices">
            {t("agentPush.runs.back")}
          </Link>
        }
      />
      <Panel bare>
        <QueryState
          loading={runs.isLoading}
          error={runs.error}
          empty={items.length === 0}
          emptyLabel={t("agentPush.runs.empty")}
        >
          <TableWrap>
            <table aria-label={t("agentPush.runs.title")}>
              <thead>
                <tr>
                  <Th>{t("agentPush.runs.subnet")}</Th>
                  <Th>{t("agentPush.runs.started")}</Th>
                  <Th>{t("agentPush.runs.state")}</Th>
                  <Th>{t("agentPush.runs.progress")}</Th>
                  <Th>{t("agentPush.runs.soFar")}</Th>
                </tr>
              </thead>
              <tbody>
                {items.map((run) => (
                  <tr key={run.id}>
                    <Td>
                      <Link to={`/devices/push-runs/${run.id}`}>
                        <code>{run.prefix}</code>
                      </Link>
                    </Td>
                    <Td>{new Date(run.created_at).toLocaleString(intlLocale())}</Td>
                    <Td>
                      <StatusPill value={run.state} label={RUN_STATE_LABELS[run.state]} kind={RUN_STATE_KIND[run.state]} />
                    </Td>
                    <Td>
                      <Progress
                        value={runProgress(run.counts, run.target_count)}
                        label={t("agentPush.runs.progressOf", { prefix: run.prefix })}
                      />
                    </Td>
                    <Td>{countsSentence(run.counts)}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        </QueryState>
      </Panel>
    </>
  );
}
