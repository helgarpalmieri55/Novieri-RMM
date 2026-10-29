import type { UseQueryResult } from "@tanstack/react-query";
import { useMemo } from "react";
import { NoValue } from "../../components/emptystate";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { FilterBar, Pager, Panel, QueryState, SearchField, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { DecisionBar, type RingOption } from "./DecisionBar";
import { severityKind, severityLabel } from "./patchState";
import type { MatchingSelection } from "./selection";
import {
  ALL_SEVERITIES_PARAM,
  DEFAULT_SEVERITIES,
  deviceCount,
  hiddenBySeverityMessage,
  pageRangeMessage,
} from "./patchCopy";
import type { PatchUpdateListResponse } from "./types";

/**
 * THE WORKING LIST, AND THE PRIMARY OBJECT OF THIS PAGE IS THE PATCH.
 *
 * The operator's report, 2026-08-25: "patch management is still a super long
 * list of missing updates and that's just one device online, that view needs to
 * change". What they were looking at was an "Exposed devices" table of one row
 * per (device, patch) pair — the whole combinatorial product, uncapped — above
 * a catalogue table of the same updates again with no fleet count on them. One
 * Ubuntu box with 300 upgradable packages rendered as ~600 rows.
 *
 * This panel is one row per PATCH, which is also the shape the decision has:
 * `patch_approvals` is `(org_id, patch_id, ring_name)` with no device column,
 * so approving has always been "a patch and a ring", never "a device". The row
 * carries the number that makes it a decision — how many machines are missing
 * it — and the rings it is already cleared for.
 *
 * EVERY FILTER IS THE SERVER'S. Search, severity, OS, sort and paging all go to
 * `GET /patch/updates`. The previous console filtered the fetched page, so a
 * search could not reach past the cap and the table answered "No patches match
 * that filter" about rows it had never asked for.
 *
 * THE SELECTION IS NO LONGER THE PAGE. It used to be intersected with the rows
 * on screen, which was the right guard when 25 rows was the only way to build
 * one: a tick that survived a filter change would approve something the
 * operator could no longer see. It is the wrong guard now that "select all 300
 * matching" exists, because that control's entire job is to select rows that
 * are not on screen. The guard moved rather than being dropped — the page
 * CLEARS the selection whenever the criterion changes (`FleetPatchPage`), so a
 * selection can only ever be the product of the filter currently in force, and
 * the bar states its size and what it was drawn from on every render.
 */

/**
 * THE OPERATING-SYSTEM SELECT IS GONE FROM THIS BAR, and it did not lose a
 * capability — it moved up a level.
 *
 * `os_family` is the page's TAB now (`patchTabs.ts`), because the operator's
 * 2026-08-26 decision was that the three ecosystems do not share one model.
 * Leaving the select in place beside the tab strip would have offered two
 * controls over one predicate, with a reachable state where the Windows tab
 * shows a list filtered to Linux — the exact class of "the number and the list
 * disagree" this panel exists to have ended.
 */
export interface UpdateFilters {
  search: string;
  severities: string[];
  offset: number;
}

export type { RingOption };

export function UpdatesPanel({
  query,
  filters,
  onFilterChange,
  pageSize,
  canApprove,
  ringOptions,
  fleetDeviceTotal,
  neverScanned,
  selectedIds,
  onSelectionChange,
  resolveMatching,
  title,
  subtitle,
  severityNote = null,
  scopeName,
  clearLabel,
}: {
  query: UseQueryResult<PatchUpdateListResponse>;
  filters: UpdateFilters;
  onFilterChange: (next: Partial<UpdateFilters>, replace?: boolean) => void;
  pageSize: number;
  /** False makes this a READ-ONLY list: no checkbox column, no decision bar.
   *  The Linux tab uses it that way — there the per-update list is evidence of
   *  what a host will receive, and the decision is made against machines. */
  canApprove: boolean;
  ringOptions: RingOption[];
  fleetDeviceTotal: number | null;
  neverScanned: boolean;
  /** Owned by the page, because it outlives this panel's paging and has to be
   *  discarded when the criterion changes. */
  selectedIds: readonly string[];
  onSelectionChange: (ids: string[]) => void;
  resolveMatching: () => Promise<MatchingSelection>;
  title?: string;
  subtitle?: string;
  /** What "severity" MEANS on the platform this list is scoped to, printed
   *  beside the control that filters on it. Three platforms, three different
   *  answers, and a reader who does not know which one applies cannot tell a
   *  narrow list from a short one. See `SEVERITY_PROVENANCE`. */
  severityNote?: string | null;
  scopeName?: string;
  /** Replaces the sentence for a measured, empty list. */
  clearLabel?: string;
}) {
  const t = useT();
  const items = query.data?.items ?? [];
  const total = query.data?.total ?? 0;
  const totalUnfiltered = query.data?.total_unfiltered ?? 0;
  // The defaults are worded when drawn, so they follow the language.
  const panelTitle = title ?? t("patch.updates.title");
  const panelSubtitle = subtitle ?? t("patch.updates.subtitle");
  const scope = scopeName ?? t("patch.updates.scopeName");

  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);
  const shownSelected = items.filter((update) => selectedSet.has(update.id)).length;
  const allShownSelected = items.length > 0 && shownSelected === items.length;

  /** The header checkbox is now "everything on THIS PAGE", not "everything".
   *  "Everything" is the decision bar's own control, which says how many that
   *  is before it does it. Two controls, two clearly different promises. */
  function toggleAllShown() {
    const pageIds = items.map((update) => update.id);
    if (allShownSelected) {
      const dropped = new Set(pageIds);
      onSelectionChange(selectedIds.filter((id) => !dropped.has(id)));
    } else {
      const next = [...selectedIds];
      for (const id of pageIds) if (!selectedSet.has(id)) next.push(id);
      onSelectionChange(next);
    }
  }

  function toggleOne(id: string) {
    onSelectionChange(
      selectedSet.has(id) ? selectedIds.filter((value) => value !== id) : [...selectedIds, id],
    );
  }

  const severityValue =
    filters.severities.length === 0 ? ALL_SEVERITIES_PARAM : filters.severities.join(",");
  const hidden = hiddenBySeverityMessage(total, totalUnfiltered);
  const lastPage = Math.max(0, Math.ceil(total / pageSize) - 1);
  const page = Math.floor(filters.offset / pageSize);

  return (
    <Panel title={panelTitle} subtitle={panelSubtitle} bare>
      <FilterBar>
        <SearchField
          placeholder={t("patch.updates.searchPlaceholder")}
          value={filters.search}
          onChange={(event) => onFilterChange({ search: event.target.value, offset: 0 }, true)}
          aria-label={t("patch.updates.searchAria", { scope })}
        />
        {/* THE CONTROL THAT WIDENS THE DEFAULT, and it is a real select rather
            than a link so the narrowed state is visibly a CHOICE that is
            currently in force. A filter the reader cannot see is the defect
            this whole panel exists to stop being. */}
        <select
          className="select-field"
          value={severityValue}
          onChange={(event) =>
            onFilterChange({
              severities:
                event.target.value === ALL_SEVERITIES_PARAM ? [] : event.target.value.split(","),
              offset: 0,
            })
          }
          aria-label={t("patch.updates.filterAria", { scope })}
        >
          {/* THE THREE OPTIONS ARE THE SAME ON EVERY TAB; only which one is in
              force by default differs (`defaultSeveritiesFor`). Offering a
              different vocabulary per platform would make the control unlearnable
              — and "critical and important" is a meaningful narrowing everywhere,
              it is just not the right STARTING point where the vendor leaves most
              updates unrated. */}
          <option value={DEFAULT_SEVERITIES.join(",")}>{t("patch.updates.optCriticalImportant")}</option>
          <option value="critical">{t("patch.updates.optCritical")}</option>
          <option value={ALL_SEVERITIES_PARAM}>{t("patch.updates.optAll")}</option>
        </select>
      </FilterBar>

      {/* WHAT THE WORD IN THAT SELECT MEANS ON THIS PLATFORM. The three
          scanners disagree, and until the page had tabs there was nowhere
          honest to say so: `apt.go` marks the security pocket `important` and
          nothing else, `winupdate.go` maps an unrated MSRC severity to
          `unknown` on most Windows 11 cumulative updates, and `mac.go` has no
          severity to read at all. A severity filter is only readable beside the
          statement of what it is filtering on. */}
      {severityNote && <p className="note px-4.5 pt-2.5">{severityNote}</p>}

      {/* WHERE THIS PAGE SITS INSIDE THE WHOLE SET, AND WHAT THE FILTER IS
          HIDING — always, not only when a cap bites. `total` is the honest
          number and this is where the reader learns it exists. The widening
          control is a button beside the sentence rather than only the select
          above, because the sentence is where the reader is when they find out
          something is missing.

          IT SITS ABOVE THE DECISION BAR ON PURPOSE: it describes the criterion,
          and the bar's "Select all N matching" acts on exactly that criterion,
          so the number in the sentence and the number on the button are read
          one after the other. */}
      {query.data != null && total > 0 && (
        <p className="note flex flex-wrap items-center gap-2 px-4.5 pt-2.5 pb-1.5">
          <span>{pageRangeMessage(items.length, filters.offset, total)}</span>
          {hidden && (
            <>
              <span>{hidden}</span>
              {/* A ghost button inside the sentence rather than a toolbar
                  action: it is the second half of a claim. v3's ghost is the
                  borderless accent-ink control, which is what a link-shaped
                  button looks like in this design. */}
              <button
                type="button"
                className="btn ghost small"
                onClick={() => onFilterChange({ severities: [], offset: 0 })}
              >
                {t("patch.updates.showAll", { count: totalUnfiltered })}
              </button>
            </>
          )}
        </p>
      )}

      {canApprove && (
        <DecisionBar
          selectedIds={selectedIds}
          onSelectionChange={onSelectionChange}
          matchingTotal={total}
          resolveMatching={resolveMatching}
          ringOptions={ringOptions}
          fleetDeviceTotal={fleetDeviceTotal}
          scopeName={scope}
        />
      )}

      <QueryState
        loading={query.isLoading}
        error={query.error}
        empty={query.data != null && items.length === 0}
        emptyIcon="patch"
        emptyLabel={
          neverScanned
            ? t("patch.updates.emptyNever")
            : totalUnfiltered === 0
              ? (clearLabel ?? t("patch.updates.emptyClear"))
              : t("patch.updates.emptyFiltered", { count: totalUnfiltered })
        }
      >
        <Table>
          <thead>
            <tr>
              {canApprove && (
                <Th label={t("patch.updates.select")}>
                  {/* NAMED, not a bare box in a header cell. A screen reader
                      reaching an unlabelled checkbox in a table header is told
                      nothing about what it selects, and this one selects
                      everything on the page — which is not everything the
                      filter matches, so the name has to say which. */}
                  <input
                    type="checkbox"
                    aria-label={t("patch.updates.selectAllAria")}
                    checked={allShownSelected}
                    onChange={toggleAllShown}
                  />
                </Th>
              )}
              <Th>{t("patch.updates.update")}</Th>
              <Th>{t("patch.updates.severity")}</Th>
              <Th>{t("patch.updates.devicesMissing")}</Th>
              <Th>{t("patch.updates.clearedFor")}</Th>
            </tr>
          </thead>
          <tbody>
            {items.map((update) => (
              <tr key={update.id}>
                {canApprove && (
                  <Td gutter={severityKind(update.severity)}>
                    <input
                      type="checkbox"
                      aria-label={t("patch.updates.selectRow", { identifier: update.identifier })}
                      checked={selectedSet.has(update.id)}
                      onChange={() => toggleOne(update.id)}
                    />
                  </Td>
                )}
                {/* v3's row: the severity edge on the first cell, a lead
                    glyph, a 600 title and a small muted sub-line. */}
                <Td
                  lead={<Icon name="patch" size={18} />}
                  gutter={canApprove ? undefined : severityKind(update.severity)}
                >
                  <div className="cell-title">{update.title}</div>
                  {/* THE SOURCE, NOT THE OS FAMILY. The family is the tab now,
                      so printing it on every row of a list that cannot contain
                      another one is a column of the same word. What the reader
                      cannot get from the tab is which mechanism found this --
                      `apt` and `wsus` and `softwareupdate` behave differently
                      enough that the identifier's shape only parses once you
                      know which one produced it. */}
                  <div className="cell-sub mono">
                    {update.identifier} · {update.source}
                  </div>
                </Td>
                <Td>
                  <StatusPill
                    value={update.severity}
                    kind={severityKind(update.severity)}
                    label={severityLabel(update.severity)}
                  />
                </Td>
                {/* THE NUMBER THE OLD PAGE COULD NOT SAY. An update missing on
                    one machine and one missing on forty rendered identically,
                    because the catalogue route returns bare `patches` rows. */}
                <Td>
                  <div className="mono">{deviceCount(update.devices_outstanding)}</div>
                  {(update.devices_approved > 0 || update.devices_failed > 0) && (
                    <div className="cell-sub">
                      {[
                        update.devices_approved > 0
                          ? t("patch.updates.awaiting", { count: update.devices_approved })
                          : null,
                        update.devices_failed > 0
                          ? t("patch.updates.failed", { count: update.devices_failed })
                          : null,
                      ]
                        .filter(Boolean)
                        .join(", ")}
                    </div>
                  )}
                </Td>
                {/* WHICH ROWS ARE STILL A DECISION. `NoValue` rather than an
                    empty cell: "not cleared for any ring" is the answer, and a
                    blank cell reads as missing data. It is a property of the
                    row now rather than a browser-side join against a capped
                    approvals page, which is what the console used to do and
                    had to disclaim. */}
                <Td className="note relative">
                  {update.cleared_rings.length > 0 ? (
                    update.cleared_rings.join(", ")
                  ) : (
                    <NoValue label={t("patch.updates.notCleared")} />
                  )}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>

      <Pager
        page={page}
        pageCount={lastPage + 1}
        onPrevious={() => onFilterChange({ offset: Math.max(0, filters.offset - pageSize) })}
        onNext={() => onFilterChange({ offset: filters.offset + pageSize })}
      />
    </Panel>
  );
}
