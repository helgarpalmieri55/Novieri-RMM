import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Badge, Table, Td, Th } from "../../components/ui";
import { ConfirmDialog } from "../../components/confirm";
import { NoValue } from "../../components/emptystate";
import {
  ERROR_GROUPS_LIST_LIMIT,
  useErrorGroupDetail,
  useErrorGroups,
  useErrorTriagePermission,
  useProfileZone,
  useResolveErrorGroupMutation,
  useUnresolveErrorGroupMutation,
} from "./api";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { formatDateTime } from "../../lib/datetime";
import { humaniseEnum } from "../../lib/labels";
import type { ErrorEvent, ErrorGroup } from "./types";
import { Icon } from "../../shell/icons";
import { KIND_ICON } from "../../shell/viz";
import { Btn, FilterBar, PageHead, Panel, QueryState, SearchField, StatCard, StatsGrid } from "../../shell/components";

// The operator-facing page for app/api/v1/errors.py: a fingerprinted-error
// registry, not a log viewer. v3 draws it (`P['error-monitor']`) as a tile
// row over one list whose rows lead with the bug glyph and carry the open
// group's warn edge; the console keeps its filters, its detail panel and its
// two-way triage on top of that shape.
//
// THE INVARIANT THIS PAGE MUST NEVER BREAK (docs/08-observability.md §3):
// neither `platform_error_group` nor `platform_error_event` stores exception
// text -- a group is only `exc_type` + raise site + fingerprint + a count.
// Nothing rendered here may be labelled "message" or "detail text", or imply
// that free text is one click away. The events panel below shows correlation
// ids only, so the operator's next move is to grep the logs for a
// `request_id` -- not to read a message that was never captured.
//
// `dropped` (spec §3.1) is the in-process capture queue's overflow count
// since process start. When it is non-zero, every count on this page is a
// floor, not a total, and the banner below says so.
//
// ---------------------------------------------------------------------------
// UX REMEDIATION CYCLE 12 — WHAT TRIAGE ON THIS PAGE GOT WRONG.
//
// Auditor B, [HIGH]: *"Clicking Resolve immediately flipped the Open KPI from
// 1 to 0. No confirmation dialog appeared, no toast, no undo … There is no
// Reopen, Unresolve or Undo control anywhere on the page."* Resolving costs a
// confirmation that NAMES the group, the outcome is ANNOUNCED, and the state
// filter below keeps a resolved group reachable rather than letting it fall
// out of the only view there was.
//
// AND THE THIRD ONE, CYCLE 13. The undo was not fixable from this file until
// `POST /platform/errors/{group_id}/unresolve` shipped (commit bd04957) —
// there were three routes and a Reopen button would have 404'd. There are
// four now, so a resolved row carries Reopen where it used to carry a
// sentence explaining that there was nothing to offer.
//
// REOPEN ASKS NOTHING FIRST, DELIBERATELY. `components/confirm.tsx` scopes
// itself to "something that cannot be undone"; this control IS the undo, and
// it removes nothing — the group goes back into the Open count and the Open
// filter, which is where it was before somebody resolved it. The one-click
// way back out is Resolve, which has its own confirmation. A dialog here
// would be the second one in a row, and a confirmation people learn to click
// through is worth less than none.
//
// BOTH CONTROLS ARE DRAWN ONLY FOR `platform.errors.write`. RBAC is enforced
// server-side and that is what decides; hiding these prevents nothing and is
// not claimed to. It stops a reader being handed a button whose only possible
// answer is 403.
//
// Auditor B, [MEDIUM]: *"The page has zero inputs … no open-vs-resolved
// toggle, no search on raise site, no date range, and — now that the only
// group is resolved — no way to view resolved groups at all."* The list route
// takes only `limit`, so filtering is client-side over the newest
// ERROR_GROUPS_LIST_LIMIT groups, and the truncation banner above already
// says when that window is not the whole fleet. THE FILTERS LIVE IN THE URL:
// triage is a thing operators paste into a ticket, and a filter held in
// component state cannot be sent to the person on call.

function formatRaiseSite(group: ErrorGroup): string {
  return `${group.module}:${group.lineno}`;
}

/** Triage state, as a filter. `all` is the default — it is what the route
 *  serves, and hiding rows by default is how a resolved group became
 *  unreachable in the first place. */
export type StateFilter = "all" | "open" | "resolved";

/** `where_` server-side: the three capture seams. */
const WHERE_VALUES = ["api", "worker", "gateway"] as const;

