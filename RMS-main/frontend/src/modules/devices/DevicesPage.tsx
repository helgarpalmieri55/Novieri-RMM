/**
 * Devices — the first page migrated onto the design (UI port spec §6). It is
 * the template the other twenty-nine copy, so the decisions here are worth
 * reading before repeating them.
 *
 * THE SERVER FILTERS, PAGES AND COUNTS; THIS PAGE ASKS AND RENDERS. Until
 * 2026-08-26 it fetched one capped page and computed the search, the
 * provenance filter and all five tab counts in the browser over those rows.
 * A `net.sweep` now records every host it finds, so a /24 adds ~200 rows in one
 * act — past the page limit the counts were wrong, the search missed and the
 * filter lied, each of them rendering as a correct-looking empty answer.
 * `GET /devices` carries `items`, `total`, `state_counts` (the tab strip) and
 * `fleet` (the stat tiles); NOTHING on this page recomputes any of them.
 *
 * WHAT THE DESIGN ASKS FOR THAT THE API DOES NOT SERVE. The prototype's
 * Devices table has eight columns; four of them — Site, Patch %, Backup, Risk
 * — have no field behind them. The same is true of two of its four stat
 * cards: "At risk" has no definition anywhere in the backend, and every
 * card's "↑ 12 vs last 7 days" delta needs a previous value no endpoint
 * serves.
 *
 * Those columns are OMITTED, not faked. Hardcoding the mock's numbers would
 * produce a screen that looks finished and lies — and it would lie most
 * convincingly on the demo where someone decides to trust it. Each gap is
 * listed in the UI port spec §7 so it becomes a backlog item rather than a
 * silent difference from the design.
 *
 * Patch % and Backup are the two worth wiring properly: both exist per-device
 * in their own modules, and joining them here means either an endpoint that
 * serves the join or N per-row requests. That is a backend decision, not
 * something to settle by picking the cheaper frontend hack.
 */

import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { humaniseEnum } from "../../lib/labels";

