// The pending-action queue and one action's confirmation screen (§6.3, §12).
//
// Two views in one file because they are one workflow: a list of what is
// awaiting a decision, and the screen where a human makes it. **THE LIST SHOWS
// NO RATIONALE.** A one-region row cannot honour §6.3's separation, and a
// laundered instruction rendered as a line in a queue of pending actions is an
// authoritative-looking action description with nothing beside it to say
// otherwise. The server omits the field from the list model too.
//
// THE LIST IS THE KIT'S LIST PATTERN (redesign plan, Task 12, 7.6; Receta L):
// chips by what became of each proposal, in `?state=`, with how many it shows
// and an empty state that says why. THE ROW IS v3's: a bare panel of rows, each led
// by the robot glyph, titled with the action and sub-titled with when it was
// proposed, and its outcome as a pill on the right. The columns the console
// adds (job type, persona, risk) are the facts a person deciding whether to
// open one needs; a destructive or tainted proposal is marked on the row
// itself, in words as well as in the gutter.

import type { ReactElement } from "react";
import { Link, useParams } from "react-router-dom";
import { describeSubmitError } from "../../components/form";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { formatDateTime } from "../../lib/datetime";
import {
  DetailHead,
  FilterChips,
  KIND_ICON,
  ListEmpty,
  ListToolbar,
  PageHead,
  Panel,
  QueryState,
  ShowingCount,
  StatusPill,
  useUrlFilters,
} from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useRecordTitle } from "../../shell/title";
import { useProfileZone } from "../ai-admin/api";
import { jobTypeLabel } from "../jobs/jobTypes";
import { personaLabel, riskClassLabel } from "./types";
import { useAiAction, useAiActions, useAiActPermissions } from "./api";
import { ConfirmDialog } from "./ConfirmDialog";
import { outcomeOf } from "./outcome";

/**
 * THE STATE CHIPS, in the outcome's own words: awaiting approval, ran
 * (confirmed or dispatched, which the outcome already names alike), declined,
 * expired. Read from the list the server answered: `GET /ai/actions` takes no
 * state filter and answers no per-state totals, so the chips carry no number
 * (the Absent Zero Rule).
 */
const STATE_FILTERS = ["previewed", "ran", "rejected", "expired"] as const;
type StateFilter = (typeof STATE_FILTERS)[number];

const STATE_FILTER_LABEL: Record<StateFilter, MessageKey> = {
  previewed: "aiAutomation.outcome.previewed",
  ran: "aiAutomation.outcome.confirmed",
  rejected: "aiAutomation.outcome.rejected",
  expired: "aiAutomation.outcome.expired",
};

function matchesState(state: string, filter: string): boolean {
  if (filter === "") return true;
  if (filter === "ran") return state === "confirmed" || state === "dispatched";
  return state === filter;
}

