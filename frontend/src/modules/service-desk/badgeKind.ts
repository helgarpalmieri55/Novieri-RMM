import type { Kind } from "../../shell/components";
import type { BadgeVariant } from "../jobs/jobState";

/**
 * A badge variant as the record head's status kind (`DetailHead`'s `status`),
 * the same table `components/ui.tsx`'s `Badge` paints by. One copy for the
 * service desk's ticket and the portal's (redesign plan, Task 7, fix round
 * 1). A file of its own and of types only, so the portal imports it without
 * the service desk module that its own state file stays apart from.
 */
export const BADGE_KIND: Readonly<Record<BadgeVariant, Kind>> = {
  muted: "mut",
  info: "info",
  success: "ok",
  destructive: "crit",
  warning: "warn",
};
