// "Before notifications can reach people": what the service desk and Settings
// have to hold for a rule on this page to have anyone to tell
// (`docs/design/cross-module-guidance.md` §2, Notifications).
//
//   1  more than one person can sign in -- a queue member or an assignee is a
//      user, and an organization of one has nobody else to notify;
//   2  a ticket queue exists -- queue-member rules route to a queue's members.
//
// Settings -> Email is NOT here, on purpose: a notification channel carries
// its own SMTP or Microsoft 365 sign-in (`notification/api.py`), and the
// realm's relay is only for invitations and password resets (doc §0.5).
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import { mayCheck } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import { useTicketQueues, useUsers } from "../settings/api";
import { settingsHref } from "../settings/tabs";

const USERS_READ = "users.read";
const TICKET_READ = "ticket.read";

export function NotificationPrerequisites() {
  const t = useT();
  const me = useMePermissions();
  const users = useUsers(mayCheck(me, USERS_READ));
  const queues = useTicketQueues(mayCheck(me, TICKET_READ));

  const usersItem = prerequisiteFromQuery({
    label: t("notification.prerequisites.usersLabel"),
    query: users,
    permission: { code: USERS_READ, me },
    isDone: (list) => list.users.filter((user) => user.status === "active").length > 1,
    done: (list) =>
      t("notification.prerequisites.usersDone", {
        count: list.users.filter((user) => user.status === "active").length,
      }),
    missing: {
      severity: "warn",
      detail: t("notification.prerequisites.usersMissing"),
      cta: { to: settingsHref("users"), label: t("notification.prerequisites.usersCta") },
    },
  });

  const queuesItem = prerequisiteFromQuery({
    label: t("notification.prerequisites.queuesLabel"),
    query: queues,
    permission: { code: TICKET_READ, me },
    isDone: (list) => list.length > 0,
    done: (list) => t("notification.prerequisites.queuesDone", { count: list.length }),
    missing: {
      severity: "warn",
      detail: t("notification.prerequisites.queuesMissing"),
      cta: { to: "/service-desk/admin", label: t("notification.prerequisites.queuesCta") },
    },
  });

  return (
    <Prerequisites
      title={t("notification.prerequisites.title")}
      subtitle={t("notification.prerequisites.subtitle")}
      items={[usersItem, queuesItem]}
    />
  );
}
