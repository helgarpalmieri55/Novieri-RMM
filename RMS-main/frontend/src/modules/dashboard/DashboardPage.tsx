/**
 * Dashboard — v3's "Dashboard" page (`P['dashboard']` in
 * `docs/design/itms-prototype-v3.html`): a tile row, "Fleet at a glance" as a
 * heat strip, patch compliance as donuts, and a device list with an OS glyph
 * on every row.
 *
 * THERE IS NO DASHBOARD ENDPOINT. Nothing on the backend aggregates the fleet,
 * so this screen is a COMPOSITION of the module queries that already exist. That
 * is the whole architecture of the page and it drives two consequences:
 *
 * 1. **Every panel is permission-gated, individually.** A dashboard that fires
 *    seven module queries for an operator who holds two of the seven read codes
 *    produces five 403s and five broken panels on the first screen they see.
 *    The gating below is `enabled:` on the query and a hidden panel — cosmetic,
 *    per invariant 6, and the server re-checks every one of them regardless. It
 *    exists so the console is not rude, not so it is safe.
 *
 * 2. **Every number is computed from a response.** The design's dashboard is
 *    the densest collection of invented figures in the prototype — 286 / 84% /
 *    12 / 7 / 96% / 23, three donuts, four sparklines and a heat strip, none
 *    of which has a request behind it. A hardcoded figure here would be the
 *    most convincing lie in the product, because this is the screen someone
 *    glances at and believes.
 *
 * WHAT v3 DRAWS THAT THIS PAGE DERIVES RATHER THAN INVENTS:
 *
 * | Design piece | What is behind it here |
 * |---|---|
 * | Sparkline on the jobs tile | Failed and stalled jobs, counted per day over the last 14 days from the job page in hand — and ONLY when that page is the whole list (`total === items.length`). A histogram of a truncated page is a line that always rises, so a partial page draws nothing |
 * | Sparkline on the alerts tile | Active alerts opened per day, from the active page, under the same completeness rule (the route pages at `ALERT_PAGE_SIZE`) |
 * | "Fleet at a glance" heat | One cell per device row in hand, read by `deviceKind`; the panel says when the rows are a page rather than the fleet |
 * | Patch compliance donuts | `compliant_pct_by_os_family` from the patch report, one donut per family the report names |
 * | Every tile's "vs last 7 days" delta | NOT DRAWN. A delta needs a previous value and nothing stores one |
 * | "Platform module directory" cards | NOT DRAWN. The counts are literals and there is no module-directory endpoint |
 * | "Devices needing a person" | The device list is the console's "Recent devices" widget, in v3's row shape; the population is the recent page, not a filter this page cannot ask the server for |
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useMemo } from "react";
import { Link } from "react-router-dom";

import { EmptyState, NoValue } from "../../components/emptystate";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { Device, Job, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { NO_DATE, formatDate, formatDateTime } from "../../lib/datetime";
import { Btn, PageHead, Panel, QueryState, StatusPill } from "../../shell/components";
import { DashboardReadiness } from "./DashboardReadiness";
import { Icon } from "../../shell/icons";
import {
  Donut,
  Facts,
  Heat,
  KV,
  KIND_ICON,
  OsIcon,
  Tile,
  kindOf,
  osLabel,
  type Kind,
} from "../../shell/viz";
import { checkLabel, describeObservation } from "../health/healthState";
import { isStuck } from "../jobs/jobState";
import { jobTypeLabel } from "../jobs/jobTypes";
import { needsYou, type NeedItem } from "./needsYou";
import { DashboardCanvas, useDashboardArrangement, type DashboardWidget } from "./DashboardCanvas";
import { dailySeries, deviceKind, hasSignal } from "./series";

/** The codes `shell/nav.ts` puts on each module's destination. One list, so a
 * panel and its sidebar link can never disagree about who may see it. */
const P = {
  devices: "devices.read",
  jobs: "jobs.read",
  health: "health.read",
  tickets: "ticket.read",
  patch: "patch.read",
  backup: "backup.read",
  remote: "remote.control",
  changes: "change.read",
} as const;

/** `lifecycle.py`'s terminal statuses, widened in
 * `modules/service-desk/serviceDeskState.ts`. A ticket that is not terminal is
 * open work; anything else would be this file inventing a second vocabulary. */
const TERMINAL_TICKET_STATUSES = new Set(["closed", "cancelled", "rejected"]);

const FAILED_JOB_STATUSES = new Set(["failed", "expired", "refused"]);
const RUNNING_JOB_STATUSES = new Set(["queued", "dispatched", "running"]);

/** The days a tile's sparkline covers. */
const TREND_DAYS = 14;

/**
 * `GET /health/alerts` pages at 200 by the route's own default
 * (`lib/api.ts#listHealthAlerts`). A page shorter than that is the whole
 * list, and only the whole list is a series worth drawing.
 */
const ALERT_PAGE_SIZE = 200;

/**
 * An OS family as its own name.
 *
 * `os_family` IS A WIRE ENUM AND IT REACHES THE SCREEN TWICE ON THIS PAGE: as
 * a patch donut's label ("windows", "linux", "macos") and inside the device
 * table's OS cell ("macos 15.6"). `humaniseEnum` cannot do this one -- it
 * would give "Macos", which is worse than the raw value because it reads as a
 * spelling mistake rather than as an untranslated identifier.
 *
 * THE NAMES CAME FROM A SECOND COPY OF THE KIT'S TABLE and now come from the
 * kit itself. `shell/viz#OS_ICON` already holds the platform name each glyph
 * is announced by, so a local map here was a second list that could disagree
 * with the one a screen reader hears beside it; `osLabel` reads that one.
 * The empty string stays this page's own answer: a device with no recorded
 * family renders nothing rather than the word "Unknown".
 */
