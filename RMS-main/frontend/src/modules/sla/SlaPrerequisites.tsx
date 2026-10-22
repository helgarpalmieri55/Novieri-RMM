// "Before a target can match a queue": what the service desk has to hold for
// the policies on this page to be aimed at part of the desk
// (`docs/design/cross-module-guidance.md` §2, Service levels).
//
// Ticket queues are the service desk's. Without one a policy can only match
// "Any queue" and a breach has nowhere to escalate to -- the policy form says
// so inside its pickers; this is the link out to where queues are made.
// Advisory: a policy still works on every queue.
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import { useMePermissions } from "../../lib/mePermissions";
import { useSlaTicketQueues } from "./api";

const TICKET_READ = "ticket.read";

export function SlaPrerequisites() {
  const t = useT();
  const me = useMePermissions();
  // The page's own queue read (same key), so this costs no request.
  const queues = useSlaTicketQueues();

  const queuesItem = prerequisiteFromQuery({
    label: t("sla.prerequisites.queueLabel"),
    query: queues,
    permission: { code: TICKET_READ, me },
    isDone: (list) => list.length > 0,
    done: (list) => t("sla.prerequisites.queueDone", { count: list.length }),
    missing: {
      severity: "warn",
      detail: t("sla.prerequisites.queueMissing"),
      cta: { to: "/service-desk/admin", label: t("sla.prerequisites.queueCta") },
    },
  });

  return (
    <Prerequisites
      title={t("sla.prerequisites.title")}
      subtitle={t("sla.prerequisites.subtitle")}
      items={[queuesItem]}
    />
  );
}
