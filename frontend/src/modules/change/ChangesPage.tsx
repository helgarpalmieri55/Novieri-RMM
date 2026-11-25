/**
 * Change management: every change request, its risk and its state.
 *
 * ON THE LIST KIT (redesign Task 11, 6.2; Receta L). The state and the type
 * the tiles open were already in the URL (`?state=`, `?type=`), read by the
 * table and named in a sentence above it; they are now chips on the same
 * keys, so the subset a tile opened is the chip that is pressed and "All" is
 * the way back. "All" and "Open" (the tiles' views) are plain chips; the seven
 * states and the three types are the kit's chip with a list (`.chip-select`,
 * as the Client chip), each option with its count: as thirteen chips they were
 * two rows at 1440 and seven at 390 before the first change (the wave's
 * critique, P1). The Client chip narrows the read (`?client=`,
 * decisions D6, D17) through `useChanges(chip)`; writes keep `useApi()`.
 * "New change" opens from `?new=change` (decision D8). The list, the approval
 * queue and the calendar are one `SectionNav`.
 *
 * THE COUNTS ARE THE SERVER'S WHOLE ANSWER. `GET /changes` returns every
 * change the session may see, not a page, so a chip's number and a tile's are
 * counts of the full answer (narrowed by the chip like the list), never of
 * rows that happen to be on screen.
 */
import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { Link } from "react-router-dom";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useClientScope } from "../../lib/clientScope";
import { formatDateTime } from "../../lib/datetime";
import { useClientName } from "../settings/api";
import { useChanges, useUserTimezone } from "./api";
import { ChangeSectionNav } from "./changeNav";
import { changeStateKind, changeStateLabel, changeTypeLabel, riskKind, riskLabel } from "./changeState";
import { ChangeReadiness } from "./ChangeReadiness";
import { ChangeRequestForm } from "./ChangeRequestForm";
import { GatesPanel } from "./GatesPanel";
import type { ChangeRequest, ChangeState, ChangeType } from "./types";
import {
  Btn,
  ClientFilter,
  FilterChips,
  ListEmpty,
  ListToolbar,
  PageHead,
  Panel,
  QueryState,
  ShowingCount,
  StatCard,
  StatsGrid,
  StatusPill,
  useNewParam,
  useUrlFilters,
  withClient,
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

/** The one non-wire value `?state=` accepts: every non-terminal state at once. */
const OPEN_VIEW = "open";

/** The states a chip can choose, in the order a change lives them. */
const STATES: readonly ChangeState[] = [
  "draft",
  "submitted",
  "approved",
  "implementing",
  "completed",
  "rejected",
  "cancelled",
];
const TYPES: readonly ChangeType[] = ["standard", "normal", "emergency"];

/**
 * Whether a change is in the state view `?state=` names. An unrecognised
 * value matches nothing rather than being ignored: a URL asking for a state
 * that does not exist must not quietly answer with the whole list.
 */
function inState(change: ChangeRequest, state: string): boolean {
  if (state === "") return true;
  if (state === OPEN_VIEW) return isOpen(change);
  return change.state === state;
}

function ofType(change: ChangeRequest, type: string): boolean {
  return type === "" || change.type === type;
}

/**
 * One filter as the kit's chip with a list (`.chip-select`, as the Client
 * chip): the visible word is the filter's name, the list its values, each
 * with its count once the list has answered.
 */
function ChipList({
  label,
  every,
  value,
  options,
  onChange,
}: {
  label: string;
  /** The first option, `""`: no value of this filter chosen. */
  every: string;
  value: string;
  options: readonly { value: string; label: string; count?: number }[];
  onChange: (value: string) => void;
}) {
  return (
    <span className={value === "" ? "chip chip-select" : "chip chip-select on"}>
      <span aria-hidden="true">{label}</span>
      {/* A person's choice is a new entry, so Back undoes it, as for any chip. */}
      <select aria-label={label} value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">{every}</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.count === undefined ? option.label : `${option.label} (${option.count})`}
          </option>
        ))}
      </select>
    </span>
  );
}

