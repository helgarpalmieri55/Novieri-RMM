import type { UseQueryResult } from "@tanstack/react-query";
import { type ReactNode, useMemo, useState } from "react";
import { RelTime } from "../../components/reltime";
import { Link } from "react-router-dom";
import { NoValue } from "../../components/emptystate";
import { Table, Td, Th } from "../../components/ui";
import { Trans } from "../../i18n/Trans";
import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import {
  Btn,
  Pager,
  Panel,
  QueryState,
  StatusPill,
} from "../../shell/components";
import { Icon } from "../../shell/icons";
import { OsIcon, Pill } from "../../shell/viz";
import { PATCH_ROLLUP_PAGE_SIZE, usePatchUpdates } from "./api";
import { DeviceOutcome } from "./DeviceOutcome";
import { DecisionBar } from "./DecisionBar";
import { severityKind, severityLabel } from "./patchState";
import { FAMILY_LABEL, type PatchFamily } from "./patchTabs";
import { machinesNeedingUpdates, pageRangeMessage } from "./patchCopy";
import { resolveMatchingUpdateIds, type SelectedHost } from "./selection";
import type { PatchDeviceRollupListResponse } from "./types";
import type { RingOption } from "./UpdatesPanel";

/**
 * WHICH MACHINES NEED ATTENTION — ONE ROW PER DEVICE, AND YOU CAN ACT FROM IT.
 *
 * This replaced "Exposed devices", which was one row per (device, patch) pair.
 * That table was `FleetReport.exposures` rendered directly: uncapped, one entry
 * for every non-compliant status row in the org, so a single Ubuntu box missing
 * 300 packages printed 300 rows under one hostname heading. It was truthful and
 * useless, which is exactly what the operator reported on 2026-08-25.
 *
 * A device row states its TOTAL outstanding count and is never severity
 * filtered. That is what makes the Updates panel's severity default safe: a
 * machine whose only outstanding update is one Microsoft left unrated still
 * visibly carries something here, and the number beside its hostname is the
 * whole truth about that machine rather than the filtered view of it.
 *
 * THE DRILL-DOWN IS NOW A WORKING SURFACE, NOT A READOUT. The operator,
 * 2026-08-27: "when i open that host i can't approve the updates for it, i
 * have to go back and approve 25 updates per page". The expansion was a
 * read-only `<ul>` — it listed what the machine was missing and offered no way
 * to do anything about it, so drilling in was a dead end that cost a
 * navigation back to the fleet list. It is a real table now, with the same
 * selection model and the same `DecisionBar` the fleet list uses.
 *
 * WHAT IT IS NOT is a per-device approval. `patch_approvals` has no device
 * column; the device is a LENS for choosing which updates and the ring is
 * still the scope, which the bar says in its own sentence before the click and
 * the confirmation repeats. See `DecisionBar`.
 */
