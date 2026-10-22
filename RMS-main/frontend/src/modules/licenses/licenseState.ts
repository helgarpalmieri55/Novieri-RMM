import type { BadgeVariant } from "../jobs/jobState";
import type { Kind } from "../../shell/viz";

// license.type vocabulary exactly as written by the backend
// (backend/app/modules/licenses/schema.py's `LicenseType` Literal is
// `Literal["perpetual", "volume", "oem", "cal", "per_device", "per_user"]`,
// the only values LicenseCreate/LicenseUpdate ever write to License.type).
// No other type value is ever persisted -- a made-up type name here would
// repeat the Phase-5 regression. "perpetual" is the one type that never
// expires or needs a renewal cadence, so it gets the distinct "info" look;
// every other (recurring/consumption) type shares the neutral "muted" look.
const LICENSE_TYPE_VARIANTS: Record<string, BadgeVariant> = {
  perpetual: "info",
  volume: "muted",
  oem: "muted",
  cal: "muted",
  per_device: "muted",
  per_user: "muted",
};

export function licenseTypeVariant(type: string): BadgeVariant {
  return LICENSE_TYPE_VARIANTS[type] ?? "muted";
}

// ComplianceRow.breach is a plain boolean computed by
// backend/app/modules/licenses/reporting.py's compliance_summary (installed
// count exceeds entitled count) -- not a vocabulary string, so there is
// nothing to invent or fall back on here.
export function complianceVariant(breach: boolean): BadgeVariant {
  return breach ? "destructive" : "success";
}

// RenewalRow.due_in_days is a plain integer computed by
// backend/app/modules/licenses/reporting.py's renewal_calendar (due_date -
// now, in days) -- not a vocabulary string. Negative means the renewal/
// maintenance date has already passed (overdue); a small non-negative window
// means it is coming up soon; anything further out is not yet urgent.
const DUE_SOON_DAYS = 14;

export function dueVariant(dueInDays: number): BadgeVariant {
  if (dueInDays < 0) return "destructive";
  if (dueInDays <= DUE_SOON_DAYS) return "warning";
  return "muted";
}

// THE v3 KINDS, beside the retired variants. `StatusPill` takes a `Kind`
// (crit / warn / ok / run / mut / info) rather than a `BadgeVariant`, and the
// mapping is the same judgement written in the new vocabulary: a perpetual
// license is the one distinct type (info), the rest are neutral (mut); a
// breach is a failure that has already happened (crit) and its absence is ok;
// a passed date is critical, one inside the fortnight is a warning, further
// out is nothing to look at yet. Nothing on a page calls the `BadgeVariant`
// functions above any more; only their tests do. The plan is that the closer
// removes them together with those test cases when `Badge` is retired from
// the kit, in one commit, rather than a page at a time here.

export function licenseTypeKind(type: string): Kind {
  return type === "perpetual" ? "info" : "mut";
}

export function complianceKind(breach: boolean): Kind {
  return breach ? "crit" : "ok";
}

export function dueKind(dueInDays: number): Kind {
  if (dueInDays < 0) return "crit";
  if (dueInDays <= DUE_SOON_DAYS) return "warn";
  return "mut";
}
