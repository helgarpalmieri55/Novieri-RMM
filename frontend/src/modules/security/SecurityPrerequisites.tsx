// "Before security events can be collected": the agent collects these only on
// Windows and macOS (`security_logs/schedule.py` SHIP_OS_FAMILIES: "Only these
// platforms have a collector"), so a fleet of Linux machines -- or no fleet --
// leaves this page empty however long anyone waits
// (`docs/design/cross-module-guidance.md` §2, Security logs).
//
// Counted with the device list's own filter: `state_counts` obeys the
// operating-system filter, and a computer runs the agent exactly when it is
// not in the `assets` tab (`all - assets`). NOTHING HERE GATES ANYTHING
// (invariant 6).

import { useQuery } from "@tanstack/react-query";

import { useT } from "../../i18n/context";
import { combineQueries, prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import type { ApiError, DevicePage } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { ADD_COMPUTER_CTA, DEVICES_READ, mayCheck } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";

/** A family the collector ships from, as `GET /devices?os_family=` spells it. */
export const COLLECTED_OS_FAMILIES = ["windows", "macos"] as const;

/** Computers of one family that run the agent. */
export function managedCount(page: DevicePage): number {
  return page.state_counts.all - page.state_counts.assets;
}

function useManagedOfFamily(osFamily: (typeof COLLECTED_OS_FAMILIES)[number]) {
  const api = useApi();
  const me = useMePermissions();
  return useQuery<DevicePage, ApiError>({
    // Under the `["devices", …]` prefix every device mutation invalidates.
    queryKey: ["devices", "managed-by-os", osFamily],
    queryFn: () => api.listDevices({ osFamily, limit: 1 }),
    enabled: mayCheck(me, DEVICES_READ),
    retry: false,
  });
}

export function SecurityPrerequisites() {
  const t = useT();
  const me = useMePermissions();
  const windows = useManagedOfFamily("windows");
  const macos = useManagedOfFamily("macos");

  const item = prerequisiteFromQuery({
    label: t("security.prerequisites.label"),
    query: combineQueries(windows, macos),
    permission: { code: DEVICES_READ, me },
    isDone: ([win, mac]) => managedCount(win) + managedCount(mac) > 0,
    // The count that picks the form is the computers, win + mac, so the noun
    // and verb agree with the number that is actually being counted.
    done: ([win, mac]) =>
      t("security.prerequisites.done", {
        count: managedCount(win) + managedCount(mac),
        windows: managedCount(win),
        mac: managedCount(mac),
      }),
    missing: {
      severity: "warn",
      detail: t("security.prerequisites.missing"),
      cta: { to: ADD_COMPUTER_CTA.to, label: t("security.prerequisites.cta") },
    },
  });

  return <Prerequisites title={t("security.prerequisites.title")} items={[item]} />;
}