export function AiActionsPage(): ReactElement {
  const t = useT();
  const actions = useAiActions();
  const zone = useProfileZone();
  const filters = useUrlFilters(["state"] as const);
  const all = actions.data ?? [];
  const items = all.filter((action) => matchesState(action.state, filters.values.state));
  return (
    <>
      <PageHead
        title={t("aiAutomation.list.title")}
        description={t("aiAutomation.list.description")}
      />
      <Panel bare>
        <ListToolbar
          chips={
            <FilterChips
              label={t("aiAutomation.list.state")}
              options={[
                { value: "", label: t("aiAutomation.list.all") },
                ...STATE_FILTERS.map((state) => ({ value: state, label: t(STATE_FILTER_LABEL[state]) })),
              ]}
              value={filters.values.state}
              onChange={(value) => filters.set("state", value)}
            />
          }
          end={actions.data ? <ShowingCount shown={items.length} total={all.length} /> : null}
        />
        <QueryState loading={actions.isPending} error={actions.error} empty={false} emptyLabel="">
          {items.length === 0 ? (
            <ListEmpty
              filtered={filters.active}
              onClear={filters.clear}
              nothingYet={t("aiAutomation.list.empty")}
              icon="automation"
            />
          ) : (
            <Table label={t("aiAutomation.list.title")}>
              <thead>
                <tr>
                  <Th>{t("aiAutomation.list.action")}</Th>
                  <Th>{t("aiAutomation.list.whatRuns")}</Th>
                  <Th>{t("aiAutomation.list.askedBy")}</Th>
                  <Th>{t("aiAutomation.list.risk")}</Th>
                  <Th>{t("aiAutomation.list.state")}</Th>
                </tr>
              </thead>
              <tbody>
                {items.map((action) => {
                  const outcome = outcomeOf(action.state);
                  return (
                    <tr key={action.id}>
                      <Td
                        lead={<Icon name="automation" size={18} />}
                        gutter={action.destructive ? "crit" : action.tainted ? "warn" : undefined}
                      >
                        <div className="cell-title">
                          <Link to={`/assistant/actions/${action.id}`}>{action.approved_action_key}</Link>
                        </div>
                        <div className="cell-sub flex flex-wrap items-center gap-2">
                          <span>{t("aiAutomation.list.proposed", { when: formatDateTime(action.created_at, zone) })}</span>
                          {action.destructive && (
                            <StatusPill value="destructive" kind="crit" label={t("aiAutomation.list.destructive")} />
                          )}
                          {action.tainted && (
                            <StatusPill value="tainted" kind="warn" label={t("aiAutomation.list.tainted")} />
                          )}
                        </div>
                      </Td>
                      <Td>
                        <div>{jobTypeLabel(action.job_type)}</div>
                        <div className="cell-sub mono">{action.job_type}</div>
                      </Td>
                      <Td>{personaLabel(action.persona)}</Td>
                      <Td>
                        <StatusPill value={action.risk_class} label={riskClassLabel(action.risk_class)} />
                      </Td>
                      <Td>
                        {outcome ? (
                          <StatusPill value={action.state} kind={outcome.kind} label={outcome.label} />
                        ) : (
                          <StatusPill value={action.state} />
                        )}
                      </Td>
                    </tr>
                  );
                })}
              </tbody>
            </Table>
          )}
        </QueryState>
      </Panel>
    </>
  );
}

export function AiActionPage(): ReactElement {
  const t = useT();
  const { actionId } = useParams<{ actionId: string }>();
  const preview = useAiAction(actionId ?? null);
  const { confirm } = useAiActPermissions();
  // THE RECORD IN THE BROWSER TAB (redesign, Receta F): the catalogue's name
  // for the action, never the model's words.
  useRecordTitle(preview.data?.name);

  return (
    <>
      {/* THE HEAD FOR THE LOADED ACTION IS `ConfirmDialog`'S, and the reason is
          not tidiness. v3 draws Reject/Approve in the head's actions slot
          (`head(..., [btn('Reject','x'), btn('Approve','check',true)])`), so
          the title and the decision are one surface and the component holding
          the mutations has to own both. What is left here is the head for the
          states where there IS no action yet: loading, and a preview that did
          not load -- the kit's record head, with its way back to the list.

          THE TITLE IS THE CATALOGUE'S NAME, never the model's words: `name` is
          a field an org admin authored through the approved-action write path,
          which is what makes it safe to be the largest text on the screen.
          Until it loads, the screen says what it is for. */}
      {preview.data === undefined && (
        <DetailHead
          back={{ to: "/assistant/actions", label: t("aiAutomation.list.title") }}
          title={t("aiAutomation.page.title")}
          context={t("aiAutomation.page.description")}
        />
      )}
      {preview.isLoading && <p className="note">{t("aiAutomation.page.loading")}</p>}
      {/* `preview.error.message` was rendered raw. A bodyless refusal carries
          `""`, so the paragraph rendered empty and the screen read as "this
          action has nothing to confirm" rather than "the preview did not
          load". Announced, because the operator arrived here to make a
          decision and there is now nothing to decide on. */}
      {preview.isError && (
        <p role="alert" className="notice red">
          <Icon name={KIND_ICON.crit} size={16} />
          <span>{describeSubmitError(preview.error)}</span>
        </p>
      )}
      {preview.data !== undefined && (
        <ConfirmDialog
          preview={preview.data}
          canConfirm={confirm}
          onReload={() => void preview.refetch()}
        />
      )}
    </>
  );
}
