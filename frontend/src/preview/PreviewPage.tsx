/**
 * A DEV-ONLY harness for looking at the design in a browser.
 *
 * WHY THIS EXISTS. Every screen in this console is behind Keycloak, so until a
 * full stack is running nobody can see the ported design — and "the CSS is
 * byte-identical to the reference" and "it looks right" are different claims.
 * Building forty more pages on top of a stylesheet no human or screenshot has
 * ever rendered is how a systemic mistake reaches every screen at once.
 *
 * IT IS NOT REACHABLE IN PRODUCTION. `App.tsx` mounts this route only under
 * `import.meta.env.DEV`, which Vite replaces with the literal `false` in a
 * production build — so the bundler drops the route and this module with it.
 * `preview.test.tsx` asserts the built bundle contains none of it, because
 * "the route is guarded" and "the code is absent" are also different claims.
 *
 * The data below is FIXTURE data and is confined to this file. Nothing in
 * `modules/` imports it, and the guard test enforces that too — a mock that
 * leaks into a page is how a hardcoded 286 ends up in front of an operator.
 */

import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { Attributes } from "../components/attributes";
import { BarChart, LineChart } from "../components/chart";
import { ConfirmDialog } from "../components/confirm";
import { EmptyState, NoValue } from "../components/emptystate";
import { EntityPicker } from "../components/picker";
import { Badge, Table, Td, Th } from "../components/ui";
import { WeekPainter } from "../components/weekpainter";
import { AppShell, HealthGlyph } from "../shell/AppShell";
import {
  Avatar,
  Chips,
  Donut,
  Facts,
  Heat,
  KV,
  OsIcon,
  Person,
  Sparkline,
  Steps,
  Tile,
  Timeline,
  Toolbar,
  type Kind,
} from "../shell/viz";
import { DashboardWidgetFrame } from "../modules/dashboard/DashboardWidgetFrame";
import type { WidgetPlacement } from "../modules/dashboard/layout";
import { Icon } from "../shell/icons";
import type { HealthReading } from "../shell/healthSignal";
import {
  Btn,
  CellMain,
  FilterBar,
  PageHead,
  Pager,
  Panel,
  Progress,
  QueryState,
  SearchField,
  StatCard,
  StatsGrid,
  StatusPill,
  TableWrap,
  Tabs,
  Toggle,
} from "../shell/components";
import type { DocDocument, DocDocumentVersion, Me } from "../lib/api";
import { DocumentViewer } from "../modules/docs/DocumentViewer";
import { DocumentView, DocumentsSection } from "../modules/docs/documents";
import {
  PATCH_APPROVALS_LIST_LIMIT,
  PATCH_APPROVALS_QUERY_KEY,
  PATCH_DEVICE_ROLLUP_QUERY_KEY,
  PATCH_ROLLUP_PAGE_SIZE,
  PATCH_UPDATES_QUERY_KEY,
  usePatchDeviceRollup,
} from "../modules/patch/api";
import { DecisionBar } from "../modules/patch/DecisionBar";
import { DevicesPanel } from "../modules/patch/DevicesPanel";
import { FleetPatchPage } from "../modules/patch/FleetPatchPage";
import { HostDecisionBar } from "../modules/patch/HostDecisionBar";
import { defaultSeveritiesFor } from "../modules/patch/patchTabs";
import type {
  ApprovalListResponse,
  FleetReport,
  PatchDeviceRollupListResponse,
  PatchRingListResponse,
  PatchUpdate,
  PatchUpdateListResponse,
} from "../modules/patch/types";

const FIXTURE_DEVICES = [
  { host: "ws-alpha", fqdn: "ws-alpha.corp.local", os: "windows 11 23H2", arch: "amd64", agent: "online", cert: "healthy", patch: 94 },
  { host: "ws-beta", fqdn: "ws-beta.corp.local", os: "macos 15.2", arch: "arm64", agent: "online", cert: "renewing", patch: 78 },
  { host: "srv-dc01", fqdn: "srv-dc01.dc.local", os: "linux 24.04", arch: "amd64", agent: "offline", cert: "expiring", patch: 52 },
  /* A REVOKED AGENT CERTIFICATE, which the fleet list could not say at all
     until this cycle -- `cert_state` is derived from the expiry date alone, so
     this row reported `ok` while the machine could no longer reach the control
     plane. It is here because the pill it paints had never been rendered by
     anything, and a badge nobody has looked at is a badge that may have no
     rule behind it. */
  { host: "ws-retired", fqdn: "ws-retired.corp.local", os: "windows 11 23H2", arch: "amd64", agent: "offline", cert: "revoked", patch: 31 },
];

/** Every permission code, so the preview shows the full sidebar. */
const ALL_PERMISSIONS = [
  "ai.use", "ai.admin", "devices.read", "health.read", "audit.read", "patch.read",
  "deploy.read", "remote.control", "credentials.read", "backup.read",
  "provisioning.read", "site.read", "ticket.read", "portal.ticket.read",
  "notification.read", "email_intake.read", "sla.read", "docs.read", "change.read",
  "bcp.read", "reporting.read", "licenses.read", "subscriptions.read", "budget.read",
  "ipam.read", "jobs.read", "platform.errors.read",
];

/**
 * THE FOUR STATES OF THE TOP BAR'S HEALTH GLYPH, as readings rather than as
 * class names — the harness has to go through `healthSignal.ts` for the same
 * reason every other fixture here goes through the real component: a hardcoded
 * `health-ok` would still render if the rule that produces it were broken.
 *
 * `unknown` IS A READING WITH NULLS IN IT, not an absent one. Nobody measured
 * this is a state, and it is the one that must never be mistaken for green.
 */
const HEALTH_AMBER: HealthReading = { openAlerts: 12, platform: "ok" };

const HEALTH_STATES: ReadonlyArray<readonly [HealthReading, string]> = [
  [{ openAlerts: 0, platform: "ok" }, "Green — a real count, and it was zero"],
  [HEALTH_AMBER, "Amber — open alerts, or a degraded dependency"],
  [
    { openAlerts: 12, platform: "down" },
    "Red — a dependency the whole control plane stands on is down",
  ],
  [
    { openAlerts: null, platform: null },
    "Grey — nobody measured this, which is not the same as fine",
  ],
];

/* ---------------------------------------------------------------- /patch ---
 *
 * THE PAGE THE OPERATOR COMPLAINED HARDEST ABOUT, AND THE ONE THIS HARNESS HAS
 * NEVER RENDERED. `scripts/a11y-browser.mjs` drives `/__preview` and nothing
 * else, and `/patch` was not on it -- not before the rollup rebuild and not
 * after -- so no axe rule, no composited-pixel sample and no reflow check had
 * ever touched a severity chip, an approval row or the three-control page head
 * that module ships. It is the same gap the health glyph had: a component is
 * covered by this pass only if this file renders it.
 *
 * WHY IT NEEDS A QUERY CLIENT OF ITS OWN. `FleetPatchPage` owns seven queries
 * and hands their results down; the panels below it read `query.data` and
 * nothing else. So the honest way to render it here is to serve those seven
 * queries the shape the API serves, which is what `patchPreviewClient()` does:
 * a cache seeded by key, `staleTime: Infinity` so nothing refetches, and
 * `retry: false` so a key this file gets WRONG shows up as a visible error
 * panel in the harness rather than a silent spinner. There is no `/api` behind
 * the dev server, so a miss cannot hide.
 *
 * THE FIXTURES ARE MIXED ON PURPOSE. Four severities, an approval that has
 * been withdrawn, a device with a critical exposure and one without: the
 * states that paint different hues are the states worth putting a browser in
 * front of, and a single-severity fleet would have measured one chip four
 * times.
 */

/* THE FIXTURES ARE SPLIT BY PLATFORM, because the page is.
 *
 * `?os=` is the tab and both rollups take `os_family`, so a single mixed page
 * is no longer a shape the API can serve to this console: every list the
 * operator sees is one platform's. Three arrays rather than one filtered at
 * module scope, for the reason `patchPreviewClient` records below -- a
 * `.filter()` call is a side effect rollup cannot shake, and it would drag
 * every fixture in this file into `dist/`.
 *
 * EACH ONE IS HONEST TO ITS OWN SCANNER, which is the point of the split and
 * was worth getting right here too:
 *   windows  a rated critical, a rated important, and MSRC ratings generally
 *   linux    `important` == the security pocket, everything else `unknown`
 *   macos    NO critical anywhere. `mac.go`'s `macSeverity` can only ever
 *            return `important` (Apple's `Recommended: YES`) or `unknown`, so
 *            a critical macOS row is a state the product cannot produce and a
 *            fixture claiming one would have measured a chip that never paints.
 */