export function DevicesPanel({
  query,
  offset,
  onOffsetChange,
  neverScanned,
  zone,
  canApprove,
  ringOptions,
  fleetDeviceTotal,
  title,
  subtitle,
  platformLabel = null,
  managedCount = null,
  selectedHosts,
  onHostSelectionChange,
  hostBar,
  defaultExpandedDeviceId = null,
}: {
  query: UseQueryResult<PatchDeviceRollupListResponse>;
  offset: number;
  onOffsetChange: (offset: number) => void;
  neverScanned: boolean;
  zone: string | null;
  canApprove: boolean;
  ringOptions: RingOption[];
  fleetDeviceTotal: number | null;
  title?: string;
  subtitle?: string;
  /**
   * "Windows", "Linux", "macOS" — the platform this panel is scoped to, or
   * `null` for the whole fleet.
   *
   * IT REPLACED A NUMBER THAT COULD NOT BE SCOPED. This panel used to print
   * "…2 are missing something critical" from `FleetReport.critical_device_count`
   * — which `reporting.fleet_report` computes for the whole ORG. The moment the
   * page grew platform tabs that number stopped describing the table under it:
   * a Windows tab would have printed the Linux box's critical exposure beside a
   * list that does not contain it, which is the tile/table disagreement this
   * module has now shipped twice.
   *
   * The fact the sentence was carrying is preserved without the number, because
   * the ORDER already carries it: `device_rollup` sorts by `worst_rank` first,
   * so anything critical is at the top by construction. Stating the ordering is
   * both true and more useful than a count — it tells the reader where to look
   * rather than how many to worry about.
   */
  platformLabel?: string | null;
  /** Machines of this platform that carry an agent — the denominator. `null`
   *  when the console may not read `GET /devices`, in which case the sentence
   *  drops the denominator rather than inventing one. */
  managedCount?: number | null;
  /** Present turns on the row checkboxes. The selection is the CALLER's,
   *  because it outlives this panel's paging and is what `POST /apply` is
   *  handed. */
  selectedHosts?: readonly SelectedHost[];
  onHostSelectionChange?: (hosts: SelectedHost[]) => void;
  /** The decision bar for that selection — `HostDecisionBar` on the Linux tab.
   *  Passed in rather than built here so this panel stays a table: the fleet
   *  update list does the same with `DecisionBar`. */
  hostBar?: ReactNode;
  /** Renders one row already expanded. Used by `/__preview` so the browser
   *  accessibility pass measures the drill-down — a component is covered by
   *  that pass only if the harness renders it, and the expansion is where the
   *  new controls live. */
  defaultExpandedDeviceId?: string | null;
}) {
  const t = useT();
  const [expanded, setExpanded] = useState<string | null>(defaultExpandedDeviceId);
  const items = query.data?.items ?? [];
  const total = query.data?.total ?? 0;
  const lastPage = Math.max(0, Math.ceil(total / PATCH_ROLLUP_PAGE_SIZE) - 1);
  const page = Math.floor(offset / PATCH_ROLLUP_PAGE_SIZE);

  const selectable = selectedHosts != null && onHostSelectionChange != null;
  const selectedSet = useMemo(
    () => new Set((selectedHosts ?? []).map((host) => host.id)),
    [selectedHosts],
  );
  const shownSelected = items.filter((device) => selectedSet.has(device.device_id)).length;
  const allShownSelected = items.length > 0 && shownSelected === items.length;
  /** The device table's column count, so the expansion cell spans the row
   *  rather than pushing the columns out of alignment. */
  const deviceColumns = selectable ? 6 : 5;

  /** "Everything on THIS PAGE", not "everything" — the same two-controls,
   *  two-promises split the update table already makes. "Everything" is the
   *  host bar's own control and it says how many that is first. */
  function toggleAllShown() {
    if (!onHostSelectionChange) return;
    const pageHosts = items.map((device) => ({
      id: device.device_id,
      hostname: device.hostname,
    }));
    if (allShownSelected) {
      const dropped = new Set(pageHosts.map((host) => host.id));
      onHostSelectionChange((selectedHosts ?? []).filter((host) => !dropped.has(host.id)));
    } else {
      const next = [...(selectedHosts ?? [])];
      for (const host of pageHosts) if (!selectedSet.has(host.id)) next.push(host);
      onHostSelectionChange(next);
    }
  }

  function toggleOne(host: SelectedHost) {
    if (!onHostSelectionChange) return;
    onHostSelectionChange(
      selectedSet.has(host.id)
        ? (selectedHosts ?? []).filter((entry) => entry.id !== host.id)
        : [...(selectedHosts ?? []), host],
    );
  }

  return (
    <Panel
      title={title ?? t("patch.devicesPanel.title")}
      subtitle={subtitle ?? t("patch.devicesPanel.subtitle")}
      bare
    >
      {/* DEVICES, AND HOW MANY THERE ARE ALTOGETHER. "1 device needs patches"
          says nothing about whether that is the whole estate or a corner of it,
          and the inverse of that mistake — reading a findings list's length as
          a device count — is what put "66 exposed devices" on this page for one
          machine missing 66 packages.

          THE DENOMINATOR IS NOW THE PLATFORM'S, not the org's. `devices_total`
          on this response comes from `fleet_scan_coverage`, which counts the
          whole organisation whatever `os_family` was filtered on — so "3 of
          286" under a Windows-only table would be three Windows machines
          measured against a fleet that is mostly not Windows. `managedCount`
          is `GET /devices?os_family=…`, which is the right denominator, and it
          is omitted rather than substituted when unreadable. */}
      {query.data != null && total > 0 && (
        <p className="note px-4.5 pt-2.5 pb-1.5">
          {machinesNeedingUpdates(total, managedCount, platformLabel)} {t("patch.devicesPanel.sortNote")}
        </p>
      )}
      {hostBar}
      <QueryState
        loading={query.isLoading}
        error={query.error}
        empty={query.data != null && items.length === 0}
        emptyIcon="devices"
        emptyLabel={
          neverScanned
            ? t("patch.devicesPanel.emptyNever")
            : t("patch.devicesPanel.emptyClear")
        }
      >
        <Table>
          <thead>
            <tr>
              {selectable && (
                <Th label={t("patch.devicesPanel.select")}>
                  {/* NAMED, and named with what it actually takes. This one
                      selects the machines ON THIS PAGE, which is not every
                      machine the filter matches — the host bar's own control is
                      that, and it states its count first. */}
                  <input
                    type="checkbox"
                    aria-label={t("patch.devicesPanel.selectAllAria")}
                    checked={allShownSelected}
                    onChange={toggleAllShown}
                  />
                </Th>
              )}
              <Th>{t("patch.devicesPanel.device")}</Th>
              <Th>{t("patch.devicesPanel.outstanding")}</Th>
              <Th>{t("patch.devicesPanel.worst")}</Th>
              <Th>{t("patch.devicesPanel.lastScan")}</Th>
              <Th>{""}</Th>
            </tr>
          </thead>
          <tbody>
            {items.map((device) => (
              // A fragment rather than a second `<tbody>`: the expansion is a
              // row of the same table, so the columns stay aligned.
              <FragmentRows
                key={device.device_id}
                deviceId={device.device_id}
                hostname={device.hostname}
                osFamily={device.os_family}
                outstanding={device.outstanding}
                failed={device.failed}
                worstSeverity={device.worst_severity}
                lastScanAt={device.last_scan_at}
                zone={zone}
                columns={deviceColumns}
                canApprove={canApprove}
                ringOptions={ringOptions}
                fleetDeviceTotal={fleetDeviceTotal}
                selected={selectable ? selectedSet.has(device.device_id) : null}
                onSelect={() =>
                  toggleOne({ id: device.device_id, hostname: device.hostname })
                }
                expanded={expanded === device.device_id}
                onToggle={() =>
                  setExpanded((current) =>
                    current === device.device_id ? null : device.device_id,
                  )
                }
              />
            ))}
          </tbody>
        </Table>
      </QueryState>
      <Pager
        page={page}
        pageCount={lastPage + 1}
        previousLabel={t("patch.devicesPanel.previous")}
        nextLabel={t("patch.devicesPanel.next")}
        onPrevious={() => onOffsetChange(Math.max(0, offset - PATCH_ROLLUP_PAGE_SIZE))}
        onNext={() => onOffsetChange(offset + PATCH_ROLLUP_PAGE_SIZE)}
      />
    </Panel>
  );
}

