import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import {
  usePatchApprovals,
  usePatchFamilyManagedCount,
  usePatchFamilyTotal,
  usePatchFleetReport,
  usePatchRings,
  usePatchScanMutation,
  usePatchZone,
} from "./api";
import { ApprovalsPanel } from "./ApprovalsPanel";
import { CompliancePanels } from "./CompliancePanels";
import { PatchReadiness } from "./PatchReadiness";
import { PatchTabPanel } from "./PatchTabPanel";
import { RestartsPanel } from "./RestartsPanel";
import { downloadBlob, scanQueuedMessage, severitiesFromParam, severityParamFor } from "./patchCopy";
import {
  FAMILY_LABEL,
  PATCH_FAMILIES,
  defaultSeveritiesFor,
  patchFamilyFromParam,
  type PatchFamily,
} from "./patchTabs";
import type { SelectedHost } from "./selection";
import { Btn, PageHead, Tabs } from "../../shell/components";
import { Icon } from "../../shell/icons";
import type { UpdateFilters } from "./UpdatesPanel";

/**
 * THE PATCH PAGE: ONE HEAD, THREE PLATFORMS, ONE RECORD.
 *
 * WHERE IT CAME FROM. 2026-08-25 this file was 1076 lines rendering an
 * "Exposed devices" table of one row per (device, patch) pair — the entire
 * uncapped combinatorial product — above a catalogue of the same updates again
 * with no fleet counts on them. One Ubuntu box with 300 upgradable packages was
 * ~600 rows and a 29,000-pixel page. The rebuild is
 * `docs/modules/patch/view-redesign.md`; its three decisions are untouched
 * here: one row per PATCH, every count aggregated in SQL, and tiles that cannot
 * disagree with the table because they read the same response objects. The
 * 2026-08-27 rework then made the device drill-down a working surface and gave
 * "select all N matching" a home; `DecisionBar` and `selection.ts` are that
 * work, reused rather than replaced.
 *
 * WHAT 2026-08-26 DECIDED AND THIS BUILDS. The operator asked to split patch
 * management into Linux, Windows (including Windows Server) and macOS,
 * "because the one model the page uses does not fit all three ecosystems". They
 * are right, and `patchTabs.ts` carries the evidence out of the three agent
 * scanners: apt publishes a security POCKET and calls it `important`, Microsoft
 * leaves most cumulative updates unrated, Apple publishes no severity at all
 * and its updates cannot be deferred. One severity default and one panel order
 * cannot be correct on all three, and until this page had tabs there was
 * nowhere to put the difference.
 *
 * THE SPLIT COST NO NEW ROUTE AND NO SCHEMA CHANGE. `Patch.os_family` is
 * `windows|linux|macos`, and both `GET /patch/updates` and
 * `GET /patch/device-rollup` already take `os_family` — so a tab is a predicate
 * these two routes were built to accept, not a new surface. The operator's
 * decision on the Linux model was explicit that no migration follows, and none
 * does: `patch_approvals` still has no device column, and the Linux tab says so
 * rather than pretending otherwise.
 *
 * HOW THIS PAGE IS ARRANGED, and the two halves are deliberately unequal:
 *
 *   HEAD + TABS   shared. The scan, the export and the policy link are about
 *                 the whole fleet, and three copies of them would be three
 *                 chances to disagree.
 *   TAB PANEL     one platform's whole working surface — its own tiles, its own
 *                 two lists in its own order. `PatchTabPanel`.
 *   THE RECORD    approvals and compliance, BELOW the tabs and outside them,
 *                 because neither is scopeable: `GET /patch/approvals` takes no
 *                 `os_family` (an approval is a patch and a ring, and the patch
 *                 knows its own platform) and the compliance split BY PLATFORM
 *                 is the cross-tab view — the readout that tells an operator
 *                 which tab to open next. Filtering it per tab would delete the
 *                 one panel the tabs make more useful.
 */
