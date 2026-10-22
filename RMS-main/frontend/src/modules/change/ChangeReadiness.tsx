// "Before changes can be approved and gated": what change management relies on
// elsewhere in the console (docs/design/cross-module-guidance.md §2, Changes).
//
//   1  SOMEONE OTHER THAN THE REQUESTER CAN APPROVE. `change_mgmt/state.py`
//      refuses "approver cannot be the change requester", so with fewer than
//      two people holding change approval, somebody's changes can never be
//      approved. Blocking.
//
//      APPROXIMATE, AND THE SCREEN SAYS SO. The only list that answers this is
//      `GET /roles`, whose `assigned_users` counts assignments per role, not
//      people: one person holding two roles that both carry approval counts
//      twice. An exact count needs a new endpoint (distinct holders of a
//      permission code), which the doc lists and this batch does not build.
//
//   2  A DEVICE GROUP EXISTS, so a change can be required on part of the fleet
//      rather than on every device or none (`GatesPanel`). Advisory.
//
// NOTHING HERE GATES ANYTHING (invariant 6). It reads lists the server already
// serves; approvals and gates are refused server-side on their own checks.

import { prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import { mayCheck, useGroupsPrerequisite } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import { useRoles } from "../settings/api";
import { ROLES_READ } from "../settings/RolesTab";
import type { RoleOut } from "../settings/types";
import { settingsHref } from "../settings/tabs";

const CHANGE_APPROVE = "change.approve";

/** Role assignments that carry change approval. An approximation of "people who can approve". */
export function approverAssignments(roles: readonly Pick<RoleOut, "codes" | "assigned_users">[]): number {
  return roles
    .filter((role) => role.codes.includes(CHANGE_APPROVE))
    .reduce((total, role) => total + role.assigned_users, 0);
}

export function ChangeReadiness() {
  const t = useT();
  const me = useMePermissions();
  const roles = useRoles(mayCheck(me, ROLES_READ));
  const approximation = t("change.readiness.approximation");

  const approverItem = prerequisiteFromQuery({
    label: t("change.readiness.approverLabel"),
    query: roles,
    permission: { code: ROLES_READ, me },
    isDone: (list) => approverAssignments(list.roles) >= 2,
    done: (list) => {
      const count = approverAssignments(list.roles);
      return t("change.readiness.approverDone", { count, approximation });
    },
    missing: (list) => {
      const count = approverAssignments(list.roles);
      return {
        severity: "crit",
        detail: t(count === 0 ? "change.readiness.approverNone" : "change.readiness.approverOne", { approximation }),
        cta: { to: settingsHref("roles"), label: t("change.readiness.approverCta") },
      };
    },
  });

  const groupItem = useGroupsPrerequisite({
    label: t("change.readiness.groupLabel"),
    missingDetail: t("change.readiness.groupMissing"),
  });

  return (
    <Prerequisites
      title={t("change.readiness.title")}
      subtitle={t("change.readiness.subtitle")}
      items={[approverItem, groupItem]}
    />
  );
}
