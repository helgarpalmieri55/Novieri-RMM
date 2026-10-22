// "Before subscriptions are fully tracked": what the register relies on
// elsewhere (docs/design/cross-module-guidance.md §2, Subscriptions).
//
//   3  SEAT HOLDERS ARE MATCHED TO PEOPLE. A seat whose holder is not linked
//      to a person drops out of "who has what" and out of an offboarding
//      checklist. Advisory.
//   4  RENEWAL AND NOTICE REMINDERS REACH SOMEONE. The platform raises an
//      event as a renewal or a notice deadline approaches; with no active
//      notification rule for them, nobody hears it and the renewal date is
//      found on the invoice. Advisory.
//
// Rows 1 and 2 (vendors, owners) are links beside the pickers in the New
// subscription dialog, where they are needed.
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import { mayCheck, useRulesPrerequisite } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import { useUnmatched } from "../people/api";

const PEOPLE_READ = "people.read";

export function SubscriptionReadiness() {
  const t = useT();
  const me = useMePermissions();
  const unmatched = useUnmatched(mayCheck(me, PEOPLE_READ));

  const seatItem = prerequisiteFromQuery({
    label: t("subscriptions.readiness.seatLabel"),
    query: unmatched,
    permission: { code: PEOPLE_READ, me },
    isDone: (data) => data.seats.length === 0,
    done: t("subscriptions.readiness.seatDone"),
    missing: (data) => ({
      severity: "warn",
      detail: t("subscriptions.readiness.seatMissing", { count: data.seats.length }),
      cta: { to: "/people/unmatched", label: t("subscriptions.readiness.seatCta") },
    }),
  });

  const reminderItem = useRulesPrerequisite("subscription.", {
    label: t("subscriptions.readiness.reminderLabel"),
    missingDetail: t("subscriptions.readiness.reminderMissing"),
    ctaLabel: t("subscriptions.readiness.reminderCta"),
  });

  return (
    <Prerequisites
      title={t("subscriptions.readiness.title")}
      subtitle={t("subscriptions.readiness.subtitle")}
      items={[seatItem, reminderItem]}
    />
  );
}
