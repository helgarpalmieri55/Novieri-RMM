/**
 * "Needs you": the dashboard's ranked to-do list.
 *
 * Built only from figures the dashboard already fetches, so it cannot
 * disagree with the tiles beside it. A source the account cannot read, or
 * that has not answered, contributes nothing rather than a zero.
 *
 * NOT HERE, AND WHY: tickets about to breach their SLA. The ticket list route
 * carries no deadline (`TicketTable.tsx`), so "breaching" cannot be computed
 * without a request per ticket.
 */

import { translate as t } from "../../i18n/core";
import type { Kind } from "../../shell/viz";
import type { AlertOut } from "../health/types";
import { checkLabel, describeObservation } from "../health/healthState";

export interface NeedItem {
  id: string;
  kind: Extract<Kind, "crit" | "warn" | "info">;
  title: string;
  sub?: string;
  to: string;
}

export interface NeedsInputs {
  /** Active alerts, or undefined when not read. */
  alerts?: readonly AlertOut[];
  failedJobs?: number;
  stuckJobs?: number;
  /** The fleet-wide counts `GET /devices` returns beside its page. */
  fleet?: { managed: number; online: number; cert_attention: number };
  untriagedTickets?: number;
  unverifiedBackups?: number;
}

/** Critical alerts named one by one before they are summed. */
const NAMED_CRITICAL = 3;


export function needsYou(input: NeedsInputs): NeedItem[] {
  const items: NeedItem[] = [];
  const alerts = input.alerts ?? [];
  const critical = alerts.filter((alert) => alert.severity === "critical");
  const warning = alerts.filter((alert) => alert.severity === "warning");

  for (const alert of critical.slice(0, NAMED_CRITICAL)) {
    items.push({
      id: `alert:${alert.id}`,
      kind: "crit",
      title: t("dashboard.needs.alertOn", { check: checkLabel(alert.check_type), host: alert.device_hostname }),
      sub: describeObservation(alert),
      to: `/health?alert=${encodeURIComponent(alert.id)}`,
    });
  }
  if (critical.length > NAMED_CRITICAL) {
    items.push({
      id: "alerts:critical-more",
      kind: "crit",
      title: t("dashboard.needs.criticalMore", { count: critical.length - NAMED_CRITICAL }),
      to: "/health",
    });
  }
  if (input.failedJobs) {
    items.push({
      id: "jobs:failed",
      kind: "crit",
      title: t("dashboard.needs.jobsFailed", { count: input.failedJobs }),
      sub: t("dashboard.needs.jobsFailedSub"),
      to: "/jobs",
    });
  }

  const offline = input.fleet ? input.fleet.managed - input.fleet.online : 0;
  if (offline > 0) {
    items.push({
      id: "devices:offline",
      kind: "warn",
      title: t("dashboard.needs.devicesOffline", { count: offline }),
      sub: t("dashboard.needs.devicesOfflineSub"),
      to: "/devices?state=offline",
    });
  }
  if (warning.length > 0) {
    items.push({
      id: "alerts:warning",
      kind: "warn",
      title: t("dashboard.needs.warningAlerts", { count: warning.length }),
      to: "/health",
    });
  }
  if (input.stuckJobs) {
    items.push({
      id: "jobs:stuck",
      kind: "warn",
      title: t("dashboard.needs.jobsStuck", { count: input.stuckJobs }),
      sub: t("dashboard.needs.jobsStuckSub"),
      to: "/jobs",
    });
  }
  if (input.fleet && input.fleet.cert_attention > 0) {
    items.push({
      id: "devices:certs",
      kind: "warn",
      title: t("dashboard.needs.certs", { count: input.fleet.cert_attention }),
      sub: t("dashboard.needs.certsSub"),
      to: "/devices?state=certs",
    });
  }

  if (input.untriagedTickets) {
    items.push({
      id: "tickets:untriaged",
      kind: "info",
      title: t("dashboard.needs.tickets", { count: input.untriagedTickets }),
      to: "/service-desk",
    });
  }
  if (input.unverifiedBackups) {
    items.push({
      id: "backup:unverified",
      kind: "info",
      title: t("dashboard.needs.backups", { count: input.unverifiedBackups }),
      sub: t("dashboard.needs.backupsSub"),
      to: "/backup",
    });
  }
  return items;
}
