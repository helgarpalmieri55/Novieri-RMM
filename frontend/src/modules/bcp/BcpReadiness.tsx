// "Before continuity tests prove anything": what BCP/DR relies on elsewhere in
// the console (docs/design/cross-module-guidance.md §2, Continuity tests).
//
//   3  BACKUPS ARE SET UP. Recording a test freezes a backup-evidence snapshot
//      for the plan's scope (`TestRecorder`); with no enabled backup policy
//      that snapshot can only ever say "nothing is protected". Advisory: a
//      tabletop exercise needs no backup at all.
//   4  SOMEONE CAN SIGN OFF A PLAN'S REVIEW. `POST /bcp/plans/{id}/review`
//      needs plan-review permission; without anyone holding it every plan
//      drifts into "review overdue" with no way out. `bcp/service.py`
//      `review_plan` allows an author to review their own plan, so one holder
//      is enough (unlike Changes, where the requester may not approve).
//      `assigned_users` counts assignments, not people, which cannot turn a
//      zero into a non-zero, so the check is exact for this question.
//
// The two other rows for this page are handled elsewhere: recovery procedures
// already link to IT documentation from the plan editor, and "devices or
// groups to scope a plan to" is a link beside each empty picker in the same
// editor, where the operator meets it.
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { useQuery } from "@tanstack/react-query";

import { prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import type { ApiError } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { mayCheck } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import type { BackupPolicy } from "../backup/types";
import { useRoles } from "../settings/api";
import { ROLES_READ } from "../settings/RolesTab";
import type { RoleOut } from "../settings/types";
import { settingsHref } from "../settings/tabs";

const BACKUP_READ = "backup.read";
const BCP_APPROVE = "bcp.approve";

/** Role assignments that allow signing off a plan's review. */
export function reviewerAssignments(roles: readonly Pick<RoleOut, "codes" | "assigned_users">[]): number {
  return roles
    .filter((role) => role.codes.includes(BCP_APPROVE))
    .reduce((total, role) => total + role.assigned_users, 0);
}

/** Same key and shape as the backup module's `useBackupPolicies`, so the cache is shared. */
function useBackupPoliciesCheck(enabled: boolean) {
  const api = useApi();
  return useQuery<BackupPolicy[], ApiError>({
    queryKey: ["backup-policies"],
    queryFn: () => api.listBackupPolicies(),
    enabled,
    retry: false,
  });
}

export function BcpReadiness() {
  const t = useT();
  const me = useMePermissions();
  const policies = useBackupPoliciesCheck(mayCheck(me, BACKUP_READ));
  const roles = useRoles(mayCheck(me, ROLES_READ));

  const backupItem = prerequisiteFromQuery({
    label: t("bcp.readiness.backupLabel"),
    query: policies,
    permission: { code: BACKUP_READ, me },
    isDone: (list) => list.some((policy) => policy.enabled),
    done: (list) => t("bcp.readiness.backupDone", { count: list.filter((policy) => policy.enabled).length }),
    missing: {
      severity: "warn",
      detail: t("bcp.readiness.backupMissing"),
      cta: { to: "/backup/policies", label: t("bcp.readiness.backupCta") },
    },
  });

  const reviewerItem = prerequisiteFromQuery({
    label: t("bcp.readiness.reviewerLabel"),
    query: roles,
    permission: { code: ROLES_READ, me },
    isDone: (list) => reviewerAssignments(list.roles) > 0,
    done: (list) => t("bcp.readiness.reviewerDone", { count: reviewerAssignments(list.roles) }),
    missing: {
      severity: "warn",
      detail: t("bcp.readiness.reviewerMissing"),
      cta: { to: settingsHref("roles"), label: t("bcp.readiness.reviewerCta") },
    },
  });

  return (
    <Prerequisites
      title={t("bcp.readiness.title")}
      subtitle={t("bcp.readiness.subtitle")}
      items={[backupItem, reviewerItem]}
    />
  );
}
