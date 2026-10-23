import { describeSubmitError } from "../../components/form";
import { NoValue } from "../../components/emptystate";
import { useT } from "../../i18n/context";
import { Badge, Button, Table, Td, Th } from "../../components/ui";
import { ApiError } from "../../lib/api";
import { formatDateTime } from "../../lib/datetime";
import { Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { Facts, KIND_ICON } from "../../shell/viz";
import {
  useCheckDeviceNowMutation,
  useDeviceHealth,
  useUserTimezone,
} from "./api";
import {
  ALERT_STATE_PRESENTATION,
  CHECK_STATE_PRESENTATION,
  SEVERITY_PRESENTATION,
  checkGlyph,
  checkLabel,
  checkStateBands,
  describeDeviceHealth,
  describeObservation,
  describeThreshold,
  formatSeconds,
  isAlertOpen,
  severityVariant,
} from "./healthState";

// One device's health (spec §13), reached from the device page. Gated on
// `devices.read` server-side, NOT `health.read`: a technician looking at a
// device needs its health without holding health administration rights.
//
// FOUR THINGS THIS PANEL REFUSES TO SAY, EACH BECAUSE IT WOULD BE GOOD NEWS
// THAT IS NOT TRUE:
//
//   1. It never says "healthy" for a device with NO EFFECTIVE PROFILE. Nothing
//      is being checked on it. `describeDeviceHealth` puts that branch first.
//   2. It never folds `unavailable` into the healthy band, and it never folds
//      `unknown` (never answered) into either. Four bands, always shown.
//   3. It never says "healthy" for a device that has gone QUIET. An offline
//      device does not clear its alerts, so the open alerts below are still
//      live and the header says so.
//   4. It names WHICH ASSIGNMENT produced the effective profile (spec §0.2) —
//"a resolution nobody can see is a resolution nobody trusts".

export function DeviceHealthPanel({
  deviceId,
  canCheckNow,
  onOpenAlert,
}: {
  deviceId: string;
  canCheckNow: boolean;
  onOpenAlert?: (alertId: string) => void;
}) {
  const t = useT();
  const health = useDeviceHealth(deviceId);
  const zone = useUserTimezone();
  const checkNow = useCheckDeviceNowMutation();

  // UNQUALIFIED, not `instanceof ApiError`: a transport failure is a plain
  // TypeError, fell past this branch, and left the panel on "Loading device
  // health…" — indefinitely, and indistinguishably from a slow server.
  if (health.error) {
    const status =
      health.error instanceof ApiError ? health.error.status : null;
    return (
      <Panel title={t("health.devicePanel.title")}>
        <p role="alert" className="note">
          {status === 403
            ? t("health.devicePanel.forbidden")
            : status === 404
              ? t("health.devicePanel.notFound")
              : describeSubmitError(health.error)}
        </p>
      </Panel>
    );
  }
  if (!health.data) {
    return (
      <Panel title={t("health.devicePanel.title")}>
        <div className="empty">
          <Icon name="clock" size={26} />
          <b>{t("health.devicePanel.loading")}</b>
        </div>
      </Panel>
    );
  }

  const device = health.data;
  const headline = describeDeviceHealth(device);
  const bands = checkStateBands(device.checks);
  const openAlerts = device.open_alerts.filter(isAlertOpen);

  return (
    <>
      <Panel
        title={t("health.devicePanel.healthOf", { host: device.hostname })}
        subtitle={headline.detail}
        actions={
          <>
            <Badge variant={headline.variant}>{headline.label}</Badge>
            <Badge variant={device.agent_online ? "success" : "warning"}>
              {device.agent_online ? t("health.devicePanel.agentReporting") : t("health.devicePanel.agentOffline")}
            </Badge>
          </>
        }
      >
        {!device.agent_online && (
          <div className="notice gold mb-3">
            <Icon name={KIND_ICON.warn} size={17} />
            <span>
              {t("health.devicePanel.quiet", {
                when: device.agent_last_seen_at
                  ? formatDateTime(device.agent_last_seen_at, zone)
                  : t("health.devicePanel.never"),
                window: formatSeconds(device.offline_after_s),
              })}
            </span>
          </div>
        )}

        {!device.has_profile ? (
          <div className="notice gold mb-3">
            <Icon name={KIND_ICON.warn} size={17} />
            {/* ONE BODY CHILD, because `.notice` is a flex row. Its shape is
              icon-then-body, so a `<strong>` and the sentence after it would
              become two flex items sitting side by side with a 10px gap. */}
            <span>
              <strong>{t("health.devicePanel.noProfileStrong")}</strong>{" "}
              {t("health.devicePanel.noProfileRest")}
            </span>
          </div>
        ) : (
          <>
            <Facts
              items={[
                { k: t("health.devicePanel.factProfile"), v: device.profile?.name },
                {
                  // Spec §0.2's requirement, in full: the level AND the target.
                  k: t("health.devicePanel.factAppliedBy"),
                  v:
                    device.resolution_level === "org"
                      ? t("health.devicePanel.appliedOrg")
                      : device.resolution_level === "group"
                        ? t("health.devicePanel.appliedGroup", { group: String(device.assignment?.device_group_id) })
                        : t("health.devicePanel.appliedDevice"),
                },
                {
                  k: t("health.devicePanel.factPolicy"),
                  v: (
                    <>
                      <span className="mono">
                        v{device.profile?.policy_version}
                      </span>
                      {device.policy_drift ? (
                        <>
                          {" "}
                          <Badge variant="warning">
                            {t("health.devicePanel.agentHas", {
                              version:
                                device.acknowledged_policy_version === null
                                  ? t("health.devicePanel.neverAcknowledged")
                                  : `v${device.acknowledged_policy_version}`,
                            })}
                          </Badge>
                        </>
                      ) : (
                        <>
                          {" "}
                          <Badge variant="success">{t("health.devicePanel.acknowledged")}</Badge>
                        </>
                      )}
                    </>
                  ),
                },
              ]}
            />

            {/* FOUR BANDS. `unavailable` and `unknown` are never folded into the
              healthy count (spec §0.7, §13). */}
            <div className="mb-3 flex flex-wrap gap-2">
              <Badge variant="success">{t("health.devicePanel.bandOk", { count: bands.ok })}</Badge>
              <Badge variant="destructive">{t("health.devicePanel.bandBreaching", { count: bands.breaching })}</Badge>
              <Badge variant="warning">{t("health.devicePanel.bandUnavailable", { count: bands.unavailable })}</Badge>
              <Badge variant="muted">{t("health.devicePanel.bandUnknown", { count: bands.unknown })}</Badge>
            </div>

            {canCheckNow && (
              <div className="mb-3">
                <Button
                  variant="outline"
                  disabled={checkNow.isPending}
                  onClick={() => checkNow.mutate(deviceId)}
                >
                  {t("health.devicePanel.sampleNow")}
                </Button>
                {/* A bodyless 503 made `describeError` return "" and this span
                  render EMPTY — the operator pressed Sample now, saw nothing
                  change, and had no way to tell a dispatched job from a
                  refused one. */}
                {checkNow.error ? (
                  <div role="alert" className="notice red mt-2">
                    <Icon name={KIND_ICON.crit} size={17} />
                    <div>
                      {t("health.devicePanel.sampleFailed", { detail: describeSubmitError(checkNow.error) })}
                    </div>
                  </div>
                ) : null}
              </div>
            )}

            {device.checks.length === 0 ? (
              <p className="note">{t("health.devicePanel.noChecks")}</p>
            ) : (
              <Table>
                <thead>
                  <tr>
                    <Th>{t("health.devicePanel.colCheck")}</Th>
                    <Th>{t("health.devicePanel.colState")}</Th>
                    <Th>{t("health.devicePanel.colLastValue")}</Th>
                    <Th>{t("health.devicePanel.colThreshold")}</Th>
                    <Th>{t("health.devicePanel.colReported")}</Th>
                  </tr>
                </thead>
                <tbody>
                  {device.checks.map((check) => {
                    const state = CHECK_STATE_PRESENTATION[check.state];
                    return (
                      <tr key={`${check.check_id}:${check.selector}`}>
                        <Td
                          lead={
                            <Icon
                              name={checkGlyph(check.check_type)}
                              size={18}
                            />
                          }
                          gutter={
                            check.state === "breaching"
                              ? "crit"
                              : check.state === "unavailable"
                                ? "warn"
                                : undefined
                          }
                        >
                          {/* ONE ALPHABET WITH THE ALERT CONSOLE. The row
                              read `disk.free_pct` while the console beside it
                              now reads "Disk free"; the key stays beneath as
                              the thing an operator greps for. */}
                          <div className="cell-title">
                            {checkLabel(check.check_type)}
                            {check.selector ? ` (${check.selector})` : ""}
                          </div>
                          <div className="cell-sub mono">{check.check_type}</div>
                        </Td>
                        <Td>
                          <Badge variant={state.variant}>{state.label}</Badge>
                          <div className="cell-sub">{state.meaning}</div>
                        </Td>
                        <Td>
                          {check.last_value ? (
                            <span className="mono">{check.last_value}</span>
                          ) : (
                            <NoValue label={t("health.devicePanel.noValue")} />
                          )}
                        </Td>
                        <Td>
                          <span className="mono">
                            {describeThreshold(check.comparator, check.threshold_display)}
                          </span>
                        </Td>
                        <Td>
                          {check.reported_at
                            ? formatDateTime(check.reported_at, zone)
                            : t("health.devicePanel.never")}
                        </Td>
                      </tr>
                    );
                  })}
                </tbody>
              </Table>
            )}
          </>
        )}
      </Panel>

      <Panel title={t("health.devicePanel.openAlerts")}>
        {openAlerts.length === 0 ? (
          <p className="note">
            {device.agent_online
              ? t("health.devicePanel.noOpenAlerts")
              : t("health.devicePanel.noOpenAlertsQuiet")}
          </p>
        ) : (
          <ul
            className="mini-list"
            style={{ listStyle: "none", margin: 0, padding: 0 }}
          >
            {openAlerts.map((alert) => (
              <li key={alert.id} className="mini-row">
                <span className="mini-icon">
                  <Icon name={checkGlyph(alert.check_type)} size={14} />
                </span>
                <span className="mini-copy">
                  <div className="mini-title">
                    {checkLabel(alert.check_type)}
                    {alert.selector ? ` (${alert.selector})` : ""}
                  </div>
                  <div className="mini-sub">{describeObservation(alert)}</div>
                </span>
                <Badge variant={severityVariant(alert.severity)}>
                  {SEVERITY_PRESENTATION[alert.severity].label}
                </Badge>
                <Badge variant={ALERT_STATE_PRESENTATION[alert.state].variant}>
                  {ALERT_STATE_PRESENTATION[alert.state].label}
                </Badge>
                {onOpenAlert && (
                  <Button variant="ghost" onClick={() => onOpenAlert(alert.id)}>
                    {t("health.devicePanel.open")}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </>
  );
}