/** "windows" as the tab spells it: "Windows". An unknown family stays as sent. */
function familyName(osFamily: string): string {
  return (FAMILY_LABEL as Record<string, string>)[osFamily as PatchFamily] ?? osFamily;
}

function FragmentRows({
  deviceId,
  hostname,
  osFamily,
  outstanding,
  failed,
  worstSeverity,
  lastScanAt,
  zone,
  columns,
  canApprove,
  ringOptions,
  fleetDeviceTotal,
  selected,
  onSelect,
  expanded,
  onToggle,
}: {
  deviceId: string;
  hostname: string;
  osFamily: string;
  outstanding: number;
  failed: number;
  worstSeverity: string | null;
  lastScanAt: string | null;
  zone: string | null;
  columns: number;
  canApprove: boolean;
  ringOptions: RingOption[];
  fleetDeviceTotal: number | null;
  /** `null` when the table has no selection column at all. */
  selected: boolean | null;
  onSelect: () => void;
  expanded: boolean;
  onToggle: () => void;
}) {
  const t = useT();
  return (
    <>
      <tr>
        {selected !== null && (
          <Td gutter={worstSeverity ? severityKind(worstSeverity) : undefined}>
            <input
              type="checkbox"
              aria-label={t("patch.devicesPanel.selectRow", { hostname })}
              checked={selected}
              onChange={onSelect}
            />
          </Td>
        )}
        {/* v3's device row: the OS glyph leads, the hostname is the title,
            the platform the sub-line, and the worst severity is the 3px edge
            on the row's first cell. */}
        <Td
          lead={<OsIcon os={osFamily} />}
          gutter={selected === null && worstSeverity ? severityKind(worstSeverity) : undefined}
        >
          <div className="cell-title">
            <Link to={`/devices/${deviceId}`}>{hostname}</Link>
          </div>
          <div className="cell-sub">{familyName(osFamily)}</div>
        </Td>
        <Td>
          <div className="mono">{outstanding}</div>
          {/* THE FAILURES, NAMED SEPARATELY. `outstanding` counts a failed
              patch -- correctly, the machine still lacks it -- which is exactly
              why an operator who just applied updates could read "12
              outstanding" and not know that four of them were errors from the
              run they were waiting on. A crit pill, so the glyph says it too. */}
          {failed > 0 && (
            <div className="mt-1">
              <Pill kind="crit">{t("patch.devicesPanel.failed", { count: failed })}</Pill>
            </div>
          )}
        </Td>
        <Td>
          {worstSeverity ? (
            <StatusPill
              value={worstSeverity}
              kind={severityKind(worstSeverity)}
              label={severityLabel(worstSeverity)}
            />
          ) : (
            <NoValue label={t("patch.devicesPanel.noSeverity")} />
          )}
        </Td>
        {/* A device that has never answered is not a device with nothing to
            report, and a dash reading as "up to date" is the same false
            all-clear the four confident zeros were. */}
        <Td>
          {lastScanAt ? (
            <RelTime iso={lastScanAt} zone={zone} />
          ) : (
            <span className="note">{t("patch.devicesPanel.neverScanned")}</span>
          )}
        </Td>
        <Td>
          <div className="flex justify-end">
            {/* NAMED WITH THE MACHINE. Twenty rows whose every control reads
                "Show 12 updates" is twenty identical buttons to a screen
                reader; the row has to be in the name. */}
            <Btn
              label={
                expanded
                  ? t("patch.devicesPanel.hide")
                  : t("patch.devicesPanel.show", { count: outstanding })
              }
              ariaLabel={
                expanded
                  ? t("patch.devicesPanel.hideAria", { hostname })
                  : t("patch.devicesPanel.showAria", { hostname, count: outstanding })
              }
              icon={expanded ? "chevron-down" : "chevron-right"}
              variant="ghost"
              small
              onClick={onToggle}
            />
          </div>
        </Td>
      </tr>
      {expanded && (
        <DeviceUpdates
          deviceId={deviceId}
          hostname={hostname}
          columns={columns}
          canApprove={canApprove}
          ringOptions={ringOptions}
          fleetDeviceTotal={fleetDeviceTotal}
        />
      )}
    </>
  );
}