const PATCH_FIXTURE_WINDOWS_UPDATES: PatchUpdate[] = [
  {
    id: "p-1", os_family: "windows", source: "wsus", identifier: "KB5041585",
    title: "2026-08 Cumulative Update for Windows 11 Version 23H2 for x64-based Systems",
    severity: "critical", kb: "KB5041585", cve: ["CVE-2026-38063"], priority: 1,
    first_seen_at: "2026-08-14T02:10:00Z",
    devices_missing: 41, devices_approved: 6, devices_failed: 2, devices_installed: 237,
    device_state: null, device_last_error: null, device_applied_at: null, devices_outstanding: 49, cleared_rings: ["test"],
  },
  {
    id: "p-4", os_family: "windows", source: "wsus", identifier: "KB5041948",
    title: "Security Update for Microsoft Edge (Chromium-based)",
    severity: "important", kb: "KB5041948", cve: ["CVE-2026-38109"], priority: 2,
    first_seen_at: "2026-08-11T04:00:00Z",
    devices_missing: 63, devices_approved: 12, devices_failed: 0, devices_installed: 211,
    device_state: null, device_last_error: null, device_applied_at: null, devices_outstanding: 75, cleared_rings: ["test", "pilot", "broad"],
  },
  {
    id: "p-6", os_family: "windows", source: "wsus", identifier: "KB5039212",
    title: ".NET 8.0.8 Security Update for Windows Server 2022",
    severity: "unknown", kb: "KB5039212", cve: null, priority: 3,
    first_seen_at: "2026-07-30T22:15:00Z",
    devices_missing: 9, devices_approved: 4, devices_failed: 0, devices_installed: 40,
    device_state: null, device_last_error: null, device_applied_at: null, devices_outstanding: 13, cleared_rings: ["test"],
  },
];

const PATCH_FIXTURE_LINUX_UPDATES: PatchUpdate[] = [
  {
    id: "p-2", os_family: "linux", source: "apt", identifier: "openssl@3.0.13-0ubuntu3.4",
    title: "openssl — remote code execution in the X.509 name-constraint parser",
    severity: "important", kb: null, cve: ["CVE-2026-2511", "CVE-2026-2512"], priority: 1,
    first_seen_at: "2026-08-19T09:41:00Z",
    devices_missing: 18, devices_approved: 0, devices_failed: 0, devices_installed: 4,
    device_state: null, device_last_error: null, device_applied_at: null, devices_outstanding: 18, cleared_rings: [],
  },
  {
    id: "p-5", os_family: "linux", source: "apt", identifier: "linux-image-generic@6.8.0-45.45",
    title: "linux — privilege escalation in the netfilter subsystem",
    severity: "important", kb: null, cve: ["CVE-2026-1086"], priority: 2,
    first_seen_at: "2026-08-18T11:26:00Z",
    devices_missing: 22, devices_approved: 0, devices_failed: 3, devices_installed: 0,
    device_state: null, device_last_error: null, device_applied_at: null, devices_outstanding: 25, cleared_rings: [],
  },
  {
    id: "p-9", os_family: "linux", source: "apt", identifier: "curl@8.5.0-2ubuntu10.6",
    title: "curl — heap overflow in the SOCKS5 handshake",
    severity: "critical", kb: null, cve: ["CVE-2026-38545"], priority: 1,
    first_seen_at: "2026-08-22T15:02:00Z",
    devices_missing: 2, devices_approved: 0, devices_failed: 0, devices_installed: 0,
    device_state: null, device_last_error: null, device_applied_at: null, devices_outstanding: 2, cleared_rings: [],
  },
];

const PATCH_FIXTURE_MACOS_UPDATES: PatchUpdate[] = [
  {
    id: "p-3", os_family: "macos", source: "softwareupdate", identifier: "macOS Sequoia 15.6.1-24G90",
    title: "macOS Sequoia 15.6.1",
    severity: "important", kb: null, cve: null, priority: 2,
    first_seen_at: "2026-08-21T17:02:00Z",
    devices_missing: 7, devices_approved: 1, devices_failed: 1, devices_installed: 12,
    device_state: null, device_last_error: null, device_applied_at: null, devices_outstanding: 9, cleared_rings: ["test", "pilot"],
  },
  {
    id: "p-7", os_family: "macos", source: "softwareupdate", identifier: "Safari18.6MontereyAuto-18.6",
    title: "Safari 18.6",
    severity: "important", kb: null, cve: ["CVE-2026-44309"], priority: 3,
    first_seen_at: "2026-08-06T13:48:00Z",
    devices_missing: 11, devices_approved: 0, devices_failed: 0, devices_installed: 9,
    device_state: null, device_last_error: null, device_applied_at: null, devices_outstanding: 11, cleared_rings: [],
  },
  {
    id: "p-10", os_family: "macos", source: "softwareupdate", identifier: "XProtectPlistConfigData-5291",
    title: "XProtect configuration data",
    severity: "unknown", kb: null, cve: null, priority: null,
    first_seen_at: "2026-08-25T06:20:00Z",
    devices_missing: 3, devices_approved: 0, devices_failed: 0, devices_installed: 14,
    device_state: null, device_last_error: null, device_applied_at: null, devices_outstanding: 3, cleared_rings: [],
  },
];

/** `sum(severity_counts) === total_unfiltered`, as the API's own contract says.
 *
 *  THE WINDOWS AND macOS TABS DO NOT NARROW, so `total === total_unfiltered`
 *  there and the "N more are outside the severity filter" line correctly does
 *  not render. THE LINUX TAB DOES -- `important` is the security pocket
 *  exactly -- so its page is three of forty-one, and that sentence, the
 *  widening button beside it and the paging controls all have something true
 *  to say. That asymmetry IS the redesign, so the fixtures have to carry it or
 *  the browser pass measures one tab three times. */
const PATCH_FIXTURE_WINDOWS_PAGE: PatchUpdateListResponse = {
  items: PATCH_FIXTURE_WINDOWS_UPDATES,
  total: 3,
  total_unfiltered: 3,
  severity_counts: { critical: 1, important: 1, unknown: 1 },
  last_scan_at: "2026-08-26T04:12:00Z",
  devices_scanned: 274,
  devices_total: 286,
};

const PATCH_FIXTURE_LINUX_PAGE: PatchUpdateListResponse = {
  items: PATCH_FIXTURE_LINUX_UPDATES,
  total: 3,
  total_unfiltered: 41,
  severity_counts: { critical: 1, important: 2, moderate: 21, unknown: 17 },
  last_scan_at: "2026-08-26T04:09:00Z",
  devices_scanned: 274,
  devices_total: 286,
};

const PATCH_FIXTURE_MACOS_PAGE: PatchUpdateListResponse = {
  items: PATCH_FIXTURE_MACOS_UPDATES,
  total: 3,
  total_unfiltered: 3,
  severity_counts: { important: 2, unknown: 1 },
  last_scan_at: "2026-08-25T22:31:00Z",
  devices_scanned: 274,
  devices_total: 286,
};

/* THE MACHINE ROLLUPS, ALSO SPLIT BY PLATFORM, AND THE LINUX ONE HOLDS TWO.
 *
 * Two Linux hosts rather than one is not padding: the Linux tab's selection is
 * over MACHINES, so a one-row table cannot show "2 hosts selected of 2", cannot
 * exercise the header checkbox against a partial selection, and cannot produce
 * the multi-host sentence `hostLensWarning` and the install confirmation both
 * build. A single row would have measured the singular branch of every string
 * on that tab. */

const PATCH_FIXTURE_WINDOWS_ROLLUP: PatchDeviceRollupListResponse = {
  items: [
    {
      device_id: "d-2", client_id: "o-1", hostname: "ws-alpha", os_family: "windows", outstanding: 12, failed: 2,
      worst_severity: "critical",
      counts_by_severity: { critical: 1, important: 3, moderate: 8 },
      last_scan_at: "2026-08-26T03:55:00Z",
    },
    {
      device_id: "d-4", client_id: "o-1", hostname: "ws-gamma", os_family: "windows", outstanding: 2, failed: 0,
      worst_severity: "low",
      counts_by_severity: { low: 2 },
      last_scan_at: "2026-08-26T04:01:00Z",
    },
  ],
  total: 2,
  last_scan_at: "2026-08-26T04:12:00Z",
  devices_scanned: 274,
  devices_total: 286,
};

const PATCH_FIXTURE_LINUX_ROLLUP: PatchDeviceRollupListResponse = {
  items: [
    {
      device_id: "d-1", client_id: "o-1", hostname: "srv-dc01", os_family: "linux", outstanding: 41, failed: 0,
      worst_severity: "critical",
      counts_by_severity: { critical: 1, important: 2, moderate: 21, unknown: 17 },
      last_scan_at: "2026-08-26T04:09:00Z",
    },
    {
      device_id: "d-5", client_id: "o-1", hostname: "srv-app02", os_family: "linux", outstanding: 18, failed: 0,
      worst_severity: "important",
      counts_by_severity: { important: 2, moderate: 9, unknown: 7 },
      // NEVER ANSWERED. A machine with no scan is a different fact from one
      // with nothing outstanding, and this column is where the difference is
      // said -- so one row in this harness has to carry it or the branch that
      // prints "Never scanned" is never painted.
      last_scan_at: null,
    },
  ],
  total: 2,
  last_scan_at: "2026-08-26T04:09:00Z",
  devices_scanned: 274,
  devices_total: 286,
};

