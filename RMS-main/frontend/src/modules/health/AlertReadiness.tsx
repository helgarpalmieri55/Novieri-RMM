// "Before alerts can reach anyone": what has to be set up for this page to
// have alerts on it at all, and for them to reach a person.
//
// THE OPERATOR ASKED FOR IT (2026-09-24, `docs/design/cross-module-guidance.md`
// §2 Alerts, §6 #4). The case that made it a priority: an organisation with no
// health profile assigned monitors nothing (`health/baseline.py`: "A fresh org
// monitors nothing until someone writes a profile"; `health/api.py` answers
// every agent with an explicit disarm). The alert list is then empty, and an
// empty alert list reads as a healthy fleet.
//
//   1  a computer runs the agent -- alerts come from checks it runs;
//   2  a health profile is assigned, to the organisation, a group or a device;
//   3  a notification rule sends alerts to someone.
//
// "Alerts can open tickets" is NOT here: the health settings panel lower on
// this page already says so, and now links to where it is fixed, rather than
// this page saying it twice. "Remediation actions exist" is not here either:
// no console screen creates one yet, so there is nowhere to send anyone.
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { useQuery } from "@tanstack/react-query";

import { prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { ApiError } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { mayCheck, useFleetPrerequisite, useRulesPrerequisite } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import type { AssignmentOut } from "./types";

export const HEALTH_READ = "health.read";

/** Where profiles are assigned: the Profiles panel on this page. */
// The label is an accessor over the catalogue, so it follows a language switch.
export const ASSIGN_PROFILE_CTA = {
  to: "/health#profiles",
  get label(): string {
    return translate("health.readiness.assignCta");
  },
};

export function AlertReadiness() {
  const t = useT();
  const api = useApi();
  const me = useMePermissions();

  const agentItem = useFleetPrerequisite("agent", {
    severity: "crit",
    missingDetail: t("health.readiness.agentMissing"),
  });

  // Same key and shape as the Profiles panel's own `useHealthAssignments()`,
  // so an assignment made there clears this row.
  const assignments = useQuery<AssignmentOut[], ApiError>({
    queryKey: ["health-assignments", null],
    queryFn: () => api.listHealthAssignments(),
    enabled: mayCheck(me, HEALTH_READ),
    retry: false,
  });
  const profileItem = prerequisiteFromQuery<AssignmentOut[]>({
    label: t("health.readiness.profileLabel"),
    query: assignments,
    permission: { code: HEALTH_READ, me },
    isDone: (list) => list.length > 0,
    done: (list) => t("health.readiness.profileDone", { count: list.length }),
    missing: {
      severity: "crit",
      detail: t("health.readiness.profileMissing"),
      cta: ASSIGN_PROFILE_CTA,
    },
  });

  const rulesItem = useRulesPrerequisite("health.alert", {
    label: t("health.readiness.rulesLabel"),
    missingDetail: t("health.readiness.rulesMissing"),
    ctaLabel: t("health.readiness.rulesCta"),
  });

  return (
    <Prerequisites
      title={t("health.readiness.title")}
      subtitle={t("health.readiness.subtitle")}
      items={[agentItem, profileItem, rulesItem]}
    />
  );
}
