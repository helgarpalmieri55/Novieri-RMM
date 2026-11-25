/**
 * THE TWO SUMMARY CARDS OF THE DEVICE RECORD (redesign plan Task 6, 1.2; the
 * "atera 8" capture's "Alert status" and "Patching status"): what is wrong
 * with this machine and what it still owes, above the tabs, each number
 * leading to the tab that holds the rest.
 *
 * They are the Overview's Health and Updates panels, moved up (the record's
 * information and activity stay in the Overview tab). Same reads, same
 * permissions as the tabs they lead to: the alert card wherever the Health tab
 * is (a managed device; `devices.read`, health spec §10), the patch card
 * wherever the Updates tab is (`patch.read`). Nothing here is counted that
 * the server did not report.
 */
import { Link } from "react-router-dom";

import { NoValue } from "../../components/emptystate";
import { RelTime } from "../../components/reltime";
import { useT } from "../../i18n/context";
import { Panel, QueryState, StatusPill } from "../../shell/components";
import { KV } from "../../shell/viz";
import { checkLabel, severityLabel } from "../health/healthState";
import type { HealthSeverity } from "../health/types";
import { restartLabel } from "../patch/restartCopy";
import { severityKind as patchSeverityKind, severityLabel as patchSeverityLabel } from "../patch/patchState";
import { alertLine, rankAlerts, updatesLine, type useDeviceSummary } from "./deviceSummary";

/** How many open alerts the card lists; the Health tab holds every one. */
const SHOWN = 5;

/** The alert card's three counts, worst first, with the kind each pill carries (its icon is the second carrier). */
const ALERT_SEVERITIES: readonly { severity: HealthSeverity; kind: "crit" | "warn" | "info" }[] = [
  { severity: "critical", kind: "crit" },
  { severity: "warning", kind: "warn" },
  { severity: "info", kind: "info" },
];

/** The patch severities the server counts, in the order a person ranks them; any other it sends follows. */
const PATCH_ORDER = ["critical", "important", "high", "moderate", "medium", "low", "informational", "unknown"];

type Summary = ReturnType<typeof useDeviceSummary>;