const PATCH_FIXTURE_MACOS_ROLLUP: PatchDeviceRollupListResponse = {
  items: [
    {
      device_id: "d-3", client_id: "o-1", hostname: "ws-beta", os_family: "macos", outstanding: 6, failed: 0,
      worst_severity: "important",
      counts_by_severity: { important: 2, moderate: 4 },
      last_scan_at: "2026-08-25T22:31:00Z",
    },
  ],
  total: 1,
  last_scan_at: "2026-08-25T22:31:00Z",
  devices_scanned: 274,
  devices_total: 286,
};

/**
 * ONE MACHINE'S OWN LIST, which is the surface the 2026-08-27 rework added and
 * therefore the surface this harness had never rendered. The device drill-down
 * used to be a read-only `<ul>`; it is a selectable table with its own
 * `DecisionBar` now, so it carries checkboxes, a ring select, a magenta commit
 * and a blast-radius sentence — none of which any browser had measured, because
 * a component enters this pass only by being on this page.
 *
 * `total` is 41 against 3 rendered rows on purpose: that is `srv-dc01`'s own
 * outstanding count from the rollup above, so the expansion's paging controls
 * and its "Showing 1-3 of 41" line both have something true to say.
 */
const PATCH_FIXTURE_DEVICE_UPDATES: PatchUpdateListResponse = {
  /* WRITTEN OUT, NOT INDEXED OUT OF `PATCH_FIXTURE_LINUX_UPDATES`. Two rows
     here are the same updates that machine carries in the fleet list, and the
     obvious way to say so is `PATCH_FIXTURE_LINUX_UPDATES[1]`. `preview.test.ts`
     failed on
     it: a member access into that array anchors the WHOLE array, so
     `KB5041585` and its five neighbours reached `dist/`. Same lesson as the
     module-scope call above, one construct along. */
  items: [
    {
      id: "p-2", os_family: "linux", source: "apt", identifier: "openssl@3.0.13-0ubuntu3.4",
      title: "openssl — remote code execution in the X.509 name-constraint parser",
      severity: "important", kb: null, cve: ["CVE-2026-2511", "CVE-2026-2512"], priority: 1,
      first_seen_at: "2026-08-19T09:41:00Z",
      devices_missing: 18, devices_approved: 0, devices_failed: 0, devices_installed: 4,
      device_state: null, device_last_error: null, device_applied_at: null, devices_outstanding: 18, cleared_rings: [],
    },
    {
      id: "p-5", os_family: "linux", source: "apt", identifier: "linux-image-generic@6.8.0-45.45",
      title: "linux — privilege escalation in the netfilter subsystem",
      severity: "important", kb: null, cve: ["CVE-2026-1086"], priority: 2,
      first_seen_at: "2026-08-18T11:26:00Z",
      devices_missing: 22, devices_approved: 0, devices_failed: 3, devices_installed: 0,
      device_state: null, device_last_error: null, device_applied_at: null, devices_outstanding: 25, cleared_rings: [],
    },
    {
      id: "p-8", os_family: "linux", source: "apt", identifier: "sudo@1.9.15p5-3ubuntu5.24.04.1",
      title: "sudo — local privilege escalation via the chroot option",
      /* `unknown`, NOT `moderate`, and apt is why: `aptSeverity` returns
         `important` for a security-pocket origin and `unknown` for everything
         else. There is no third value it can produce, so a `moderate` apt row
         is a state this product cannot reach. */
      severity: "unknown", kb: null, cve: ["CVE-2026-32462"], priority: 3,
      first_seen_at: "2026-08-23T06:14:00Z",
      devices_missing: 1, devices_approved: 0, devices_failed: 0, devices_installed: 0,
      device_state: null, device_last_error: null, device_applied_at: null, devices_outstanding: 1, cleared_rings: [],
    },
  ],
  total: 41,
  total_unfiltered: 41,
  severity_counts: { critical: 1, important: 2, moderate: 21, unknown: 17 },
  last_scan_at: "2026-08-26T04:09:00Z",
  devices_scanned: 274,
  devices_total: 286,
};

const PATCH_FIXTURE_REPORT: FleetReport = {
  compliant_pct_by_os_family: { windows: 94.2, linux: 71.5, macos: 88.0 },
  compliant_pct_by_severity: { critical: 96.1, important: 84.7, moderate: 62.3, low: 55.0 },
  /* EMPTY, AND THAT IS THE POINT: this is the uncapped (device, patch) product
     the rebuild stopped rendering. The percentages above are computed from the
     same query, which is why the field survives at all. */
  exposures: [],
  exposed_device_count: 4,
  critical_device_count: 2,
  last_scan_at: "2026-08-26T04:12:00Z",
  devices_scanned: 274,
  devices_total: 286,
};

const PATCH_FIXTURE_RINGS: PatchRingListResponse = {
  items: [
    { ring_name: "test", ring_order: 0, device_count: 12, client_device_counts: [{ client_id: "o-1", device_count: 12 }] },
    { ring_name: "pilot", ring_order: 1, device_count: 47, client_device_counts: [{ client_id: "o-1", device_count: 47 }] },
    { ring_name: "broad", ring_order: 2, device_count: 227, client_device_counts: [{ client_id: "o-1", device_count: 227 }] },
  ],
  total: 3,
  device_total: 286,
};

const PATCH_FIXTURE_APPROVALS: ApprovalListResponse = {
  items: [
    {
      id: "a-1", client_id: null, patch_id: "p-1", patch_identifier: "KB5041585", ring_name: "test", approved_by: "alex.morgan@toeshee.example",
      approved_at: "2026-08-24T08:30:00Z", revoked: false, revoked_by: null, revoked_at: null,
    },
    {
      id: "a-2", client_id: null, patch_id: "p-4", patch_identifier: "KB5041948", ring_name: "broad", approved_by: null,
      approved_at: "2026-08-22T02:00:00Z", revoked: false, revoked_by: null, revoked_at: null,
    },
    /* A WITHDRAWN CLEARANCE. The row survives the revoke so the trail holds,
       which means this list has to RENDER one as revoked rather than drop it --
       a state with its own paint, and one no browser had ever measured. */
    {
      id: "a-3", client_id: null, patch_id: "p-3", patch_identifier: "macOS-15.6.1", ring_name: "pilot", approved_by: "sam.okafor@toeshee.example",
      approved_at: "2026-08-20T14:05:00Z", revoked: true,
      revoked_by: "alex.morgan@toeshee.example", revoked_at: "2026-08-25T09:12:00Z",
    },
  ],
  total: 3,
};


/**
 * THE PATCH SURFACES A RESTING PAGE CANNOT SHOW, AND THERE ARE FOUR NOW.
 *
 * A harness that renders a page renders the page's RESTING state, and every
 * control this cycle is about lives past a click. `/patch` above opens on the
 * Windows tab with nothing selected, so without this block the browser pass
 * would never composite:
 *
 *   1. the Linux tab at all — a different panel order, a read-only update list
 *      and a machine table with a checkbox column;
 *   2. `HostDecisionBar` in its COMMITTED state, which is where the one magenta
 *      on that tab lives, beside the sentence about maintenance windows;
 *   3. the device drill-down, which opens on a button; and
 *   4. `DecisionBar` with a selection standing — the magenta approve beside a
 *      ring select, which is a different control on a different ground.
 *
 * None of the four had a measured contrast ratio, a measured reflow at 320px
 * or an axe pass before it appeared here, because a component enters this pass
 * only by being on this page.
 *
 * They read the SEEDED CACHE through the module's own hooks rather than being
 * handed a hand-built `UseQueryResult`: a cast would let this file drift from
 * what the panels actually receive, which is the drift `patchPreviewClient`
 * exists to prevent one level up.
 */
