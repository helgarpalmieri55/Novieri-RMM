// The pending-action queue and one action's confirmation screen (§6.3, §12).
//
// Two views in one file because they are one workflow: a list of what is
// awaiting a decision, and the screen where a human makes it. **THE LIST SHOWS
// NO RATIONALE.** A one-region row cannot honour §6.3's separation, and a
// laundered instruction rendered as a line in a queue of pending actions is an
// authoritative-looking action description with nothing beside it to say
// otherwise. The server omits the field from the list model too.
//
// THE LIST IS v3's `assistant/actions` SCREEN: a bare panel of rows, each led
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
import { lazyFields } from "../../i18n/lazy";
import { formatDateTime } from "../../lib/datetime";
import { KIND_ICON, PageHead, Panel, QueryState, StatusPill, type Kind } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useProfileZone } from "../ai-admin/api";
import { jobTypeLabel } from "../jobs/jobTypes";
import { personaLabel, riskClassLabel } from "./types";
import { useAiAction, useAiActions, useAiActPermissions } from "./api";
import { ConfirmDialog } from "./ConfirmDialog";

/**
 * What became of a proposal, in the operator's words. The wire's `state` is
 * the server's vocabulary (`app/models/ai.py`); anything this table does not
 * name renders as itself through `StatusPill`'s own classification, so an
 * unknown state is visible rather than mislabelled.
 */
const OUTCOME = {
  previewed: lazyFields({ kind: "warn" as Kind }, { label: "aiAutomation.outcome.previewed" }),
  confirmed: lazyFields({ kind: "ok" as Kind }, { label: "aiAutomation.outcome.confirmed" }),
  dispatched: lazyFields({ kind: "ok" as Kind }, { label: "aiAutomation.outcome.dispatched" }),
  rejected: lazyFields({ kind: "mut" as Kind }, { label: "aiAutomation.outcome.rejected" }),
  expired: lazyFields({ kind: "mut" as Kind }, { label: "aiAutomation.outcome.expired" }),
} as const;

function outcomeOf(state: string): { label: string; kind: Kind } | undefined {
  return (OUTCOME as Record<string, { label: string; kind: Kind } | undefined>)[state];
}

export function AiActionsPage(): ReactElement {
  const t = useT();
  const actions = useAiActions();
  const zone = useProfileZone();
  const items = actions.data ?? [];
  return (
    <>
      <PageHead
        title={t("aiAutomation.list.title")}
        description={t("aiAutomation.list.description")}
      />
      <Panel bare>
        <QueryState
          loading={actions.isLoading}
          error={actions.error}
          empty={items.length === 0}
          emptyLabel={t("aiAutomation.list.empty")}
          emptyIcon="automation"
        >
          <Table>
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

  return (
    <>
      {/* THE HEAD FOR THE LOADED ACTION IS `ConfirmDialog`'S, and the reason is
          not tidiness. v3 draws Reject/Approve in the head's actions slot
          (`head(..., [btn('Reject','x'), btn('Approve','check',true)])`), so
          the title and the decision are one surface and the component holding
          the mutations has to own both. What is left here is the head for the
          states where there IS no action yet: loading, and a preview that did
          not load.

          THE TITLE IS THE CATALOGUE'S NAME, never the model's words: `name` is
          a field an org admin authored through the approved-action write path,
          which is what makes it safe to be the largest text on the screen.
          Until it loads, the screen says what it is for. */}
      {preview.data === undefined && (
        <PageHead
          title={t("aiAutomation.page.title")}
          description={t("aiAutomation.page.description")}
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