export type SortKey = "last_seen" | "first_seen" | "events";

/** The time range, as whole days of `last_seen`. `""` is "any time" — the
 *  default, because a range that hides rows by default is the same mistake as
 *  a state filter that hides them. */
const SINCE_DAYS: Record<string, number> = { "1": 1, "7": 7, "30": 30 };

export interface GroupFilters {
  query: string;
  state: StateFilter;
  where: string;
  sort: SortKey;
  /** `""`, or a key of SINCE_DAYS. */
  since: string;
  /** The instant "now" is measured from. Passed in rather than read inside,
   *  so the window is testable and so every row in one render is judged
   *  against one clock. */
  now?: number;
}

export const DEFAULT_FILTERS: GroupFilters = {
  query: "",
  state: "all",
  where: "",
  sort: "last_seen",
  since: "",
};

function isDefault(filters: GroupFilters): boolean {
  return (
    filters.query.trim() === "" &&
    filters.state === DEFAULT_FILTERS.state &&
    filters.where === DEFAULT_FILTERS.where &&
    filters.sort === DEFAULT_FILTERS.sort &&
    filters.since === DEFAULT_FILTERS.since
  );
}

/**
 * The list, filtered and sorted.
 *
 * EXPORTED AND PURE so the ordering and the matching can be tested without a
 * DOM: a sort that quietly compares strings instead of instants is invisible
 * in a rendered table until the day two rows are one hour apart.
 */
export function selectErrorGroups(groups: ErrorGroup[], filters: GroupFilters): ErrorGroup[] {
  const needle = filters.query.trim().toLowerCase();
  const days = SINCE_DAYS[filters.since];
  const cutoff = days == null ? null : (filters.now ?? Date.now()) - days * 86_400_000;
  const matched = groups.filter((group) => {
    if (cutoff != null) {
      const lastSeen = new Date(group.last_seen).getTime();
      // An unparseable last_seen is KEPT rather than silently dropped: a row
      // the page cannot date is exactly the row an operator should still see.
      if (!Number.isNaN(lastSeen) && lastSeen < cutoff) return false;
    }
    if (filters.state === "open" && group.resolved_at != null) return false;
    if (filters.state === "resolved" && group.resolved_at == null) return false;
    if (filters.where !== "" && group.where !== filters.where) return false;
    if (needle === "") return true;
    // The raise site as the operator reads it, so "dispatch:142" matches.
    return [group.exc_type, group.module, group.qualname, group.where, formatRaiseSite(group)]
      .join(" ")
      .toLowerCase()
      .includes(needle);
  });

  const instant = (iso: string) => {
    const at = new Date(iso).getTime();
    return Number.isNaN(at) ? 0 : at;
  };
  return [...matched].sort((left, right) => {
    if (filters.sort === "events") return right.event_count - left.event_count;
    if (filters.sort === "first_seen") return instant(right.first_seen) - instant(left.first_seen);
    return instant(right.last_seen) - instant(left.last_seen);
  });
}

