// Devices → Push runs (design 2026-09-30 §1.7): every subnet push a person
// confirmed, newest first, with where each stands.
//
// ON THE LIST KIT (redesign plan Task 6, 1.7; Receta L): the state chips live
// in `?state=` (the states `runState.ts` knows, with no count: the server
// counts none), "Showing x of y", and an empty list that says its cause. NO
// CLIENT CHIP: a run is already one client's, and its row says which. Pause,
// Resume and Cancel stay on the run's own page (a person's acts, 2a(e)).

import { Link } from "react-router-dom";

import { useT } from "../../i18n/context";
import { Td, Th } from "../../components/ui";
import {
  FilterChips,
  ListEmpty,
  ListToolbar,
  PageHead,
  Panel,
  Progress,
  QueryState,
  ShowingCount,
  StatusPill,
  TableWrap,
  useUrlFilters,
} from "../../shell/components";
import { Icon } from "../../shell/icons";
import { usePushPermissions, usePushRuns } from "./api";
import { RUN_STATE_KIND, RUN_STATE_LABELS, countsSentence, runProgress } from "./runState";
import { intlLocale } from "../../lib/datetime";
import type { PushRunState } from "./types";

/** The run states, in the order a run lives them. */
const RUN_STATES: readonly PushRunState[] = ["running", "paused", "stopped_credential", "cancelled", "completed"];

export function PushRunsPage() {
  const t = useT();
  const runs = usePushRuns();
  const permissions = usePushPermissions();
  const filters = useUrlFilters(["state"] as const);
  const all = runs.data ?? [];
  const items = filters.values.state === "" ? all : all.filter((run) => run.state === filters.values.state);
  // A push names machines a person ticked or a subnet's list they confirm:
  // both start on Devices, where "Push install the agent…" opens (`?new=push`).
  const installOnMany = permissions.run ? (
    <Link className="btn" to="/devices?new=push">
      {t("agentPush.runs.installOnMany")}
    </Link>
  ) : undefined;
  return (
    <>
      <Link className="back-link" to="/devices">
        <Icon name="chevron-left" size={14} />
        {t("agentPush.runs.back")}
      </Link>
      <PageHead title={t("agentPush.runs.title")} description={t("agentPush.runs.description")} />
      <Panel bare>
        <ListToolbar
          chips={
            <FilterChips
              label={t("agentPush.runs.stateLabel")}
              options={[
                { value: "", label: t("agentPush.runs.allStates") },
                ...RUN_STATES.map((state) => ({ value: state, label: RUN_STATE_LABELS[state] })),
              ]}
              value={filters.values.state}
              onChange={(value) => filters.set("state", value)}
            />
          }
          // No count before the list has answered: "Showing 0 of 0" would be an invented zero.
          end={runs.data ? <ShowingCount shown={items.length} total={all.length} /> : null}
        />
        <QueryState loading={runs.isLoading} error={runs.error} empty={false} emptyLabel="">
          {items.length === 0 ? (
            <ListEmpty
              filtered={filters.active}
              onClear={filters.clear}
              nothingYet={t("agentPush.runs.empty")}
              icon="network"
              action={installOnMany}
            />
          ) : (
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
          )}
        </QueryState>
      </Panel>
    </>
  );
}
