// "Before compliance can be counted": what the license register relies on
// elsewhere (docs/design/cross-module-guidance.md §2, Licenses).
//
//   2  INSTALLED-SOFTWARE INVENTORY EXISTS. Compliance compares entitlements
//      with installs, and installs come only from the agent's software
//      inventory (`licenses/matching.py`). With no computer running the agent,
//      every compliance figure is "no data" -- not "compliant". Blocking for
//      compliance.
//   3  INSTALLED SOFTWARE IS MAPPED TO PRODUCTS. Software the matcher could
//      not place sits in the review queue and counts against no license until
//      someone maps it. Advisory. It waits on 2: an empty queue on a fleet
//      with no inventory means nothing was found, not that everything is
//      mapped.
//
// Row 1 (products and vendors) is already a link beside the New license
// pickers. Row 4 (allocating seats) has no console control yet -- the
// allocation route exists but nothing calls it -- so it has no CTA to point
// at and is not drawn.
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { useQuery } from "@tanstack/react-query";

import { prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import type { ApiError } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { mayCheck, useFleetPrerequisite, useFleetQuery } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import type { ReviewQueueRow } from "./types";

const LICENSES_READ = "licenses.read";

export function LicenseReadiness() {
  const t = useT();
  const api = useApi();
  const me = useMePermissions();
  const fleet = useFleetQuery();
  // `useReviewQueue`'s key and shape, so mapping a title there refreshes this.
  const queue = useQuery<ReviewQueueRow[], ApiError>({
    queryKey: ["license-review-queue"],
    queryFn: () => api.reviewQueue(),
    enabled: mayCheck(me, LICENSES_READ),
    retry: false,
  });

  const inventoryItem = useFleetPrerequisite("agent", {
    label: t("licenses.readiness.inventoryLabel"),
    severity: "crit",
    missingDetail: t("licenses.readiness.inventoryMissing"),
    doneDetail:
      fleet.data !== undefined
        ? t("licenses.readiness.inventoryDone", { count: fleet.data.managed })
        : undefined,
  });

  const noInventoryYet = fleet.isSuccess && fleet.data.managed === 0;
  const mappingItem = prerequisiteFromQuery({
    label: t("licenses.readiness.mappingLabel"),
    query: queue,
    permission: { code: LICENSES_READ, me },
    waitingOn: noInventoryYet ? t("licenses.readiness.waitingOn") : null,
    isDone: (rows) => rows.length === 0,
    done: t("licenses.readiness.mappingDone"),
    missing: (rows) => ({
      severity: "warn",
      detail: t("licenses.readiness.mappingMissing", { count: rows.length }),
      cta: { to: "/licenses/review-queue", label: t("licenses.readiness.mappingCta") },
    }),
  });

  return (
    <Prerequisites
      title={t("licenses.readiness.title")}
      subtitle={t("licenses.readiness.subtitle")}
      items={[inventoryItem, mappingItem]}
    />
  );
}
