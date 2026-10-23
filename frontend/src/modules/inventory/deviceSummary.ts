/**
 * What the device page's header and Overview tab summarise, from routes that
 * already exist. Each query is enabled only where it can answer: the agent-fed
 * ones need an agent, and each module's read needs its permission, so an
 * operator without `backup.read` gets no backup line rather than a 403.
 *
 * The header and the Overview call this with the same keys as the tabs, so
 * TanStack Query fetches each once.
 */

import { useQuery } from "@tanstack/react-query";

import { translate } from "../../i18n/core";
import type { Device } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import type { Kind } from "../../shell/viz";
import type { DeviceBackupStatus } from "../backup/types";
import type { AlertOut, DeviceHealthOut } from "../health/types";
import type { DeviceStatus } from "../patch/types";
import { TERMINAL_STATUSES } from "../service-desk/serviceDeskState";
import type { TicketOut } from "../service-desk/types";

export function useDeviceSummary(
  deviceId: string | undefined,
  device: Device | undefined,
  permissions: readonly string[],
) {
  const api = useApi();
  const managed = device?.agent_status != null;
  const can = (code: string) => permissions.includes(code);
  const id = deviceId ?? "";

  // `devices.read`, like the page itself (health spec §10).
  const health = useQuery<DeviceHealthOut>({
    queryKey: ["health-device", deviceId],
    queryFn: () => api.getDeviceHealth(id),
    enabled: managed && id !== "",
    retry: false,
  });
  const patch = useQuery<DeviceStatus>({
    queryKey: ["patch-device", deviceId],
    queryFn: () => api.getDevicePatchStatus(id),
    enabled: managed && id !== "" && can("patch.read"),
  });
  const backup = useQuery<DeviceBackupStatus>({
    queryKey: ["backup-device", deviceId],
    queryFn: () => api.getDeviceBackup(id),
    enabled: managed && id !== "" && can("backup.read"),
  });
  const tickets = useQuery({
    queryKey: ["service-desk-tickets", { device_id: deviceId, limit: 50 }],
    queryFn: () => api.listTickets({ device_id: id, limit: 50 }),
    enabled: id !== "" && can("ticket.read"),
  });
  return { managed, health, patch, backup, tickets };
}

/** Tickets still being worked: not closed, cancelled, rejected or resolved. */
export function openTickets(items: readonly TicketOut[] | undefined): TicketOut[] {
  return (items ?? []).filter(
    (ticket) => !TERMINAL_STATUSES.has(ticket.status) && ticket.status !== "resolved",
  );
}

const SEVERITY_RANK: Record<string, number> = { critical: 3, warning: 2, info: 1 };

/** Open alerts, the worst first, then the newest. */
export function rankAlerts(alerts: readonly AlertOut[]): AlertOut[] {
  return [...alerts].sort(
    (a, b) =>
      (SEVERITY_RANK[b.severity] ?? 0) - (SEVERITY_RANK[a.severity] ?? 0) ||
      Date.parse(b.opened_at) - Date.parse(a.opened_at),
  );
}

/** "3 open alerts", and the kind of the worst of them. */
export function alertLine(alerts: readonly AlertOut[]): { text: string; kind: Kind } {
  if (alerts.length === 0) return { text: translate("inventory.summary.noAlerts"), kind: "ok" };
  const worst = rankAlerts(alerts)[0]?.severity;
  const kind: Kind = worst === "critical" ? "crit" : worst === "warning" ? "warn" : "info";
  return { text: translate("inventory.summary.alerts", { count: alerts.length }), kind };
}

/** The updates line: what is missing or failed, and whether a restart is owed. */
export function updatesLine(status: DeviceStatus | undefined): { text: string; kind: Kind } | null {
  if (!status) return null;
  if (status.last_scan_at == null) return { text: translate("inventory.summary.updatesNever"), kind: "mut" };
  const missing = status.counts_by_state.missing ?? 0;
  const failed = status.counts_by_state.failed ?? 0;
  const restartOwed =
    status.pending_reboot ||
    (status.restart != null && ["pending", "dispatched", "scheduled"].includes(status.restart.state));
  if (failed > 0) return { text: translate("inventory.summary.failed", { count: failed }), kind: "crit" };
  if (missing > 0) return { text: translate("inventory.summary.missing", { count: missing }), kind: "warn" };
  if (restartOwed) return { text: translate("inventory.summary.restartDue"), kind: "warn" };
  return { text: translate("inventory.summary.upToDate"), kind: "ok" };
}

/** The certificate line, for the header: only worth words when it needs attention. */
export function certificateLine(device: Device | undefined): { text: string; kind: Kind } | null {
  if (!device || device.agent_status == null) return null;
  if (device.cert_revoked) return { text: translate("inventory.summary.certRevoked"), kind: "crit" };
  switch (device.cert_state) {
    case "expired":
      return { text: translate("inventory.summary.certExpired"), kind: "crit" };
    case "expiring":
      return { text: translate("inventory.summary.certExpiring"), kind: "warn" };
    case "renewing":
      return { text: translate("inventory.summary.certRenewing"), kind: "info" };
    case "ok":
      return { text: translate("inventory.summary.certValid"), kind: "ok" };
    default:
      return null;
  }
}
