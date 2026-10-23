/**
 * Audit actions as a reader says them. The action code stays on screen as
 * secondary text, because it is what the log holds and what an auditor
 * filters by; this is only the line that leads the row.
 *
 * The words come from `enums.AuditAction` in both languages. Not exhaustive:
 * every module records its own actions, and an unmapped code is split into
 * words ("ipam.sweep_rejected" -> "IPAM sweep rejected") rather than hidden.
 * That derived text is English; nothing stored in an entry is ever rewritten
 * (spec §3.6), only the label around it.
 */

import { humaniseEnum } from "../../lib/labels";

/** A dotted code is keyed with `_`: a `.` separates catalogue paths. */
export function auditActionLabel(action: string): string {
  const key = action.replace(/\./g, "_");
  return humaniseEnum(key, "AuditAction");
}

/** The kind of actor or target, in words; the id stays beside it. */
export function auditActorTypeLabel(type: string): string {
  return humaniseEnum(type, "AuditActorType");
}

export function auditTargetTypeLabel(type: string): string {
  return humaniseEnum(type, "AuditTargetType");
}
