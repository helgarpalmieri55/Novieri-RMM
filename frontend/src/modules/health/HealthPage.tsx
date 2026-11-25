import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useScrollToHash } from "../../lib/useScrollToHash";
import { formatDate } from "../../lib/datetime";
import { PageHead, UrlTabs, useNarrowedApi, useUrlFilters, useUrlTab, withClient } from "../../shell/components";
import { Drawer } from "../../shell/Drawer";
import { Icon } from "../../shell/icons";
import { KIND_ICON, Tile, type Kind } from "../../shell/viz";
import { dailySeries, hasSignal } from "../dashboard/series";
import { AlertConsole } from "./AlertConsole";
import { AlertReadiness } from "./AlertReadiness";
import { AlertDetail } from "./AlertDetail";
import { ApprovedActionList } from "./ApprovedActionList";
import { HealthReportPanel } from "./HealthReportPanel";
import { HealthSettingsPanel } from "./HealthSettingsPanel";
import { ProfileAssignments } from "./ProfileAssignments";
import { ProfileEditor } from "./ProfileEditor";
import { ProfileList } from "./ProfileList";
import { useHealthAlerts } from "./api";
import { checkLabel } from "./healthState";
import type { AlertOut, AlertSummary } from "./types";
import { useEverywherePermissions } from "../../lib/mePermissions";

// The endpoint-health console (spec §13), in four VIEWS on one route
// (redesign decision D21): the alert queue with its tiles and its report;
// the health profiles with their editor and assignments; the approved-action
// registry; and the org settings. Each was a panel further down one long page.
//
// THE `canX` FLAGS BELOW DECIDE WHAT RENDERS AND NOTHING ELSE. Every route is
// gated server-side by `require_permission` and that check is the only
// authority (invariant #6) -- hiding a control here is a usability affordance
// and prevents nothing. The five codes are deliberately separate and are read
// separately: `health.alert.manage` (technician: acknowledge/mute/close),
// `health.admin` (what the fleet alarms on), `health.remediate` (put a signed
// job on an endpoint), `health.action.admin` (decide what MAY run
// automatically as SYSTEM/root). A page that collapsed them into one "is an
// admin" boolean would offer a technician buttons that 403, and would hide from
// an action-admin the registry they are the only person who can edit.

/** The days the warning tile's sparkline covers. */
const TREND_DAYS = 14;

/** `GET /health/alerts` pages at 200 by the route's default (`lib/api.ts`). */
const ALERT_PAGE_SIZE = 200;

/** The page's views, in the URL as `?view=` (decision D21); the queue is the default and writes nothing. */
const VIEWS = ["alerts", "profiles", "actions", "settings"] as const;
type View = (typeof VIEWS)[number];

const VIEW_LABEL: Readonly<Record<View, MessageKey>> = {
  alerts: "health.views.alerts",
  profiles: "health.views.profiles",
  actions: "health.views.actions",
  settings: "health.views.settings",
};

/** The anchors other modules link to, and the view that now holds each (decision D21). */
const HASH_VIEW: Readonly<Record<string, "profiles" | "actions" | "settings">> = {
  "#profiles": "profiles",
  "#approved-actions": "actions",
  "#health-settings": "settings",
};

/** What a check type's first segment is about, in words; an unknown one is shown as it came. */
function subjectLabel(subject: string): string {
  const key = `health.subject.${subject}` as MessageKey;
  const label = translate(key);
  return label === key ? subject : label;
}

/** "8 cpu · 3 memory": the warning tile's breakdown, by what the checks are about. */
function breakdown(alerts: readonly AlertOut[]): string | undefined {
  const counts = new Map<string, number>();
  for (const alert of alerts) {
    const subject = alert.check_type.split(".")[0] ?? alert.check_type;
    counts.set(subject, (counts.get(subject) ?? 0) + 1);
  }
  const top = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 2);
  if (top.length === 0) return undefined;
  return top.map(([subject, count]) => `${count} ${subjectLabel(subject)}`).join(" · ");
}