function PatchWorkingSurfaces() {
  const devices = usePatchDeviceRollup({ os_family: "linux" });
  /* INSIDE THE COMPONENT, and that is not a style choice — it is the same
     lesson `patchPreviewClient` records a few lines up. A module-scope
     `PATCH_FIXTURE_RINGS.items.map(...)` is a CALL, and rollup cannot prove
     `Array.prototype.map` free of side effects, so it retains the call and
     with it every fixture the call references. `preview.test.ts` caught this
     one in `dist/` before it shipped. */
  const rings = useMemo(
    () =>
      PATCH_FIXTURE_RINGS.items.map((ring) => ({
        name: ring.ring_name,
        deviceCount: ring.device_count,
        clientCounts: Object.fromEntries(
          ring.client_device_counts.map((row) => [row.client_id, row.device_count]),
        ),
      })),
    [],
  );
  /* A SELECTION OF MACHINES, STANDING. Two of two, so the bar renders its
     committed state — the count, the magenta install and the clear — and the
     header checkbox paints CHECKED rather than empty. Built here rather than
     at module scope for the `.map` reason above. */
  const selectedHosts = useMemo(
    () => [
      { id: "d-1", hostname: "srv-dc01" },
      { id: "d-5", hostname: "srv-app02" },
    ],
    [],
  );
  return (
    <>
      <DevicesPanel
        query={devices}
        offset={0}
        onOffsetChange={() => undefined}
        neverScanned={false}
        zone="Europe/London"
        canApprove
        ringOptions={rings}
        fleetDeviceTotal={PATCH_FIXTURE_RINGS.device_total}
        platformLabel="Linux"
        managedCount={31}
        title="Linux machines"
        subtitle="The unit of decision on Linux. Pick the hosts to install on; the count beside each is every outstanding package on it, whatever the severity filter below is set to. Open one to see and approve its own list."
        selectedHosts={selectedHosts}
        onHostSelectionChange={() => undefined}
        hostBar={
          <HostDecisionBar
            selected={selectedHosts}
            onSelectionChange={() => undefined}
            matchingTotal={2}
            resolveMatching={() =>
              Promise.resolve({ hosts: selectedHosts, matching: 2, truncated: false })
            }
            familyLabel="Linux"
            canApply
          />
        }
        defaultExpandedDeviceId="d-1"
      />
      <div style={{ marginTop: 14 }}>
        <Panel
          title="The decision bar, with a selection standing"
          subtitle="What the Windows and macOS update lists look like once rows are ticked: the count, what it was drawn from, the ring it will be cleared for, and the one magenta control on that screen."
          bare
        >
          <DecisionBar
            selectedIds={["p-1", "p-4"]}
            onSelectionChange={() => undefined}
            matchingTotal={3}
            resolveMatching={() => Promise.resolve({ ids: ["p-1"], matching: 3, truncated: false })}
            ringOptions={rings}
            fleetDeviceTotal={PATCH_FIXTURE_RINGS.device_total}
            scopeName="fleet updates, preview"
          />
        </Panel>
      </div>
    </>
  );
}

/** Every patch permission, so the head renders all three of its controls --
 *  Policies, Export evidence and Scan. That row is what overflowed below
 *  450px, and it cannot be measured on a head that draws one button. */
const PATCH_FIXTURE_ME: Me = {
  user_id: "u-1",
  org_id: "o-1",
  email: "alex.morgan@toeshee.example",
  display_name: "Alex Morgan",
  /* EVERY PATCH PERMISSION *AND* `devices.read`. `patch.apply` is what draws
     the Linux tab's host checkboxes and its one magenta control, and
     `devices.read` is what lets the page count this platform's machines — the
     number that separates "no Linux fleet" from "clean Linux fleet". A fixture
     missing either would render a harness that measures the *degraded* screen
     while looking like the whole one. */
  permissions: [
    "patch.read",
    "patch.approve",
    "patch.apply",
    "patch.scan",
    "patch.policy.manage",
    "devices.read",
  ],
  timezone: "Europe/London",
};

/**
 * A cache seeded by key, and nothing that can reach the network.
 *
 * The keys are built from the module's OWN exported constants rather than
 * copied as literals, so a page size or a default severity changing upstream
 * moves this with it instead of silently missing and rendering an error.
 */
function patchPreviewClient(): QueryClient {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: Infinity,
        gcTime: Infinity,
        retry: false,
        refetchOnMount: false,
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
      },
    },
  });
  client.setQueryData(["me"], PATCH_FIXTURE_ME);

  /* THE THREE PLATFORM TABS. Both rollups take `os_family`, so the tab IS the
     predicate and every list on this page carries it. `severity: []` on the
     Windows key is `defaultSeveritiesFor("windows")` — that tab does not narrow,
     because Microsoft leaves most cumulative updates unrated.

     THE KEYS ARE BUILT FROM THE MODULE'S OWN CONSTANTS rather than copied as
     literals, so a page size or a default severity changing upstream moves this
     with it instead of silently missing and rendering an error panel. */
  client.setQueryData(
    [
      PATCH_UPDATES_QUERY_KEY,
      { limit: PATCH_ROLLUP_PAGE_SIZE, os_family: "windows", severity: defaultSeveritiesFor("windows") },
    ],
    PATCH_FIXTURE_WINDOWS_PAGE,
  );
  client.setQueryData(
    [
      PATCH_UPDATES_QUERY_KEY,
      { limit: PATCH_ROLLUP_PAGE_SIZE, os_family: "linux", severity: defaultSeveritiesFor("linux") },
    ],
    PATCH_FIXTURE_LINUX_PAGE,
  );
  client.setQueryData(
    [
      PATCH_UPDATES_QUERY_KEY,
      { limit: PATCH_ROLLUP_PAGE_SIZE, os_family: "macos", severity: defaultSeveritiesFor("macos") },
    ],
    PATCH_FIXTURE_MACOS_PAGE,
  );
  client.setQueryData(
    [PATCH_DEVICE_ROLLUP_QUERY_KEY, { limit: PATCH_ROLLUP_PAGE_SIZE, os_family: "windows" }],
    PATCH_FIXTURE_WINDOWS_ROLLUP,
  );
  client.setQueryData(
    [PATCH_DEVICE_ROLLUP_QUERY_KEY, { limit: PATCH_ROLLUP_PAGE_SIZE, os_family: "linux" }],
    PATCH_FIXTURE_LINUX_ROLLUP,
  );
  client.setQueryData(
    [PATCH_DEVICE_ROLLUP_QUERY_KEY, { limit: PATCH_ROLLUP_PAGE_SIZE, os_family: "macos" }],
    PATCH_FIXTURE_MACOS_ROLLUP,
  );

  /* THE COUNT ON EACH TAB, and it is the same route asked for one row. The
     strip's number is `total_unfiltered` off these, which is the same field the
     open tab's first tile prints — so seeding them from the same objects is not
     laziness, it is the guarantee that the strip and the tile cannot disagree
     in the harness any more than they can in the product. */
  client.setQueryData(
    [PATCH_UPDATES_QUERY_KEY, { os_family: "windows", limit: 1 }],
    PATCH_FIXTURE_WINDOWS_PAGE,
  );
  client.setQueryData(
    [PATCH_UPDATES_QUERY_KEY, { os_family: "linux", limit: 1 }],
    PATCH_FIXTURE_LINUX_PAGE,
  );
  client.setQueryData(
    [PATCH_UPDATES_QUERY_KEY, { os_family: "macos", limit: 1 }],
    PATCH_FIXTURE_MACOS_PAGE,
  );

  /* HOW MANY WINDOWS MACHINES CARRY AN AGENT — `GET /devices?os_family=…`,
     which is the denominator the patch API cannot give, because
     `fleet_scan_coverage` counts the whole org whatever was filtered on. The
     device panel's opening sentence reads it, and so does the empty state that
     has to tell "no Windows fleet" from "clean Windows fleet". */
  client.setQueryData(["patch-family-managed", "windows"], 214);
  client.setQueryData(["patch-family-managed", "linux"], 31);
  client.setQueryData(["patch-family-managed", "macos"], 41);

  /* THE EXPANSION'S OWN QUERY. `GET /patch/updates?device_id=…` is the same
     route the fleet list uses with one more predicate, and the drill-down is
     mounted expanded below, so this key has to answer or the harness measures
     an error panel instead of the surface. */
  client.setQueryData(
    [PATCH_UPDATES_QUERY_KEY, { limit: PATCH_ROLLUP_PAGE_SIZE, device_id: "d-1", severity: [] }],
    PATCH_FIXTURE_DEVICE_UPDATES,
  );
  client.setQueryData(["patch-fleet"], PATCH_FIXTURE_REPORT);
  client.setQueryData(["patch-rings"], PATCH_FIXTURE_RINGS);
  client.setQueryData(
    [PATCH_APPROVALS_QUERY_KEY, { limit: PATCH_APPROVALS_LIST_LIMIT }],
    PATCH_FIXTURE_APPROVALS,
  );
  /* NO `patch-catalog` SEED. The page used to fetch `GET /patch/patches` at
     its 500-row ceiling purely to name the patch on each approval row;
     `ApprovalOut.patch_identifier` carries the name now, so there is no query
     to seed and the fixture that fed it is gone. */
  return client;
}

/** Built once: a client rebuilt on every render would throw the seeded
 *  cache away and put the page into a permanent loading state. */