export function AlertStatusCard({
  deviceId,
  summary,
  permissions,
  zone,
  now,
}: {
  deviceId: string;
  summary: Summary;
  permissions: readonly string[];
  zone?: string | null;
  now: number;
}) {
  const t = useT();
  const here = `/devices/${deviceId}`;
  const { health } = summary;
  const alerts = rankAlerts(health.data?.open_alerts ?? []);
  const line = health.data ? alertLine(health.data.open_alerts) : null;
  // Opening the alert list (or one alert) needs `health.read`; the device's own Health tab does not.
  const canOpenAlerts = permissions.includes("health.read");
  const alertTo = (id: string) => (canOpenAlerts ? `/health?alert=${id}` : `${here}?tab=health`);
  return (
    <Panel
      title={t("inventory.cards.alerts")}
      actions={
        line ? (
          <Link to={canOpenAlerts ? `/health?device=${deviceId}&state=open` : `${here}?tab=health`}>{line.text}</Link>
        ) : undefined
      }
    >
      <QueryState loading={health.isLoading} error={health.error} empty={false} emptyLabel="" emptyIcon="health">
        {/* COUNTED BY SEVERITY, each count leading to the Health tab (the
            capture's "Critical 1 · Warning 4 · Information 0"). A zero is a
            count the server gave (its open alerts, none of that severity),
            not an absent number. */}
        <div className="flex flex-wrap items-center gap-3">
          {ALERT_SEVERITIES.map(({ severity, kind }) => {
            const count = alerts.filter((alert) => alert.severity === severity).length;
            return (
              <Link
                key={severity}
                to={`${here}?tab=health`}
                className="inline-flex items-center gap-2"
                aria-label={t("inventory.cards.alertCount", { severity: severityLabel(severity), count })}
              >
                <StatusPill value={severity} kind={count > 0 ? kind : "mut"} label={severityLabel(severity)} />
                <span className="mono">{count}</span>
              </Link>
            );
          })}
        </div>
        {alerts.length === 0 ? (
          <p className="note mt-3">{t("inventory.overview.noAlerts")}</p>
        ) : (
          <ul className="mini-list mt-2">
            {alerts.slice(0, SHOWN).map((alert) => (
              <li key={alert.id} className="mini-row">
                <StatusPill value={alert.severity} label={severityLabel(alert.severity)} />
                <span className="mini-copy">
                  <Link to={alertTo(alert.id)}>{checkLabel(alert.check_type)}</Link>
                  <span className="mini-sub block">
                    {t("inventory.overview.since")} <RelTime iso={alert.opened_at} zone={zone} now={now} />
                  </span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </QueryState>
    </Panel>
  );
}

export function PatchStatusCard({
  deviceId,
  summary,
  zone,
  now,
}: {
  deviceId: string;
  summary: Summary;
  zone?: string | null;
  now: number;
}) {
  const t = useT();
  const tab = `/devices/${deviceId}?tab=patch`;
  const { patch } = summary;
  const updates = updatesLine(patch.data);
  const restart = patch.data?.restart;
  const restartOwed = restart != null && ["pending", "dispatched", "scheduled"].includes(restart.state);
  // What is still outstanding, by severity, as the server counts it (`counts_by_severity`).
  const bySeverity = Object.entries(patch.data?.counts_by_severity ?? {})
    .filter(([, count]) => count > 0)
    .sort(([a], [b]) => rank(a) - rank(b));
  return (
    <Panel title={t("inventory.cards.patch")} actions={<Link to={tab}>{t("inventory.overview.reviewUpdates")}</Link>}>
      <QueryState loading={patch.isLoading} error={patch.error} empty={false} emptyLabel="" emptyIcon="patch">
        {bySeverity.length > 0 || patch.data?.pending_reboot || restartOwed ? (
          <div className="mb-3 flex flex-wrap items-center gap-3">
            {bySeverity.map(([severity, count]) => (
              <Link
                key={severity}
                to={tab}
                className="inline-flex items-center gap-2"
                aria-label={t("inventory.cards.patchCount", { severity: patchSeverityLabel(severity), count })}
              >
                <StatusPill value={severity} kind={patchSeverityKind(severity)} label={patchSeverityLabel(severity)} />
                <span className="mono">{count}</span>
              </Link>
            ))}
            {/* THE CAPTURE'S "Reboot required": a restart the machine owes. */}
            {patch.data?.pending_reboot || restartOwed ? (
              <Link to={tab}>
                <StatusPill value="restart" kind="warn" label={t("inventory.summary.restartDue")} />
              </Link>
            ) : null}
          </div>
        ) : null}
        <KV
          items={[
            [
              t("inventory.overview.state"),
              updates ? (
                <Link to={tab}>
                  <StatusPill value={updates.text} kind={updates.kind} label={updates.text} />
                </Link>
              ) : (
                <NoValue label={t("inventory.overview.notKnown")} />
              ),
            ],
            [
              t("inventory.overview.restart"),
              restart && restartOwed ? (
                <span>{t("inventory.overview.inWindow", { label: restartLabel(restart.state).label })}</span>
              ) : patch.data?.pending_reboot ? (
                t("inventory.overview.dueInWindow")
              ) : (
                t("inventory.overview.noneDue")
              ),
            ],
            [t("inventory.overview.lastChecked"), <RelTime iso={patch.data?.last_scan_at} zone={zone} now={now} />],
          ]}
        />
      </QueryState>
    </Panel>
  );
}

function rank(severity: string): number {
  const at = PATCH_ORDER.indexOf(severity);
  return at === -1 ? PATCH_ORDER.length : at;
}
