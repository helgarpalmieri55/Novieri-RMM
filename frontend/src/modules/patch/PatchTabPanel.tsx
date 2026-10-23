import { APP_MANAGER, AppsPanel } from "./AppsPanel";
import { useCallback, useMemo, type ReactNode, type RefObject } from "react";
import { Link } from "react-router-dom";
import { EmptyState, type EmptyStateProps } from "../../components/emptystate";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime } from "../../lib/datetime";
import { Panel, StatCard, StatsGrid } from "../../shell/components";
import { Icon } from "../../shell/icons";
import {
  PATCH_ROLLUP_PAGE_SIZE,
  usePatchDeviceRollup,
  usePatchUpdates,
} from "./api";
import { DevicesPanel } from "./DevicesPanel";
import { HostDecisionBar } from "./HostDecisionBar";
import { ALL_SEVERITIES_PARAM, tabEmptyReason } from "./patchCopy";
import { FAMILY_LABEL, SEVERITY_PROVENANCE, type PatchFamily } from "./patchTabs";
import {
  resolveMatchingHostIds,
  resolveMatchingUpdateIds,
  type SelectedHost,
} from "./selection";
import {
  UpdatesPanel,
  type RingOption,
  type UpdateFilters,
} from "./UpdatesPanel";
import type { PatchUpdateFilters } from "./types";

/**
 * ONE PLATFORM'S TAB: THE SAME TWO LISTS, IN THE ORDER THAT PLATFORM DECIDES IN.
 *
 * WHAT THE OPERATOR ASKED FOR, 2026-08-26: split patch management into Linux,
 * Windows (including Windows Server) and macOS, "because the one model the page
 * uses does not fit all three ecosystems". `patchTabs.ts` carries the evidence
 * from the three scanners; this component is what the split looks like.
 *
 * THE TWO PANELS ARE THE SAME TWO PANELS ON EVERY TAB. What differs is which
 * one comes first, because that is what "unit of decision" means on a screen:
 *
 *   WINDOWS / macOS   UPDATES first. `patch_approvals` is
 *                     `(org_id, patch_id, ring_name)` with no device column,
 *                     which IS the WSUS model — approve this update for this
 *                     computer group. The list an operator works down is keyed
 *                     the way the decision is, and the machine list below is
 *                     the same decision seen from the other side.
 *
 *   LINUX             MACHINES first, and the update list demoted to evidence.
 *                     The operator's own reasoning: "apt/dnf resolve
 *                     dependencies as a SET — cherry-picking single packages
 *                     often isn't actionable anyway." So the Linux update list
 *                     keeps its rows and loses its checkboxes: it answers "what
 *                     will this host receive", not "which of these do I want".
 *
 * NOTHING HERE FORKED THE KERNEL TO DO IT. `UpdatesPanel`, `DevicesPanel`,
 * `DecisionBar` and `selection.ts` are the 2026-08-27 rework's components,
 * used as they are; the Linux path adds one bar (`HostDecisionBar`) and one
 * resolver (`resolveMatchingHostIds`) that are the machine-shaped mirror of
 * what already existed for updates.
 *
 * EVERY NUMBER ON THIS TAB IS SCOPED TO THIS TAB, and that is load-bearing
 * rather than tidy. Both rollups take `os_family`, so the tiles, the tables and
 * the strip above all read the same two responses — and `sum(severity_counts)
 * == total_unfiltered` BY CONSTRUCTION on the server, so the two severity tiles
 * are buckets of the total beside them and can never exceed it. A tile that
 * could disagree with the table under it is the defect this module shipped
 * twice.
 */