/**
 * One machine's own outstanding updates, and the decision that can be made
 * from them.
 *
 * FETCHED FOR THAT MACHINE, not sliced out of a fleet-wide page. It is
 * `GET /patch/updates?device_id=…` — the same route the panel above uses with
 * one more predicate — so it is paged and severity-unfiltered like any other
 * call, and expanding a device costs one request for that device rather than
 * the whole fleet's exposures.
 *
 * The component is mounted only while the row is expanded, which is what keeps
 * the request from being made for every row on the page, and what discards the
 * selection when the row is closed: a tick that survived a collapse would be a
 * decision the operator can no longer see.
 *
 * THE COLUMNS MATCH `DevicePatchTab`. Update / Severity / Also missing on is
 * what the device's own page shows, so drilling in from the fleet list and
 * opening the machine itself speak the same language rather than two dialects
 * of the same table.
 */
function DeviceUpdates({
  deviceId,
  hostname,
  columns,
  canApprove,
  ringOptions,
  fleetDeviceTotal,
}: {
  deviceId: string;
  hostname: string;
  columns: number;
  canApprove: boolean;
  ringOptions: RingOption[];
  fleetDeviceTotal: number | null;
}) {
  const t = useT();
  const api = useApi();
  const [offset, setOffset] = useState(0);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);

  // NEVER SEVERITY FILTERED. This is the machine's own list, and the fleet
  // view's default narrowing must not follow the reader down into it.
  const criterion = useMemo(() => ({ device_id: deviceId, severity: [] as string[] }), [deviceId]);
  const query = usePatchUpdates({ ...criterion, offset: offset || undefined });
  const items = query.data?.items ?? [];
  const total = query.data?.total ?? 0;
  const lastPage = Math.max(0, Math.ceil(total / PATCH_ROLLUP_PAGE_SIZE) - 1);
  const page = Math.floor(offset / PATCH_ROLLUP_PAGE_SIZE);

  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);
  const shownSelected = items.filter((update) => selectedSet.has(update.id)).length;
  const allShownSelected = items.length > 0 && shownSelected === items.length;

  function toggleAllShown() {
    const pageIds = items.map((update) => update.id);
    if (allShownSelected) {
      const dropped = new Set(pageIds);
      setSelectedIds((current) => current.filter((id) => !dropped.has(id)));
    } else {
      setSelectedIds((current) => [...current, ...pageIds.filter((id) => !current.includes(id))]);
    }
  }

  return (
    <tr>
      <Td colSpan={columns} className="bg-surface-2">
        {/* A v3 panel inside the row: the drill-down is a list of its own,
            with the same head-line, decision bar, table and foot as the
            fleet list above it, on the panel's own surface and radius. */}
        <section className="panel">
          <p className="note px-4.5 pt-2.5 pb-1.5">
            {total > 0
              ? t("patch.devicesPanel.range", {
                  hostname,
                  range: pageRangeMessage(items.length, offset, total),
                })
              : t("patch.devicesPanel.nothingOutstanding", { hostname })}
          </p>

          {canApprove && (
            <DecisionBar
              selectedIds={selectedIds}
              onSelectionChange={setSelectedIds}
              matchingTotal={total}
              resolveMatching={() =>
                resolveMatchingUpdateIds(
                  (params) => api.listPatchUpdates(params),
                  criterion,
                )
              }
              ringOptions={ringOptions}
              fleetDeviceTotal={fleetDeviceTotal}
              scopeHostname={hostname}
              scopeName={hostname}
            />
          )}

          <QueryState
            loading={query.isLoading}
            error={query.error}
            empty={query.data != null && items.length === 0}
            emptyIcon="patch"
            emptyLabel={t("patch.devicesPanel.emptyDevice")}
          >
            <Table>
              <thead>
                <tr>
                  {canApprove && (
                    <Th label={t("patch.devicesPanel.select")}>
                      <input
                        type="checkbox"
                        aria-label={t("patch.devicesPanel.selectShownAria", { hostname })}
                        checked={allShownSelected}
                        onChange={toggleAllShown}
                      />
                    </Th>
                  )}
                  <Th>{t("patch.devicesPanel.update")}</Th>
                  <Th>{t("patch.devicesPanel.severity")}</Th>
                  <Th>{t("patch.devicesPanel.outcome")}</Th>
                </tr>
              </thead>
              <tbody>
                {items.map((update) => (
                  <tr key={update.id}>
                    {canApprove && (
                      <Td gutter={severityKind(update.severity)}>
                        <input
                          type="checkbox"
                          aria-label={t("patch.devicesPanel.selectOn", { identifier: update.identifier, hostname })}
                          checked={selectedSet.has(update.id)}
                          onChange={() =>
                            setSelectedIds((current) =>
                              current.includes(update.id)
                                ? current.filter((id) => id !== update.id)
                                : [...current, update.id],
                            )
                          }
                        />
                      </Td>
                    )}
                    <Td
                      lead={<Icon name="patch" size={18} />}
                      gutter={canApprove ? undefined : severityKind(update.severity)}
                    >
                      <div className="cell-title">{update.title}</div>
                      <div className="cell-sub mono">{update.identifier}</div>
                    </Td>
                    <Td>
                      <StatusPill
                        value={update.severity}
                        kind={severityKind(update.severity)}
                        label={severityLabel(update.severity)}
                      />
                    </Td>
                    {/* WHAT HAPPENED TO IT HERE. This column used to carry
                        fleet context ("also missing on 40 devices") and could
                        not: `GET /patch/updates?device_id=` scopes its counts
                        to the device asked for, so `devices_outstanding` was
                        always 1 and the cell always printed "only this
                        device" -- a column that could state only one thing.
                        What it says now is the thing this list could not say
                        at all: an update that was tried and failed rendered
                        identically to one nothing has attempted. */}
                    <Td>
                      <DeviceOutcome update={update} />
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </QueryState>

          {/* PAGED, rather than "showing the first 25 — open the device for the
              rest". A machine with 300 outstanding packages is the case this
              module exists for, and sending the operator to another page to
              see rows 26 onward is the navigation the drill-down was supposed
              to save them. */}
          <Pager
            page={page}
            pageCount={lastPage + 1}
            scopeName={t("patch.devicesPanel.pagerScope", { hostname })}
            onPrevious={() => setOffset(Math.max(0, offset - PATCH_ROLLUP_PAGE_SIZE))}
            onNext={() => setOffset(offset + PATCH_ROLLUP_PAGE_SIZE)}
          />

          <p className="note px-4.5 pt-2 pb-2.5">
            <Trans
              k="patch.devicesPanel.open"
              vars={{ hostname }}
              components={{ link: <Link className="panel-link" to={`/devices/${deviceId}`} /> }}
            />
          </p>
        </section>
      </Td>
    </tr>
  );
}