/**
 * BUILT LAZILY, INSIDE A COMPONENT, AND THAT IS NOT A STYLE CHOICE.
 *
 * These were `const X_PREVIEW_CLIENT = xPreviewClient()` at module scope, and
 * the consequence was measurable in `dist/`: rollup drops the `/__preview`
 * route and the `PreviewPage` component under `import.meta.env.DEV`, but a
 * module-scope CALL whose body runs `client.setQueryData(...)` is a side
 * effect it cannot prove away -- so the call was retained, and with it every
 * fixture the call references. `KB5041585`, `openssl=3.0.13-0ubuntu3.4`,
 * `srv-dc01` and `Alex Morgan` were all in the shipped production bundle,
 * against a file whose own header says "The data below is FIXTURE data and is
 * confined to this file".
 *
 * `preview.test.ts` was green throughout, because the two strings it greps for
 * (`ws-alpha.corp.local`, `srv-dc01.dc.local`) live in `FIXTURE_DEVICES` -- a
 * plain array nothing calls, which rollup DOES shake. The guard was checking
 * the one fixture family that was already fine. It now greps both, which is
 * the half of it that was missing.
 *
 * A `useMemo` in the component gives identical behaviour in the harness -- one
 * client for the life of the page -- and leaves module scope free of calls, so
 * the whole thing goes when the component goes.
 */

/* ---------------------------------------------------------------- /docs ---
 *
 * THE DOCUMENT VIEWER, AND IT IS THE REASON THE CONSOLE WAS ITS OWN CYCLE.
 *
 * `spec-attachments.md` §0.4 builds this module's XSS containment out of three
 * walls instead of a second hostname, and the client holds the third. The two
 * server-side walls are pinned by pytest. The third is a client-side constant
 * plus what a BROWSER does with it, and the spec is explicit about the cost of
 * being wrong: "if someone later adds `text/html` to the render map, walls 1
 * and 2 do not save us -- a `Blob` of type `text/html` opened from the console
 * origin executes in it". `deploy/docker-compose.yml` routes MinIO on that
 * same origin, so "the console origin" is every operator in the tenant.
 *
 * jsdom answers "is this string in `textContent` or in `innerHTML`" exactly,
 * and `DocumentViewer.test.tsx` asks it four ways. What jsdom cannot answer is
 * whether a REAL parser, given these bytes through this component, executes
 * anything -- it has no script execution to observe. That question needs
 * Chromium, and that is what this block is for.
 *
 * THE FIXTURE IS A LIVE XSS PAYLOAD ON PURPOSE. Three vectors that a browser
 * really runs: a `<script>` element, an `<img onerror>` and an `<svg onload>`,
 * each setting the SAME global. A browser pass can then assert one thing --
 * `window.__ITMS_XSS_EXECUTED` is undefined -- and have it mean "none of the
 * three fired". Rendered as characters, it is inert; rendered as markup, all
 * three fire before the page finishes painting.
 *
 * This is exactly the file an operator uploads: `filetypes.py` stores HTML,
 * SVG and XML as `text/plain` with `viewer="text"` (they decode as UTF-8, and
 * the text branch emits exactly three content types, none of which names a
 * rendering engine), so this is the row the server really produces.
 */

// A PLAIN TEMPLATE LITERAL, NOT `[...].join("\n")`, and the difference is
// visible in `dist/`. An array literal is a value rollup can shake; a `.join()`
// on it is an unknown METHOD CALL at module scope, which rollup must assume
// has a side effect and therefore retains -- along with the array it is called
// on. Written as `join`, this payload shipped in the production bundle while
// the component that renders it did not. See the note above
// `patchPreviewClient` for the same lesson one level up.
const DOCS_XSS_PAYLOAD = `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" onload="window.__ITMS_XSS_EXECUTED=true">
  <script>window.__ITMS_XSS_EXECUTED = true;</script>
  <image href="x" onerror="window.__ITMS_XSS_EXECUTED=true" />
  <text x="10" y="20">core switch — building 2</text>
</svg>`;

const DOCS_MARKDOWN = `# Finance share recovery

1. Confirm the share is unmounted on **srv-fs01**.
2. Restore from the most recent verified point.

| Step | Owner |
| --- | --- |
| Restore | Storage |
| Verify | Service desk |`;

// The digests are written out rather than built with `.repeat(64)` for the
// same reason the payload above is a template literal: a method call at module
// scope is a side effect rollup keeps, and it keeps the fixture with it.
const DOCS_FIXTURE_VERSIONS: DocDocumentVersion[] = [
  {
    version: 3,
    filename: "core-switch-diagram.svg",
    byte_size: 18_442,
    // SERVER-SNIFFED. An SVG is `text/plain`/`text` and never
    // `image/svg+xml` -- `filetypes.py` cannot emit that value for any input.
    content_type: "text/plain",
    viewer: "text",
    sha256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    created_at: "2026-08-24T08:14:00Z",
    uploaded_by: "u1",
    purged_at: null,
  },
  {
    version: 2,
    filename: "core-switch-diagram-draft.svg",
    byte_size: 17_003,
    content_type: "text/plain",
    viewer: "text",
    sha256: "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    created_at: "2026-08-19T11:02:00Z",
    uploaded_by: "u1",
    // A TOMBSTONE. The bytes are gone and the row is not: filename, size and
    // digest survive to say what was destroyed.
    purged_at: "2026-08-25T16:40:00Z",
  },
];

const DOCS_FIXTURE_DOCUMENT: DocDocument = {
  id: "doc-1",
  client_id: null,
  title: "Core switch cabling diagram",
  description: "Building 2 comms room, as-built after the August move.",
  category: "Diagrams",
  archived: false,
  current_version: 3,
  updated_at: "2026-08-24T08:14:00Z",
  current: DOCS_FIXTURE_VERSIONS[0] ?? null,
  links: [
    { id: "lnk-1", target_type: "device", target_id: "3f2b7c11-0d4e-4a52-9c31-8e1f0b6a4d77" },
    { id: "lnk-2", target_type: "ipam_subnet", target_id: "9a1c40e2-77b5-4c0f-8d3a-1b6e2f905c48" },
  ],
};

const DOCS_FIXTURE_LIST = {
  items: [
    {
      id: "doc-1", client_id: null, title: "Core switch cabling diagram", description: "", category: "Diagrams",
      archived: false, current_version: 3, updated_at: "2026-08-24T08:14:00Z", link_count: 2,
    },
    {
      id: "doc-2", client_id: null, title: "Acme support contract 2026", description: "", category: "Contracts",
      archived: false, current_version: 1, updated_at: "2026-08-02T09:30:00Z", link_count: 0,
    },
    {
      // A DOCUMENT WITH NO FILE, which is a legal state (§4.1) and the row an
      // operator misreads as broken if the list does not say so.
      id: "doc-3", client_id: null, title: "Firewall configuration export", description: "", category: "Contracts",
      archived: false, current_version: 0, updated_at: "2026-08-25T14:00:00Z", link_count: 1,
    },
  ],
  total: 3,
};

const DOCS_FIXTURE_MARKDOWN_VERSION: DocDocumentVersion = {
  version: 1,
  filename: "finance-share-recovery.md",
  byte_size: 2_310,
  content_type: "text/markdown",
  viewer: "markdown",
  sha256: "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
  created_at: "2026-08-12T07:45:00Z",
  uploaded_by: "u1",
  purged_at: null,
};

const DOCS_FIXTURE_ZIP_VERSION: DocDocumentVersion = {
  version: 1,
  filename: "wsus-export.zip",
  byte_size: 41_884_112,
  content_type: "application/zip",
  viewer: "none",
  sha256: "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
  created_at: "2026-08-05T16:20:00Z",
  uploaded_by: "u1",
  purged_at: null,
};

/**
 * A client whose cache already holds every answer, so nothing here reaches the
 * network -- there is no `/api` behind the dev server, and a view is an
 * audited download, which is not a thing a design harness should be writing.
 *
 * The bytes entries carry the shape `DocumentViewer`'s query function returns:
 * `{ url, text }`, with `url: null` on the two branches that build no Blob at
 * all. That `null` is the fixture asserting the property as much as the test
 * does -- a text document has no object URL, so there is nothing with a
 * rendering MIME for a browser to reach even if the markup survived.
 */
function docsPreviewClient(): QueryClient {
  const client = new QueryClient({
    defaultOptions: { queries: { staleTime: Infinity, gcTime: Infinity, retry: false } },
  });
  client.setQueryData(["doc-documents"], DOCS_FIXTURE_LIST);
  client.setQueryData(["doc-document", "doc-1"], DOCS_FIXTURE_DOCUMENT);
  client.setQueryData(["doc-document-versions", "doc-1"], DOCS_FIXTURE_VERSIONS);
  client.setQueryData(["doc-document-bytes", "doc-1", 3], { url: null, text: DOCS_XSS_PAYLOAD });
  client.setQueryData(["doc-document-bytes", "doc-md", 1], { url: null, text: DOCS_MARKDOWN });
  return client;
}