export function HealthPage() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  // Configuration every client shares (GLOBAL_WRITES): the code on EVERY client (phase 3 design §2).
  const everywhere = useEverywherePermissions();
  const canRead = permissions.includes("health.read");
  const canAdmin = everywhere.has("health.admin");
  const canManageAlerts = permissions.includes("health.alert.manage");
  const canRemediate = permissions.includes("health.remediate");
  const canActionAdmin = everywhere.has("health.action.admin");

  const [view, setView] = useUrlTab<View>(VIEWS, "alerts", "view");

  // A LINK FROM BEFORE THE VIEWS (`/health#profiles`; three modules still
  // write them) opens the view that holds its panel, keeping the fragment, so
  // `useScrollToHash` below still lands on it.
  const location = useLocation();
  const navigate = useNavigate();
  const redirecting = HASH_VIEW[location.hash] !== undefined && !new URLSearchParams(location.search).has("view");
  useEffect(() => {
    const target = HASH_VIEW[location.hash];
    if (!target || new URLSearchParams(location.search).has("view")) return;
    const next = new URLSearchParams(location.search);
    next.set("view", target);
    void navigate({ search: `?${next.toString()}`, hash: location.hash }, { replace: true });
  }, [location.hash, location.search, navigate]);

  // A deep link to a panel (`#profiles`, `#approved-actions`,
  // `#health-settings`) scrolls once its view is the one drawn: each panel is
  // the first thing in its view, so nothing above it fills in after.
  useScrollToHash(me.isFetched && !redirecting);

  return (
    <div>
      <PageHead title={t("health.page.title")} description={t("health.page.description")} />

      {!canRead && (
        <div className="notice">
          <Icon name={KIND_ICON.info} size={17} />
          <span>{t("health.page.noPermission")}</span>
        </div>
      )}

      <UrlTabs
        label={t("health.views.label")}
        items={VIEWS.map((key) => ({ key, label: t(VIEW_LABEL[key]) }))}
        active={view}
        onSelect={setView}
      >
        {view === "alerts" ? (
          <AlertsView
            canRead={canRead}
            canManageAlerts={canManageAlerts}
            canRemediate={canRemediate}
            timezone={me.data?.timezone}
          />
        ) : view === "profiles" ? (
          <ProfilesView canAdmin={canAdmin} />
        ) : view === "actions" ? (
          // THE ANCHORS OTHER PAGES LINK TO (`/health#approved-actions`, and
          // `#profiles`, `#health-settings` in the other views). `Panel`
          // takes no id, so each anchor is a wrapper; `useScrollToHash` above
          // makes arriving at one actually move the page.
          <div id="approved-actions">
            <ApprovedActionList canActionAdmin={canActionAdmin} />
          </div>
        ) : (
          <div id="health-settings">
            <HealthSettingsPanel canAdmin={canAdmin} />
          </div>
        )}
      </UrlTabs>
    </div>
  );
}

/** The health profiles, with the selected one's checks and assignments (`#profiles`). */
function ProfilesView({ canAdmin }: { canAdmin: boolean }) {
  const [selectedProfileId, setSelectedProfileId] = useState<string | undefined>();
  return (
    <div id="profiles">
      <ProfileList canAdmin={canAdmin} selectedProfileId={selectedProfileId} onSelect={setSelectedProfileId} />
      {selectedProfileId && <ProfileEditor profileId={selectedProfileId} canAdmin={canAdmin} />}
      {selectedProfileId && <ProfileAssignments profileId={selectedProfileId} canAdmin={canAdmin} />}
    </div>
  );
}

/**
 * The queue: what has to be set up for alerts to reach anyone, v3's four
 * tiles, the alert list with the open alert in its drawer, and the report of
 * the alerts below it.
 */
