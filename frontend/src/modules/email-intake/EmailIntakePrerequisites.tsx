// "Before mail can become tickets": what the service desk and Settings have to
// hold for a mailbox on this page to turn mail into tickets
// (`docs/design/cross-module-guidance.md` §2, Email intake).
//
//   1  a ticket category exists -- every mailbox needs a default category
//      (`default_category_id` is required), so none means no mailbox can be
//      saved at all;
//   2  the people writing in are users -- mail from an address that is not a
//      user here is quarantined as "unknown sender" (`identity.py`), so a
//      helpdesk whose staff were never added quarantines their mail.
//
// The Microsoft 365 sign-in's missing public console address is already said
// where it bites, inside `MicrosoftConnection`, and is not repeated here.
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import { mayCheck } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import { useTicketCategories } from "../settings/api";
import { settingsHref } from "../settings/tabs";
import { useEmailIntakeMessages } from "./api";
import type { InboundMessageOut } from "./types";

const TICKET_READ = "ticket.read";
const EMAIL_INTAKE_READ = "email_intake.read";

/** How many recent quarantined messages the check reads. */
export const UNKNOWN_SENDER_WINDOW = 50;

/** The quarantine reason `identity.py` writes for a sender who is not a user. */
export function isUnknownSender(message: Pick<InboundMessageOut, "reason">): boolean {
  return message.reason.startsWith("unknown sender");
}

export function EmailIntakePrerequisites() {
  const t = useT();
  const me = useMePermissions();
  const categories = useTicketCategories(mayCheck(me, TICKET_READ));
  const quarantined = useEmailIntakeMessages({ disposition: "quarantined", limit: UNKNOWN_SENDER_WINDOW });

  const categoryItem = prerequisiteFromQuery({
    label: t("emailIntake.prerequisites.categoryLabel"),
    query: categories,
    permission: { code: TICKET_READ, me },
    isDone: (list) => list.length > 0,
    done: (list) => t("emailIntake.prerequisites.categoryDone", { count: list.length }),
    missing: {
      severity: "crit",
      detail: t("emailIntake.prerequisites.categoryMissing"),
      cta: { to: "/service-desk/admin", label: t("emailIntake.prerequisites.categoryCta") },
    },
  });

  const senderItem = prerequisiteFromQuery({
    label: t("emailIntake.prerequisites.senderLabel"),
    query: quarantined,
    permission: { code: EMAIL_INTAKE_READ, me },
    isDone: (log) => !log.messages.some(isUnknownSender),
    done: t("emailIntake.prerequisites.senderDone"),
    missing: (log) => {
      const senders = new Set(log.messages.filter(isUnknownSender).map((m) => m.from_address.toLowerCase()));
      return {
        severity: "warn",
        detail: t("emailIntake.prerequisites.senderMissing", { count: senders.size }),
        cta: { to: settingsHref("users"), label: t("emailIntake.prerequisites.senderCta") },
      };
    },
  });

  return (
    <Prerequisites
      title={t("emailIntake.prerequisites.title")}
      subtitle={t("emailIntake.prerequisites.subtitle")}
      items={[categoryItem, senderItem]}
    />
  );
}