function osName(family: string | null | undefined): string {
  const key = (family ?? "").trim().toLowerCase();
  return key === "" ? "" : osLabel(key);
}

/** A percentage as a severity: the thresholds the patch and backup panels share. */
function pctKind(value: number): Kind {
  if (value < 60) return "crit";
  if (value < 80) return "warn";
  return "ok";
}

function pct(value: number): number {
  return Math.round(value * 100);
}

/**
 * A KPI tile, on v3's `Tile`, with the two rules `StatCard` holds and this
 * page keeps while it draws its own sparklines:
 *
 * ZERO IS A MEASUREMENT, NOT AN ABSENCE. A zero tile drops out of the urgent
 * treatment and says which zero it is; "0 open" wearing an alarm wash reads as
 * a perfect score. An unresolved query reads `NO_DATE`, never `0`.
 *
 * THE `.stat-card` / `.stat-label` / `.stat-value` / `.urgent` HOOKS STAY, as
 * the class names the page's tests locate tiles by.
 */
function KpiTile({
  label,
  value,
  icon,
  to,
  kind,
  urgent = false,
  sub,
  zeroLabel,
  spark,
}: {
  label: string;
  value: number | null;
  icon: string;
  to: string;
  /** The wash the tile wears when it is loud; `ok` colours the figure only. */
  kind?: Kind;
  urgent?: boolean;
  /** The breakdown under a non-zero figure: "21 failed · 7 stalled". */
  sub?: string;
  zeroLabel: string;
  spark?: readonly number[];
}) {
  const measured = value !== null;
  const isZero = value === 0;
  const loud = urgent && measured && !isZero;
  return (
    <Tile
      className={`stat-card${loud ? " urgent" : ""}`}
      parts={{ label: "stat-label", value: "stat-value" }}
      label={label}
      value={measured ? String(value) : NO_DATE}
      icon={icon}
      to={to}
      kind={loud || kind === "ok" ? kind : undefined}
      sub={isZero ? zeroLabel : measured ? sub : undefined}
      spark={spark}
    />
  );
}

/** v3's ghost "View all" action in a panel head, as a real link. */
function PanelLink({ to, children }: { to: string; children: string }) {
  return (
    <Link className="btn ghost small" to={to}>
      <span>{children}</span>
      <Icon name="arrow-right" size={14} />
    </Link>
  );
}

/**
 * The page's verdict: what needs a person, most urgent first, each linked to
 * where it is handled. Outside the customizable arrangement on purpose -- a
 * to-do list someone has moved below the fold has stopped doing its job.
 */
function NeedsYouPanel({ items, loading }: { items: readonly NeedItem[]; loading: boolean }) {
  const t = useT();
  return (
    <Panel title={t("dashboard.needs.title")} subtitle={t("dashboard.needs.subtitle")}>
      {items.length > 0 ? (
        <div className="mini-list">
          {items.map((item) => (
            <div className="mini-row" key={item.id}>
              <span
                className={`mini-icon ${item.kind === "crit" ? "red" : item.kind === "warn" ? "gold" : ""}`}
              >
                <Icon name={KIND_ICON[item.kind]} size={14} />
              </span>
              <span className="mini-copy">
                <div className="mini-title">
                  <Link to={item.to}>{item.title}</Link>
                </div>
                {item.sub ? <div className="mini-sub">{item.sub}</div> : null}
              </span>
            </div>
          ))}
        </div>
      ) : loading ? (
        <div className="empty">
          <Icon name="clock" size={26} />
          <b>{t("dashboard.page.loading")}</b>
        </div>
      ) : (
        <EmptyState
          icon="circle-check"
          title={t("dashboard.needs.emptyTitle")}
          body={t("dashboard.needs.emptyBody")}
        />
      )}
    </Panel>
  );
}

