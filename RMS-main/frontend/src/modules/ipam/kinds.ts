import type { BadgeVariant } from "../jobs/jobState";
import type { Kind } from "../../shell/viz";

/**
 * The module's `Presentation.variant` vocabulary as v3 kinds, so a
 * presentation table written against the previous design's badge names
 * drives a `StatusPill` without a second table beside it.
 */
const VARIANT_KIND: Record<BadgeVariant, Kind> = {
  muted: "mut",
  info: "info",
  success: "ok",
  destructive: "crit",
  warning: "warn",
};

export function variantKind(variant: BadgeVariant): Kind {
  return VARIANT_KIND[variant];
}
