import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { formatDateTime } from "../../lib/datetime";
import { Panel, QueryState, StatusPill } from "../../shell/components";
import { KV } from "../../shell/viz";
import { Icon } from "../../shell/icons";
import { useDevicePatchStatus, usePatchRuns, usePatchUpdates, usePatchZone } from "./api";
import { DeviceOutcome } from "./DeviceOutcome";
import { LastApplyCard } from "./LastApplyCard";
import { restartLabel, restartSentence } from "./restartCopy";
import type { PatchAppSource } from "./types";
import { patchStateKind, patchStateLabel, severityKind, severityLabel } from "./patchState";

/**
 * One device's patch posture: three count cards, and then the actual list.
 *
 * THE (device, patch) DETAIL BELONGS HERE. It used to be on the fleet page, as
 * an "Exposed devices" table of one row per pair across the whole org — which
 * is why one machine missing 300 packages made the fleet view unreadable. The
 * fleet page now rolls up to one row per update and one row per device; the
 * per-machine list is this, fetched for this machine alone.
 */
export function DevicePatchTab({ deviceId }: { deviceId: string }) {
  const t = useT();
  const status = useDevicePatchStatus(deviceId);
  const runs = usePatchRuns(deviceId);
  const countsByState = status.data?.counts_by_state ?? {};
  const countsBySeverity = status.data?.counts_by_severity ?? {};
  // No severity filter here, ever: this is the machine's own list, and the
  // fleet view's default narrowing must not follow the reader down into it.
  const updates = usePatchUpdates({ device_id: deviceId, severity: [] });
  const items = updates.data?.items ?? [];
  const total = updates.data?.total ?? 0;
  const lastScanAt = status.data?.last_scan_at ?? null;
  const restart = status.data?.restart ?? null;
  const openRestart =
    restart != null && ["pending", "dispatched", "scheduled"].includes(restart.state);
  const zone = usePatchZone();
  const appSources = status.data?.app_sources ?? [];

  return (
    <div className="p-4">
      {/* WHAT THE LAST APPLY DID, above the standing counts. The three cards
          below state the machine's POSITION, which is the durable truth and
          cannot say that a run happened: a patch that failed on Tuesday and
          installed on Wednesday shows as `installed`, with nothing anywhere
          recording Tuesday. `patch_runs` has held that record since the module
          shipped and no route read it. */}
      <LastApplyCard runs={runs} />
      <div className="grid grid-3">
        <Panel title={t("patch.deviceTab.restart")}>
          {/* "Up to date" claimed something this endpoint does not know:
              `pending_reboot: false` means no patch has asked for a restart, not
              that the device is patched. The honest negative is the absence of
              the thing the field actually reports. */}
          {openRestart && restart ? (
            <>
              <StatusPill
                value={restart.state}
                kind={restartLabel(restart.state).kind}
                label={restartLabel(restart.state).label}
              />
              <div className="note mt-2">{restartSentence(restart, zone)}</div>
            </>
          ) : status.data?.pending_reboot ? (
            <StatusPill value="pending" kind="warn" label={t("patch.deviceTab.restartPending")} />
          ) : (
            <StatusPill value="ok" kind="ok" label={t("patch.deviceTab.noRestart")} />
          )}
          {/* WHEN THIS MACHINE LAST ANSWERED. The counts beside it are
              unreadable without it: an all-zero "By state" is the same shape on
              a fully patched machine and on one that has never reported. */}
          <div className="note mt-2">
            {lastScanAt
              ? t("patch.deviceTab.lastScan", { when: formatDateTime(lastScanAt, zone) })
              : t("patch.deviceTab.neverScanned")}
          </div>
        </Panel>
        <Panel title={t("patch.deviceTab.byState")}>
          {Object.keys(countsByState).length === 0 ? (
            <p className="note">{t("patch.deviceTab.byStateEmpty")}</p>
          ) : (
            <KV
              items={Object.entries(countsByState).map(([state, count]) => [
                <StatusPill
                  key={state}
                  value={state}
                  kind={patchStateKind(state)}
                  label={patchStateLabel(state)}
                />,
                <span className="mono">{count}</span>,
              ])}
            />
          )}
        </Panel>
        <Panel title={t("patch.deviceTab.bySeverity")}>
          {Object.keys(countsBySeverity).length === 0 ? (
            <p className="note">{t("patch.deviceTab.byStateEmpty")}</p>
          ) : (
            <KV
              items={Object.entries(countsBySeverity).map(([severity, count]) => [
                <StatusPill
                  key={severity}
                  value={severity}
                  kind={severityKind(severity)}
                  label={severityLabel(severity)}
                />,
                <span className="mono">{count}</span>,
              ])}
            />
          )}
        </Panel>
      </div>

      <div className="mt-4">
        <Panel
          title={t("patch.deviceTab.outstanding")}
          subtitle={total > 0 ? t("patch.deviceTab.onDevice", { count: total }) : undefined}
          bare
        >
          <QueryState
            loading={updates.isLoading}
            error={updates.error}
            empty={updates.data != null && items.length === 0}
            emptyIcon="patch"
            emptyLabel={
              lastScanAt == null
                ? t("patch.deviceTab.emptyNever")
                : t("patch.deviceTab.emptyNone")
            }
          >
            <Table>
              <thead>
                <tr>
                  <Th>{t("patch.deviceTab.update")}</Th>
                  <Th>{t("patch.deviceTab.severity")}</Th>
                  <Th>{t("patch.deviceTab.outcome")}</Th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <tr key={row.id}>
                    <Td lead={<Icon name="patch" size={18} />} gutter={severityKind(row.severity)}>
                      <div className="cell-title">{row.title}</div>
                      <div className="cell-sub mono">{row.identifier}</div>
                    </Td>
                    <Td>
                      <StatusPill
                        value={row.severity}
                        kind={severityKind(row.severity)}
                        label={severityLabel(row.severity)}
                      />
                    </Td>
                    {/* WHAT HAPPENED TO IT HERE. The column used to carry
                        fleet context and could not: this query is scoped to one
                        device, so `devices_outstanding` is always 1 and the cell
                        always printed "only this device". What it says now is
                        the thing the list could not say at all: a `failed` row
                        and an untried one were the same three words. */}
                    <Td>
                      <DeviceOutcome update={row} />
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            {total > items.length && (
              <p className="note px-4.5 py-2.5">
                {t("patch.deviceTab.showingFirst", { count: items.length, total })}
              </p>
            )}
          </QueryState>
          {appSources.map((row) => (
            <p key={row.source} className="note px-4.5 py-2.5">
              {appSourceLine(row)}
            </p>
          ))}
        </Panel>
      </div>
    </div>
  );
}

const MANAGER_NAME: Record<string, string> = { winget: "winget", homebrew: "Homebrew" };

/** "Apps from winget were checked." / "Apps from Homebrew could not be checked: …" */
export function appSourceLine(row: PatchAppSource): string {
  const name = MANAGER_NAME[row.source] ?? row.source;
  if (row.state === "scanned") return translate("patch.deviceTab.appChecked", { name });
  // The server's own words, appended after a colon when it gave any.
  const detail = row.detail ? `: ${row.detail}` : "";
  return translate(
    row.state === "failed" ? "patch.deviceTab.appCouldNot" : "patch.deviceTab.appNotAvailable",
    { name, detail },
  );
}