export function DashboardPage() {
  const t = useT();
  const api = useApi();

  // A FIXED window, computed once per mount. Recomputing `now` on every render
  // drifts the query key on every render and the panel can never settle out of
  // `loading` -- exactly the defect found in SecurityPage during this port
  // (`useMemo` there, `useState` initialiser here, same reason).
  const [changeWindow] = useState(() => {
    const from = new Date();
    const to = new Date(from.getTime() + 14 * 24 * 60 * 60 * 1000);
    return { from: from.toISOString(), to: to.toISOString() };
  });

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  // EVERY time on this page is rendered in the signed-in user's zone (cycle 5).
  // The dashboard is the screen where a five-hour shift is least likely to be
  // noticed and most likely to be believed: three panels here each rendered a
  // date through a different ad-hoc `toLocaleString()`, so one device could
  // report one last-contact time here and another on its own page.
  const zone = me.data?.timezone;
  const held = useMemo(() => new Set(me.data?.permissions ?? []), [me.data]);
  const can = (code: string) => held.has(code);

  const devices = useQuery({
    queryKey: ["devices"],
    queryFn: () => api.listDevices(),
    enabled: can(P.devices),
  });
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api.listJobs(),
    enabled: can(P.jobs),
  });
  const alerts = useQuery({
    queryKey: ["health-alerts", "active"],
    queryFn: () => api.listHealthAlerts({ state: "active" }),
    enabled: can(P.health),
  });
  /* THE TILE'S NUMBER DOES NOT COME FROM THE LIST ABOVE, AND CANNOT.
   *
   * `listHealthAlerts` is a page of rows, and its `state` filter takes ONE
   * value -- so counting its length under-reports twice over: it is bounded by
   * the page size, and `active` is only one of the three states the health
   * module calls open (an ACKNOWLEDGED alert is owned and still open; a MUTED
   * one is silenced and still open). The list below wants exactly those active
   * rows and keeps asking for them. The tile wants a total, which is a
   * different question, and `/health/alerts/summary` is the route that answers
   * it in SQL.
   *
   * It is also the number the top bar's health glyph reads. A KPI tile and a
   * badge that both link to /health and disagree is the defect this cycle has
   * now found three times; one source is how it stops recurring. */
  const alertSummary = useQuery({
    queryKey: ["health-alert-summary"],
    queryFn: () => api.getHealthAlertSummary(),
    enabled: can(P.health),
  });
  const tickets = useQuery({
    queryKey: ["tickets", "dashboard"],
    queryFn: () => api.listTickets({ limit: 200 }),
    enabled: can(P.tickets),
  });
  const patch = useQuery({
    queryKey: ["patch-report"],
    queryFn: () => api.getPatchFleetReport(),
    enabled: can(P.patch),
  });
  const backup = useQuery({
    queryKey: ["backup-report"],
    queryFn: () => api.getBackupReport(),
    enabled: can(P.backup),
  });
  const changes = useQuery({
    queryKey: ["dashboard", "change-calendar", changeWindow.from, changeWindow.to],
    queryFn: () => api.getChangeCalendar(changeWindow.from, changeWindow.to),
    enabled: can(P.changes),
  });
  const remote = useQuery({
    queryKey: ["remote-sessions", "active"],
    queryFn: () => api.listRemoteSessions({ state: "active" }),
    enabled: can(P.remote),
  });

  const deviceItems: Device[] = useMemo(() => devices.data?.items ?? [], [devices.data]);
  const jobItems: Job[] = useMemo(() => jobs.data?.items ?? [], [jobs.data]);
  const alertItems = useMemo(() => alerts.data ?? [], [alerts.data]);
  const ticketItems = useMemo(() => tickets.data?.items ?? [], [tickets.data]);

  /**
   * "Jobs needing attention: 0" WHILE FOUR OF FIVE JOBS HAD SAT QUEUED FOR
   * OVER 24 HOURS with 0 attempts. That was the audit's headline number, and
   * the cause was this count: it read only the terminal failure statuses, so a
   * job that never started at all — the one state an operator most needs to
   * see — was reported as healthy. A dashboard that says nothing needs
   * attention is worse than no dashboard, because it is believed.
   *
   * `isStuck` is the same predicate /jobs badges its Waiting column with
   * (jobs/jobState.ts), so the two pages cannot disagree about which jobs are
   * in trouble. ONE `now` for the whole render: rows computed against
   * `Date.now()` each can disagree about two jobs issued in the same second.
   */
  const now = Date.now();
  const troubled = jobItems.filter((job) => FAILED_JOB_STATUSES.has(job.status) || isStuck(job, now));
  const stuckJobs = jobItems.filter((job) => isStuck(job, now)).length;
  const failedJobs = jobItems.filter((job) => FAILED_JOB_STATUSES.has(job.status)).length;
  const needAttention = failedJobs + stuckJobs;
  const runningJobs = jobItems.filter((job) => RUNNING_JOB_STATUSES.has(job.status)).length;
  const openTickets = ticketItems.filter(
    (ticket) => !TERMINAL_TICKET_STATUSES.has(ticket.status),
  ).length;
  const untriagedTickets = ticketItems.filter((ticket) => ticket.status === "new").length;
  /* WHO OWNS THE OPEN WORK. The third fact on the Service desk panel was
     "Loaded 12" -- the number of rows this client happened to fetch, which is
     a fact about the request and not about the queue, sitting between two real
     counts. Unassigned open tickets is the queue fact that number was standing
     in front of: work that exists and has nobody. */
  const unassignedTickets = ticketItems.filter(
    (ticket) => !TERMINAL_TICKET_STATUSES.has(ticket.status) && ticket.assigned_to === null,
  ).length;
  /* AND WHEN THE COUNTS ARE OVER A PAGE RATHER THAN THE QUEUE. `listTickets`
     is asked for 200 rows; a busier queue than that would have the three
     figures counting a page and saying nothing about it. `total` is the
     server's own count, so the panel says which it is rather than being
     quietly wrong. */
  const ticketsComplete = tickets.data != null && tickets.data.total === ticketItems.length;

  /* THE SPARKLINES, AND WHEN THEY ARE NOT DRAWN. v3 puts a fourteen-day line
     on the jobs and alerts tiles. Nothing serves a series, so each is a daily
     count of the rows in hand -- which is only a trend when the rows in hand
     are all of them. A page cut at 50 of 400 jobs is a histogram that always
     rises towards today, so a truncated page draws no line at all, and a
     window with nothing in it draws none either: a flat zero is not a trend. */
  const jobsComplete = jobs.data != null && jobs.data.total === jobItems.length;
  const jobSpark = useMemo(() => {
    if (!jobsComplete) return undefined;
    const series = dailySeries(troubled.map((job) => job.issued_at), TREND_DAYS, now);
    return hasSignal(series) ? series : undefined;
    // `troubled` and `now` are recomputed every render; the series depends on the rows.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobsComplete, jobItems]);
  const alertsComplete = alerts.data != null && alertItems.length < ALERT_PAGE_SIZE;
  const alertSpark = useMemo(() => {
    if (!alertsComplete) return undefined;
    const series = dailySeries(alertItems.map((alert) => alert.opened_at), TREND_DAYS, now);
    return hasSignal(series) ? series : undefined;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [alertsComplete, alertItems]);

  const patchByOs = useMemo(
    () => Object.entries(patch.data?.compliant_pct_by_os_family ?? {}),
    [patch.data],
  );
  const patchScannedAt = patch.data?.last_scan_at ?? null;

  /* THE HEAT STRIP: one cell per device row in hand, in v3's four kinds. */
  const fleetKinds = useMemo(() => deviceItems.map(deviceKind), [deviceItems]);
  const fleetCount = (kind: Kind) => fleetKinds.filter((k) => k === kind).length;
  const fleetTotal = devices.data?.total ?? 0;
  const severity = alertSummary.data?.by_severity;

  /* EVERY TILE CARRIES A BREAKDOWN, OR SAYS NOTHING -- NEVER A NUMBER MADE UP
     TO FILL THE LINE. Three of the six had no supporting line at all beside
     three that did, so "Managed endpoints 14" read as a lesser fact than
     "Open tickets 3 · 1 untriaged" sitting next to it. Each line below is a
     second figure the page already holds:

       * the fleet's counts are each other's context ("of 14 assets" under
         the managed count, "of 10 with an agent" under the online count),
         exactly as /devices draws them, from the same server-side `fleet`;
       * a live remote session's consent mode is the fact worth a second line,
         because "unattended" is somebody working on a machine with no one
         watching, and that is a governance question, not a count.

     `remote.data.total` is the server's count; the consent split is counted
     over the rows in hand, so it is only claimed when the page IS the whole
     set. */
  const fleet = devices.data?.fleet;
  const remoteItems = remote.data?.items ?? [];
  const remoteComplete = remote.data != null && remote.data.total === remoteItems.length;
  const unattendedRemote = remoteItems.filter(
    (session) => session.consent_mode === "unattended",
  ).length;
  const attendedRemote = remoteItems.length - unattendedRemote;

  /* EVERY WIDGET ON THE DASHBOARD, IN THE STOCK ORDER.

     This array IS the default arrangement, and the ids in it are the ids
     stored in `dashboard_layouts`. Two rules follow from that, and neither is
     optional:

       1. AN ID IS PERMANENT ONCE SHIPPED. Renaming one silently drops that
          widget out of every layout an operator has already saved --
          `resolveLayout` cannot tell a renamed widget from a removed one, and
          appends the "new" one at the end of their arrangement.
       2. A NEW WIDGET GOES WHERE IT BELONGS HERE, and appears at the END for
          anyone who has customized. That is `resolveLayout`'s doing and it is
          the right trade: appending is visible, and hiding what a stored row
          does not name would make every new dashboard feature invisible to
          exactly the operators who use the product enough to have arranged it.
          `fleet-glance` (the v3 port) is the first widget to arrive this way.

     `permission` filters this list BEFORE the layout is resolved, so a widget
     the caller may not see is not in the arrangement at all -- rather than
     being present-and-hidden, which would rewrite somebody's saved layout
     because their role changed for an afternoon. The server re-checks every
     query behind every one of these regardless (invariant 6); the filtering
     here is so the console is not rude, not so it is safe.

     TWO THINGS THE ORDER AND THE SIZES ARE DOING, neither of them decoration:

       1. THE TWO URGENT TILES COME FIRST, as v3's do. Leading with Managed
          endpoints and Online now pushed "Jobs needing attention" onto the
          second row, so the only two tiles that are ever tinted read as an
          afterthought below six neutral ones. What needs a person tonight is
          the first thing on the page.
       2. A PANEL IS PAIRED WITH ONE OF ITS OWN HEIGHT, and the two long lists
          are `wide`. One six-column grid puts each row at the height of its
          tallest member, so a heat strip beside a six-row device table left
          ~460px of bare page background under it, and Service desk left
          ~230px. Pairing the two donut panels, the two mini-lists and the two
          compact summaries -- and giving the device table and the change
          calendar a row of their own, which is exactly how v3 draws its
          device table -- is what closes those gaps from inside this file. The
          residual is the grid's stretch behaviour and is reported as a kit
          gap; nothing here can make a panel fill a row taller than itself. */
  const catalogue: DashboardWidget[] = [
    {
      id: "jobs-attention",
      title: t("dashboard.jobsAttention.title"),
      permission: P.jobs,
      defaultSize: "small",
      node: (
        <KpiTile
          label={t("dashboard.jobsAttention.title")}
          value={jobs.data ? needAttention : null}
          icon="jobs"
          to="/jobs"
          kind="crit"
          urgent
          sub={`${t("dashboard.jobsAttention.failed", { count: failedJobs })} · ${t("dashboard.jobsAttention.stalled", { count: stuckJobs })}`}
          zeroLabel={t("dashboard.jobsAttention.zero")}
          spark={jobSpark}
        />
      ),
    },
    {
      id: "open-alerts",
      title: t("dashboard.openAlerts.title"),
      permission: P.health,
      defaultSize: "small",
      node: (
        <KpiTile
          label={t("dashboard.openAlerts.title")}
          /* "OPEN", NOT "ACTIVE", because that is what the number counts:
             active plus acknowledged plus muted. The panel below is still
             headed "Active alerts" and still lists exactly those -- two
             different questions, each labelled with the one it answers. */
          value={alertSummary.data ? alertSummary.data.open : null}
          icon="health"
          to="/health"
          kind="warn"
          urgent
          sub={
            severity
              ? `${t("dashboard.openAlerts.critical", { count: severity.critical })} · ${t("dashboard.openAlerts.warning", { count: severity.warning })}`
              : undefined
          }
          /* WHICH zero this is: the health module answered, and nothing is
             firing. It is not "nothing has been measured" — that reads the
             same on screen and means the opposite. */
          zeroLabel={t("dashboard.openAlerts.zero")}
          spark={alertSpark}
        />
      ),
    },
    {
      id: "managed-endpoints",
      title: t("dashboard.managed.title"),
      permission: P.devices,
      defaultSize: "small",
      node: (
        /* MACHINES WITH AN AGENT, counted by the server exactly as /devices
           counts them. This read `total` -- every asset, printers and
           switches included -- so the two pages disagreed. */
        <KpiTile
          label={t("dashboard.managed.title")}
          value={fleet ? fleet.managed : null}
          icon="devices"
          to="/devices"
          sub={fleet ? t("dashboard.managed.sub", { total: fleet.total }) : undefined}
          zeroLabel={t("dashboard.managed.zero")}
        />
      ),
    },
    {
      id: "online-now",
      title: t("dashboard.online.title"),
      permission: P.devices,
      defaultSize: "small",
      node: (
        <KpiTile
          label={t("dashboard.online.title")}
          value={fleet ? fleet.online : null}
          icon="activity"
          /* `state` is the param /devices already filters on
             (DevicesPage's `useSearchParams`), so this lands on the online
             rows rather than dropping the operator into the whole fleet to
             find them again. */
          to="/devices?state=online"
          kind="ok"
          sub={fleet ? t("dashboard.online.sub", { managed: fleet.managed }) : undefined}
          zeroLabel={t("dashboard.online.zero")}
        />
      ),
    },
    {
      id: "open-tickets",
      title: t("dashboard.openTickets.title"),
      permission: P.tickets,
      defaultSize: "small",
      node: (
        <KpiTile
          label={t("dashboard.openTickets.title")}
          value={tickets.data ? openTickets : null}
          icon="ticket"
          to="/service-desk"
          sub={t("dashboard.openTickets.sub", { count: untriagedTickets })}
          zeroLabel={t("dashboard.openTickets.zero")}
        />
      ),
    },
    {
      id: "remote-sessions",
      title: t("dashboard.remote.title"),
      permission: P.remote,
      defaultSize: "small",
      node: (
        <KpiTile
          label={t("dashboard.remote.title")}
          value={remote.data ? remote.data.total : null}
          icon="remote"
          to="/remote"
          sub={
            remoteComplete && remoteItems.length > 0
              ? t("dashboard.remote.sub", { attended: attendedRemote, unattended: unattendedRemote })
              : undefined
          }
          zeroLabel={t("dashboard.remote.zero")}
        />
      ),
    },
    {
      id: "fleet-glance",
      title: t("dashboard.fleet.title"),
      permission: P.devices,
      defaultSize: "medium",
      node: (
        <Panel
          title={t("dashboard.fleet.title")}
          subtitle={
            devices.data
              ? deviceItems.length < fleetTotal
                ? t("dashboard.fleet.subtitleSome", { shown: deviceItems.length, total: fleetTotal })
                : t("dashboard.fleet.subtitleAll")
              : undefined
          }
          actions={<PanelLink to="/devices">{t("dashboard.link.viewAll")}</PanelLink>}
        >
          <QueryState
            loading={devices.isLoading}
            error={devices.error}
            empty={deviceItems.length === 0}
            emptyLabel={t("dashboard.fleet.empty")}
          >
            {/* THE BREAKDOWN IS IN THE PANEL NOW, NOT ONLY IN ITS SUBTITLE.

                The heat strip is one 10px chip per device, so on a fourteen-
                machine fleet it is a single short row at the top-left of a
                card that stretches to the height of "Service desk" beside it
                -- about 100px of blank card under four lines of content
                (design review M9). The counts the subtitle was carrying as
                one run-on sentence are the content that was missing, so they
                move into the body as the same `Facts` strip Service desk uses
                in the panel next door. Nothing new is computed: these are the
                identical `fleetCount` calls the subtitle made.

                IT ALSO STOPS COLOUR BEING THE ONLY CARRIER of the strip's
                meaning on the screen. `Heat` puts its summary on an
                `aria-label`, which is the right thing for a screen reader and
                invisible to a person who cannot separate the red chip from
                the amber one.

                THE SUBTITLE KEEPS THE SCOPE, which is the half that has to
                stay next to the title: a breakdown of the fourteen most
                recent devices must never read as a breakdown of the fleet. */}
            <Heat cells={fleetKinds} label={t("dashboard.fleet.heatLabel")} />
            <Facts
              items={[
                { k: t("dashboard.fleet.needAPerson"), v: String(fleetCount("crit")) },
                { k: t("dashboard.fleet.degraded"), v: String(fleetCount("warn")) },
                { k: t("dashboard.fleet.working"), v: String(fleetCount("run")) },
                { k: t("dashboard.fleet.healthy"), v: String(fleetCount("ok")) },
                // OMITTED AT ZERO, exactly as the subtitle omitted it: a fleet
                // where every machine runs an agent should not be told it holds
                // none without one.
                ...(fleetCount("mut") > 0
                  ? [{ k: t("dashboard.fleet.noAgent"), v: String(fleetCount("mut")) }]
                  : []),
              ]}
            />
          </QueryState>
        </Panel>
      ),
    },
    {
      id: "service-desk",
      title: t("dashboard.serviceDesk.title"),
      permission: P.tickets,
      defaultSize: "medium",
      node: (
        <Panel
          title={t("dashboard.serviceDesk.title")}
          subtitle={
            ticketsComplete
              ? t("dashboard.serviceDesk.subtitleAll")
              : t("dashboard.serviceDesk.subtitleSome", {
                  shown: ticketItems.length,
                  total: tickets.data?.total ?? ticketItems.length,
                })
          }
          actions={<PanelLink to="/service-desk">{t("dashboard.link.viewQueue")}</PanelLink>}
        >
          <QueryState
            loading={tickets.isLoading}
            error={tickets.error}
            empty={ticketItems.length === 0}
            emptyIcon="ticket"
            emptyLabel={t("dashboard.serviceDesk.empty")}
          >
            <Facts
              items={[
                { k: t("dashboard.serviceDesk.open"), v: String(openTickets) },
                { k: t("dashboard.serviceDesk.untriaged"), v: String(untriagedTickets) },
                { k: t("dashboard.serviceDesk.unassigned"), v: String(unassignedTickets) },
              ]}
            />
          </QueryState>
        </Panel>
      ),
    },
    {
      id: "recent-devices",
      title: t("dashboard.recentDevices.title"),
      permission: P.devices,
      defaultSize: "wide",
      node: (
        <Panel
          title={t("dashboard.recentDevices.title")}
          subtitle={t("dashboard.recentDevices.subtitle")}
          bare
          actions={<PanelLink to="/devices">{t("dashboard.link.viewAll")}</PanelLink>}
        >
          <QueryState
            loading={devices.isLoading}
            error={devices.error}
            empty={deviceItems.length === 0}
            emptyLabel={t("dashboard.recentDevices.empty")}
          >
            <Table>
              <thead>
                <tr>
                  <Th>{t("dashboard.recentDevices.device")}</Th>
                  <Th>{t("dashboard.recentDevices.os")}</Th>
                  <Th>{t("dashboard.recentDevices.agent")}</Th>
                  <Th>{t("dashboard.recentDevices.lastSeen")}</Th>
                </tr>
              </thead>
              <tbody>
                {deviceItems.slice(0, 6).map((device) => (
                  <tr key={device.id}>
                    <Td
                      lead={<OsIcon os={device.os_family ?? ""} />}
                      gutter={deviceKind(device)}
                    >
                      <div className="cell-title">
                        <Link to={`/devices/${device.id}`}>{device.hostname}</Link>
                      </div>
                      <div className="cell-sub">
                        {device.fqdn ?? <NoValue label={t("dashboard.recentDevices.noFqdn")} />}
                      </div>
                    </Td>
                    <Td>{`${osName(device.os_family)} ${device.os_version}`.trim()}</Td>
                    <Td>
                      <StatusPill value={device.agent_status ?? "no agent"} />
                    </Td>
                    <Td>{formatDateTime(device.last_seen_at, zone)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </QueryState>
        </Panel>
      ),
    },
    {
      id: "active-alerts",
      title: t("dashboard.activeAlerts.title"),
      permission: P.health,
      defaultSize: "medium",
      node: (
        <Panel
          title={t("dashboard.activeAlerts.title")}
          subtitle={t("dashboard.activeAlerts.subtitle")}
          actions={<PanelLink to="/health">{t("dashboard.link.viewAll")}</PanelLink>}
        >
          <QueryState
            loading={alerts.isLoading}
            error={alerts.error}
            empty={alertItems.length === 0}
            emptyIcon="health"
            emptyLabel={t("dashboard.activeAlerts.empty")}
          >
            <div className="mini-list">
              {alertItems.slice(0, 6).map((alert) => {
                const kind = kindOf(alert.severity);
                return (
                  <div className="mini-row" key={alert.id}>
                    <span className={`mini-icon ${kind === "crit" ? "red" : kind === "warn" ? "gold" : ""}`}>
                      <Icon name={KIND_ICON[kind]} size={14} />
                    </span>
                    <span className="mini-copy">
                      {/* THE SELECTOR IS OPTIONAL, and the separator was not:
                          a CPU or memory alert carries no selector, so five of
                          six rows ended in a dangling "·". */}
                      <div className="mini-title">
                        {/* `checkLabel` for the same reason as
                            `describeObservation` below it: the health module
                            owns what an alert is CALLED, and a dashboard that
                            heads the row `cpu.busy_pct` while /health heads it
                            "CPU busy" is two names for one incident. */}
                        {checkLabel(alert.check_type)}
                        {alert.selector ? ` · ${alert.selector}` : ""}
                      </div>
                      {/* `describeObservation` is the health module's own, so
                          this line and the alert console cannot come to
                          describe the same alert in two alphabets -- and
                          neither of them prints `gt` at a person. */}
                      <div className="mini-sub">
                        {alert.device_hostname} · {describeObservation(alert)}
                      </div>
                    </span>
                    <span className="mini-value">
                      <StatusPill value={alert.severity} kind={kind} />
                    </span>
                  </div>
                );
              })}
            </div>
          </QueryState>
        </Panel>
      ),
    },
    {
      id: "recent-jobs",
      title: t("dashboard.recentJobs.title"),
      permission: P.jobs,
      defaultSize: "medium",
      node: (
        <Panel
          title={t("dashboard.recentJobs.title")}
          subtitle={t("dashboard.recentJobs.subtitle")}
          actions={<PanelLink to="/jobs">{t("dashboard.link.viewEngine")}</PanelLink>}
        >
          <QueryState
            loading={jobs.isLoading}
            error={jobs.error}
            empty={jobItems.length === 0}
            emptyIcon="jobs"
            emptyLabel={t("dashboard.recentJobs.empty")}
          >
            <>
              <div className="mini-list">
                {jobItems.slice(0, 5).map((job) => {
                  const kind = kindOf(job.status);
                  return (
                    <div className="mini-row" key={job.id}>
                      <span className={`mini-icon ${kind === "crit" ? "red" : kind === "ok" ? "green" : ""}`}>
                        <Icon name={KIND_ICON[kind]} size={14} />
                      </span>
                      <span className="mini-copy">
                        {/* The name /jobs uses; the wire type is not for
                            the dashboard. */}
                        <div className="mini-title">{jobTypeLabel(job.type)}</div>
                        <div className="mini-sub">{formatDateTime(job.issued_at, zone)}</div>
                      </span>
                      <span className="mini-value">
                        <StatusPill value={job.status} />
                      </span>
                    </div>
                  );
                })}
              </div>
              <p className="note">
                {t("dashboard.recentJobs.summary", { running: runningJobs, attention: needAttention })}
                {stuckJobs > 0 ? ` ${t("dashboard.recentJobs.waiting", { count: stuckJobs })}` : ""}
              </p>
            </>
          </QueryState>
        </Panel>
      ),
    },
    {
      id: "patch-compliance",
      title: t("dashboard.patch.title"),
      permission: P.patch,
      defaultSize: "medium",
      node: (
        <Panel
          title={t("dashboard.patch.title")}
          subtitle={t("dashboard.patch.subtitle")}
          /* A FLEET THAT HAS NEVER BEEN SCANNED IS SENT TO SCAN, not merely
             to the page (cross-module-guidance §2 Dashboard #2): the empty
             state below says no scan has reported, and this is where one is
             started. */
          actions={
            <PanelLink to="/patch">
              {patch.data && patch.data.devices_scanned === 0
                ? t("dashboard.link.scanForUpdates")
                : t("dashboard.link.openPatching")}
            </PanelLink>
          }
        >
          <QueryState
            loading={patch.isLoading}
            error={patch.error}
            empty={patchByOs.length === 0}
            emptyIcon="patch"
            emptyLabel={t("dashboard.patch.empty")}
          >
            {/* EVERY DONUT ON THIS PAGE CARRIES ITS BASIS, and these three
                carried a bare percentage while the two backup donuts below
                them named a denominator -- one page, two conventions.

                THE BASIS IS RECENCY, NOT A DENOMINATOR, and that is not a
                second-best: `GET /patch/report` has no per-family device
                count, so "168 of 179" under "Windows" would be a number this
                console made up. What it does have is WHEN the fleet was last
                scanned, which is the context a bare compliance figure is
                actually missing -- 40% measured an hour ago and 40% measured
                three weeks ago are different facts. A report that has never
                scanned draws v3's `n/a` arc rather than a percentage over
                nothing.

                ROUND 3 ASKED AGAIN, for "168 of 179" under each family the way
                the Backup donuts beside them read "7 of 14", and the answer is
                still that the wire does not carry it: `compliant_pct_by_os_family`
                is a bare `dict[str, float]`, and `reporting.py` DOES count
                `os_family_totals` / `os_family_compliant` on its way to those
                percentages and then throws both away. Deriving a denominator
                from this console's own device page would be a DIFFERENT
                denominator -- the percentage is over machines that have a patch
                status row, the device page counts machines -- so "5 of 12"
                would sit under a figure computed as 5/9. The exact wire change
                is in this agent's report.

                What the report DOES carry is the coverage of the whole
                measurement, and that is a denominator this panel can state
                truthfully, so it does: the row of percentages now says what
                share of the fleet any of them was measured over. */}
            <div className="dons">
              {patchByOs.map(([family, value]) => (
                <Donut
                  key={family}
                  pct={patchScannedAt ? value : null}
                  kind={pctKind(value)}
                  label={osName(family)}
                  sub={
                    patchScannedAt
                      ? t("dashboard.patch.scanned", { date: formatDate(patchScannedAt, zone) })
                      : t("dashboard.patch.never")
                  }
                />
              ))}
            </div>
            {patch.data ? (
              <KV
                items={[
                  [
                    t("dashboard.patch.devicesScanned"),
                    t("dashboard.patch.ofTotal", {
                      scanned: patch.data.devices_scanned,
                      total: patch.data.devices_total,
                    }),
                  ],
                ]}
              />
            ) : null}
          </QueryState>
        </Panel>
      ),
    },
    {
      id: "backup-coverage",
      title: t("dashboard.backup.title"),
      permission: P.backup,
      defaultSize: "medium",
      node: (
        <Panel
          title={t("dashboard.backup.title")}
          subtitle={t("dashboard.backup.subtitle")}
          actions={<PanelLink to="/backup">{t("dashboard.link.openBackup")}</PanelLink>}
        >
          <QueryState
            loading={backup.isLoading}
            error={backup.error}
            empty={backup.data == null}
            emptyIcon="backup"
            emptyLabel={t("dashboard.backup.empty")}
          >
            {backup.data ? (
              <>
                <div className="dons">
                  <Donut
                    pct={pct(backup.data.coverage_rate)}
                    kind={pctKind(pct(backup.data.coverage_rate))}
                    label={t("dashboard.backup.covered")}
                    sub={t("dashboard.backup.ofTotal", {
                      covered: backup.data.devices_covered,
                      total: backup.data.devices_total,
                    })}
                  />
                  <Donut
                    pct={pct(backup.data.success_rate)}
                    kind={pctKind(pct(backup.data.success_rate))}
                    label={t("dashboard.backup.runSuccess")}
                    sub={t("dashboard.backup.acrossRuns")}
                  />
                </div>
                {/* A restore is only proven once verified, so the count that
                    is not yet proven sits beside the two rates. */}
                <KV
                  items={[
                    [t("dashboard.backup.pointsNeeding"), String(backup.data.points_needing_verification)],
                  ]}
                />
              </>
            ) : null}
          </QueryState>
        </Panel>
      ),
    },
    {
      id: "upcoming-changes",
      title: t("dashboard.changes.title"),
      permission: P.changes,
      defaultSize: "wide",
      node: (
        <Panel
          title={t("dashboard.changes.title")}
          subtitle={t("dashboard.changes.subtitle")}
          actions={<PanelLink to="/changes/calendar">{t("dashboard.link.openCalendar")}</PanelLink>}
        >
          <QueryState
            loading={changes.isLoading}
            error={changes.error}
            empty={(changes.data?.entries.length ?? 0) === 0}
            emptyIcon="changes"
            emptyLabel={t("dashboard.changes.empty")}
          >
            <div className="mini-list">
              {(changes.data?.entries ?? []).slice(0, 6).map((entry) => (
                <div className="mini-row" key={entry.change_id}>
                  <span className="mini-icon">
                    <Icon name="changes" size={14} />
                  </span>
                  <span className="mini-copy">
                    <div className="mini-title">{entry.title}</div>
                    {/* "No window set" rather than NO_DATE here: an absent
                        window is a state of the change, not a missing
                        field, and an em dash in a list of scheduled work
                        reads as "the date failed to load". */}
                    <div className="mini-sub">
                      {entry.window_start
                        ? formatDateTime(entry.window_start, zone)
                        : t("dashboard.changes.noWindow")}
                    </div>
                  </span>
                  <StatusPill value={entry.risk} />
                </div>
              ))}
            </div>
          </QueryState>
        </Panel>
      ),
    },
  ];

  // Each source contributes only once it has answered; `undefined` is "not
  // read", never zero.
  const needs = needsYou({
    alerts: alerts.data,
    failedJobs: jobs.data ? failedJobs : undefined,
    stuckJobs: jobs.data ? stuckJobs : undefined,
    fleet,
    untriagedTickets: tickets.data ? untriagedTickets : undefined,
    unverifiedBackups: backup.data?.points_needing_verification,
  });
  const needsSources = [
    [P.devices, devices],
    [P.jobs, jobs],
    [P.health, alerts],
    [P.tickets, tickets],
    [P.backup, backup],
  ] as const;
  const needsVisible = needsSources.some(([code]) => can(code));
  const needsLoading = needsSources.some(([code, query]) => can(code) && query.isLoading);

  const arrangement = useDashboardArrangement(catalogue, can);
  const [editing, setEditing] = useState(false);
  const readsNothing = ![
    P.devices,
    P.jobs,
    P.health,
    P.tickets,
    P.patch,
    P.backup,
    P.remote,
    P.changes,
  ].some(can);

  return (
    <>
      <PageHead
        title={t("dashboard.page.title")}
        description={t("dashboard.page.description")}
        actions={
          arrangement.visible.length > 0 ? (
            <Btn
              label={editing ? t("dashboard.page.done") : t("dashboard.page.customize")}
              icon={editing ? "check" : "adjustments"}
              variant={editing ? "primary" : "secondary"}
              // It is a toggle, so it says so. `Btn` has carried `pressed`
              // since the kit closed that gap; this caller never passed it,
              // and a screen reader heard "Customize" then "Done" with no
              // indication that one control had changed state.
              pressed={editing}
              disabled={arrangement.loading}
              onClick={() => setEditing((current) => !current)}
            />
          ) : undefined
        }
      />

      {/* THE ARRANGEMENT. One grid for the whole page, which is what makes
          "order it whichever way i want" expressible at all: the dashboard
          used to be a `.stats-grid` followed by two `.grid-2` rows, and a
          widget cannot move from the second row into the first when the rows
          are different elements with different column definitions. */}
      {needsVisible ? <NeedsYouPanel items={needs} loading={needsLoading} /> : null}

      {/* SECOND, NOT FIRST: "Needs you" leads the page by design (what needs
          a person today); what is not set up yet is the next thing to read. */}
      <DashboardReadiness />

      <DashboardCanvas arrangement={arrangement} editing={editing} />

      {/* An operator who holds none of the read codes above would otherwise see
          a page head over nothing at all. */}
      {readsNothing ? (
        <Panel>
          {me.isLoading ? (
            <div className="empty">
              <Icon name="clock" size={26} />
              <b>{t("dashboard.page.loading")}</b>
            </div>
          ) : (
            <EmptyState
              icon="lock"
              title={t("dashboard.page.nothingTitle")}
              body={t("dashboard.page.nothingBody")}
            />
          )}
        </Panel>
      ) : null}
    </>
  );
}
