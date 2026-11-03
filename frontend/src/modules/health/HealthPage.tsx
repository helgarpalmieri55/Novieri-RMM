import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useScrollToHash } from "../../lib/useScrollToHash";
import { NO_DATE, formatDate } from "../../lib/datetime";
import { PageHead } from "../../shell/components";
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
import { ProfileEditor } from "./ProfileEditor";
import { ProfileList } from "./ProfileList";
import { useHealthAlerts } from "./api";
import { checkLabel } from "./healthState";
import type { AlertOut, AlertSummary } from "./types";
import { useEverywherePermissions } from "../../lib/mePermissions";

// The endpoint-health console (spec §13): v3's four tiles over the alert
// list, then the detail, the reporting panel, profiles and their checks, the
// approved-action registry and the org settings.
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

  /* THE TILES READ THE SUMMARY, NOT THE LIST. `/health/alerts/summary` counts
     open alerts in SQL, by state and by severity; the list below is a page.
     Same key as the shell's health glyph, so the rail badge and this row
     cannot disagree. The list in hand supplies only what a count cannot: the
     breakdown under each figure and the fourteen-day line on the warning tile,
     and only when that list is complete rather than a page of a larger one. */
  const summary = useQuery<AlertSummary>({
    queryKey: ["health-alert-summary"],
    queryFn: () => api.getHealthAlertSummary(),
    enabled: canRead,
  });
  const alerts = useHealthAlerts({});
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
  const [selectedProfileId, setSelectedProfileId] = useState<string | undefined>();

  // A deep link to a panel below (`#profiles`, `#approved-actions`,
  // `#health-settings`) scrolls once the alert list above it has settled, so
  // the list filling in does not push the target back off screen.
  useScrollToHash(me.isFetched && !alerts.isLoading);

  const counts = summary.data;
  const figure = (value: number | undefined) => (value === undefined ? NO_DATE : String(value));
  const loud = (value: number | undefined, kind: Kind): Kind | undefined =>
    value !== undefined && value > 0 ? kind : undefined;
  const ackPeople = new Set(acknowledged.map((alert) => alert.acknowledged_by).filter(Boolean)).size;
  const muteEnds = muted
    .map((alert) => alert.muted_until)
    .filter((at): at is string => at !== null)
    .sort()[0];

  return (
    <div>
      <PageHead
        title={t("health.page.title")}
        description={t("health.page.description")}
      />

      <AlertReadiness />

      {!canRead && (
        <div className="notice">
          <Icon name={KIND_ICON.info} size={17} />
          <span>{t("health.page.noPermission")}</span>
        </div>
      )}

      {/* EVERY TILE OPENS THE ROWS IT COUNTED: the console below reads
          `severity` and `state` from the URL, so each figure lands on exactly
          its own population. */}
      {canRead && (
        <div className="tiles">
          <Tile
            label={t("health.page.tileCritical")}
            icon="alert-triangle"
            value={figure(counts?.by_severity.critical)}
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
            to="/health?severity=critical"
          />
          <Tile
            label={t("health.page.tileWarning")}
            icon="clock-exclamation"
            value={figure(counts?.by_severity.warning)}
            kind={loud(counts?.by_severity.warning, "warn")}
            sub={
              counts === undefined
                ? undefined
                : counts.by_severity.warning === 0
                  ? t("health.page.tileWarningNone")
                  : breakdown(warning)
            }
            spark={warningSpark}
            to="/health?severity=warning"
          />
          <Tile
            label={t("health.page.tileAcknowledged")}
            icon="check"
            value={figure(counts?.by_state.acknowledged)}
            sub={
              counts === undefined
                ? undefined
                : counts.by_state.acknowledged === 0
                  ? t("health.page.tileAckNone")
                  : ackPeople > 0
                    ? t("health.page.tileAckBy", { count: ackPeople })
                    : t("health.page.tileAckOwned")
            }
            to="/health?state=acknowledged"
          />
          <Tile
            label={t("health.page.tileMuted")}
            icon="bell"
            value={figure(counts?.by_state.muted)}
            sub={
              counts === undefined
                ? undefined
                : counts.by_state.muted === 0
                  ? t("health.page.tileMutedNone")
                  : muteEnds
                    ? t("health.page.tileMutedUntil", { date: formatDate(muteEnds, me.data?.timezone) })
                    : t("health.page.tileMutedOpen")
            }
            to="/health?state=muted"
          />
        </div>
      )}

      <AlertConsole onSelect={selectAlert} selectedId={selectedAlertId} />

      <Drawer open={selectedAlertId !== undefined} onClose={closeAlert} title={t("health.page.drawerTitle")}>
        {selectedAlertId ? (
          <AlertDetail
            alertId={selectedAlertId}
            canManageAlerts={canManageAlerts}
            canRemediate={canRemediate}
          />
        ) : null}
      </Drawer>

      <HealthReportPanel />

      {/* THE ANCHORS OTHER PAGES LINK TO (`/health#profiles`,
          `#approved-actions`, `#health-settings`). `Panel` takes no id, so
          each anchor is a wrapper; `useScrollToHash` above makes arriving
          at one actually move the page. */}
      <div id="profiles">
        <ProfileList
          canAdmin={canAdmin}
          selectedProfileId={selectedProfileId}
          onSelect={setSelectedProfileId}
        />

        {selectedProfileId && <ProfileEditor profileId={selectedProfileId} canAdmin={canAdmin} />}
      </div>

      <div id="approved-actions">
        <ApprovedActionList canActionAdmin={canActionAdmin} />
      </div>

      <div id="health-settings">
        <HealthSettingsPanel canAdmin={canAdmin} />
      </div>
    </div>
  );
}
