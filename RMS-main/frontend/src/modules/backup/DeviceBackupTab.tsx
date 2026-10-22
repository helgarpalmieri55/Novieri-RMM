/**
 * A single device's backup status, on the device detail page, as v3's
 * `facts` strip with the Run control beside its title.
 *
 * CYCLE 4 + 5 CHANGED THREE THINGS HERE, and all three were the same defect
 * seen from different sides: the console asserting more than it knew.
 *
 *  - Six dates were rendered `new Date(x).toLocaleString()`, in the browser's
 *    zone and the browser's format. The same backup point shown on
 *    BackupPointsPage disagreed with this tab in both, which is the audit's
 *    "the same device reports two different last-contact times in two
 *    different date formats". They go through `formatDateTime` now, in the
 *    operator's profile zone.
 *  - A failed "Run now" rendered `String(run.error)`. A bodyless 503 carries
 *    `message === ""`, so that printed "Error: " into an unannounced <p>.
 *  - A SUCCESSFUL "Run now" said nothing at all. The mutation invalidates
 *    this device's query, but a run that dispatched no job changes nothing on
 *    screen, so success and "the server queued nothing" were the same
 *    non-event. `runQueuedMessage` names which one happened.
 */

import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { NoValue } from "../../components/emptystate";
import { Form, FormActions } from "../../components/form";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime } from "../../lib/datetime";
import { Panel, StatusPill } from "../../shell/components";
import { Facts } from "../../shell/viz";
import { useDeviceBackup, useRunBackupMutation } from "./api";
import { backupRunStateKind, backupRunStateLabel } from "./backupState";
import { runQueuedMessage } from "./runMessage";

export function DeviceBackupTab({ deviceId }: { deviceId: string }) {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canRun = (me.data?.permissions ?? []).includes("backup.run");
  // The operator's own zone (`/me`, cycle 2); null falls back to the browser's.
  const zone = me.data?.timezone;

  const status = useDeviceBackup(deviceId);
  const run = useRunBackupMutation();
  const data = status.data;

  // This tab is not remounted when the device detail page switches devices, so
  // without this the "Backup queued — 1 job dispatched." from the last device
  // sits here for the next one, claiming a run that was never requested for it
  // (the same defect DispatchJobDialog documents).
  useEffect(() => {
    run.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed on the
    // device only; `run` is a fresh object every render.
  }, [deviceId]);

  function submitRun() {
    // So a previous outcome cannot be read as this one.
    run.reset();
    run.mutate(deviceId);
  }

  return (
    <Panel
      title={t("backup.deviceTab.title")}
      actions={
        canRun ? (
          // A real <form>: `ui.tsx`'s Button sets no type, so there was no
          // submit control on this tab and Enter did nothing. `FormActions`
          // carries the failure (role="alert") and the confirmation
          // (role="status") that this control used to leave unsaid.
          <Form onSubmit={submitRun}>
            <FormActions
              label={t("backup.deviceTab.runNow")}
              pendingLabel={t("backup.deviceTab.running")}
              pending={run.isPending}
              error={run.error}
              saved={run.isSuccess}
              savedMessage={runQueuedMessage(run.data)}
              variant="outline"
            />
          </Form>
        ) : undefined
      }
    >
      <Facts
        items={[
          {
            k: t("backup.deviceTab.lastRunState"),
            v: data?.last_run_state ? (
              // The same humanised label BackupPointsPage renders. The raw
              // wire value here and a sentence-case one there is how one
              // module comes to contradict itself between two views.
              <StatusPill
                value={data.last_run_state}
                kind={backupRunStateKind(data.last_run_state)}
                label={backupRunStateLabel(data.last_run_state)}
              />
            ) : (
              <NoValue label={t("backup.deviceTab.noRun")} />
            ),
          },
          { k: t("backup.deviceTab.lastRunFinished"), v: formatDateTime(data?.last_run_finished_at, zone) },
          {
            k: t("backup.deviceTab.repository"),
            v: data?.repository?.name ?? <NoValue label={t("backup.deviceTab.noRepository")} />,
          },
          { k: t("backup.deviceTab.lastGoodPoint"), v: formatDateTime(data?.last_good_point?.taken_at, zone) },
          {
            k: t("backup.deviceTab.verified"),
            // The PILL keeps its words rather than a dash: "not verified" is a
            // state the operator must act on, and NO_DATE would read as "we do
            // not know", which is a different claim.
            v: (
              <StatusPill
                value={data?.verified_at ? "verified" : "unverified"}
                kind={data?.verified_at ? "ok" : "mut"}
                label={
                  data?.verified_at ? formatDateTime(data.verified_at, zone) : t("backup.deviceTab.notVerified")
                }
              />
            ),
          },
          {
            k: t("backup.deviceTab.restoreTested"),
            v: (
              <StatusPill
                value={data?.restore_tested_at ? "tested" : "untested"}
                kind={data?.restore_tested_at ? "ok" : "mut"}
                label={
                  data?.restore_tested_at
                    ? formatDateTime(data.restore_tested_at, zone)
                    : t("backup.deviceTab.notTested")
                }
              />
            ),
          },
        ]}
      />
    </Panel>
  );
}
