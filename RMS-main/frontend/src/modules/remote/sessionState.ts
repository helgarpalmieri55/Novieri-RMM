import { translate } from "../../i18n/core";
import type { RemoteSessionState } from "../../lib/api";
import type { BadgeVariant } from "../jobs/jobState";

export const REMOTE_SESSION_STATES: readonly RemoteSessionState[] = [
  "awaiting_consent",
  "connecting",
  "active",
  "ended",
] as const;

const STATE_VARIANTS: Record<RemoteSessionState, BadgeVariant> = {
  awaiting_consent: "warning",
  connecting: "info",
  active: "success",
  ended: "muted",
};

// Server may report states outside the known set in the future; fall back to
// "muted" rather than throwing (mirrors statusVariant's exhaustive intent
// without assuming the frontend is always in lockstep with the backend).
export function remoteStateVariant(state: string): BadgeVariant {
  return STATE_VARIANTS[state as RemoteSessionState] ?? "muted";
}

export function consentModeLabel(mode: string): string {
  return translate(mode === "unattended" ? "remote.consent.unattended" : "remote.consent.attended");
}

export function formatDuration(startedAt: string | null, endedAt: string | null): string {
  if (!startedAt) return "—";
  const start = new Date(startedAt).getTime();
  const end = endedAt ? new Date(endedAt).getTime() : Date.now();
  const totalSeconds = Math.max(0, Math.floor((end - start) / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return translate("remote.duration", { minutes, seconds: seconds.toString().padStart(2, "0") });
}