function AlertsView({
  canRead,
  canManageAlerts,
  canRemediate,
  timezone,
}: {
  canRead: boolean;
  canManageAlerts: boolean;
  canRemediate: boolean;
  timezone: string | null | undefined;
}) {
  const t = useT();
  // The console's own filters, read here for the Client chip alone: the
  // tiles, the chip counts and the report count the list's own client.
  const filters = useUrlFilters(["client"] as const);
  const chip = filters.values.client;
  const { api: summaryApi, client: sent, ready } = useNarrowedApi(chip);

  /* THE TILES AND THE CHIP COUNTS READ THE SUMMARY OF THE LIST'S OWN CLIENT
     (redesign plan, O3 3.1, verification M-e). With no Client chip, the
     shell's key, so the rail badge and this row cannot disagree; under a
     chip, that client's counts and nobody else's. `/health/alerts/summary`
     counts open alerts in SQL, by state and by severity; the list below is a
     page. The list in hand supplies only what a count cannot: the breakdown
     under each figure and the fourteen-day line on the warning tile, and
     only when that list is complete rather than a page of a larger one. */
  const summary = useQuery<AlertSummary>({
    queryKey: sent === null ? ["health-alert-summary"] : ["health-alert-summary", { client: sent }],
    queryFn: () => summaryApi.getHealthAlertSummary(),
    enabled: canRead && ready,
  });
  const alerts = useHealthAlerts({}, chip);
  const rows = useMemo(() => alerts.data ?? [], [alerts.data]);
  const complete = alerts.data != null && rows.length < ALERT_PAGE_SIZE;
  const open = useMemo(() => rows.filter((alert) => alert.state !== "cleared"), [rows]);
  const critical = open.filter((alert) => alert.severity === "critical");
  const warning = open.filter((alert) => alert.severity === "warning");
  const acknowledged = open.filter((alert) => alert.state === "acknowledged");
  const muted = open.filter((alert) => alert.state === "muted");
  const warningSpark = useMemo(() => {
    if (!complete) return undefined;
    const series = dailySeries(
      rows.filter((alert) => alert.severity === "warning").map((alert) => alert.opened_at),
      TREND_DAYS,
      Date.now(),
    );
    return hasSignal(series) ? series : undefined;
  }, [complete, rows]);

  // THE OPEN ALERT LIVES IN THE URL (`?alert=`), so the dashboard's Needs
  // you list can link straight to one and Back closes it. It opens in a
  // drawer over the list; it used to render below the whole table.
  const [params, setParams] = useSearchParams();
  const selectedAlertId = params.get("alert") ?? undefined;
  const selectAlert = (alertId: string) => {
    const next = new URLSearchParams(params);
    next.set("alert", alertId);
    setParams(next);
  };
  const closeAlert = () => {
    const next = new URLSearchParams(params);
    next.delete("alert");
    setParams(next, { replace: true });
  };

  const counts = summary.data;
  // NO NUMBER AND NO CLAIM WITHOUT THE SUMMARY (clarification 10a): while it
  // is pending (under the chip, the wait for `/me` too) or after it failed,
  // each tile is the kit's pending tile, the absent mark with no line under it.
  const noData = !counts;
  const figure = (value: number | undefined) => String(value ?? 0);
  const loud = (value: number | undefined, kind: Kind): Kind | undefined =>
    value !== undefined && value > 0 ? kind : undefined;
  const ackPeople = new Set(acknowledged.map((alert) => alert.acknowledged_by).filter(Boolean)).size;
  const muteEnds = muted
    .map((alert) => alert.muted_until)
    .filter((at): at is string => at !== null)
    .sort()[0];
  // A tile opens the rows it counted, under the same Client chip.
  const to = (path: string) => withClient(path, sent);

  return (
    <>
      <AlertReadiness />

      {/* EVERY TILE OPENS THE ROWS IT COUNTED: the console below reads
          `severity` and `state` from the URL, so each figure lands on exactly
          its own population. */}
      {canRead && (
        <div className="tiles">
          <Tile
            label={t("health.page.tileCritical")}
            icon="alert-triangle"
            value={figure(counts?.by_severity.critical)}
            pending={noData}
            kind={loud(counts?.by_severity.critical, "crit")}
            sub={
              counts === undefined
                ? undefined
                : counts.by_severity.critical === 0
                  ? t("health.page.tileCriticalNone")
                  : critical[0]
                    ? t("health.page.tileCriticalOn", {
                        check: checkLabel(critical[0].check_type),
                        host: critical[0].device_hostname,
                      })
                    : undefined
            }
            to={to("/health?severity=critical")}
          />
          <Tile
            label={t("health.page.tileWarning")}
            icon="clock-exclamation"
            value={figure(counts?.by_severity.warning)}
            pending={noData}
            kind={loud(counts?.by_severity.warning, "warn")}
            sub={
              counts === undefined
                ? undefined
                : counts.by_severity.warning === 0
                  ? t("health.page.tileWarningNone")
                  : breakdown(warning)
            }
            spark={warningSpark}
            to={to("/health?severity=warning")}
          />
          <Tile
            label={t("health.page.tileAcknowledged")}
            icon="check"
            value={figure(counts?.by_state.acknowledged)}
            pending={noData}
            sub={
              counts === undefined
                ? undefined
                : counts.by_state.acknowledged === 0
                  ? t("health.page.tileAckNone")
                  : ackPeople > 0
                    ? t("health.page.tileAckBy", { count: ackPeople })
                    : t("health.page.tileAckOwned")
            }
            to={to("/health?state=acknowledged")}
          />
          <Tile
            label={t("health.page.tileMuted")}
            icon="bell"
            value={figure(counts?.by_state.muted)}
            pending={noData}
            sub={
              counts === undefined
                ? undefined
                : counts.by_state.muted === 0
                  ? t("health.page.tileMutedNone")
                  : muteEnds
                    ? t("health.page.tileMutedUntil", { date: formatDate(muteEnds, timezone) })
                    : t("health.page.tileMutedOpen")
            }
            to={to("/health?state=muted")}
          />
        </div>
      )}

      <AlertConsole onSelect={selectAlert} selectedId={selectedAlertId} counts={counts} />

      <Drawer open={selectedAlertId !== undefined} onClose={closeAlert} title={t("health.page.drawerTitle")}>
        {selectedAlertId ? (
          <AlertDetail alertId={selectedAlertId} canManageAlerts={canManageAlerts} canRemediate={canRemediate} />
        ) : null}
      </Drawer>

      {/* The report of the alerts above: it is theirs, so it sits under the queue. */}
      <HealthReportPanel chip={chip} />
    </>
  );
}
