// "Before the dashboard fills in": what other modules have to be doing for
// the widgets below to show anything but zeros.
//
// THE OPERATOR ASKED FOR IT (2026-09-24, `docs/design/cross-module-guidance.md`
// §2 Dashboard). Two rows, both advisory -- the dashboard works either way:
//
//   1  a computer runs the agent (its widgets read agent data);
//   2  backups are scheduled -- an enabled backup policy exists.
//
// The third row in the doc, "a patch scan has run", is not a row here: the
// Patch compliance panel already says it, and its link now reads "Scan for
// updates" when no scan has reported, so it is said once, where it applies.
//
// ONE DELIBERATE DIFFERENCE FROM EVERY OTHER PANEL: a row this account cannot
// check is LEFT OUT, not shown as "cannot be checked". The dashboard's
// convention is that each widget exists only for an account holding its read
// permission (and an account without `devices.read` lands here by default,
// `App.tsx`), so a row about a widget the reader never sees would be a
// standing "cannot be checked" on their home page with nothing behind it.
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { useQuery } from "@tanstack/react-query";

import { prerequisiteFromQuery, Prerequisites, type Prerequisite } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { DEVICES_READ, useFleetPrerequisite } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import type { BackupPolicy } from "../backup/types";

export const BACKUP_READ = "backup.read";

export function DashboardReadiness() {
  const t = useT();
  const api = useApi();
  const me = useMePermissions();

  const agentItem = useFleetPrerequisite("agent", {
    missingDetail: t("dashboard.readiness.agentMissing"),
  });

  // `["backup-policies"]`, the Backup policies page's own key and shape.
  const policies = useQuery<BackupPolicy[]>({
    queryKey: ["backup-policies"],
    queryFn: () => api.listBackupPolicies(),
    enabled: me.has(BACKUP_READ),
    retry: false,
  });
  const backupItem = prerequisiteFromQuery<BackupPolicy[]>({
    label: t("dashboard.readiness.backupLabel"),
    query: policies,
    permission: { code: BACKUP_READ, me },
    isDone: (list) => list.some((policy) => policy.enabled),
    done: t("dashboard.readiness.backupDone"),
    missing: {
      severity: "warn",
      detail: t("dashboard.readiness.backupMissing"),
      cta: { to: "/backup/policies", label: t("dashboard.readiness.backupCta") },
    },
  });

  const items: Prerequisite[] = [
    ...(me.has(DEVICES_READ) ? [agentItem] : []),
    ...(me.has(BACKUP_READ) ? [backupItem] : []),
  ];

  return (
    <Prerequisites
      title={t("dashboard.readiness.title")}
      subtitle={t("dashboard.readiness.subtitle")}
      items={items}
    />
  );
}
