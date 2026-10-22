import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Button, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime } from "../../lib/datetime";
import { useChanges, useUserTimezone } from "./api";
import { changeStateKind, changeStateLabel, changeTypeLabel, riskKind, riskLabel } from "./changeState";
import { ChangeReadiness } from "./ChangeReadiness";
import { ChangeRequestForm } from "./ChangeRequestForm";
import { GatesPanel } from "./GatesPanel";
import type { ChangeRequest } from "./types";
import {
  PageHead,
  Panel,
  QueryState,
  StatCard,
  StatsGrid,
  StatusPill,
} from "../../shell/components";
import { Icon } from "../../shell/icons";

// Terminal states a change can end its life in. Anything else is "open" —
// mirrors change_mgmt/state.py's next_state, which is the single source for
// this vocabulary; a state invented here would drift from what the backend
// actually assigns.
const TERMINAL_STATES = new Set(["completed", "cancelled", "rejected"]);

/**
 * ONE expression for the KPI and for the `?state=open` view behind it.
 *
 * The BCP module learned this the expensive way (see `bcp/reviewState.ts`): a
 * count computed by one expression and a list filtered by another drift apart,
 * and the tile then opens a list that disagrees with the number printed on it.
 * That is worse than a dead tile — it is a tile that lies.
 */
function isOpen(change: ChangeRequest): boolean {
  return !TERMINAL_STATES.has(change.state);
}

/**
 * The query-string keys this list is addressable by.
 *
 * WHY THEY EXIST NOW. Every tile above the table has to name where its number
 * lives, and for two of the three that place is this same table showing a
 * SUBSET of itself. Without a filter in the URL the only honest destination was
 * `/changes` — the whole list, with the operator left to find the rows again by
 * eye, which is the "dead tile" in a politer costume. Same mechanism and the
 * same param name `DevicesPage` already uses for `?state=online`.
 */
const STATE_PARAM = "state";
const TYPE_PARAM = "type";

/** The one non-wire value `?state=` accepts: every non-terminal state at once. */
const OPEN_VIEW = "open";