function DocumentsPreview() {
  const documents = useQuery<{ items: typeof DOCS_FIXTURE_LIST.items; total: number }>({
    queryKey: ["doc-documents"],
    queryFn: () => Promise.resolve(DOCS_FIXTURE_LIST),
  });
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <div>
        <DocumentsSection
          documents={documents}
          selectedId="doc-1"
          onSelect={() => undefined}
          canManage
          onUpload={() => undefined}
        />
      </div>
      <div className="lg:col-span-2">
        <DocumentView
          documentId="doc-1"
          canManage
          canPurge
          zone="Europe/London"
          onCleared={() => undefined}
        />
        <div style={{ marginTop: 14 }}>
          <Panel
            title="The other viewer branches"
            subtitle="One entry per kind, and an unknown kind degrades to download"
          >
            <div style={{ padding: 14 }}>
              <DocumentViewer documentId="doc-md" version={DOCS_FIXTURE_MARKDOWN_VERSION} />
              <div style={{ marginTop: 18 }}>
                <DocumentViewer documentId="doc-zip" version={DOCS_FIXTURE_ZIP_VERSION} />
              </div>
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}



/** Three widgets in edit mode, one hidden, one wearing the drop-target ring —
 *  every state of `DashboardWidgetFrame` the contrast sweep needs to see. */
const DASH_PREVIEW: WidgetPlacement[] = [
  { id: "managed-endpoints", size: "small", hidden: false },
  { id: "open-alerts", size: "small", hidden: false },
  { id: "open-tickets", size: "small", hidden: true },
];

const DASH_PREVIEW_TITLES: Record<string, string> = {
  "managed-endpoints": "Managed endpoints",
  "open-alerts": "Open alerts",
  "open-tickets": "Open tickets",
};