import type { Device, DeviceListParams, DeviceProvenance, DeviceState, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { RelTime } from "../../components/reltime";
import { saveBlob } from "../../lib/download";
import { useLiveEvents } from "../../lib/ws";
import { Btn, FilterBar, PageHead, Panel, QueryState, ResultCount, SearchField, StatCard, StatsGrid, StatusPill, TableFoot, TableWrap, Tabs, type Kind } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { ADD_COMPUTER_TO } from "../../shell/nav";
import { OsIcon } from "../../shell/viz";
import { NoValue } from "../../components/emptystate";
import { describeSubmitError } from "../../components/form";
import { Td } from "../../components/ui";
import { DispatchJobDialog } from "../jobs/DispatchJobDialog";
import { PushInstallChooser } from "../agent-push/PushInstallChooser";
import { AGENT_PUSH_RUN, PUSH_BATCH_MAX } from "../agent-push/types";
import { AddAssetDialog } from "./AddAssetDialog";
import { ApproveAllDialog, approveAllMessage, type ApproveAllOutcome } from "./ApproveAllDialog";
import { osLabel } from "./osLabel";
import { ManualRemoval, ReviewActions, REVIEW_PERMISSION } from "./ReviewActions";
import { isWaitingForReview, joinedThroughLink, manualRemovalStatus, recordedByLabel } from "./reviewLabels";

function deviceOs(device: Device): string | null {
  // An asset may have no OS at all -- a switch is none of windows/linux/macos
  // (asset-inventory spec §1). `null` renders as `NoValue` ("not known") where
  // "undefined 11 23H2" would say something false.
  //
  // THE FAMILY IS A LABEL, NOT THE ENUM. This joined `os_family` to
  // `os_version` verbatim, so the column read "macos 15.6" and
  // "windows Server 2022" on every row that had an OS. `osLabel` owns the
  // three spellings (and `macOS`, which no case rule produces).
  return osLabel(device.os_family, device.os_version);
}

/**
 * One name for a row, in the order an operator would recognise it.
 *
 * `??` would be wrong: an EMPTY hostname is as unhelpful as a missing one, and
 * `||` falls through both. A row always has an id, so this never returns
 * nothing -- but it never invents a hostname either, which is what rendering
 * `String(device.hostname)` would do the moment the value is null.
 */
function assetLabel(device: Device): string {
  return device.hostname || device.primary_ip || device.description || translate("devices.row.unnamed");
}

/**
 * WHO, IF ANYONE, STOOD BEHIND THIS ROW -- in the operator's words.
 *
 * THIS COLUMN EXISTS BECAUSE THE FLEET LIST STOPPED BEING A LIST OF MACHINES
 * SOMEBODY CHOSE. Since 2026-08-26 a subnet sweep records every host that
 * answers it, so the table now mixes rows a person typed in with rows the
 * platform wrote after an echo reply came back — a printer, a phone, a
 * colleague's laptop on the wrong VLAN, the coffee machine. Those are not the
 * same kind of claim, and a table that renders them identically is the exact
 * failure the old design predicted.
 *
 * "Found by a sweep" is the label that has to do the work: it says what the
 * evidence was, so nobody reads the row as something a colleague vetted.
 */
const PROVENANCE_KNOWN: ReadonlySet<string> = new Set<DeviceProvenance>(["agent", "operator", "promoted", "sweep"]);

/** The filter's options, in the order an operator would rank the evidence. */
const PROVENANCE_OPTIONS: DeviceProvenance[] = [
  "agent",
  "operator",
  "promoted",
  "sweep",
];

function provenanceLabel(device: Device): string {
  // An unknown value renders as itself rather than as a blank cell: a row this
  // page cannot classify is a fact worth seeing, not one worth hiding.
  if (PROVENANCE_KNOWN.has(device.provenance)) return humaniseEnum(device.provenance, "DeviceProvenance");
  return device.provenance ?? "—";
}

/**
 * The search term, short enough to quote back inside a sentence.
 *
 * The audit pasted 500 characters into this box; echoing that verbatim into
 * the empty state would push the message — and the way out of it — off the
 * card. An ellipsis is not censorship here: the full value is still in the
 * input the user is looking at.
 */
function shortTerm(term: string): string {
  const trimmed = term.trim();
  return trimmed.length > 60 ? `${trimmed.slice(0, 60)}…` : trimmed;
}

/**
 * THE ROW'S SEVERITY, as v3's 3px gutter draws it. Colour is never the only
 * carrier: the same fact is in the Agent and Certificate pills beside it.
 * Revocation and expiry are critical, an agent that has stopped answering is
 * critical, a certificate the platform has been failing to renew warns, an
 * in-flight renewal is the running blue, and an asset with no agent draws
 * nothing -- offline is a claim about something that should be answering.
 */
function rowKind(device: Device): Kind | undefined {
  if (device.cert_revoked || device.cert_state === "expired") return "crit";
  if (device.agent_status === "offline") return "crit";
  if (device.cert_state === "expiring") return "warn";
  if (device.cert_state === "renewing") return "run";
  return undefined;
}

/** `label` is a catalogue key, translated where the strip is drawn. */
const TABS = [
  { key: "all", label: "devices.tabs.all" },
  { key: "online", label: "devices.tabs.online" },
  { key: "offline", label: "devices.tabs.offline" },
  // Rows with no agent: warehouse stock, network gear, anything recorded by
  // hand. They are deliberately NOT in "Offline" -- offline is a claim about
  // something that should be answering, and a switch is not offline
  // (asset-inventory spec §5).
  { key: "assets", label: "devices.tabs.assets" },
  { key: "certs", label: "devices.tabs.certs" },
  // Machines that joined through the install link and wait for a person
  // (install-link spec §1.5). They are ALSO in Online/Offline: the tabs were
  // never a partition. This tab alone also lists a rejected machine whose
  // agent may still be installed, until someone says they removed it.
  { key: "review", label: "devices.tabs.review" },
] as const;

/**
 * How many rows one request asks for.
 *
 * THE PAGE IS A PAGE NOW. This screen used to fetch one uncapped-looking page
 * and do everything else in the browser; a /24 sweep adds ~200 rows in one act,
 * so "everything else" was computed over whichever hundred came back. Paging,
 * filtering and every count on this screen are now SQL
 * (`backend/app/services/device_list.py`), and this is the page size that goes
 * with the request.
 */
export const DEVICES_PAGE_SIZE = 50;

/**
 * WHERE `needsCertAttention` WENT, AND WHY IT IS NOT HERE ANY MORE.
 *
 * "Certificates the platform is worried about" -- revoked, expiring or expired,
 * and deliberately not `renewing`, which is the healthy automatic path -- used
 * to be a predicate in this file that the tile, the tab count and the row
 * filter all called. That was the right shape while the page held the whole
 * fleet: one function, three readers, incapable of computing different sets.
 *
 * It cannot stay that shape once the fleet does not fit in the browser. The
 * predicate is now `_cert_attention` in `app/services/device_list.py`, which
 * feeds the same three numbers from one SQL scan -- `fleet.cert_attention`,
 * `state_counts.certs` and the `?state=certs` row filter. The guarantee is
 * unchanged and the mechanism moved; what this page must not do is compute a
 * FOURTH answer locally, which is how a tile comes to disagree with the table
 * under it.
 */

export function DevicesPage() {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();
  const [target, setTarget] = useState<Device | null>(null);
  const [adding, setAdding] = useState(false);
  /**
   * THE EXPORT IS A REQUEST, NOT A LINK, and that is the whole fix.
   *
   * This was `<a className="btn" href="/api/v1/export/devices.csv" download>`.
   * The browser follows a link itself and sends no `Authorization` header, so
   * the route answered 401 and the browser saved the refusal — which is
   * exactly what the operator described: "export inventory exports a .json
   * instead of an .xlsx". The `.json` was the error body. The export had
   * therefore never worked on a deployed console and could not have.
   *
   * `lib/download.ts` explains the fetch-then-save shape; `.xlsx` is the
   * operator's other ask and the backend now serves it beside the CSV.
   */
  const exporter = useMutation({
    mutationFn: () => api.exportInventory("devices"),
    onSuccess: (blob) => saveBlob(blob, "devices.xlsx"),
  });
  /**
   * THE FILTER STATE LIVES IN THE URL.
   *
   * Auditor A: "[MEDIUM] Filter tabs do not change the URL, so filtered views
   * cannot be linked, bookmarked or restored … Reloading returns to 'All
   * devices'; the browser Back button does not undo the tab change." A fleet
   * console is a thing people paste at each other — "the four machines with
   * certificate trouble" has to survive being sent to a colleague.
   *
   * The search term REPLACES rather than pushes: one history entry per
   * keystroke would make Back useless for exactly the person trying to undo a
   * filter.
   */
  const [params, setParams] = useSearchParams();
  const tab = params.get("state") ?? "all";
  const search = params.get("q") ?? "";
  // IN THE URL like every other filter on this page, for the same reason:
  // "the eleven machines nobody vouched for" is a thing one technician sends
  // another, and a filter that lives in component state cannot be sent.
  const provenance = params.get("provenance") ?? "";
  // IN THE URL for the same reason as everything else here, plus one of its
  // own: an operator who found a machine on page four and followed the row to
  // its detail page has to come BACK to page four.
  const offset = Number(params.get("offset") ?? 0) || 0;

  function update(patch: Record<string, string>, replace = false) {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(patch)) {
      // An empty value is ABSENT, not `?q=`: a URL carrying an empty filter
      // reads as a filter that is set to nothing.
      if (value === "") next.delete(key);
      else next.set(key, value);
    }
    setParams(next, { replace });
  }

  // EVERY FILTER RESETS THE OFFSET. Narrowing a 4 000-row estate to eleven
  // rows while still asking for rows 150-200 of it returns an empty table, and
  // an empty table is the one answer this page must never give by accident.
  const setTab = (key: string) => update({ state: key === "all" ? "" : key, offset: "" });
  const setProvenance = (value: string) => update({ provenance: value, offset: "" });
  const setSearch = (value: string) => update({ q: value, offset: "" }, true);
  const setOffset = (next: number) => update({ offset: next === 0 ? "" : String(next) });

  /**
   * **THE SERVER FILTERS, PAGES AND COUNTS. THIS PAGE ASKS.**
   *
   * It used to call `listDevices()` with no arguments, take the one page that
   * came back, and compute the search, the provenance filter and all five tab
   * counts over it. Since the operator's ruling of 2026-08-26 a `net.sweep`
   * records every host it finds -- a /24 in one act, a /16 far more -- so past
   * the page limit the counts were wrong, the search missed rows it should have
   * found, and the provenance filter reported nothing on an estate full of
   * swept rows. All three render as a correct-looking empty answer, which is
   * why none of them was ever reported as a bug.
   *
   * The response carries three things this page must NOT recompute: `items`
   * (this page of the filtered set), `state_counts` (what each tab would show
   * from this search) and `fleet` (the estate, unfiltered -- the stat tiles).
   */
  const query: DeviceListParams = {
    q: search.trim() || undefined,
    state: tab as DeviceState,
    // `as` because a URL is a string and this is a closed vocabulary: an
    // unrecognised value is refused by the route's own pattern, which is the
    // right place for it -- the console never gates anything.
    provenance: (provenance || undefined) as Device["provenance"] | undefined,
    limit: DEVICES_PAGE_SIZE,
    offset,
  };
  const devices = useQuery({
    queryKey: ["devices", query],
    queryFn: () => api.listDevices(query),
    // The table keeps the rows it has while the next page loads. Without this
    // every keystroke in the search box blanks the table to "Loading…", which
    // on a filter people type into is a flicker, not feedback. The FIRST load
    // still reports loading, because there is nothing to keep.
    placeholderData: keepPreviousData,
  });
  // THE ZONE COMES FROM THE PROFILE, not from whichever machine the technician
  // happens to be sitting at (cycle 5). Same `["me"]` key and staleTime the
  // shell already uses, so this is served from cache rather than a second
  // request -- and `timezone` is nullable, which `formatDateTime` reads as
  // "fall back to the browser".
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const zone = me.data?.timezone;
  // Whether the "Assigned to" column and its search are offered. Cosmetic --
  // the server decides whether `assigned_to` is sent and searched at all.
  const canReadPeople = (me.data?.permissions ?? []).includes("people.read");
  // Cosmetic: both review routes check `devices.review` themselves.
  const canReview = (me.data?.permissions ?? []).includes(REVIEW_PERMISSION);
  // The sentence a Reject returned. Held HERE, because the rejected row -- and
  // its actions -- leave the table the moment the list refreshes.
  //
  // HELD AS DATA where this page words it: an Approve all result is kept as its
  // counts and drawn through `approveAllMessage`, so a language switch rewords
  // it. A Reject's sentence comes from the server, already in the request's
  // language, and is held as the text it is.
  const [reviewNotice, setReviewNotice] = useState<
    { kind: "text"; text: string } | ({ kind: "approveAll" } & ApproveAllOutcome) | null
  >(null);
  // Approve all (GPO MSI spec 2026-09-30 §2.5): open while its dialog is.
  const [approvingAll, setApprovingAll] = useState(false);
  // One instant per render, so two rows cannot disagree about "now".
  const now = Date.now();

  // PUSH INSTALL: MACHINES A PERSON TICKED, ONE BY ONE (spec-push-install
  // §10.4; invariant 2a(f)). There is no select-all, no "all matching", no
  // group and no filter here, and never more than the server's batch ceiling:
  // a push names explicit addresses. The selection is held by device across
  // pages and filters -- ticking is the act, not the view -- and offered only
  // to a holder of `agent.push.run`. Cosmetic: the server checks that code,
  // `credentials.checkout`, every bound and the ceiling on each request.
  const canPush = (me.data?.permissions ?? []).includes(AGENT_PUSH_RUN);
  const [selected, setSelected] = useState<Device[]>([]);
  const [pushing, setPushing] = useState(false);
  const isSelected = (device: Device) => selected.some((d) => d.id === device.id);
  function toggle(device: Device) {
    setSelected((current) =>
      current.some((d) => d.id === device.id)
        ? current.filter((d) => d.id !== device.id)
        : current.length >= PUSH_BATCH_MAX
          ? current
          : [...current, device],
    );
  }
  function unselect(id: string) {
    const next = selected.filter((d) => d.id !== id);
    setSelected(next);
    if (next.length === 0) setPushing(false);
  }

  useLiveEvents(() => {
    void queryClient.invalidateQueries({ queryKey: ["devices"] });
  });

  // THE ROWS THE SERVER RETURNED, unfiltered by anything here. A local filter
  // on top of a server-filtered page would be a second answer to a question
  // that already has one, and the two only ever agree while the page is the
  // whole set -- which is the assumption this change exists to remove.
  const items = devices.data?.items ?? [];
  // The stat cards stay FLEET-WIDE on purpose: "Managed endpoints" is a fact
  // about the estate, and a number that shrank as somebody typed in a search
  // box would be a different -- and much worse -- version of the same defect.
  // `fleet` is counted with no filter applied at all.
  const fleet = devices.data?.fleet;
  // What each tab would show FROM THIS SEARCH, counted in SQL. `total` is this
  // object's entry for the active tab, by construction on the server, so the
  // strip and the table under it cannot disagree.
  const tabCounts = devices.data?.state_counts;
  const total = devices.data?.total ?? 0;
  const lastOffset = Math.max(0, Math.ceil(total / DEVICES_PAGE_SIZE) - 1) * DEVICES_PAGE_SIZE;

  return (
    <>
      <PageHead
        title={t("devices.page.title")}
        description={t("devices.page.description")}
        actions={
          <>
            {/* NOT gated on a permission in the console. The route is gated
              * `assets.manage` server-side, and hiding the button would only
              * decide WHERE a technician without the code learns they cannot
              * do this -- the frontend never gates security (invariant 6). */}
            <Btn label={t("devices.page.addAsset")} icon="plus" onClick={() => setAdding(true)} />
            <Btn
              label={t("devices.page.exportInventory")}
              onClick={() => exporter.mutate()}
              disabled={exporter.isPending}
            />
            {/* ONE "ADD A COMPUTER" ENTRY, the same page as the top bar's. It
                replaces "Enroll device" and "Add devices", which read as two
                tasks and were one: the page offers the one-machine install
                first and the network sweep below it
                (`docs/modules/agent-distribution/spec-push-install.md` §10.2).
                UNGATED: the page reads `devices.read`, which anyone here holds,
                and each step names the permission its own destination needs. */}
            {/* PUSH INSTALL, TICKED OR BY SUBNET (design 2026-09-30 §1.1).
                Offered to a holder of `agent.push.run`; cosmetic, because the
                server checks that code and everything else on every request. */}
            {canPush ? (
              <>
                <Btn label={t("devices.page.pushInstall")} icon="play" onClick={() => setPushing(true)} />
                <Link className="btn" to="/devices/push-runs">
                  {t("devices.page.pushRuns")}
                </Link>
              </>
            ) : null}
            <Link className="btn primary" to={ADD_COMPUTER_TO}>
              <Icon name="plus" size={15} />
              <span>{t("devices.page.addComputer")}</span>
            </Link>
          </>
        }
      />

      {/* A REFUSED EXPORT HAS TO BE SAID OUT LOUD. The anchor this replaces
          failed silently by construction — the browser saved the 401 body and
          the console never knew a thing had gone wrong. `describeSubmitError`
          is the one translation in the product, so a bodyless 503 does not
          render as the word "Error" and a JSON payload is not printed at the
          operator. */}
      {exporter.isError && (
        <p role="alert" className="form-error">
          {t("devices.page.exportFailed")} {describeSubmitError(exporter.error)}
        </p>
      )}

      {/* EVERY TILE OPENS THE ROWS IT COUNTED. The filter already lives in the
          URL (see `useSearchParams` above), so "Online now" and "Certificate
          attention" hand over the exact tab their number was computed from —
          the tile and the tab strip cannot drift apart, because the tile links
          to the tab.

          The first two are honest about a gap: there is no `managed`-only tab,
          and "Assets" is the whole estate, so both go to the unfiltered list.
          That is not a no-op — arriving from `?state=certs` it is what clears
          the filter — but it is the weakest link in this row, and inventing a
          `?state=managed` the page does not read would have been worse.

          Only certificate attention is `urgent`: an expiring agent certificate
          is a machine about to fall off the fleet, and a revoked one is a
          machine that already has. It was the one number here with a deadline
          attached; it is now the one with a fact attached too. */}
      {/* EVERY TILE CARRIES ITS BREAKDOWN, as v3's do ("272 of 286", "4
          offline, 3 certs"). Until now only `zeroLabel` was passed, so a tile
          said anything beyond its figure only in the one case where the figure
          was zero -- a bare number and no denominator on every populated
          fleet.

          EACH FOOT IS TWO SERVER COUNTS OR NONE. `fleet` is the estate,
          counted unfiltered in SQL beside the rows; a foot here restates or
          subtracts those four numbers and derives nothing from the page of
          rows on screen, which is the rule the header of this file sets and
          the one a tile disagreeing with its own table would break.

          NO SPARKLINE, and that is the same refusal the head of this file
          records for the design's "↑ 12 vs last 7 days": `StatCard` accepts
          `spark`, and every number that could fill it would have to be
          invented. No endpoint serves a history of these four counts. An
          absent trend is a gap; a drawn one would be a lie on the tile most
          likely to be believed. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("devices.tiles.managed")}
          value={String(fleet?.managed ?? 0)}
          icon="devices"
          to="/devices"
          tone="blue"
          zeroLabel={t("devices.tiles.managedZero")}
          foot={fleet && fleet.managed > 0 ? t("devices.tiles.managedFoot", { count: fleet.total }) : undefined}
        />
        <StatCard
          label={t("devices.tiles.assets")}
          value={String(fleet?.total ?? 0)}
          icon="package"
          to="/devices"
          tone="neutral"
          zeroLabel={t("devices.tiles.assetsZero")}
          foot={fleet && fleet.total > 0 ? t("devices.tiles.assetsFoot", { count: fleet.total - fleet.managed }) : undefined}
        />
        <StatCard
          label={t("devices.tiles.online")}
          value={String(fleet?.online ?? 0)}
          icon="activity"
          to="/devices?state=online"
          tone="green"
          zeroLabel={t("devices.tiles.onlineZero")}
          foot={fleet && fleet.online > 0 ? t("devices.tiles.onlineFoot", { managed: fleet.managed }) : undefined}
        />
        <StatCard
          label={t("devices.tiles.cert")}
          value={String(fleet?.cert_attention ?? 0)}
          icon="certificate"
          to="/devices?state=certs"
          tone="gold"
          urgent
          /* "0 expiring" and "no certificate state recorded" are the same
             pixel otherwise; this says which one the operator is reading. */
          zeroLabel={t("devices.tiles.certZero")}
          /* WHAT THE NUMBER COUNTS, on the one tile whose figure is a
             judgement rather than a total. `_cert_attention` in
             `app/services/device_list.py` is these three states and
             deliberately not `renewing`; an operator reading "2" is entitled
             to know which two questions it answers. */
          foot={fleet && fleet.cert_attention > 0 ? t("devices.tiles.certFoot") : undefined}
        />
      </StatsGrid>

      {/* SECTION HEADINGS. Auditor A: "[LOW] Page has a single `<h1>` and no
          section headings … Screen-reader users navigate by heading. With one
          heading per page, the heading shortcut is useless and the page's
          structure is invisible to assistive tech." `sr-only` because the
          design gives these blocks no visible title and inventing two would be
          a different page; what was missing is the structure. */}
      {pushing ? (
        <PushInstallChooser
          machines={selected}
          onRemove={unselect}
          onClose={() => setPushing(false)}
        />
      ) : null}

      <h2 className="sr-only">{t("devices.page.fleetSummary")}</h2>
      <h2 className="sr-only">{t("devices.page.deviceList")}</h2>
      {/* THE TABS NOW NAME THE PANEL THEY SWITCH. Auditor A: "[MEDIUM] Tabs
          are missing `aria-controls`/`tabpanel` wiring and roving tabindex …
          no element on the page has `role='tabpanel'` (count = 0), every tab
          has `aria-controls: null`". `Tabs` supplies the roving tabindex and
          the arrow keys; the panel and its id have to come from the page,
          because only the page knows what the strip actually filters. */}
      <Tabs
        items={TABS.map((item) => ({ key: item.key, label: t(item.label), count: tabCounts?.[item.key] ?? 0 }))}
        active={tab}
        onSelect={setTab}
        panelId="devices-tabpanel"
      />

      <Panel bare>
        <div
          id="devices-tabpanel"
          role="tabpanel"
          aria-label={t(TABS.find((item) => item.key === tab)?.label ?? "devices.tabs.all")}
          tabIndex={-1}
        >
        <FilterBar>
          <SearchField
            placeholder={
              canReadPeople
                ? t("devices.filters.searchPeople")
                : t("devices.filters.search")
            }
            /* BOUNDED. Auditor A: "[LOW] Search input has no maxlength and
               accepts 500+ characters … unbounded input is passed to the
               filter (and, in list views that search server-side, to the API).
               Capping it prevents accidental paste of an entire document and
               keeps the control legible." 128 is the audit's own suggestion. */
            maxLength={128}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            aria-label={t("devices.filters.searchAria")}
          />
          {/* A WAY OUT OF A SEARCH THAT FOUND NOTHING. Auditor A: "[MEDIUM]
              No-results empty state offers no way to clear the search … the
              user must manually select the text and delete it. With a
              500-character value the input scrolls horizontally and shows only
              the trailing characters, making manual clearing harder still."
              Rendered only when there is something to clear, so it is never a
              dead control. */}
          {search !== "" && (
            <Btn label={t("devices.filters.clear")} icon="close" small onClick={() => setSearch("")} />
          )}
          {/* "WHICH OF THESE DID A PERSON VOUCH FOR?" as a control. With a
              sweep recording everything it finds, that question is asked of
              this table constantly and could not previously be asked at all.
              The default is every row: a fleet list that quietly hid the
              discovered half would be a different lie from the one this
              answers. */}
          <select
            className="select-field"
            value={provenance}
            onChange={(event) => setProvenance(event.target.value)}
            aria-label={t("devices.filters.provenanceAria")}
          >
            <option value="">{t("devices.filters.provenanceAny")}</option>
            {PROVENANCE_OPTIONS.map((key) => (
              <option key={key} value={key}>
                {humaniseEnum(key, "DeviceProvenance")}
              </option>
            ))}
          </select>
          <span className="filter-spacer" />
        </FilterBar>

        {canPush && selected.length > 0 ? (
          <FilterBar>
            <p className="note" role="status">
              {t("devices.filters.selected", { count: selected.length, max: PUSH_BATCH_MAX })}
            </p>
            <Btn
              label={t("devices.filters.pushSelected", { count: selected.length })}
              icon="play"
              onClick={() => setPushing(true)}
            />
            <Btn
              label={t("devices.filters.clearSelection")}
              small
              onClick={() => {
                setSelected([]);
                setPushing(false);
              }}
            />
          </FilterBar>
        ) : null}
        {/* APPROVE ALL, on the Waiting for review tab only: after a Group
            Policy rollout the tab holds hundreds of machines, one Approve
            each. Cosmetic like every gate here: the route checks
            `devices.review`, and the dialog confirms a named list. */}
        {tab === "review" && canReview && (tabCounts?.review ?? 0) > 0 ? (
          <FilterBar>
            <Btn label={t("devices.filters.approveAll")} icon="check" onClick={() => setApprovingAll(true)} />
          </FilterBar>
        ) : null}
        <ApproveAllDialog
          open={approvingAll}
          onClose={() => setApprovingAll(false)}
          onDone={(outcome) => setReviewNotice({ kind: "approveAll", ...outcome })}
        />
        {reviewNotice ? (
          <p role="status" className="note">
            {reviewNotice.kind === "text"
              ? reviewNotice.text
              : approveAllMessage(reviewNotice.result, { capped: reviewNotice.capped })}
          </p>
        ) : null}
        <QueryState
          loading={devices.isLoading}
          error={devices.error}
          empty={items.length === 0}
          emptyLabel={
            // WHICH EMPTY THIS IS, and the distinction now comes from the
            // server rather than from the length of a page. `fleet.total` is
            // the estate with no filter applied, so zero there is a greenfield
            // deployment; zero rows with a non-zero estate is a filter that
            // matched nothing.
            fleet?.total === 0
              ? t("devices.empty.none")
              : search !== ""
                ? t("devices.empty.search", { term: shortTerm(search) })
                : t("devices.empty.filter")
          }
        >
          <TableWrap>
            <table>
              <thead>
                <tr>
                  {/* Auditor A: "[LOW] Table header cells omit `scope='col'`
                      … Without `scope`, assistive technology has to guess the
                      header-to-cell association." */}
                  {/* NAME AND STATUS FIRST. On a phone the table scrolls
                      sideways, and the Status column was off-screen at 390px
                      -- the one column a phone user needs (review
                      2026-09-23). */}
                  {canPush ? (
                    <th scope="col">
                      <span className="sr-only">{t("devices.table.selectForPush")}</span>
                    </th>
                  ) : null}
                  <th scope="col">{t("devices.table.device")}</th>
                  <th scope="col">{t("devices.table.agent")}</th>
                  <th scope="col">{t("devices.table.lastSeen")}</th>
                  {/* WHO HOLDS IT (operator decision 2026-09-23: People folded
                      into Devices). Only for a `people.read` holder, which is
                      also the only caller the server sends `assigned_to` to:
                      for anyone else this column would be a row of blanks
                      reading as "nobody holds anything". */}
                  {canReadPeople ? <th scope="col">{t("devices.table.assignedTo")}</th> : null}
                  <th scope="col">{t("devices.table.certificate")}</th>
                  <th scope="col">{t("devices.table.os")}</th>
                  <th scope="col">{t("devices.table.architecture")}</th>
                  <th scope="col">{t("devices.table.recordedBy")}</th>
                  <th scope="col">{t("devices.table.actions")}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((device) => {
                  // A REJECTED ROW IS LISTED FOR ONE REASON: its agent may
                  // still be on the computer. It is not a device any more --
                  // its page answers 404 -- so the name is not a link, and the
                  // only action is saying the agent has been removed.
                  const removal = manualRemovalStatus(device);
                  // A push could be for this machine: a computer or server
                  // (the agent runs on nothing else), no agent yet, an address
                  // to name, a person stood behind the row (a sweep row is
                  // refused by the server as `no_asset`), and not marked as
                  // unable to run an agent or retired.
                  const pushable =
                    canPush &&
                    (device.asset_type === "computer" || device.asset_type === "server") &&
                    device.agent_status == null &&
                    !removal &&
                    !isWaitingForReview(device) &&
                    device.primary_ip != null &&
                    device.provenance !== "sweep" &&
                    device.lifecycle !== "unmanageable" &&
                    device.lifecycle !== "retired";
                  return (
                    <tr key={device.id}>
                      {canPush ? (
                        <td>
                          {pushable ? (
                            <input
                              type="checkbox"
                              checked={isSelected(device)}
                              disabled={!isSelected(device) && selected.length >= PUSH_BATCH_MAX}
                              onChange={() => toggle(device)}
                              aria-label={t("devices.row.selectAria", { name: assetLabel(device) })}
                            />
                          ) : null}
                        </td>
                      ) : null}
                      {/* v3's row: the OS glyph leads, the severity is the 3px
                          gutter, the title is the name and the sub-line the
                          FQDN. A switch or a boxed laptop has no OS family;
                          `OsIcon` names it "Unknown platform" rather than
                          painting a Windows glyph on a printer. */}
                      {/* THE COLUMN STATES ITS MEASURE, and the review's I9 is
                          why. At 1440 this column settled at ~230px while
                          Operating system and Architecture -- both holding short
                          values -- took width they had no use for, so every FQDN
                          broke across two or three lines (`lis-ws-` /
                          `041.corp.toeshee.example`). There was spare width in
                          the row; nothing was asking for it. 300px, clipped, and
                          the name and FQDN each read as one line; the full value
                          is on the cell and the name is the link to the device. */}
                      <Td
                        lead={<OsIcon os={device.os_family ?? ""} />}
                        gutter={rowKind(device)}
                        clip={300}
                      >
                        <div className="cell-title">
                          {removal ? (
                            assetLabel(device)
                          ) : (
                            <Link to={`/devices/${device.id}`}>{assetLabel(device)}</Link>
                          )}
                        </div>
                        {isWaitingForReview(device) || removal ? (
                          <div className="cell-sub">
                            {joinedThroughLink(device) ?? t("devices.row.waitingForReview")},{" "}
                            <RelTime iso={device.link_joined_at ?? null} zone={zone} now={now} />
                          </div>
                        ) : device.fqdn ?? device.description ? (
                          <div className="cell-sub">{device.fqdn ?? device.description}</div>
                        ) : null}
                      </Td>
                      <td>
                        <StatusPill value={device.agent_status ?? "no agent"} />
                        {isWaitingForReview(device) ? (
                          <>
                            {" "}
                            <StatusPill value="pending" label={t("devices.row.waitingForReview")} kind="warn" />
                          </>
                        ) : removal ? (
                          <>
                            {" "}
                            <StatusPill value="rejected" label={t("devices.row.rejected")} kind="crit" />
                          </>
                        ) : null}
                      </td>
                      {/* Relative, with the instant on hover; NO_DATE for an
                          asset that has never contacted anything. One line: a
                          date broken over two reads as two facts. */}
                      <td className="whitespace-nowrap">
                        <RelTime iso={device.last_seen_at} zone={zone} now={now} />
                      </td>
                      {canReadPeople ? (
                        <td>
                          {device.assigned_to ? (
                            <Link to={`/people/${device.assigned_to.id}`}>
                              {device.assigned_to.display_name}
                            </Link>
                          ) : (
                            <NoValue label={t("devices.row.notAssigned")} />
                          )}
                        </td>
                      ) : null}
                      <td>
                        {/* REVOCATION OUTRANKS THE DERIVED STATE, and does not
                            sit beside it. `cert_state` is computed from the
                            expiry date and nothing else, so on a revoked row it
                            reads `ok` -- a second pill saying the opposite of
                            the first, and the one an operator scanning the
                            column would believe. What is true about this machine
                            is that its certificate has been withdrawn. */}
                        {device.cert_revoked ? (
                          <StatusPill value="revoked" />
                        ) : device.cert_state ? (
                          <StatusPill value={device.cert_state} />
                        ) : (
                          <NoValue label={t("devices.row.noCert")} />
                        )}
                      </td>
                      <td>
                        {deviceOs(device) ?? <NoValue label={t("devices.row.noOs")} />}
                      </td>
                      <td>
                        {device.architecture || <NoValue label={t("devices.row.noArch")} />}
                      </td>
                      <td>
                        {/* NOT a `StatusPill`: this is not a health state, and
                            colouring "Found by a sweep" red or green would make
                            a provenance read as a problem or an all-clear. It is
                            a fact about where the row came from. */}
                        <span className="note">{recordedByLabel(device, provenanceLabel(device))}</span>
                      </td>
                      <td>
                        {/* `ariaLabel` is why this was once written by hand:
                            two hundred rows whose every action reads "Run job"
                            is two hundred identical buttons to a screen reader
                            (Auditor A, [LOW]). Visible text unchanged.

                            NOT OFFERED ON A ROW WITH NO AGENT. A signed job
                            dispatches over the agent's own connection, and a
                            printer or a switch recorded by a sweep has none to
                            dispatch to -- the control has nothing it could do. */}
                        {removal ? (
                          <ManualRemoval
                            device={device}
                            label={assetLabel(device)}
                            status={removal}
                            canAcknowledge={canReview}
                          />
                        ) : isWaitingForReview(device) ? (
                          canReview ? (
                            <ReviewActions
                              device={device}
                              label={assetLabel(device)}
                              onRejected={(text) => setReviewNotice({ kind: "text", text })}
                            />
                          ) : (
                            <NoValue label={t("devices.row.waitingForApprover")} />
                          )
                        ) : device.agent_status != null ? (
                          <Btn
                            small
                            icon="play"
                            label={t("devices.row.runJob")}
                            ariaLabel={t("devices.row.runJobAria", { name: assetLabel(device) })}
                            onClick={() => setTarget(device)}
                          />
                        ) : (
                          <NoValue label={t("devices.row.noAgentJob")} />
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </TableWrap>
        </QueryState>

        {/* WHERE THIS PAGE SITS INSIDE THE WHOLE SET, ALWAYS -- not only once a
            cap bites. The estate no longer fits on one page, and a table that
            does not say so is a table an operator reads as complete: "no
            devices match" and "no devices match ON THIS PAGE" were the same
            pixels for as long as the filtering was local. `total` is the
            server's count of the rows this tab and this search select, so the
            sentence is the honest number and the buttons move through it.

            Rendered whenever there are rows, even when there is only one page:
            a note that appears only when something is missing teaches the
            reader nothing about the times it is absent. */}
        {/* `TableFoot`, NOT `Pager`: the kit's pager hides itself on a
            single-page list, and the paragraph above is the argument for why
            this count must not. */}
        {items.length > 0 && (
          <TableFoot>
            <span className="filter-spacer" />
            {/* NOT `.mono`. A result count is a sentence with figures in it,
                not a code sample: in the mono face this one broke across three
                lines at 390 beside the pager, where every comparable count in
                the console ("7 of 7", "4 shown") sits inline. `ResultCount`
                keeps the page's own face and makes only the figures tabular.
                The pattern was seeded by `Pager` itself, which wrapped its own
                count this way; it no longer does. */}
            <ResultCount>
              {t("devices.foot.showing", { from: offset + 1, to: offset + items.length, total, count: total })}
            </ResultCount>
            <Btn
              label={t("devices.foot.previous")}
              small
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - DEVICES_PAGE_SIZE))}
            />
            <Btn
              label={t("devices.foot.next")}
              small
              disabled={offset >= lastOffset}
              onClick={() => setOffset(offset + DEVICES_PAGE_SIZE)}
            />
          </TableFoot>
        )}
        </div>
      </Panel>

      <DispatchJobDialog device={target} onClose={() => setTarget(null)} />
      <AddAssetDialog open={adding} onClose={() => setAdding(false)} />
    </>
  );
}