export function ErrorMonitorPage() {
  const t = useT();
  const groups = useErrorGroups();
  const [selectedId, setSelectedId] = useState<string | undefined>(undefined);
  const detail = useErrorGroupDetail(selectedId);
  const resolve = useResolveErrorGroupMutation();
  const reopen = useUnresolveErrorGroupMutation();
  const canWrite = useErrorTriagePermission();
  // One zone for both tables. `formatDateTime` also renders an absent or
  // unparseable timestamp as NO_DATE rather than "Invalid Date", which the
  // hand-rolled `new Date(iso).toLocaleString()` this replaces did not.
  const zone = useProfileZone();

  const [params, setParams] = useSearchParams();
  const filters: GroupFilters = {
    query: params.get("q") ?? "",
    state: (["all", "open", "resolved"] as const).includes(
      (params.get("state") ?? "") as StateFilter,
    )
      ? (params.get("state") as StateFilter)
      : DEFAULT_FILTERS.state,
    where: WHERE_VALUES.includes((params.get("where") ?? "") as (typeof WHERE_VALUES)[number])
      ? (params.get("where") as string)
      : "",
    sort: (["last_seen", "first_seen", "events"] as const).includes(
      (params.get("sort") ?? "") as SortKey,
    )
      ? (params.get("sort") as SortKey)
      : DEFAULT_FILTERS.sort,
    since: Object.hasOwn(SINCE_DAYS, params.get("since") ?? "")
      ? (params.get("since") as string)
      : DEFAULT_FILTERS.since,
  };

  /** One writer for the query string; a key set to its default is dropped
   *  rather than written, so a URL says only what was actually chosen. */
  function setFilter(key: "q" | "state" | "where" | "sort" | "since", value: string) {
    const next = new URLSearchParams(params);
    const defaults: Record<string, string> = {
      q: "",
      state: "all",
      where: "",
      sort: "last_seen",
      since: "",
    };
    if (value === "" || value === defaults[key]) next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  }

  /** The group awaiting confirmation. Nothing is written while this is set. */
  const [pendingResolve, setPendingResolve] = useState<ErrorGroup | null>(null);
  /** What just happened, for anything that is not looking at the KPI row. */
  const [announcement, setAnnouncement] = useState<string | null>(null);

  const data = groups.data;
  const allGroups = useMemo(() => data?.groups ?? [], [data]);
  const openCount = allGroups.filter((group) => group.resolved_at == null).length;
  const totalEvents = allGroups.reduce((sum, group) => sum + group.event_count, 0);
  const shown = useMemo(() => selectErrorGroups(allGroups, filters), [allGroups, filters]);
  const filtered = !isDefault(filters);
  // Whichever write last failed. `reset()` is called on the other one when a
  // control is pressed, so this cannot show a stale complaint about the
  // opposite direction.
  const triageError = resolve.isError ? resolve.error : reopen.isError ? reopen.error : null;

  /** Reopen fires here rather than through a dialog. The announcement is the
   *  feedback: the badge and the Open KPI both move, and neither says
   *  anything to somebody who is not watching that number. */
  function reopenGroup(group: ErrorGroup) {
    resolve.reset();
    reopen.reset();
    reopen.mutate(group.id, {
      onSuccess: () =>
        setAnnouncement(
          t("errorMonitor.triage.reopened", { type: group.exc_type, site: formatRaiseSite(group) }),
        ),
    });
  }

  return (
    <>
      <PageHead
        title={t("errorMonitor.page.title")}
        description={t("errorMonitor.page.description")}
      />

      {data && data.dropped > 0 && (
        <div role="alert" className="notice gold">
          <Icon name={KIND_ICON.warn} size={17} />
          <div>
            {t("errorMonitor.page.dropped", { count: data.dropped })}
          </div>
        </div>
      )}

      {data && data.groups.length === ERROR_GROUPS_LIST_LIMIT && (
        <div role="alert" className="notice gold">
          <Icon name={KIND_ICON.info} size={17} />
          <div>
            {t("errorMonitor.page.truncated", { limit: ERROR_GROUPS_LIST_LIMIT })}
          </div>
        </div>
      )}

      {/* EVERY TILE OPENS THE ROWS IT COUNTED, and on this page that means its
          own table — the groups are served by `GET /platform/errors` and
          rendered nowhere else in the console. The filters already live in the
          URL (see `useSearchParams` above), so these are three genuinely
          different views and not three copies of one href: the bare route
          clears whatever filter is set, `?state=open` is the triage queue, and
          `?sort=events` orders the table by the very number the third tile
          sums.

          COLOUR IS CATEGORY: neutral for the inert total, red for unresolved
          failures, blue for an informational count. WEIGHT IS URGENCY, and
          exactly one of the three is a queue somebody has to work.

          A PENDING QUERY IS NOT A ZERO. `allGroups` defaults to `[]`, so all
          three of these read "0" during the first paint and after a failed
          load — "nothing has broken" and "we have not been told" printed
          identically, which is the defect `zeroLabel` exists to close. An
          unresolved query gets an em dash; a zero on screen is then always a
          measured zero. */}
      <StatsGrid columns={3}>
        <StatCard
          label={t("errorMonitor.tiles.groups")}
          value={data ? String(allGroups.length) : "—"}
          icon="warning"
          to="/error-monitor"
          tone="neutral"
          zeroLabel={t("errorMonitor.tiles.groupsZero")}
        />
        <StatCard
          label={t("errorMonitor.tiles.open")}
          value={data ? String(openCount) : "—"}
          icon="clock"
          to="/error-monitor?state=open"
          tone="red"
          urgent
          zeroLabel={t("errorMonitor.tiles.openZero")}
        />
        <StatCard
          label={t("errorMonitor.tiles.events")}
          value={data ? String(totalEvents) : "—"}
          icon="logs"
          to="/error-monitor?sort=events"
          tone="blue"
          zeroLabel={t("errorMonitor.tiles.eventsZero")}
        />
      </StatsGrid>

      {/* THE OUTCOME OF THE WRITE, ANNOUNCED. Resolving changed the Open KPI
          and emptied a cell, and said nothing to anybody who was not watching
          the number. `role="status"` is polite: it does not interrupt, it just
          means the change is not silent. */}
      <p role="status" className="sr-only">
        {announcement ?? ""}
      </p>

      {/* ONE ERROR SURFACE FOR BOTH DIRECTIONS. `String(resolve.error)`
          printed "ApiError: " and then whatever the response body happened
          to be — the class name for the operator to read, and nothing at all
          when the body was empty. Reopen shares this line rather than
          growing a second one: only one of the two can be in flight from a
          given row, and two alert regions saying the same kind of thing is
          how a page ends up with an error nobody reads. */}
      {triageError != null && (
        <div role="alert" className="notice red">
          <Icon name={KIND_ICON.crit} size={17} />
          <div>{describeSubmitError(triageError)}</div>
        </div>
      )}

      <Panel
        title={t("errorMonitor.groups.title")}
        subtitle={t("errorMonitor.groups.subtitle")}
        bare
      >
        <FilterBar>
          <SearchField
            placeholder={t("errorMonitor.groups.searchPlaceholder")}
            /* Bounded, like every other search field in the console: a
               dotted module path is well under this. */
            maxLength={128}
            value={filters.query}
            onChange={(event) => setFilter("q", event.target.value)}
            aria-label={t("errorMonitor.groups.searchAria")}
          />
          <select
            className="select-field"
            value={filters.state}
            onChange={(event) => setFilter("state", event.target.value)}
            aria-label={t("errorMonitor.groups.stateAria")}
          >
            <option value="all">{t("errorMonitor.groups.allStates")}</option>
            <option value="open">{t("errorMonitor.groups.open")}</option>
            <option value="resolved">{t("errorMonitor.groups.resolved")}</option>
          </select>
          <select
            className="select-field"
            value={filters.where}
            onChange={(event) => setFilter("where", event.target.value)}
            aria-label={t("errorMonitor.groups.seamAria")}
          >
            <option value="">{t("errorMonitor.groups.allSeams")}</option>
            {WHERE_VALUES.map((where) => (
              <option key={where} value={where}>
                {humaniseEnum(where, "ErrorWhere")}
              </option>
            ))}
          </select>
          <select
            className="select-field"
            value={filters.since}
            onChange={(event) => setFilter("since", event.target.value)}
            aria-label={t("errorMonitor.groups.sinceAria")}
          >
            <option value="">{t("errorMonitor.groups.anyTime")}</option>
            <option value="1">{t("errorMonitor.groups.since1")}</option>
            <option value="7">{t("errorMonitor.groups.since7")}</option>
            <option value="30">{t("errorMonitor.groups.since30")}</option>
          </select>
          <select
            className="select-field"
            value={filters.sort}
            onChange={(event) => setFilter("sort", event.target.value)}
            aria-label={t("errorMonitor.groups.sortAria")}
          >
            <option value="last_seen">{t("errorMonitor.groups.sortLastSeen")}</option>
            <option value="first_seen">{t("errorMonitor.groups.sortFirstSeen")}</option>
            <option value="events">{t("errorMonitor.groups.sortEvents")}</option>
          </select>
          <span className="filter-spacer" />
          {filtered ? (
            <Btn
              label={t("errorMonitor.groups.clearFilters")}
              small
              onClick={() => setParams(new URLSearchParams(), { replace: true })}
            />
          ) : null}
        </FilterBar>

        <QueryState
          loading={groups.isLoading}
          error={groups.error}
          empty={shown.length === 0}
          emptyLabel={
            allGroups.length === 0
              ? t("errorMonitor.groups.emptyNone")
              : t("errorMonitor.groups.emptyFiltered")
          }
          emptyIcon="warning"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("errorMonitor.groups.type")}</Th>
                <Th>{t("errorMonitor.groups.raiseSite")}</Th>
                <Th>{t("errorMonitor.groups.where")}</Th>
                <Th>{t("errorMonitor.groups.firstSeen")}</Th>
                <Th>{t("errorMonitor.groups.lastSeen")}</Th>
                <Th>{t("errorMonitor.groups.eventsColumn")}</Th>
                <Th>{t("errorMonitor.groups.resolvedColumn")}</Th>
                <Th>{t("errorMonitor.groups.actions")}</Th>
              </tr>
            </thead>
            <tbody>
                {shown.map((group) => (
                  <ErrorGroupRow
                    key={group.id}
                    group={group}
                    selected={group.id === selectedId}
                    onSelect={() => setSelectedId(group.id)}
                    onResolve={() => {
                      resolve.reset();
                      reopen.reset();
                      setPendingResolve(group);
                    }}
                    onReopen={() => reopenGroup(group)}
                    resolving={resolve.isPending && resolve.variables === group.id}
                    reopening={reopen.isPending && reopen.variables === group.id}
                    canWrite={canWrite}
                    zone={zone}
                  />
                ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      {selectedId && (
        <Panel
          title={t("errorMonitor.events.title")}
          subtitle={t("errorMonitor.events.subtitle")}
          bare
        >
          <QueryState
            loading={detail.isLoading}
            error={detail.error}
            empty={detail.data != null && detail.data.events.length === 0}
            emptyLabel={t("errorMonitor.events.empty")}
            emptyIcon="logs"
          >
            <Table>
              <thead>
                <tr>
                  <Th>{t("errorMonitor.events.occurred")}</Th>
                  <Th>{t("errorMonitor.events.requestId")}</Th>
                  <Th>{t("errorMonitor.events.org")}</Th>
                  <Th>{t("errorMonitor.events.user")}</Th>
                  <Th>{t("errorMonitor.events.job")}</Th>
                  <Th>{t("errorMonitor.events.device")}</Th>
                </tr>
              </thead>
              <tbody>
                {(detail.data?.events ?? []).map((event) => (
                  <EventRow key={event.id} event={event} zone={zone} />
                ))}
              </tbody>
            </Table>
          </QueryState>
        </Panel>
      )}

      {/* THE CONFIRMATION NAMES THE GROUP, because the mistake it exists to
          catch is having the wrong row, and it states the consequence that
          survives the undo: a fingerprint raised again does not come back as
          Open on its own.

          IT NO LONGER CLAIMS THIS IS ONE-WAY. The sentence used to read "it
          cannot be reopened from here — the control plane serves no unresolve
          route", which was true until that route shipped. Leaving it would
          have been worse than never having written it: an operator who
          believes a reversible action is permanent hesitates over the one
          that matters, and an operator who finds one sentence overstated
          reads the next one less carefully. */}
      <ConfirmDialog
        open={pendingResolve != null}
        title={t("errorMonitor.triage.confirmTitle")}
        subject={
          pendingResolve ? `${pendingResolve.exc_type} — ${formatRaiseSite(pendingResolve)}` : ""
        }
        body={t("errorMonitor.triage.confirmBody")}
        confirmLabel={t("errorMonitor.triage.confirmLabel")}
        destructive
        onCancel={() => setPendingResolve(null)}
        onConfirm={() => {
          const group = pendingResolve;
          setPendingResolve(null);
          if (!group) return;
          resolve.mutate(group.id, {
            onSuccess: () =>
              setAnnouncement(
                t("errorMonitor.triage.resolved", { type: group.exc_type, site: formatRaiseSite(group) }),
              ),
          });
        }}
      />
    </>
  );
}

function ErrorGroupRow({
  group,
  selected,
  onSelect,
  onResolve,
  onReopen,
  resolving,
  reopening,
  canWrite,
  zone,
}: {
  group: ErrorGroup;
  selected: boolean;
  onSelect: () => void;
  onResolve: () => void;
  onReopen: () => void;
  resolving: boolean;
  reopening: boolean;
  canWrite: boolean;
  zone: string | null;
}) {
  const t = useT();
  // THE ROW IS NOT A BUTTON. It used to carry `role="button"` and a tab stop
  // so the keyboard could open it, but a row that holds Resolve and Reopen
  // is then an interactive control nested inside another (axe
  // `nested-interactive`, serious). The keyboard and assistive-technology
  // way in is the Events control at the end of the row; the click on the row
  // stays as the pointer convenience it always was.
  return (
    <tr onClick={onSelect} className="cursor-pointer" aria-current={selected ? "true" : undefined}>
      {/* v3's row: the bug glyph leads, and an OPEN group carries the warn
          edge so the queue reads as a queue before the pills do. */}
      <Td lead={<Icon name="bug" size={18} />} gutter={group.resolved_at ? undefined : "warn"}>
        <div className="cell-title">{group.exc_type}</div>
      </Td>
      <Td>
        <div className="mono">{formatRaiseSite(group)}</div>
        <div className="cell-sub">{group.qualname}</div>
      </Td>
      {/* `api` / `worker` / `gateway` are wire identifiers; the console reads
          them as words. */}
      <Td>{humaniseEnum(group.where, "ErrorWhere")}</Td>
      <Td>{formatDateTime(group.first_seen, zone)}</Td>
      <Td>{formatDateTime(group.last_seen, zone)}</Td>
      <Td>
        <span className="mono">{group.event_count}</span>
      </Td>
      <Td>
        {group.resolved_at ? (
          <Badge variant="success">{t("errorMonitor.groups.resolved")}</Badge>
        ) : (
          <Badge variant="muted">{t("errorMonitor.groups.open")}</Badge>
        )}
      </Td>
      <Td>
        {/* ONE LINE, NOT TWO. These two controls sat as inline-blocks
            separated by a text space, so the cell wrapped wherever the
            longer label ("Resolve…") no longer fitted: rows with an open
            group stacked `Events` above `Resolve…` and grew ~20px taller,
            while resolved rows kept `Events | Reopen` on one line. One
            table, two row heights and a ragged action column (design review
            M3). A nowrap flex row makes the column declare the width it
            actually needs -- the same fix, and the same three utilities,
            `health/ProfileList` already uses on its four-control cell. */}
        <div className="flex flex-nowrap items-center gap-1">
          <Btn
            label={t("errorMonitor.groups.events")}
            small
            pressed={selected}
            ariaLabel={t("errorMonitor.groups.eventsOf", { type: group.exc_type })}
            onClick={(event) => {
              event.stopPropagation();
              onSelect();
            }}
          />
          {!canWrite ? (
            // Not a bare em dash: a screen reader announces that as "em dash" or
            // skips it, and the reader is left unable to tell "no action here"
            // from "action missing".
            <NoValue label={t("errorMonitor.groups.needsPermission")} />
          ) : group.resolved_at ? (
            // NO ELLIPSIS, and it is not a typo. In this console "Resolve…"
            // means a dialog follows; Reopen writes on the press, so promising
            // a further step would be a lie about what the click does.
            //
            // `Btn` NOW, and the prop these two asked for is why: both sit
            // inside a row that opens the group on click, so each needs the
            // event to stop propagating or resolving a group would also
            // navigate into it. `Btn.onClick` receives it.
            <Btn
              label={reopening ? t("errorMonitor.groups.reopening") : t("errorMonitor.groups.reopen")}
              small
              disabled={reopening}
              onClick={(event) => {
                event.stopPropagation();
                onReopen();
              }}
            />
          ) : (
            <Btn
              label={resolving ? t("errorMonitor.groups.resolving") : t("errorMonitor.groups.resolve")}
              small
              disabled={resolving}
              onClick={(event) => {
                event.stopPropagation();
                onResolve();
              }}
            />
          )}
        </div>
      </Td>
    </tr>
  );
}

/**
 * One correlation id, or an honest blank.
 *
 * Auditor B, [MEDIUM]: *"ORG, USER, JOB and DEVICE all render `—` … It's
 * unclear from the UI whether this means 'not applicable to this error' or
 * 'capture is broken' — and that ambiguity is the real defect."* The API
 * cannot tell those apart either: each id is `None` exactly when that
 * contextvar was unset at capture time. So the cell says which of the two it
 * is in words — the id was not in scope — rather than leaving a dash to be
 * read as a failure, and `NoValue` gives that sentence to a screen reader
 * instead of announcing "em dash".
 */
function Correlation({ value, missing }: { value: string | null; missing: MessageKey }) {
  const t = useT();
  if (value != null) return <span className="mono">{value}</span>;
  return <NoValue label={t(missing)} />;
}

function EventRow({ event, zone }: { event: ErrorEvent; zone: string | null }) {
  return (
    <tr>
      <Td>{formatDateTime(event.occurred_at, zone)}</Td>
      <Td>
        <Correlation value={event.request_id} missing="errorMonitor.events.noRequest" />
      </Td>
      <Td>
        <Correlation value={event.org_id} missing="errorMonitor.events.noOrganization" />
      </Td>
      <Td>
        <Correlation value={event.user_id} missing="errorMonitor.events.noUser" />
      </Td>
      <Td>
        <Correlation value={event.job_id} missing="errorMonitor.events.noJob" />
      </Td>
      <Td>
        <Correlation value={event.device_id} missing="errorMonitor.events.noDevice" />
      </Td>
    </tr>
  );
}
