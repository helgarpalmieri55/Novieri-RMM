import { EmptyState, NoValue } from "../../components/emptystate";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { humaniseEnum } from "../../lib/labels";
import { Table, Td, Th } from "../../components/ui";
import { formatDateTime } from "../../lib/datetime";
import { Icon } from "../../shell/icons";
import { StatusPill } from "../../shell/components";
import { useDeviceDeployments, useUserTimezone } from "./api";
import { deploymentResultStateKind } from "./deployState";

// The wire word of each action, lower case as the table has always printed it.
const ACTION_WORDS: Record<string, MessageKey> = {
  install: "deploy.device.action.install",
  update: "deploy.device.action.update",
  uninstall: "deploy.device.action.uninstall",
};

export function DeviceDeployTab({ deviceId }: { deviceId: string }) {
  const t = useT();
  const history = useDeviceDeployments(deviceId);
  // This tab is mounted by the devices module with only `deviceId`, so the
  // zone is read from the shared `["me"]` cache here rather than threaded
  // through a prop chain in a file this module does not own (cycle 5).
  const zone = useUserTimezone();

  /**
   * NEVER A HEADER ROW WITH A STUB ROW UNDER IT. The audit found six empty
   * states across six tabs of this one page and singled this one out: the
   * table and its six column headers were kept and the message stuffed into a
   * row, "leaving five trailing empty cells". Its fix note is explicit,
   * "Never leave a header row with a stub row beneath it", so the table goes
   * away entirely and the shared EmptyState takes its place, the same one the
   * other tabs use.
   */
  if (history.data && history.data.length === 0) {
    return (
      <EmptyState
        icon="package"
        title={t("deploy.device.emptyTitle")}
        body={t("deploy.device.emptyBody")}
      />
    );
  }

  return (
    <Table>
      <thead>
        <tr>
          <Th>{t("deploy.device.colDeployment")}</Th>
          <Th>{t("deploy.device.colPackage")}</Th>
          <Th>{t("deploy.device.colAction")}</Th>
          <Th>{t("deploy.device.colState")}</Th>
          <Th>{t("deploy.device.colExit")}</Th>
          <Th>{t("deploy.device.colFinished")}</Th>
        </tr>
      </thead>
      <tbody>
        {history.data?.map((entry) => {
          const kind = deploymentResultStateKind(entry.state);
          return (
            <tr key={entry.deployment_id}>
              <Td lead={<Icon name="package" size={18} />} gutter={kind}>
                <span className="mono">{entry.deployment_id}</span>
              </Td>
              <Td>
                <span className="mono">{entry.package_id}</span>
              </Td>
              <Td>{ACTION_WORDS[entry.action] ? t(ACTION_WORDS[entry.action] as MessageKey) : entry.action}</Td>
              <Td>
                <StatusPill value={entry.state} kind={kind} label={humaniseEnum(entry.state, "DeploymentResultState")} />
              </Td>
              <Td>
                {entry.exit_code == null ? (
                  <NoValue label={t("deploy.device.noExit")} />
                ) : (
                  <span className="mono">{entry.exit_code}</span>
                )}
              </Td>
              {/* `formatDateTime` returns NO_DATE for a deployment that has
                  not finished, so the ternary is gone with the ad-hoc
                  `toLocaleString()` that produced this device's second date
                  format. */}
              <Td>{formatDateTime(entry.finished_at, zone)}</Td>
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}