export function ChangesPage() {
  const t = useT();
  const api = useApi();
  // New → Change (redesign decision D8): `?new=change` opens the new form.
  const created = useNewParam("change");
  const filters = useUrlFilters(["state", "type", "client"] as const);

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canRequest = (me.data?.permissions ?? []).includes("change.request");
  const canManageGates = (me.data?.permissions ?? []).includes("change.gate.manage");
  // Same `["me"]` query as the line above, read for the profile zone rather
  // than the permissions -- see `useUserTimezone` in ./api.
  const zone = useUserTimezone();
  // WHOSE EACH ROW IS, with every client chosen (design §3.4); with one
  // chosen every row is that client's, and the column would only repeat it.
  const { selected } = useClientScope();
  const clientName = useClientName();
  const showClient = selected === null;

  // The Client chip narrows this read, and only this read (decision D6).
  const changes = useChanges(filters.values.client);
  const items = useMemo(() => changes.data ?? [], [changes.data]);
  // "The list answered" is a different fact from "the list is empty", and the
  // KPIs below must not read 0 for the first one: nine tiles reading 0 on a
  // page where nothing had been measured is what this console is unlearning.
  // No data, pending or failed: the tiles read n/a and say nothing under them (ruling 10a).
  const noData = changes.data === undefined;
  const measured = !noData;

  const stateFilter = filters.values.state;
  const typeFilter = filters.values.type;

  const openCount = items.filter(isOpen).length;
  const awaitingApproval = items.filter((change) => change.state === "submitted").length;
  const implementing = items.filter((change) => change.state === "implementing").length;
  const emergencyCount = items.filter((change) => change.type === "emergency").length;

  // The rows a tile's destination actually shows.
  const visible = useMemo(
    () => items.filter((change) => inState(change, stateFilter) && ofType(change, typeFilter)),
    [items, stateFilter, typeFilter],
  );
  // Each chip counts within the other row's choice, so "Emergency 1" under
  // "Open" is the one open emergency change the chip would show.
  const stateCount = (state: string) =>
    measured ? items.filter((change) => ofType(change, typeFilter) && inState(change, state)).length : undefined;
  const typeCount = (type: string) =>
    measured ? items.filter((change) => inState(change, stateFilter) && ofType(change, type)).length : undefined;

  // UNDER THE CLIENT CHIP A TILE COUNTS ONE CLIENT'S ROWS, AND OPENS THEM:
  // its link carries the chip, so the list it opens is the one it counted.
  const chip = filters.values.client;
  const listOf = (query: string) => withClient(`/changes?${query}`, chip);

  const newChange = canRequest ? (
    <Btn label={t("change.list.newChange")} icon="plus" variant="primary" onClick={created.start} />
  ) : undefined;

  return (
    <div>
      <PageHead title={t("change.list.title")} description={t("change.list.description")} actions={newChange} />
      {/* THE APPROVAL QUEUE EXISTS AND NOTHING POINTED AT IT (Auditor B: the
          approval workflow "cannot be exercised"). The rail is the shell's;
          the module's own pages are this strip. */}
      <ChangeSectionNav />

      <ChangeReadiness />

      {/* FOUR NUMBERS, FOUR DESTINATIONS. Only the approval queue is
          `urgent` — it is the one count where somebody is blocked until an
          operator acts, so it is the one tile that takes the warn wash.
          "Emergency changes" is a census of a TYPE, most of whose rows are
          long finished, so it stays a plain tile. Before the list answers,
          a tile is unmeasured ("n/a"), never a zero and never a dash. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("change.list.openChanges")}
          value={String(openCount)}
          pending={noData}
          icon="git-branch"
          to={listOf(`state=${OPEN_VIEW}`)}
          tone="violet"
          zeroLabel={t("change.list.nothingInFlight")}
        />
        <StatCard
          label={t("change.list.awaitingApproval")}
          value={String(awaitingApproval)}
          pending={noData}
          icon="clock"
          /* The dedicated queue, not `?state=submitted`: the approve/reject
             controls and the segregation-of-duties guard live there, and this
             count is defined by exactly the rows that page lists. Under the
             Client chip the count is one client's, which the queue (every
             client's) would not show: the tile opens the list at
             `?state=submitted` with the chip instead. */
          to={chip ? listOf("state=submitted") : "/changes/approvals"}
          tone="gold"
          urgent
          zeroLabel={t("change.list.nobodyWaiting")}
        />
        <StatCard
          label={t("change.list.implementing")}
          value={String(implementing)}
          pending={noData}
          icon="progress"
          to={listOf("state=implementing")}
          zeroLabel={t("change.list.nothingImplementing")}
        />
        <StatCard
          label={t("change.list.emergencyChanges")}
          value={String(emergencyCount)}
          pending={noData}
          icon="alert-triangle"
          to={listOf("type=emergency")}
          tone="red"
          zeroLabel={t("change.list.noneEmergency")}
        />
      </StatsGrid>

      <Panel bare>
        <ListToolbar
          chips={
            <>
              {/* The two views the tiles open. With one state chosen in the
                  list beside them, neither is pressed. */}
              <FilterChips
                label={t("change.list.viewLabel")}
                options={[
                  { value: "", label: t("change.list.allStates") },
                  { value: OPEN_VIEW, label: t("change.list.chipOpen"), count: stateCount(OPEN_VIEW) },
                ]}
                value={stateFilter}
                onChange={(value) => filters.set("state", value)}
              />
              <ChipList
                label={t("change.list.stateLabel")}
                every={t("change.list.anyState")}
                // "Open" is every open state at once, not one of them; an
                // unknown state matches nothing and the empty state says so.
                value={(STATES as readonly string[]).includes(stateFilter) ? stateFilter : ""}
                options={STATES.map((state) => ({ value: state, label: changeStateLabel(state), count: stateCount(state) }))}
                onChange={(value) => filters.set("state", value)}
              />
              <ChipList
                label={t("change.list.typeLabel")}
                every={t("change.list.allTypes")}
                value={(TYPES as readonly string[]).includes(typeFilter) ? typeFilter : ""}
                options={TYPES.map((type) => ({ value: type, label: changeTypeLabel(type), count: typeCount(type) }))}
                onChange={(value) => filters.set("type", value)}
              />
              <ClientFilter filters={filters} />
            </>
          }
          // No count before the list has answered: "Showing 0 of 0" would be an invented zero.
          end={measured ? <ShowingCount shown={visible.length} total={items.length} /> : null}
        />
        {/* `isPending`, not `isLoading`: under the chip the read waits for
            `/me` (disabled until `ready`), and a disabled query is not
            "loading" -- the empty state would flash before any read (general
            ruling 10). */}
        <QueryState loading={changes.isPending} error={changes.error} empty={false} emptyLabel="">
          {visible.length === 0 ? (
            <ListEmpty
              filtered={filters.active}
              onClear={filters.clear}
              nothingYet={t("change.list.emptyNone")}
              icon="git-branch"
              action={newChange}
            />
          ) : (
            <Table label={t("change.list.tableLabel")}>
                <thead>
                  <tr>
                    <Th>{t("change.common.name")}</Th>
                    {showClient ? <Th>{t("change.list.client")}</Th> : null}
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
                      {showClient ? <Td>{clientName(change.client_id)}</Td> : null}
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
          )}
        </QueryState>
      </Panel>

      <GatesPanel canManage={canManageGates} />

      {/* Open only for someone who may request a change: the same code the
          button checks, so a bare `?new=change` opens nothing for a reader.
          KEYED on being open, so it starts empty whatever opened it (the
          button, a link, Back or Forward), never with the last attempt. */}
      <ChangeRequestForm
        key={created.open && canRequest ? "open" : "closed"}
        change={created.open && canRequest ? "new" : null}
        onClose={created.close}
      />
    </div>
  );
}
