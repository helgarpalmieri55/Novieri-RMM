import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import type { BadgeVariant } from "../jobs/jobState";

/** The event types the security log carries. A literal union, so the enum coverage test reads it. */
export type SecurityEventType =
  | "logon_success"
  | "logon_failure"
  | "logoff"
  | "account_lockout"
  | "privilege_elevation"
  | "admin_group_change"
  | "policy_change";

const VARIANTS: Record<SecurityEventType, BadgeVariant> = {
  logon_failure: "destructive",
  account_lockout: "destructive",
  privilege_elevation: "warning",
  admin_group_change: "warning",
  policy_change: "warning",
  logon_success: "muted",
  logoff: "muted",
};

export function eventBadgeVariant(eventType: string): BadgeVariant {
  return Object.hasOwn(VARIANTS, eventType) ? VARIANTS[eventType as SecurityEventType] : "muted";
}

/**
 * An event type as the operator reads it. A type this client has not met (a
 * newer agent) is humanised through the shared status vocabulary, never printed
 * raw.
 */
export function securityEventLabel(eventType: string): string {
  return Object.hasOwn(VARIANTS, eventType)
    ? humaniseEnum(eventType, "SecurityEventType")
    : humaniseStatus(eventType);
}
