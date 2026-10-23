// "Before expected spend can be projected": what the budget relies on
// elsewhere (docs/design/cross-module-guidance.md §2, IT budget).
//
//   4  SUBSCRIPTIONS OR LICENSES ARE RECORDED. Expected spend is projected
//      from exactly those two registries (`budget/feeds.py`): with neither,
//      the budget shows only what someone typed in by hand, and no renewal
//      will ever appear as a coming cost. Advisory.
//
// Needs two read permissions, one per registry. The row is "cannot be
// checked" if either is missing: one empty registry proves nothing when the
// other could not be looked at.
//
// Rows 1-3 are already guidance on this page: "No budget yet" links to the
// Planner, the expense pickers link to budget settings, and the unconvertible
// notice links to the exchange rates.
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { useQuery } from "@tanstack/react-query";

import { combineQueries, prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import type { ApiError } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { mayCheck } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import type { LicenseOut } from "../licenses/types";
import type { SubscriptionOut } from "../subscriptions/types";

const SUBSCRIPTIONS_READ = "subscriptions.read";
const LICENSES_READ = "licenses.read";

export function BudgetReadiness() {
  const t = useT();
  const api = useApi();
  const me = useMePermissions();
  // The owning modules' keys and shapes (`useSubscriptions`, `useLicenses`).
  const subscriptions = useQuery<SubscriptionOut[], ApiError>({
    queryKey: ["subscriptions"],
    queryFn: () => api.listSubscriptions(),
    enabled: mayCheck(me, SUBSCRIPTIONS_READ),
    retry: false,
  });
  const licenses = useQuery<LicenseOut[], ApiError>({
    queryKey: ["licenses"],
    queryFn: () => api.listLicenses(),
    enabled: mayCheck(me, LICENSES_READ),
    retry: false,
  });

  const feedItem = prerequisiteFromQuery({
    label: t("budget.readiness.label"),
    query: combineQueries(subscriptions, licenses),
    // Whichever code the account lacks, so the sentence names the right one.
    permission: { code: me.lacks(SUBSCRIPTIONS_READ) ? SUBSCRIPTIONS_READ : LICENSES_READ, me },
    isDone: ([subs, lics]) => subs.length + lics.length > 0,
    done: ([subs, lics]) =>
      t("budget.readiness.done", {
        subscriptions: t("budget.readiness.subscriptions", { count: subs.length }),
        licenses: t("budget.readiness.licenses", { count: lics.length }),
      }),
    missing: {
      severity: "warn",
      detail: t("budget.readiness.missing"),
      cta: { to: "/subscriptions", label: t("budget.readiness.cta") },
    },
  });

  return (
    <Prerequisites
      title={t("budget.readiness.title")}
      subtitle={t("budget.readiness.subtitle")}
      items={[feedItem]}
    />
  );
}
