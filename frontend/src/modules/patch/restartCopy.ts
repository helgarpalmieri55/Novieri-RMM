import { translate } from "../../i18n/core";
import { formatDateTime } from "../../lib/datetime";
import type { Kind } from "../../shell/viz";
import type { PatchRestart, PatchRestartWindow } from "./types";

/** A restart's state as a short label and a pill colour. */
export function restartLabel(state: string): { label: string; kind: Kind } {
  switch (state) {
    case "pending":
      return { label: translate("patch.restart.pending"), kind: "warn" };
    case "dispatched":
      return { label: translate("patch.restart.dispatched"), kind: "info" };
    case "scheduled":
      return { label: translate("patch.restart.scheduled"), kind: "info" };
    case "completed":
      return { label: translate("patch.restart.completed"), kind: "ok" };
    case "cancelled":
      return { label: translate("patch.restart.cancelled"), kind: "ok" };
    case "failed":
      return { label: translate("patch.restart.failed"), kind: "crit" };
    default:
      return { label: state, kind: "info" };
  }
}

/** One sentence saying what will happen, in the given zone. */
export function restartSentence(
  restart: Pick<PatchRestart, "state" | "scheduled_for" | "user_present" | "reason" | "closed_at"> &
    Partial<Pick<PatchRestart, "origin">>,
  zone: string | null,
): string {
  switch (restart.state) {
    case "pending":
      return restart.origin === "operator"
        ? translate("patch.restart.pendingOperator")
        : translate("patch.restart.pendingUpdates");
    case "dispatched":
      return translate("patch.restart.dispatchedSentence");
    case "scheduled": {
      const when = formatDateTime(restart.scheduled_for, zone);
      return restart.user_present
        ? translate("patch.restart.scheduledWarned", { when })
        : translate("patch.restart.scheduledPlain", { when });
    }
    case "completed":
      return restart.closed_at
        ? translate("patch.restart.completedAt", { when: formatDateTime(restart.closed_at, zone) })
        : translate("patch.restart.completedPlain");
    case "cancelled":
    case "failed":
      return restart.reason ? capitalise(restart.reason) + "." : "";
    default:
      return "";
  }
}

/** "15 minutes", "8 hours", "2 days": the largest whole unit. */
export function describeMinutes(minutes: number): string {
  if (minutes >= 1440 && minutes % 1440 === 0) return translate("patch.restart.days", { count: minutes / 1440 });
  if (minutes >= 60 && minutes % 60 === 0) return translate("patch.restart.hours", { count: minutes / 60 });
  return translate("patch.restart.minutes", { count: minutes });
}

/** When a restart booked now is sent, as one sentence. */
export function restartWhen(window: PatchRestartWindow | null | undefined, zone: string | null): string {
  if (!window) return translate("patch.restart.whenNoWindow");
  if (window.open_now) return translate("patch.restart.whenOpen");
  if (window.opens_at) {
    const at = formatDateTime(window.opens_at, zone);
    return zone
      ? translate("patch.restart.whenOpensZone", { at, zone })
      : translate("patch.restart.whenOpens", { at });
  }
  return translate("patch.restart.whenFar");
}

/**
 * What the Restart confirmation says: when it happens, in the org's zone, and
 * the warning a signed-in user gets. `window` is null when it could not be
 * read (no permission to view updates, or still loading).
 */
export function restartConfirmBody(window: PatchRestartWindow | null | undefined, zone: string | null): string {
  const when = restartWhen(window, zone);
  const warning = window
    ? translate("patch.restart.warnKnown", { duration: describeMinutes(window.user_warning_minutes) })
    : translate("patch.restart.warnUnknown");
  return `${when} ${warning} ${translate("patch.restart.offline")}`;
}

function capitalise(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}
