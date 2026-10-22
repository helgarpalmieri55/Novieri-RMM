// "Before the assistant can act": what the ceilings on this page need from
// elsewhere to govern anything (`docs/design/cross-module-guidance.md` §2,
// AI automation).
//
//   1  the assistant is switched on -- with no model provider configured on
//      the server it proposes nothing, whatever the ceilings say. It is set in
//      the server's configuration; Assistant settings says what is missing;
//   2  approved actions exist -- act mode proposes only actions from the
//      approved-action registry (`core/ai/act.py`), kept on the Alerts page,
//      so with none there is nothing for a ceiling to allow.
//
// Both are blocking for this page's purpose. NOTHING HERE GATES ANYTHING
// (invariant 6).

import { useQuery } from "@tanstack/react-query";

import { prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import type { ApiError } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { mayCheck } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import { AI_STATUS_KEY } from "../ai-admin/api";
import type { AiStatus } from "../ai-admin/types";
import type { ApprovedActionOut } from "../health/types";

const AI_USE = "ai.use";
const HEALTH_READ = "health.read";

export function AutomationPrerequisites() {
  const t = useT();
  const api = useApi();
  const me = useMePermissions();
  // The keys the assistant's and the Alerts page's own reads use.
  const status = useQuery<AiStatus, ApiError>({
    queryKey: [AI_STATUS_KEY],
    queryFn: () => api.getAiStatus(),
    enabled: mayCheck(me, AI_USE),
    retry: false,
  });
  const actions = useQuery<ApprovedActionOut[], ApiError>({
    queryKey: ["approved-actions"],
    queryFn: () => api.listApprovedActions(),
    enabled: mayCheck(me, HEALTH_READ),
    retry: false,
  });

  const statusItem = prerequisiteFromQuery({
    label: t("aiAutomation.prerequisites.statusLabel"),
    query: status,
    permission: { code: AI_USE, me },
    isDone: (s) => s.enabled,
    done: t("aiAutomation.prerequisites.statusDone"),
    missing: (s) => ({
      severity: "crit",
      detail: t("aiAutomation.prerequisites.statusMissing", { reason: s.reason }),
      cta: { to: "/assistant/settings", label: t("aiAutomation.prerequisites.statusCta") },
    }),
  });

  const actionsItem = prerequisiteFromQuery({
    label: t("aiAutomation.prerequisites.actionsLabel"),
    query: actions,
    permission: { code: HEALTH_READ, me },
    isDone: (list) => list.length > 0,
    done: (list) => t("aiAutomation.prerequisites.actionsDone", { count: list.length }),
    missing: {
      severity: "crit",
      detail: t("aiAutomation.prerequisites.actionsMissing"),
      cta: { to: "/health#approved-actions", label: t("aiAutomation.prerequisites.actionsCta") },
    },
  });

  return (
    <Prerequisites
      title={t("aiAutomation.prerequisites.title")}
      subtitle={t("aiAutomation.prerequisites.subtitle")}
      items={[statusItem, actionsItem]}
    />
  );
}