export function PreviewPage() {
  const [tab, setTab] = useState("all");
  // See the note above `patchPreviewClient`: at module scope these two calls
  // kept every fixture they reference in the production bundle.
  const patchClient = useMemo(() => patchPreviewClient(), []);
  const docsClient = useMemo(() => docsPreviewClient(), []);
  const [picked, setPicked] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [chip, setChip] = useState(0);
  const [cronWindow, setCronWindow] = useState<string | null>("0 2-5 * * 6");
  const [customWindow, setCustomWindow] = useState<string | null>("30 2 * * 6");
  // The three kit pieces added in the closing wave, each drawn in the state
  // that paints something the others do not: a pressed button, a switch that
  // is on, and a page in the middle of a list.
  const [showEvents, setShowEvents] = useState(true);
  const [autoApprove, setAutoApprove] = useState(true);
  const [page, setPage] = useState(1);

  return (
    <AppShell
      permissions={ALL_PERMISSIONS}
      orgName="Toeshee Operations"
      section="Fleet"
      title="Devices"
      badges={{ "/health": 12, "/service-desk": 23 }}
      /* THE TOP BAR'S GLYPH WAS PERMANENTLY GREY HERE. `badges` was passed and
         `health` was not, so the harness rendered the `unknown` state on every
         run and the design pass and the axe sweep had never laid eyes on
         green, amber or red. One reading here paints the real top bar; the
         panel below draws all four side by side. */
      health={HEALTH_AMBER}
      user={{ name: "Alex Morgan", role: "Administrator" }}
      onSignOut={() => undefined}
    >
      <PageHead
        title="Devices"
        description="Hardware, software, network interfaces, ownership, agent state, and job history from Windows, Linux, and macOS endpoints."
        actions={<Btn label="Enroll device" icon="plus" variant="primary" />}
      />

      {/* THE v3 KIT, EVERY PIECE, on the one page the browser pass reads. A
          component enters that pass only by being here, so each new piece is
          drawn in the state that paints a hue the others do not: every pill
          kind, the crit and warn tiles beside a plain one, a measured donut
          beside an unmeasured one, the painter beside the expression it
          cannot draw. */}
      <div className="tiles">
        <Tile label="Jobs needing attention" icon="alert-triangle" value="28" sub="21 failed, 7 stalled over 1h" kind="crit" spark={[9, 8, 10, 14, 12, 15, 18, 17, 21, 24, 22, 26, 28, 28]} to="/jobs?state=failed" />
        <Tile label="Open alerts" icon="heartbeat" value="12" sub="1 critical, 11 warning" kind="warn" spark={[18, 16, 15, 17, 14, 13, 15, 12, 11, 13, 12, 10, 11, 12]} to="/health" />
        <Tile label="Healthy" icon="circle-check" value="261" sub="Reporting and current" kind="ok" to="/devices?state=healthy" />
        <Tile label="Agent coverage" icon="cpu" value="95" unit="%" sub="272 of 286" to="/devices" />
      </div>

      <div className="g2" style={{ gridTemplateColumns: "1.4fr 1fr" }}>
        <Panel title="Fleet at a glance" subtitle="7 need a person, 18 degraded, 4 working, 257 healthy">
          <Heat
            label="devices"
            cells={[
              ...Array<Kind>(7).fill("crit"),
              ...Array<Kind>(18).fill("warn"),
              ...Array<Kind>(4).fill("run"),
              ...Array<Kind>(257).fill("ok"),
            ]}
          />
        </Panel>
        <Panel title="Patch compliance" subtitle="By operating system">
          <div className="dons">
            <Donut pct={94} kind="ok" label="Windows" sub="168 of 179" />
            <Donut pct={71} kind="warn" label="Linux" sub="62 of 87" />
            <Donut pct={null} kind="mut" label="macOS" sub="never scanned" />
          </div>
        </Panel>
      </div>

      <Panel title="Every pill kind" subtitle="Each carries its glyph, so the wash is never the only carrier">
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
          <StatusPill value="failed" />
          <StatusPill value="warning" />
          <StatusPill value="healthy" />
          <StatusPill value="running" />
          <StatusPill value="not_applicable" />
          <StatusPill value="info" />
          <StatusPill value="cert_revoked" label="Certificate revoked" />
          <StatusPill value="open" kind="warn" label="Breach in 40m" />
          <Badge variant="destructive">Refused</Badge>
          <Badge variant="success">Verified</Badge>
        </div>
        <div style={{ marginTop: 14, display: "flex", flexWrap: "wrap", gap: 14, alignItems: "center" }}>
          <Person initials="AO" index={1} name="Adaeze Okonkwo" />
          <Person initials="MR" index={2} name="Mateus Ribeiro" />
          <Avatar initials="HP" index={5} size={30} />
          <OsIcon os="windows" />
          <OsIcon os="macos" />
          <OsIcon os="ubuntu" />
          <OsIcon os="debian" />
          <OsIcon os="plan9" />
          <Sparkline values={[4, 5, 4, 6, 9, 8, 6, 5, 7, 11, 9, 8, 7, 7]} kind="run" className="free" />
        </div>
      </Panel>

      <Facts
        items={[
          { k: "Agent", v: <><span className="mono">1.8.2</span> current</> },
          { k: "Certificate", v: <>Valid to <span className="mono">14 Nov 2026</span></> },
          { k: "Site server", v: "rack-b4-relay" },
          { k: "Outstanding", v: <><span className="mono">41</span>, 3 critical</> },
          { k: "State", v: <StatusPill value="offline" label="Offline 3d" /> },
        ]}
      />

      <div className="g2" style={{ gridTemplateColumns: "1.5fr 1fr" }}>
        <Panel title="Last six hours" subtitle="Signed jobs, by device">
          <Timeline
            runs={[
              { kind: "crit", at: "patch.apply", label: "sentinel-db-02", start: 6, width: 17 },
              { kind: "warn", at: "inventory.collect", label: "build-runner-07", start: 22, width: 55 },
              { kind: "run", at: "patch.apply", label: "mbp-okonkwo", start: 64, width: 11 },
              { kind: "ok", at: "backup.run", label: "fs-archive-01", start: 44, width: 9 },
              { kind: "mut", at: "patch.scan", label: "ws-reception" },
            ]}
            ticks={["04:00", "05:00", "06:00", "07:00", "08:00", "09:00", "10:00"]}
          />
        </Panel>
        <Panel title="Details">
          <KV
            items={[
              ["Requester", <Person key="r" initials="AO" index={1} name="Adaeze Okonkwo" />],
              ["Device", <a key="d" href="/devices/dev-1">ws-holloway</a>],
              ["Priority", "P2"],
              ["SLA", "Respond 1h, resolve 4h"],
            ]}
          />
        </Panel>
      </div>

      <Panel title="Getting started" subtitle="Five steps: one done, one running, one held, one blocked, one still to do">
        {/* `kind` ON A STEP is what draws v3's `.step.crit` bar and the
            `.step.warn` twin beside it. The state pill and the words carry it
            too, so the bar is never the only carrier. */}
        <Steps
          items={[
            { label: "Register the network", state: "done", detail: "3 subnets recorded." },
            { label: "Authorize a sweep", state: "now", detail: "A person names the segment; nothing is scanned by itself." },
            {
              label: "Reconcile the drift",
              state: "now",
              kind: "warn",
              detail: "Held: 4 addresses answer that the registry does not list.",
            },
            {
              label: "Issue the site server certificate",
              state: "todo",
              kind: "crit",
              detail: "Blocked: the intermediate that signs it expired on 2 September.",
            },
            { label: "Promote a site server", state: "todo", detail: "Push installs originate from it." },
          ]}
        />
      </Panel>

      <div className="g2" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <Panel title="A control that is on or off" subtitle="A pressed button and a switch, both announced">
          <div className="flex flex-wrap items-center gap-3">
            <Btn label="Events" small pressed={showEvents} onClick={() => setShowEvents(!showEvents)} />
            <Btn label="Resolved" small pressed={false} onClick={() => undefined} />
          </div>
          <div className="mt-3">
            <Toggle
              label="Auto-approve security patches"
              checked={autoApprove}
              onChange={setAutoApprove}
            />
          </div>
        </Panel>
        <Panel title="The selected row" subtitle="The accent wash, which is not what hover paints">
          <Table>
            <thead>
              <tr>
                <Th>Group</Th>
                <Th>Last seen</Th>
              </tr>
            </thead>
            <tbody>
              <tr aria-current="true">
                <Td lead={<Icon name="bug" size={18} />}>
                  <div className="cell-title">IntegrityError</div>
                </Td>
                <Td><span className="mono">4 min ago</span></Td>
              </tr>
              <tr>
                <Td lead={<Icon name="bug" size={18} />}>
                  <div className="cell-title">TimeoutError</div>
                </Td>
                <Td><span className="mono">2 h ago</span></Td>
              </tr>
              <tr className="wash-crit">
                <Td gutter="crit" lead={<Icon name="robot" size={18} />}>
                  <div className="cell-title">Runs itself on 400 machines</div>
                </Td>
                <Td><span className="mono">yesterday</span></Td>
              </tr>
            </tbody>
          </Table>
        </Panel>
      </div>

      <Panel bare>
        <Toolbar
          left={
            <>
              <SearchField placeholder="Hostname, IP, serial, owner" aria-label="Search devices" />
              <Chips items={["Needs a person", "Windows", "Linux", "macOS"]} active={chip} onSelect={setChip} />
            </>
          }
          right={<span className="mono">7 of 286</span>}
        />
        <Table>
          <thead>
            <tr>
              <Th>Name</Th>
              <Th>State</Th>
              <Th>Owner</Th>
              <Th>Outstanding</Th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <Td gutter="crit" lead={<OsIcon os="ubuntu" />}>
                <CellMain glyph="server" title="sentinel-db-02" sub={<span className="mono">10.20.4.31</span>} />
              </Td>
              <Td><StatusPill value="offline" label="Offline 3d" /></Td>
              <Td><Person initials="IC" index={0} name="Ines Caetano" /></Td>
              <Td><span className="mono">41</span></Td>
            </tr>
            <tr>
              <Td gutter="warn" lead={<OsIcon os="debian" />}>
                <CellMain glyph="server" title="build-runner-07" sub={<span className="mono">10.20.4.77</span>} />
              </Td>
              <Td><StatusPill value="degraded" label="Agent behind" /></Td>
              <Td><Person initials="MR" index={2} name="Mateus Ribeiro" /></Td>
              <Td><span className="mono">3</span></Td>
            </tr>
            <tr>
              <Td gutter="ok" lead={<OsIcon os="windows" />}>
                <CellMain glyph="devices" title="fs-archive-01" sub={<span className="mono">10.20.4.12</span>} />
              </Td>
              <Td><StatusPill value="healthy" /></Td>
              <Td><Person initials="TK" index={3} name="Tomas Kalu" /></Td>
              <Td><span className="mono">0</span></Td>
            </tr>
          </tbody>
        </Table>
        {/* THE FOOT OF A PAGED LIST, which is not the head drawn upside down:
            it rules along its TOP, the edge it shares with the table. */}
        <Pager
          page={page}
          pageCount={6}
          scopeName="devices"
          onPrevious={() => setPage(Math.max(0, page - 1))}
          onNext={() => setPage(Math.min(5, page + 1))}
        />
      </Panel>

      <div className="g2" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <Panel title="Maintenance window" subtitle="Drag to paint; the sentence says what is selected">
          <WeekPainter value={cronWindow} onChange={setCronWindow} label="Maintenance window" />
        </Panel>
        <Panel title="A window the grid cannot draw" subtitle="A minute other than 0 is shown as a custom expression">
          <WeekPainter value={customWindow} onChange={setCustomWindow} label="Backup window" />
        </Panel>
      </div>

      {/* THE TWO WEIGHTS, side by side, which is the only way to judge whether
          the hierarchy actually reads. "At risk" is the one that needs a
          person: the crit wash and a trend. The other three are the same shape
          on a plain card. Six equally-loud tiles were the wall of numbers this
          replaces. */}
      <StatsGrid columns={4}>
        <StatCard
          label="At risk"
          value="18"
          icon="warning"
          to="/devices?state=at-risk"
          tone="red"
          urgent
          trend={{ direction: "up", label: "+4 since yesterday" }}
        />
        <StatCard label="Managed endpoints" value="286" icon="devices" to="/devices" tone="primary" />
        <StatCard label="Online now" value="272" icon="activity" to="/devices?state=online" tone="green" />
        <StatCard
          label="Agent current"
          value="94%"
          icon="certificate"
          to="/agent-releases"
          tone="blue"
        />
      </StatsGrid>

      {/* THE DASHBOARD IN EDIT MODE, because the a11y sweep only ever sees
          `/__preview` and the customize toolbar is a new visual surface: 12px
          muted labels, ghost buttons at a small size, and a DASHED OUTLINE
          around each widget, which is a non-text UI boundary and therefore
          SC 1.4.11's 3:1 (the rule that caught `--line` on the control
          borders in cycle D2). A surface the harness cannot reach is a
          surface nobody measured. Three widgets, one of them hidden, so the
          dimmed empty-frame state is measured too. */}
      <div style={{ marginTop: 14 }}>
        <Panel
          title="Dashboard arrangement"
          subtitle="Edit mode — the move, resize and hide controls the operator sees on the dashboard"
        >
          <div className="dash-grid">
            {DASH_PREVIEW.map((placement, index) => (
              <DashboardWidgetFrame
                key={placement.id}
                placement={placement}
                title={DASH_PREVIEW_TITLES[placement.id] ?? placement.id}
                editing
                first={index === 0}
                last={index === DASH_PREVIEW.length - 1}
                dragging={false}
                dropTarget={index === 1}
                onMove={() => undefined}
                onResize={() => undefined}
                onToggleHidden={() => undefined}
                onDragStart={() => undefined}
                onDragOver={() => undefined}
                onDrop={() => undefined}
                onDragEnd={() => undefined}
              >
                <StatCard
                  label={DASH_PREVIEW_TITLES[placement.id] ?? placement.id}
                  value="12"
                  icon="devices"
                  to="/devices"
                  tone="blue"
                />
              </DashboardWidgetFrame>
            ))}
          </div>
        </Panel>
      </div>

      {/* A MEASURED ZERO, which is a different thing from an unmeasured one and
          must not wear the alarm treatment. The audit found nine tiles reading
          0 on a page where nothing had been measured at all. */}
      <div style={{ marginTop: 14 }}>
        <StatsGrid columns={4}>
          <StatCard
            label="Breached SLAs"
            value="0"
            icon="clock"
            to="/sla"
            tone="red"
            urgent
            zeroLabel="Nothing breached in the last 24 hours"
          />
        </StatsGrid>
      </div>

      <Tabs
        items={[
          { key: "all", label: "All devices", count: 286 },
          { key: "online", label: "Online", count: 272 },
          { key: "risk", label: "At risk", count: 18 },
        ]}
        active={tab}
        onSelect={setTab}
      />

      <Panel bare>
        <FilterBar>
          <SearchField placeholder="Search device, user, IP, site or OS" aria-label="Search devices" />
          <span className="filter-spacer" />
          <Btn label="Bulk actions" small />
        </FilterBar>
        <TableWrap>
          <table>
            <thead>
              <tr>
                <th>Device</th>
                <th>Operating system</th>
                <th>Architecture</th>
                <th>Agent</th>
                <th>Certificate</th>
                <th>Patch</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {FIXTURE_DEVICES.map((device) => (
                <tr key={device.host}>
                  <td>
                    <CellMain
                      glyph={device.host.startsWith("srv") ? "server" : "devices"}
                      title={device.host}
                      sub={device.fqdn}
                    />
                  </td>
                  <td>{device.os}</td>
                  <td>{device.arch}</td>
                  <td>
                    <StatusPill value={device.agent} />
                  </td>
                  <td>
                    <StatusPill value={device.cert} />
                  </td>
                  <td style={{ minWidth: 90 }}>
                    <Progress value={device.patch} tone={device.patch < 60 ? "red" : device.patch < 80 ? "gold" : ""} label={`Patch level, ${device.host}`} />
                  </td>
                  <td>
                    <Btn label="Connect" icon="remote" small />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      </Panel>

      {/* THE TRI-STATE THE HARNESS COULD NOT SEE. `scripts/a11y-browser.mjs`
          drives `/__preview` and nothing else, and this page rendered the glyph
          in exactly one state — `unknown` — so the three status hues had never
          been measured against the button's white by a browser at any viewport.
          `contrast.test.ts` now grades the tokens too; this is the half that
          puts real pixels in front of the sweep, and in front of a person. */}
      <div style={{ marginTop: 18 }}>
        <Panel
          title="The top bar's health glyph"
          subtitle="All four states — the harness rendered only the grey one"
        >
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: 18,
              padding: 14,
              alignItems: "flex-start",
            }}
          >
            {HEALTH_STATES.map(([reading, caption]) => (
              <div key={caption} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <HealthGlyph reading={reading} />
                <span className="cell-sub" style={{ maxWidth: 260 }}>
                  {caption}
                </span>
              </div>
            ))}
          </div>
        </Panel>
      </div>

      {/* THE CALLOUT FAMILY, WHICH NO BROWSER PASS HAD EVER SEEN EITHER.
          `.notice` is the component every module reaches for -- jobs,
          credentials, enrolment, the error monitor, the ticket copilot -- and
          it was not on this page, so its three tones had never been graded
          against their own washes at a real viewport, and its type size had
          never been read beside the rungs above and below it. It sits next to
          a panel title (15px) and a cell sub-line (12px) on purpose: the
          question "is a notice body prose or a heading?" is answered by
          looking at it in that company, not by reading the number.

          ONE CHILD FOR THE BODY, in all three. The rule is a flex row shaped
          icon-then-body, so a body that is more than one element splits into
          two columns with a 10px gap between them -- invisible until it
          happens, and it has caught three conversions. */}
      <div style={{ marginTop: 18 }}>
        <Panel title="Callouts" subtitle="One component, three tones — default informs, gold warns, red alerts">
          <div style={{ padding: 14, display: "grid", gap: 10 }}>
            <div className="notice">
              <Icon name="info" size={17} />
              <div>
                <strong>Point-of-use boundary</strong>
                <br />
                An authorized checkout states a reason, which is written to the audit log. The
                secret is decrypted only for that checkout and shown in this console once.
              </div>
            </div>
            <div role="status" className="notice gold">
              <Icon name="warning" size={17} />
              <div>
                This device is offline. The job will be queued and delivered the next time its
                agent connects, which may be after the maintenance window closes.
              </div>
            </div>
            <div role="alert" className="notice red">
              <Icon name="warning" size={17} />
              <div>
                The signing key could not be read, so nothing was dispatched. No unsigned job
                path exists — this is a refusal, not a partial send.
              </div>
            </div>
            <span className="cell-sub">
              A cell sub-line, at the Label rung (12px), for scale.
            </span>
          </div>
        </Panel>
      </div>

      <div style={{ marginTop: 18 }}>
        <Panel title="Empty and error states" subtitle="What the prototype has no concept of">
          <QueryState loading={false} error={null} empty emptyLabel="No devices match that filter.">
            <p>unreachable</p>
          </QueryState>
        </Panel>
      </div>

      {/* THE REMEDIATION PRIMITIVES, on the same page as the design they have
          to live inside. Every one of these was written against an audit
          finding and verified by a unit test — but a unit test asserts that
          the right text is in the DOM, not that a hatch pattern is
          distinguishable, that a 12px uppercase header is legible, or that a
          chart's legend does not collide with its axis at 640px. That is the
          argument this harness was built on, and these are the components it
          did not yet cover. */}
      <div style={{ marginTop: 18 }} className="grid grid-2">
        <Panel title="Charts" subtitle="Cycle 11 — the two analytics modules had none">
          <div style={{ padding: 14 }}>
            <BarChart
              title="SLA attainment, 1 Jul – 19 Aug 2026"
              categories={["Service desk", "Field ops", "Security"]}
              series={[
                { label: "Response met", values: [42, 18, 7] },
                { label: "Response breached", values: [3, 9, 1] },
              ]}
              unit="tickets"
              target={{ value: 40, label: "Target" }}
            />
            <div style={{ marginTop: 20 }}>
              <LineChart
                title="Cumulative IT spend, FY2026"
                categories={["Q1", "Q2", "Q3", "Q4"]}
                series={[
                  { label: "Plan", values: [120, 240, 360, 480] },
                  { label: "Actual", values: [131, 249, 402, 470] },
                  { label: "Committed", values: [140, 260, 430, 505] },
                ]}
                unit="USD thousands"
              />
            </div>
          </div>
        </Panel>

        <div>
          <Panel title="Structured attributes" subtitle="Cycle 11 — 35 of 105 cells were raw JSON">
            <div style={{ padding: 14 }}>
              <Attributes
                value={{
                  mac: "3c:a8:2a:15:6b:11",
                  name: "eno2",
                  addresses: [
                    { family: "ipv4", origin: "static", address: "172.22.104.28/29" },
                    { family: "ipv6", origin: "link_local", address: "fe80::3ea8:2aff:fe15:6b11%eno2/64" },
                  ],
                }}
                empty="No attributes recorded"
              />
              <div style={{ marginTop: 12 }}>
                <Attributes value={{ model: "LOGICAL VOLUME", size_bytes: 256022937600 }} empty="No attributes recorded" />
              </div>
              <div style={{ marginTop: 12 }}>
                Serial: <NoValue label="No serial number recorded" />
              </div>
            </div>
          </Panel>

          <div style={{ marginTop: 14 }}>
            <Panel title="One empty state" subtitle="Cycle 11 — six treatments on six tabs of one page">
              <EmptyState
                icon="backup"
                title="No deployments recorded"
                body="This device has never been targeted by a deployment. Create one to push a package to it."
                action={<Btn label="Create deployment" icon="plus" small />}
              />
            </Panel>
          </div>

          <div style={{ marginTop: 14 }}>
            <Panel title="Picking, and confirming" subtitle="Cycles 6 and 8">
              <div style={{ padding: 14 }}>
                <EntityPicker
                  label="Target device"
                  options={FIXTURE_DEVICES.map((device) => ({ id: device.fqdn, label: device.host, hint: device.fqdn }))}
                  value={picked}
                  onChange={setPicked}
                  emptyHint="No devices are enrolled yet."
                />
                <div style={{ marginTop: 12 }}>
                  <Btn label="Delete device" icon="warning" small onClick={() => setConfirming(true)} />
                </div>
              </div>
            </Panel>
          </div>
        </div>
      </div>

      {/* THE FLEET PATCH PAGE, RENDERED WHOLE. Not a panel of its parts: the
          head's three controls, the severity chips, the approval table and the
          two compliance panels are what the operator objected to, and each of
          them only enters this pass by being on this page. `FleetPatchPage`
          draws its own `PageHead`, so this is a second `h1` in the harness --
          deliberate, and the same shape the real route has. */}
      <div style={{ marginTop: 24 }}>
        <QueryClientProvider client={patchClient}>
          <FleetPatchPage />
        </QueryClientProvider>
      </div>

      {/* THE TWO PATCH SURFACES THE DEFAULT RENDER OF THAT PAGE CANNOT REACH.

          A harness that renders a page renders the page's RESTING state, and
          both of the controls the 2026-08-27 rework is about live past a
          click: the device drill-down opens on a button, and the decision
          bar's committed state needs a row ticked. Neither had ever been
          composited, so neither the checkbox column inside an expanded row nor
          the magenta commit beside a ring select had a measured contrast
          ratio, a measured reflow at 320px, or an axe pass.

          The panel below is the same `DevicesPanel` the page draws, mounted
          with one row already open; the bar beneath it is the same
          `DecisionBar`, in the state a selection puts it in. */}
      <div style={{ marginTop: 24 }}>
        <QueryClientProvider client={patchClient}>
          <PatchWorkingSurfaces />
        </QueryClientProvider>
      </div>

      {/* THE DOCUMENTS SCREENS. Here for the same reason `/patch` is: a
          component is covered by the browser pass only if this file renders
          it, and the document viewer is the one surface in this product where
          a rendering mistake is stored XSS across a whole tenant rather than a
          layout defect. The fixture's current version is a live three-vector
          XSS payload; a browser pass asserts `window.__ITMS_XSS_EXECUTED` is
          still undefined after the page settles. */}
      <div style={{ marginTop: 24 }}>
        <QueryClientProvider client={docsClient}>
          <DocumentsPreview />
        </QueryClientProvider>
      </div>

      <ConfirmDialog
          open={confirming}
          title="Delete this device?"
          subject="ws-alpha.corp.local"
          body="Its inventory, job history and patch state are removed. The agent will re-enrol if it is still running."
          confirmLabel="Delete device"
          destructive
          requireTyping
          onConfirm={() => setConfirming(false)}
          onCancel={() => setConfirming(false)}
        />
    </AppShell>
  );
}