export function FleetPatchPage() {
  const t = useT();
  const api = useApi();
  // The failure itself, not its sentence: the sentence is worded at render, so a
  // language switch re-words it.
  const [exportFailure, setExportFailure] = useState<{ error: unknown } | null>(null);

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("patch.read");
  const canApprove = permissions.includes("patch.approve");
  const canApply = permissions.includes("patch.apply");
  const canScan = permissions.includes("patch.scan");
  const canManagePolicy = permissions.includes("patch.policy.manage");
  const canReadDevices = permissions.includes("devices.read");
  // Not a gate on anything: it decides only whether the Approvals table can put
  // a name on the account id an approval carries (`ApprovalsPanel`).
  const canReadUsers = permissions.includes("users.read");

  // The zone every timestamp on this page is rendered in — read from the
  // profile, not the browser (`lib/datetime.ts` records why). `me` is already
  // fetched above for the permission codes, so this costs no second request.
  const zone = usePatchZone();

  /**
   * EVERY FILTER LIVES IN THE URL, INCLUDING WHICH TAB IS OPEN.
   *
   * THE TAB IS `?os=`, AND IT IS THE `os_family` FILTER THIS PAGE ALREADY HAD.
   * It used to be a select in the update list's filter bar. Promoting it to the
   * page's primary navigation is the whole mechanism of the split: one
   * predicate, one place it is expressed, and no reachable state where the
   * Windows tab shows a list filtered to Linux.
   *
   * WHICH HOUSE PATTERN, AND WHY THAT ONE. Two precedents exist. `IpamNav`
   * makes each surface a ROUTE (`/ipam/drift`), which is right when the
   * surfaces are genuinely different pages with different data. The devices
   * page makes its tab strip a SEARCH PARAM (`?state=`) over one dataset, with
   * `Tabs` supplying the roving tabindex and a `role="tabpanel"` the strip
   * names. This is the second case and not the first: all three tabs are the
   * same two routes with one predicate changed, under one page head, above one
   * shared record. Three routes would be three copies of this file.
   *
   * It satisfies the requirement either way — `useSearchParams` reads the URL,
   * so a reload or a pasted link lands on the tab it names, and the browser's
   * Back button walks the tabs.
   */
  const [params, setParams] = useSearchParams();
  const family = patchFamilyFromParam(params.get("os"));

  const filters: UpdateFilters = {
    search: params.get("q") ?? "",
    // The DEFAULT is the tab's, not the module's. See `defaultSeveritiesFor`:
    // narrowing to critical+important is right on apt, where `important` means
    // the security pocket exactly, and wrong on Windows, where it hides the
    // monthly rollup Microsoft never rated.
    severities: severitiesFromParam(params.get("severity"), defaultSeveritiesFor(family)),
    offset: Number(params.get("offset") ?? 0) || 0,
  };
  const deviceOffset = Number(params.get("devices") ?? 0) || 0;

  /**
   * WHICH UPDATES ARE ABOUT TO BE APPROVED, AND WHICH MACHINES ARE ABOUT TO
   * INSTALL. Both held by the page, both for the same reason: a selection
   * outlives the panel's paging and has to be discarded the moment the
   * criterion under it changes.
   *
   * The guard MOVED rather than being dropped when "select all N matching"
   * arrived — a selection intersected with the rows on screen was the right
   * guard when 25 rows was the only way to build one, and the wrong guard for a
   * control whose entire job is selecting rows that are not on screen. So the
   * page clears both whenever the criterion changes, and each bar states its
   * size and what it was drawn from on every render.
   */
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [selectedHosts, setSelectedHosts] = useState<SelectedHost[]>([]);

  function updateParams(patch: Record<string, string>, replace = false) {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(patch)) {
      // Absent, not `?q=`: an empty filter in the URL reads as a filter set to
      // nothing.
      if (value === "") next.delete(key);
      else next.set(key, value);
    }
    setParams(next, { replace });
  }

  const onFilterChange = (next: Partial<UpdateFilters>, replace = false) => {
    const patch: Record<string, string> = {};
    if (next.search !== undefined) patch.q = next.search;
    if (next.severities !== undefined) patch.severity = severityParamFor(next.severities);
    if (next.offset !== undefined) patch.offset = next.offset === 0 ? "" : String(next.offset);
    // PAGING KEEPS THE SELECTION; CHANGING THE CRITERION DOES NOT. Ticking six
    // rows on page 1 and four on page 2 is one decision and the operator would
    // be right to expect it to survive Next. Narrowing the severity filter is a
    // different question being asked, and carrying an answer to the old one
    // across is how a batch comes to contain rows nobody looked at.
    const criterionChanged = Object.keys(next).some((key) => key !== "offset");
    if (criterionChanged) setSelectedIds([]);
    updateParams(patch, replace);
  };

  /**
   * SWITCHING TABS CLEARS EVERYTHING THE OLD TAB WAS ABOUT.
   *
   * Not tidiness — correctness, in three places. The severity DEFAULT differs
   * per platform, so carrying `?severity=critical,important` from Linux onto
   * macOS would apply a filter to a field Apple does not publish and show an
   * empty list under a tab whose own count says otherwise. Both PAGE OFFSETS
   * are positions in a different list. And both SELECTIONS are sets of ids that
   * belong to the platform they were made on — approving them from another tab
   * would be a batch nobody looked at, which is the one thing every control on
   * this page is built to prevent.
   */
  const onTabChange = (key: string) => {
    const next = new URLSearchParams(params);
    if (key === "windows") next.delete("os");
    else next.set("os", key);
    for (const stale of ["severity", "q", "offset", "devices"]) next.delete(stale);
    setSelectedIds([]);
    setSelectedHosts([]);
    setParams(next);
  };

  const report = usePatchFleetReport();
  const approvals = usePatchApprovals();
  const rings = usePatchRings();
  const scan = usePatchScanMutation();

  /**
   * THE COUNT ON EACH TAB, FROM THE SAME ROUTE THE TAB'S TABLE IS PAGED FROM.
   *
   * A tab strip is where an operator decides which platform to look at, so a
   * strip with no counts makes the default tab a coin toss: land on Windows,
   * see nothing, and never learn that Linux is carrying 300 packages. Each
   * count is `total_unfiltered` off `GET /patch/updates?os_family=…`, which is
   * the same field the tab's first tile prints — so the strip, the tile and the
   * table are one arithmetic rather than three.
   *
   * The hooks run for all three tabs at once and that is the point: two of them
   * are one row each, and being able to see the shape of all three fleets
   * without clicking is what the strip is FOR.
   */
  const windowsTotal = usePatchFamilyTotal("windows");
  const linuxTotal = usePatchFamilyTotal("linux");
  const macosTotal = usePatchFamilyTotal("macos");
  const familyTotals: Record<PatchFamily, number | undefined> = {
    windows: windowsTotal.data?.total_unfiltered,
    linux: linuxTotal.data?.total_unfiltered,
    macos: macosTotal.data?.total_unfiltered,
  };

  /** How many machines of the OPEN platform carry an agent — the denominator
   *  the patch API cannot supply, because `fleet_scan_coverage` counts the
   *  whole org whatever `os_family` was filtered on. Only the open tab's is
   *  fetched: it is the only one whose empty state or device sentence needs it. */
  const managed = usePatchFamilyManagedCount(family, canReadDevices);
  const managedCount = canReadDevices ? (managed.data ?? null) : null;

  async function handleExport() {
    setExportFailure(null);
    try {
      const blob = await api.exportPatchReport();
      downloadBlob(blob, `patch-report-${new Date().toISOString().slice(0, 10)}.csv`);
    } catch (error) {
      // `String(error)` printed "Error: " for the bodyless 503 the audit filed
      // as a BLOCKER — an empty message, announced to nobody.
      setExportFailure({ error });
    }
  }

  /**
   * A TILE'S LINK CARRIES THE CONTEXT ITS NUMBER WAS COUNTED IN, INCLUDING THE
   * TAB. `total_unfiltered` and `severity_counts` apply every filter except
   * severity — and `os_family` is one of those filters now — so with a search
   * in force the tiles read "outstanding on this platform, among what you are
   * looking at". A tile linking to a bare `/patch?severity=all` would print one
   * number and land on a different, larger list. So the destination is THIS
   * URL with the severity parameter swapped and the page reset.
   */
  const severityHref = (severity: string): string => {
    const next = new URLSearchParams(params);
    next.set("severity", severity);
    next.delete("offset");
    // Commas back out of `%2C`: `URLSearchParams` percent-encodes them, and a
    // comma is a legal sub-delimiter in a query value (RFC 3986 §3.4). This URL
    // gets pasted into chat and read aloud, so `severity=critical,important`
    // beats `severity=critical%2Cimportant` for no behavioural difference.
    return `/patch?${next.toString().replace(/%2C/g, ",")}`;
  };

  /** The machine tile points at the machine list, which is on this tab. */
  const devicesHref = (): string => {
    const query = params.toString().replace(/%2C/g, ",");
    return `/patch${query ? `?${query}` : ""}#${DEVICES_ANCHOR}`;
  };

  /**
   * ...AND A LINK TO A FRAGMENT HAS TO ACTUALLY GO THERE.
   *
   * React Router does not scroll to a hash — it changes the URL and re-renders,
   * which for a same-page anchor looks exactly like a link that did nothing.
   * Focus moves with the scroll, not just the viewport: a keyboard user who
   * activates a tile and is left in the tile row has been moved on screen and
   * nowhere in the tab order, and their next Tab goes to the fourth tile.
   */
  const location = useLocation();
  const devicesSection = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (location.hash !== `#${DEVICES_ANCHOR}`) return;
    const node = devicesSection.current;
    if (!node) return;
    node.scrollIntoView({ block: "start" });
    node.focus({ preventScroll: true });
  }, [location.hash, family]);

  /**
   * WHICH RINGS THE PAGE MAY APPROVE INTO, AND HOW BIG EACH ONE IS.
   *
   * The server's list comes first and in its own order, which is ROLLOUT order
   * (`ring_order`) rather than alphabetical — test before pilot before broad is
   * the staged rollout, and a list sorted any other way invites the operator to
   * reach for the widest ring first. `CANONICAL_RINGS` is appended for the ones
   * no policy has named yet; those genuinely hold no devices, because the route
   * lists every ring any policy names plus the org default, so a ring missing
   * from it is a ring nothing resolves into.
   *
   * NOT SCOPED BY PLATFORM, and it cannot be: a ring is a set of DEVICES
   * resolved through policies and groups, and nothing constrains a group to one
   * operating system. "test — 12 devices" on the macOS tab means twelve
   * machines of every kind, which is the honest reading of what approving there
   * does.
   */
  const ringOptions = useMemo(() => {
    const served = rings.data?.items ?? [];
    const names = new Set(served.map((ring) => ring.ring_name));
    return [
      ...served.map((ring) => ({ name: ring.ring_name, deviceCount: ring.device_count })),
      ...CANONICAL_RINGS.filter((name) => !names.has(name)).map((name) => ({
        name,
        deviceCount: rings.data ? 0 : null,
      })),
    ];
  }, [rings.data]);

  return (
    <div>
      <PageHead
        title={t("patch.fleet.title")}
        /* v3's sentence, plus the one thing this page has to say because it
           has three tabs: the gate is the same on every platform even though
           the shape of the decision is not. Nothing in any of these lists
           installs until it is approved. */
        description={t("patch.fleet.description")}
        actions={
          <>
            {/* ONE element, not a `<button>` inside an `<a>`: the accessibility
                tree exposed two overlapping controls for one visual target, and
                middle-click, Ctrl-click and "copy link address" all broke
                through the nested button. v3 draws it with the adjustments
                glyph. */}
            {canManagePolicy && (
              <Link className="btn" to="/patch/policies">
                <Icon name="adjustments" size={14} />
                <span>{t("patch.fleet.policies")}</span>
              </Link>
            )}
            {canRead && (
              <Btn label={t("patch.fleet.exportEvidence")} icon="download" onClick={() => void handleExport()} />
            )}
            {canScan && (
              // THE THREE HEAD ACTIONS ARE THREE OF THE SAME CONTROL, and this
              // one was not. It was a `Form` wrapping `FormActions`, which
              // brings `.form-actions` and its own button metrics into a row
              // built from `.btn`: it rendered ~14px shorter than the "Policies"
              // link and "Export evidence" beside it, sat off their top and
              // bottom edges, and was the only one of the three with no glyph.
              // A fleet scan takes no operator input, so there was never a form
              // here in any meaningful sense — nothing to fill in, nothing to
              // validate, one POST. It is a button, and now it is the kit's.
              //
              // WHAT `FormActions` WAS DOING THAT STILL HAS TO HAPPEN. Its
              // other half was the announcement: a `role="alert"` sentence on
              // failure and a `role="status"` one on success, never optimistic.
              // Both move to the notice row directly below the head, where
              // export already puts its own failure — so the two page-level
              // outcomes now read as one row of sentences instead of one
              // hanging off a button and one under it.
              //
              // THE KIT'S DEFAULT SECONDARY WEIGHT, which is what the old call
              // was reaching for when it passed `variant="outline"`: scanning
              // measures and changes nothing, and on the Linux tab this sits
              // near "Install approved updates", which really does run code as
              // root on other people's machines. Scanning informs, installing
              // commits, and the two are not painted alike.
              //
              // `Scan…` while in flight is `FormActions`' own spelling, kept so
              // the accessible name and the visible label stay the same string.
              <Btn
                label={scan.isPending ? t("patch.fleet.scanning") : t("patch.fleet.scan")}
                icon="refresh"
                disabled={scan.isPending}
                onClick={() => scan.mutate({})}
              />
            )}
          </>
        }
      />
      {/* THE PAGE-LEVEL OUTCOMES, ALL IN ONE PLACE. Scan and export are the
          two things the head can do, neither has a form, and both have to say
          what happened. v3's notice, with its kind's glyph, so the red and the
          blue are never the only thing telling a refusal from a confirmation.
          Each renders only when there IS one: an unconditional wrapper is a
          16px band of nothing under the head on every other render.

          NO OPTIMISTIC SUCCESS, the rule `FormActions` held before this: the
          queued sentence is the server's 2xx and nothing else, and a failure
          suppresses it. */}
      {scan.error ? (
        <p role="alert" className="notice red mb-4">
          <Icon name="circle-x" size={16} />
          <span>{describeSubmitError(scan.error)}</span>
        </p>
      ) : scan.isSuccess ? (
        <p role="status" className="notice mb-4">
          <Icon name="circle-check" size={16} />
          <span>{scanQueuedMessage(scan.data)}</span>
        </p>
      ) : null}

      {exportFailure && (
        <p role="alert" className="notice red mb-4">
          <Icon name="circle-x" size={16} />
          <span>{describeSubmitError(exportFailure.error)}</span>
        </p>
      )}

      <PatchReadiness />

      {/* THE STRIP CARRIES EACH PLATFORM'S OUTSTANDING COUNT, and that is what
          makes a fixed default tab safe: an operator who lands on Windows and
          sees "Linux 300" beside it has been told where the work is before
          reading a row. `Tabs` supplies the roving tabindex and the arrow keys;
          the panel and its id come from here, because only this page knows what
          the strip switches. */}
      <Tabs
        items={PATCH_FAMILIES.map((key) => ({
          key,
          label: FAMILY_LABEL[key],
          count: familyTotals[key],
        }))}
        active={family}
        onSelect={onTabChange}
        panelId={TAB_PANEL_ID}
      />

      <div id={TAB_PANEL_ID} role="tabpanel" aria-label={t("patch.fleet.tabAria", { family: FAMILY_LABEL[family] })}>
        <PatchTabPanel
          key={family}
          family={family}
          filters={filters}
          onFilterChange={onFilterChange}
          deviceOffset={deviceOffset}
          onDeviceOffsetChange={(offset) =>
            updateParams({ devices: offset === 0 ? "" : String(offset) })
          }
          selectedIds={selectedIds}
          onSelectionChange={setSelectedIds}
          selectedHosts={selectedHosts}
          onHostSelectionChange={setSelectedHosts}
          canApprove={canApprove}
          canApply={canApply}
          canScan={canScan}
          ringOptions={ringOptions}
          fleetDeviceTotal={rings.data?.device_total ?? null}
          zone={zone}
          managedCount={managedCount}
          severityHref={severityHref}
          devicesHref={devicesHref}
          devicesAnchorId={DEVICES_ANCHOR}
          devicesAnchorRef={devicesSection}
        />
      </div>

      {/* ---- the record, and it belongs to the whole fleet ----

          28px rather than 16, and the extra 12 is the whole of the hierarchy
          between the working surface and the record. `Panel` renders its title
          as an `<h2>` and lives in `shell/`, so a section heading here would
          have to be a second `<h2>` claiming to be the parent of the ones below
          it. A gap is not a lie.

          OUTSIDE THE TABPANEL, deliberately. Neither of these is scopeable to a
          platform — `GET /patch/approvals` takes no `os_family`, and the
          compliance split BY PLATFORM is precisely the cross-tab readout that
          tells an operator which tab to open next. Putting them inside a tab
          would either fork them three ways or delete the one panel the split
          makes more useful. */}
      <div className="mt-7">
        <h2 className="sr-only">{t("patch.fleet.acrossEvery")}</h2>
        <ApprovalsPanel
          query={approvals}
          canApprove={canApprove}
          canReadUsers={canReadUsers}
          zone={zone}
        />
      </div>

      {canRead && (
        <div className="mt-4">
          <RestartsPanel zone={zone} />
        </div>
      )}

      {/* `CompliancePanels` routes its own load failure through `QueryState`'s
          announced `role="alert"`, so there is no second error surface here. */}
      <div className="mt-4">
        <CompliancePanels report={report} canScan={canScan} />
      </div>
    </div>
  );
}

/** The fragment the machine tile links to, and the id of the section it lands
 *  on. One constant so the href and the target cannot drift apart. */
const DEVICES_ANCHOR = "machines-needing-updates";

/** The id the tab strip names with `aria-controls` and the panel answers to.
 *  One constant for the same reason. */
const TAB_PANEL_ID = "patch-tabpanel";

/**
 * The rings this console offers even when no policy names them.
 *
 * test -> pilot -> broad is the standard staged rollout (spec PD-04), and
 * `app/modules/patch/reporting.py` carries the same three for the same reason —
 * ordering the "Cleared for" column on a deployment that has written no policy.
 */
const CANONICAL_RINGS = ["test", "pilot", "broad"] as const;