export function ChangesPage() {
  const t = useT();
  const api = useApi();
  const [editing, setEditing] = useState<ChangeRequest | "new" | null>(null);

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canRequest = (me.data?.permissions ?? []).includes("change.request");
  const canManageGates = (me.data?.permissions ?? []).includes("change.gate.manage");
  // Same `["me"]` query as the line above, read for the profile zone rather
  // than the permissions -- see `useUserTimezone` in ./api.
  const zone = useUserTimezone();

  const changes = useChanges();
  const items = useMemo(() => changes.data ?? [], [changes.data]);
  // "The list answered" is a different fact from "the list is empty", and the
  // KPIs below must not read 0 for the first one: nine tiles reading 0 on a
  // page where nothing had been measured is what this console is unlearning.
  const measured = changes.data !== undefined;

  const [params] = useSearchParams();
  const stateFilter = params.get(STATE_PARAM) ?? "";
  const typeFilter = params.get(TYPE_PARAM) ?? "";

  const openCount = items.filter(isOpen).length;
  const awaitingApproval = items.filter((change) => change.state === "submitted").length;
  const emergencyCount = items.filter((change) => change.type === "emergency").length;

  // The rows a tile's destination actually shows. An unrecognised `?state=`
  // matches nothing rather than being ignored — a URL asking for a state that
  // does not exist must not quietly answer with the whole list.
  const visible = useMemo(
    () =>
      items.filter((change) => {
        const stateOk =
          stateFilter === ""
            ? true
            : stateFilter === OPEN_VIEW
              ? isOpen(change)
              : change.state === stateFilter;
        return stateOk && (typeFilter === "" || change.type === typeFilter);
      }),
    [items, stateFilter, typeFilter],
  );

  // Named in the operator's words, so the page says which subset it is showing
  // rather than looking like a list that lost most of its rows.
  const filterWords = [
    stateFilter === OPEN_VIEW
      ? t("change.list.filterOpen")
      : stateFilter !== ""
        ? t("change.list.filterState", { state: changeStateLabel(stateFilter).toLocaleLowerCase() })
        : null,
    typeFilter !== "" ? t("change.list.filterType", { type: changeTypeLabel(typeFilter).toLocaleLowerCase() }) : null,
  ].filter((part): part is string => part !== null);

  return (
    <div>
      {/* THE APPROVAL QUEUE EXISTS AND NOTHING POINTED AT IT.
          `/changes/approvals` and `/changes/calendar` are real routes in
          App.tsx, but no shell nav entry links to either -- which is why
          Auditor B, walking the product, reported that the approval workflow
          "cannot be exercised" and found that "the list page's only button is
          'New change'". The sidebar is shared and is not this module's to
          edit; the module's own landing page linking to its own screens is.
          v3 draws them as the head's secondary buttons beside the one primary. */}
      <PageHead
        title={t("change.list.title")}
        description={t("change.list.description")}
        actions={
          <>
            <Link className="btn" to="/changes/approvals">
              <Icon name="circle-check" size={14} />
              {t("change.list.approvalQueue")}
            </Link>
            <Link className="btn" to="/changes/calendar">
              <Icon name="calendar-week" size={14} />
              {t("change.list.calendar")}
            </Link>
            {canRequest && (
              <Button onClick={() => setEditing("new")}>
                <Icon name="plus" size={14} />
                {t("change.list.newChange")}
              </Button>
            )}
          </>
        }
      />

      <ChangeReadiness />

      {/* THREE NUMBERS, THREE DESTINATIONS. Only the approval queue is
          `urgent` — it is the one count where somebody is blocked until an
          operator acts, so it is the one tile that takes the warn wash.
          "Emergency changes" is a census of a TYPE, most of whose rows are
          long finished, so it stays a plain tile. */}
      <StatsGrid columns={3}>
        <StatCard
          label={t("change.list.openChanges")}
          value={measured ? String(openCount) : "—"}
          icon="git-branch"
          to={`/changes?${STATE_PARAM}=${OPEN_VIEW}`}
          tone="violet"
          zeroLabel={t("change.list.nothingInFlight")}
        />
        <StatCard
          label={t("change.list.awaitingApproval")}
          value={measured ? String(awaitingApproval) : "—"}
          icon="clock"
          /* The dedicated queue, not `?state=submitted`: the approve/reject
             controls and the segregation-of-duties guard live there, and this
             count is defined by exactly the rows that page lists. */
          to="/changes/approvals"
          tone="gold"
          urgent
          zeroLabel={t("change.list.nobodyWaiting")}
        />
        <StatCard
          label={t("change.list.emergencyChanges")}
          value={measured ? String(emergencyCount) : "—"}
          icon="alert-triangle"
          to={`/changes?${TYPE_PARAM}=emergency`}
          tone="red"
          zeroLabel={t("change.list.noneEmergency")}
        />
      </StatsGrid>

      {/* Rendered ONLY when a filter is set, and it is the way back out. A
          tile that narrows the list to four rows and leaves no sign that it
          did is how an operator concludes the other rows were deleted. */}
      {filterWords.length > 0 ? (
        <p className="note mb-3">
          {t("change.list.showing", { shown: visible.length, total: items.length, filters: filterWords.join(", ") })}{" "}
          <Link to="/changes">{t("change.list.showAll")}</Link>
        </p>
      ) : null}

      <Panel bare>
        <QueryState
          loading={changes.isLoading}
          error={changes.error}
          empty={visible.length === 0}
          emptyLabel={
            items.length === 0
              ? t("change.list.emptyNone")
              : t("change.list.emptyView", { filters: filterWords.join(", ") })
          }
          emptyIcon="git-branch"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("change.common.name")}</Th>
                <Th>{t("change.common.risk")}</Th>
                <Th>{t("change.common.state")}</Th>
                <Th>{t("change.list.created")}</Th>
              </tr>
            </thead>
            <tbody>
              {visible.map((change) => (
                <tr key={change.id}>
                  {/* v3's row: the risk as the gutter, the branch glyph as
                      the lead, the title over the window it is planned for. */}
                  {/* The identifying column carries the measure. Without one
                      it was starved at 390 -- an auto-layout column has no
                      floor of its own -- and the change title wrapped one word
                      per line. `clip` states the column at 260px and
                      ellipsises; the title is the link to the change, and
                      `title` carries it whole. */}
                  <Td lead={<Icon name="git-branch" size={18} />} gutter={riskKind(change.risk)} clip={260}>
                    <div className="cell-title">
                      <Link to={`/changes/${change.id}`}>{change.title}</Link>
                    </div>
                    {/* v3's sub-line is the window ("Sat 14 Sep 02:00 · 40 min
                        window"); both ends are kept here because an absent
                        end is a fact the operator reads off this line. */}
                    <div className="cell-sub">
                      {changeTypeLabel(change.type)} · {formatDateTime(change.window_start, zone)}
                      {" – "}
                      {formatDateTime(change.window_end, zone)}
                      {change.origin === "auto" ? ` · ${t("change.list.recordedAutomatically")}` : ""}
                    </div>
                  </Td>
                  <Td>
                    <StatusPill value={change.risk} label={riskLabel(change.risk)} kind={riskKind(change.risk)} />
                  </Td>
                  <Td>
                    {/* Wire vocabulary, read as words: `humaniseEnum` is the one
                        place this console turns an identifier into a label. */}
                    <StatusPill value={change.state} label={changeStateLabel(change.state)} kind={changeStateKind(change.state)} />
                  </Td>
                  {/* The window is the row's sub-line, as v3 draws it; a
                      second copy in its own column was the same instant
                      twice on one row. */}
                  <Td>
                    <span className="mono" style={{ whiteSpace: "nowrap" }}>
                      {formatDateTime(change.created_at, zone)}
                    </span>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      <GatesPanel canManage={canManageGates} />

      <ChangeRequestForm change={editing} onClose={() => setEditing(null)} />
    </div>
  );
}