export function PatchTabPanel({
  family,
  filters,
  onFilterChange,
  deviceOffset,
  onDeviceOffsetChange,
  selectedIds,
  onSelectionChange,
  selectedHosts,
  onHostSelectionChange,
  canApprove,
  canApply,
  canScan,
  ringOptions,
  fleetDeviceTotal,
  zone,
  managedCount,
  severityHref,
  devicesHref,
  devicesAnchorId,
  devicesAnchorRef,
}: {
  family: PatchFamily;
  filters: UpdateFilters;
  onFilterChange: (next: Partial<UpdateFilters>, replace?: boolean) => void;
  deviceOffset: number;
  onDeviceOffsetChange: (offset: number) => void;
  selectedIds: readonly string[];
  onSelectionChange: (ids: string[]) => void;
  selectedHosts: readonly SelectedHost[];
  onHostSelectionChange: (hosts: SelectedHost[]) => void;
  canApprove: boolean;
  canApply: boolean;
  canScan: boolean;
  ringOptions: RingOption[];
  fleetDeviceTotal: number | null;
  zone: string | null;
  /** Machines of this platform carrying an agent, or `null` when this operator
   *  may not read `GET /devices`. The empty state needs it to tell "no fleet"
   *  from "clean fleet"; nothing else on the tab does. */
  managedCount: number | null;
  severityHref: (severity: string) => string;
  devicesHref: () => string;
  devicesAnchorId: string;
  devicesAnchorRef: RefObject<HTMLDivElement | null>;
}) {
  const t = useT();
  const api = useApi();
  const label = FAMILY_LABEL[family];

  /** The criterion this tab's update list is showing, in the API's own
   *  vocabulary — built once and used for the table's query AND for the
   *  resolver behind "select all N matching", so the ids that come back cannot
   *  be drawn from a different filter than the one on screen. */
  const updateCriterion = useMemo<PatchUpdateFilters>(
    () => ({
      // App updates have their own list below (`AppsPanel`).
      kind: "os",
      severity: filters.severities,
      os_family: family,
      q: filters.search.trim() || undefined,
    }),
    [filters.severities, filters.search, family],
  );

  const updates = usePatchUpdates({
    ...updateCriterion,
    offset: filters.offset || undefined,
  });
  const devices = usePatchDeviceRollup({
    os_family: family,
    offset: deviceOffset || undefined,
  });

  const resolveMatchingUpdates = useCallback(
    () =>
      resolveMatchingUpdateIds(
        (request) => api.listPatchUpdates(request),
        updateCriterion,
      ),
    [api, updateCriterion],
  );
  const resolveMatchingHosts = useCallback(
    () =>
      resolveMatchingHostIds((request) => api.listPatchDeviceRollup(request), {
        os_family: family,
      }),
    [api, family],
  );

  /**
   * NOTHING HAS EVER REPORTED, which is not the same as "nothing is wrong".
   * `devices_scanned` is read off completed `patch.scan` jobs, so the page can
   * say the thing it means rather than inferring it from four empty lists.
   *
   * IT IS ORG-WIDE AND THE EMPTY STATE KNOWS THAT. `fleet_scan_coverage`
   * counts every device in the organisation whatever `os_family` was filtered
   * on, so "no scan has completed" is a true statement about the org and NOT a
   * statement about this platform. That is exactly why `tabEmptyReason` asks
   * about enrolment first: a zero here on a tab with no machines of its own
   * would name the wrong cause.
   */
  const devicesScanned = updates.data?.devices_scanned ?? 0;
  const neverScanned = updates.data != null && devicesScanned === 0;

  const severityCounts = updates.data?.severity_counts ?? {};
  const outstanding = updates.data?.total_unfiltered ?? 0;
  const deviceTotal = devices.data?.total ?? 0;
  const criticalCount = severityCounts.critical ?? 0;
  const importantCount = severityCounts.important ?? 0;
  const lastScanAt = updates.data?.last_scan_at ?? null;

  /**
   * THE SUPPORTING LINE EVERY TILE ON THIS PAGE CARRIES, and the reason it is
   * one function rather than four literals.
   *
   * A tile is a figure and a caption, and a caption alone does not say what
   * the figure counts — "Critical 2" is two of what, over which machines,
   * measured when. The tiles on `/remote` and `/security` in this same wave
   * each carry a line under the number; these four carried one only when the
   * fleet had never been scanned, so in the ordinary case a reader got a bare
   * digit and the neighbouring pages disagreed about what a tile even is.
   *
   * THREE STATES, AND THEY ARE DIFFERENT FACTS. Unmeasured says so and stops.
   * A measured zero already has a sentence of its own — `zeroLabel`, which
   * says which zero it is (A68) — and stacking a second line under it would
   * print two captions saying the same thing in different words. Everything
   * else gets the scope: what the number counts and over what.
   */
  const tileFoot = (count: number, words: string): string | undefined => {
    if (neverScanned) return t("patch.tab.neverScanned");
    return count === 0 ? undefined : words;
  };

  /** Both lists answered, and both are empty. Not "the filter matched nothing"
   *  — the severity filter cannot reach `total_unfiltered`, and the device
   *  rollup is never severity-filtered at all, so this really is "this platform
   *  has nothing outstanding" and the only open question is why. */
  const tabIsEmpty =
    updates.data != null &&
    devices.data != null &&
    outstanding === 0 &&
    deviceTotal === 0;

  const linux = family === "linux";

  const updatesNode = (
    <UpdatesPanel
      query={updates}
      filters={filters}
      onFilterChange={onFilterChange}
      pageSize={PATCH_ROLLUP_PAGE_SIZE}
      /* THE LINUX LIST IS READ-ONLY, AND THAT IS THE OPERATOR'S OWN REASONING
         RENDERED. Approving one apt package out of a resolved dependency set is
         "often not actionable anyway", so this list stops offering it: no
         checkbox column, no decision bar. It is evidence of what a host will
         receive when its window opens, and the decision is made against
         machines in the panel above. Windows and macOS keep theirs — a KB
         article and a softwareupdate label are discrete things, which is the
         whole difference. */
      canApprove={canApprove && !linux}
      ringOptions={ringOptions}
      fleetDeviceTotal={fleetDeviceTotal}
      neverScanned={neverScanned}
      selectedIds={selectedIds}
      onSelectionChange={onSelectionChange}
      resolveMatching={resolveMatchingUpdates}
      title={linux ? t("patch.tab.linuxUpdatesTitle") : t("patch.tab.osUpdatesTitle", { label })}
      subtitle={linux ? t("patch.tab.linuxUpdatesSub") : t("patch.tab.osUpdatesSub")}
      severityNote={SEVERITY_PROVENANCE[family]}
      scopeName={t("patch.tab.updatesScope", { label })}
    />
  );

  const machinesNode = (
    <div
      id={devicesAnchorId}
      ref={devicesAnchorRef}
      tabIndex={-1}
      // The panel below carries the visible heading; this wrapper exists to be
      // linked to and focused, so it names itself for the reader who arrives on
      // it rather than announcing an unlabelled group.
      role="group"
      aria-label={t("patch.tab.machinesAria", { label })}
    >
      <DevicesPanel
        query={devices}
        offset={deviceOffset}
        onOffsetChange={onDeviceOffsetChange}
        neverScanned={neverScanned}
        zone={zone}
        canApprove={canApprove}
        ringOptions={ringOptions}
        fleetDeviceTotal={fleetDeviceTotal}
        platformLabel={label}
        managedCount={managedCount}
        title={linux ? t("patch.tab.linuxMachinesTitle") : t("patch.tab.osMachinesTitle", { label })}
        subtitle={linux ? t("patch.tab.linuxMachinesSub") : t("patch.tab.osMachinesSub")}
        /* THE CHECKBOX COLUMN IS GATED ON `patch.apply`, NOT ON THE TAB. A
           selection with no control that can act on it is dead interface: an
           operator who may read the fleet but not install would get a column of
           ticks, a bar that renders nothing, and no way to find out why. RBAC
           is enforced server-side either way; this is about not offering a
           decision that cannot be made. */
        selectedHosts={linux && canApply ? selectedHosts : undefined}
        onHostSelectionChange={linux && canApply ? onHostSelectionChange : undefined}
        hostBar={
          linux ? (
            <HostDecisionBar
              selected={selectedHosts}
              onSelectionChange={onHostSelectionChange}
              matchingTotal={deviceTotal}
              resolveMatching={resolveMatchingHosts}
              familyLabel={label}
              canApply={canApply}
            />
          ) : undefined
        }
      />
    </div>
  );

  return (
    <>
      <PlatformNote family={family} />

      {/* FOUR NUMBERS, FOUR DESTINATIONS, AND ALL FOUR ARE FIELDS OF THE
              TWO RESPONSES THE TABLES BELOW ARE PAGED FROM.

              The first three are ONE HISTOGRAM. `sum(severity_counts) ==
              total_unfiltered` is the API's own stated contract, so "Critical"
              and "Important" are two buckets of the total beside them — not
              three counts that could drift apart, and never larger than it.
              (They do not SUM to it: moderate, low and unknown are the rest.)
              The fourth is the device rollup's `total`, and its link is the
              list it came from.

              WHAT LEFT THIS ROW: "Devices with critical exposure", which read
              `FleetReport.critical_device_count` — computed for the whole ORG
              by `reporting.fleet_report`, with no `os_family` predicate
              anywhere in it. On a platform tab that number describes a
              different set of machines from the table it links to, which is
              precisely the tile/table disagreement this page was rebuilt to
              end. `severity_counts.critical` is the scoped fact that survives,
              and it lands on the rows it counted. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("patch.tab.updatesOutstanding")}
          value={neverScanned ? "—" : String(outstanding)}
          icon="patch"
          to={severityHref(ALL_SEVERITIES_PARAM)}
          tone="neutral"
          foot={tileFoot(outstanding, t("patch.tab.updatesFoot", { label }))}
          zeroLabel={t("patch.tab.updatesZero", { label })}
        />
        <StatCard
          label={t("patch.tab.critical")}
          value={neverScanned ? "—" : String(criticalCount)}
          icon="alert-triangle"
          to={severityHref("critical")}
          tone="red"
          urgent={criticalCount > 0}
          foot={tileFoot(criticalCount, t("patch.tab.criticalFoot"))}
          zeroLabel={
            family === "macos" ? t("patch.tab.criticalZeroMac") : t("patch.tab.criticalZero")
          }
        />
        <StatCard
          label={t("patch.tab.important")}
          value={neverScanned ? "—" : String(importantCount)}
          icon="clock-exclamation"
          to={severityHref("important")}
          tone="orange"
          /* v3 washes both severity tiles (crit and warn); the console's
             rule that a measured zero is never loud still holds inside
             `StatCard`. */
          urgent={importantCount > 0}
          foot={tileFoot(importantCount, t("patch.tab.importantFoot"))}
          zeroLabel={
            family === "linux" ? t("patch.tab.importantZeroLinux") : t("patch.tab.importantZero")
          }
        />
        <StatCard
          label={t("patch.tab.machines")}
          value={neverScanned ? "—" : String(deviceTotal)}
          icon="devices"
          to={devicesHref()}
          tone="blue"
          foot={tileFoot(deviceTotal, t("patch.tab.machinesFoot", { label }))}
          zeroLabel={t("patch.tab.machinesZero", { label })}
        />
      </StatsGrid>

      {/* THE TILES STAY EVEN WHEN EVERY ONE OF THEM IS ZERO, and the panels
          below them do not.

          A zero on this page is a claim, and "0 outstanding" versus "never
          measured" are the same pixel unless the tile says which — that is A68,
          and it is why every tile here carries a `zeroLabel` and a "Never
          scanned" foot. Hiding the row when the tab is empty would delete
          exactly the numbers whose provenance the rule is about.

          What the tiles CANNOT say is which of three empty states this is, so
          that is the panel's job and it replaces the two working lists rather
          than sitting under them. Number above, reason below. */}
      {tabIsEmpty ? (
        <TabEmptyPanel
          family={family}
          managedCount={managedCount}
          devicesScanned={devicesScanned}
          lastScanAt={lastScanAt}
          zone={zone}
          canScan={canScan}
        />
      ) : (
        <>
          {/* THE ORDER IS THE MODEL. On Linux the machine is decided first and
              the package list is what it explains; on Windows and macOS the
              update is decided first and the machine list is the same decision
              seen from the other side. Same two components either way. */}
          {linux ? (
            <>
              {machinesNode}
              <div className="mt-4">{updatesNode}</div>
            </>
          ) : (
            <>
              {updatesNode}
              <div className="mt-4">{machinesNode}</div>
            </>
          )}
          {APP_MANAGER[family] && (
            <div className="mt-4">
              <AppsPanel
                family={family}
                canApprove={canApprove}
                ringOptions={ringOptions}
                fleetDeviceTotal={fleetDeviceTotal}
              />
            </div>
          )}
        </>
      )}
    </>
  );
}

/**
 * WHAT THIS PLATFORM CANNOT DO ON macOS.
 *
 * Windows and Linux each carried a note here too, and both were removed on
 * 2026-09-09 under the operator's console-copy ruling — "one short line, no
 * teaching". The Windows one taught the WSUS model and the Linux one taught
 * apt's dependency resolution, both before the reader had seen an update;
 * a mechanism belongs in /docs, and the consequence of approving already
 * lives in `DecisionBar`'s confirmation, where it is read at the moment it
 * matters.
 *
 * WHAT SURVIVES IS A WARNING, NOT A LESSON. Three things WSUS can do that
 * this platform cannot on macOS, each a property of `mac.go` or of
 * `agent-capabilities.md` §4, and none of them visible anywhere else on the
 * screen — the alternative is a tab that looks like the other two and
 * quietly cannot do what they do, which is how an operator comes to believe
 * a Mac is patched.
 *
 * "No severity" was a fourth bullet and is gone: `SEVERITY_PROVENANCE.macos`
 * says it beside the filter it is about, and a subtitle that restates a
 * visible line is noise.
 */
function PlatformNote({ family }: { family: PatchFamily }) {
  const t = useT();
  if (family !== "macos") return null;

  return (
    <div className="notice gold mb-4">
      <Icon name="alert-triangle" size={16} />
      <div>
        <p className="cell-title">{t("patch.tab.macTitle")}</p>
        <ul className="note mt-1.5 ml-4 list-disc">
          <li>
            <Trans k="patch.tab.macMajor" components={{ strong: <strong /> }} />
          </li>
          <li>
            <Trans k="patch.tab.macWarning" components={{ strong: <strong /> }} />
          </li>
          <li>
            <Trans k="patch.tab.macSilicon" components={{ strong: <strong /> }} />
          </li>
        </ul>
      </div>
    </div>
  );
}

/**
 * AN EMPTY TAB HAS THREE CAUSES AND THEY ARE NOT INTERCHANGEABLE.
 *
 * "No Windows machine is enrolled", "no scan has run" and "every Windows
 * machine is up to date" render as the same empty table, mean opposite things,
 * and lead somewhere different. This module has already conflated them twice —
 * `CompliancePanels` painted a row of 0% bars on a fleet nothing had scanned,
 * and the dashboard put nine confident zeros on an unmeasured page — so the
 * discrimination is a pure function (`tabEmptyReason`) with its own tests, and
 * this component only chooses words.
 *
 * THE FOURTH BRANCH IS THE HONEST ONE. Separating "no fleet" from "clean fleet"
 * needs a count of this platform's machines, which lives behind `devices.read`.
 * An operator without it gets told that the console cannot tell, which is worse
 * news and better information than picking whichever reading sounds nicer.
 */
function TabEmptyPanel({
  family,
  managedCount,
  devicesScanned,
  lastScanAt,
  zone,
  canScan,
}: {
  family: PatchFamily;
  managedCount: number | null;
  devicesScanned: number;
  lastScanAt: string | null;
  zone: string | null;
  canScan: boolean;
}) {
  const t = useT();
  const label = FAMILY_LABEL[family];
  const reason = tabEmptyReason({ enrolled: managedCount, devicesScanned });
  const machines = t("patch.family.machines", { count: managedCount ?? 0, label });

  const scanAction = canScan ? t("patch.tab.scanCan") : t("patch.tab.scanCannot");

  const content: Record<
    typeof reason,
    {
      icon: EmptyStateProps["icon"];
      title: string;
      body: string;
      action?: ReactNode;
    }
  > = {
    unenrolled: {
      icon: "devices",
      title: t("patch.tab.unenrolledTitle", { label }),
      body: t("patch.tab.unenrolledBody", { label }),
      action: (
        <Link className="btn" to="/devices/onboard">
          <Icon name="plus" size={14} />
          <span>{t("patch.tab.addDevices")}</span>
        </Link>
      ),
    },
    unscanned: {
      icon: "clock",
      title: t("patch.tab.unscannedTitle"),
      /* THE COUNT IS OPTIONAL AND THE CLAIM IS NOT. `managedCount` is `null`
         for an operator who cannot read the device list, and an earlier draft
         interpolated it anyway — "null Windows machines carry an agent". A
         sentence that degrades to a JavaScript literal is worse than one that
         simply says less, so the machine count is a clause rather than the
         subject. */
      body:
        managedCount == null
          ? t("patch.tab.unscannedNoCount", { action: scanAction })
          : t("patch.tab.unscannedCount", { count: managedCount, machines, action: scanAction }),
    },
    compliant: {
      icon: "check",
      title: t("patch.tab.compliantTitle", { label }),
      body: `${t("patch.tab.compliantBody", { count: managedCount ?? 0, machines })}${
        lastScanAt ? ` ${t("patch.tab.mostRecent", { when: formatDateTime(lastScanAt, zone) })}` : ""
      }`,
    },
    uncounted: {
      icon: "patch",
      title: t("patch.tab.uncountedTitle", { label }),
      body: t("patch.tab.uncountedBody", { label }),
    },
  };

  const { icon, title, body, action } = content[reason];
  return (
    <Panel>
      <EmptyState icon={icon} title={title} body={body} action={action} />
    </Panel>
  );
}
