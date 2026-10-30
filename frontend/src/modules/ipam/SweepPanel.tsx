import { useState } from "react";
import { Link } from "react-router-dom";
import { Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import {
  useDispatchIpamSweepMutation,
  useIpamScanAuthorizations,
  useIpamSweepRuns,
  useSweeperChoices,
  useUpdateIpamSweepSettingsMutation,
} from "./api";
import { ErrorLine } from "./ErrorLine";
import { INSTALL_ON_SEGMENT_CTA } from "./IpamReadiness";
import {
  describeSweepRun,
  effectiveSweepScope,
  formatAge,
  lastSeenMeaning,
  sweepStatePresentation,
} from "./ipamState";
import { variantKind } from "./kinds";
import { humaniseEnum } from "../../lib/labels";
import type { SubnetOut } from "./types";
import { DEFAULT_SWEEP_INTERVAL_HOURS, MIN_SWEEP_INTERVAL_HOURS } from "./types";
import { Icon } from "../../shell/icons";
import { KV } from "../../shell/viz";
import { Btn, Panel, StatusPill } from "../../shell/components";

// The sweep panel: authorization state first, then the designated agent, the
// interval, and the run history including `no_agent`.
//
// AUTHORIZATION STATE COMES FIRST because it is the answer to the only question
// that matters before pressing a button that puts packets on somebody's wire.
// It is deny-by-default: no active `ipam_scan_authorization` covering this
// prefix means no sweep will ever target it, and having documented the subnet
// is NOT an authorization: filing and authorising are different decisions by
// different people.
//
// What this panel computes is a PREVIEW. `authorized_targets` recomputes the
// set server-side on every dispatch and refuses before any job row is created;
// this exists so an operator can see WHY a button will fail, and see which
// grant makes the segment sweepable, rather than reading a bare 403.
//
// THE INTERVAL FLOOR IS SHOWN, NOT JUST ENFORCED. A sub-hourly sweep interval
// is the exact mechanism by which an address plan becomes a ping monitor, and
// the API refuses anything shorter with a 400.
//
// THE DESIGNATED AGENT IS PICKED FROM THE SUBNET'S CLIENT'S MACHINES (MSP
// clients phase 2): two clients can both run 10.0.0.0/24 on unrelated wires,
// so the server refuses another client's machine and never borrows one to
// sweep. The picker offers only those the server would accept that the fleet
// list can show -- approved, with an agent -- and keeps a designation it cannot
// list, so saving the interval never silently clears it.
export function SweepPanel({
  subnet,
  canRun,
  canManage,
}: {
  subnet: SubnetOut;
  canRun: boolean;
  canManage: boolean;
}) {
  const t = useT();
  const scopes = useIpamScanAuthorizations();
  const runs = useIpamSweepRuns(subnet.id);
  const dispatch = useDispatchIpamSweepMutation();
  const settings = useUpdateIpamSweepSettingsMutation();
  const machines = useSweeperChoices(subnet.client_id, canManage);

  const [enabled, setEnabled] = useState(subnet.sweep_enabled);
  const [deviceId, setDeviceId] = useState(subnet.sweep_device_id ?? "");
  const [interval, setInterval] = useState(
    String(subnet.sweep_interval_hours ?? DEFAULT_SWEEP_INTERVAL_HOURS),
  );

  const scope = effectiveSweepScope(subnet, scopes.data ?? []);
  const latest = runs.data?.[0];
  const intervalNumber = Number(interval);
  const intervalTooShort =
    Number.isFinite(intervalNumber) && intervalNumber < MIN_SWEEP_INTERVAL_HOURS;

  return (
    <>
      <Panel
        title={t("ipam.sweep.title")}
        subtitle={t("ipam.sweep.subtitle")}
        actions={
          canRun ? (
            <Btn
              label={dispatch.isPending ? t("ipam.sweep.starting") : t("ipam.sweep.now")}
              icon="search"
              variant="primary"
              disabled={dispatch.isPending || subnet.kind !== "assignable"}
              onClick={() => dispatch.mutate(subnet.id)}
            />
          ) : undefined
        }
      >
        <div className={scope.authorized ? "notice gold" : "notice"} style={{ marginBottom: 14 }}>
          <Icon name={scope.authorized ? "alert-triangle" : "info"} size={16} />
          <div>
            <p style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span>{t("ipam.sweep.authorization")}</span>
              {scope.authorized ? (
                <StatusPill value="authorized" kind="warn" label={t("ipam.sweep.authorized")} />
              ) : (
                <StatusPill value="not-authorized" kind="mut" label={t("ipam.sweep.notAuthorized")} />
              )}
            </p>
            <p style={{ marginTop: 4 }}>{scope.explanation}</p>
            {scope.covering.length > 0 && (
              <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                {scope.covering.map((item) => (
                  <li key={item.id}>
                    <span className="mono">{item.prefix}</span>
                    {item.note ? <span> {item.note}</span> : null}
                  </li>
                ))}
              </ul>
            )}
            {/* THE WAY OUT OF "NOT AUTHORIZED". The sentence above says why the
                button will be refused; this is where it stops being refused.
                Only once the authorizations have answered: until then the
                pill above reads "Not authorized" for want of data, and a link
                offering to fix it would be offering to fix nothing. */}
            {scopes.isSuccess && !scope.authorized && (
              <p style={{ marginTop: 10 }}>
                <Link className="btn" to="/ipam/scan-authorizations">
                  {t("ipam.fields.authorize")}
                </Link>
              </p>
            )}
          </div>
        </div>

        <KV
          items={[
            [t("ipam.sweep.scheduled"), subnet.sweep_enabled ? t("ipam.sweep.enabled") : t("ipam.sweep.disabled")],
            [
              t("ipam.sweep.designatedAgent"),
              subnet.sweep_device_id ? (
                <span className="mono">{subnet.sweep_device_id}</span>
              ) : (
                t("ipam.sweep.autoSelectedNone")
              ),
            ],
            [
              t("ipam.sweep.interval"),
              <>
                <span className="mono">{subnet.sweep_interval_hours ?? DEFAULT_SWEEP_INTERVAL_HOURS} h</span>
                <span className="note">{t("ipam.sweep.floor", { hours: MIN_SWEEP_INTERVAL_HOURS })}</span>
              </>,
            ],
            [
              t("ipam.sweep.lastRun"),
              <span title={lastSeenMeaning()}>{describeSweepRun(latest)}</span>,
            ],
          ]}
        />
        {/* `no_agent` WITH ITS REMEDY. "Nothing could look" is the one outcome
            an operator cannot fix from this page, and the fix is the thing
            nobody guesses: a sweep runs from an enrolled agent inside the
            segment, never from the control plane (invariant 2), so the first
            machine on a new segment is installed by hand. */}
        {latest?.state === "no_agent" && (
          <div role="status" className="notice gold" style={{ marginTop: 14 }}>
            <Icon name="alert-triangle" size={16} />
            <div>
              <p>{t("ipam.sweep.noAgentNotice")}</p>
              <p style={{ marginTop: 10 }}>
                <Link className="btn" to={INSTALL_ON_SEGMENT_CTA.to}>
                  {INSTALL_ON_SEGMENT_CTA.label}
                </Link>
              </p>
            </div>
          </div>
        )}
        {dispatch.isError && <ErrorLine error={dispatch.error} />}

        {canManage && (
          <div style={{ marginTop: 16 }}>
            <div className="form-grid">
              <Field
                label={t("ipam.sweep.designatedAgentField")}
                help={t("ipam.sweep.designatedAgentHelp")}
              >
                <select
                  className={inputClass}
                  value={deviceId}
                  onChange={(e) => setDeviceId(e.target.value)}
                >
                  <option value="">{t("ipam.sweep.designatedAgentAuto")}</option>
                  {deviceId !== "" && !(machines.data ?? []).some((d) => d.id === deviceId) && (
                    <option value={deviceId}>{deviceId}</option>
                  )}
                  {machines.data?.map((machine) => (
                    <option key={machine.id} value={machine.id}>
                      {machine.hostname || machine.primary_ip || machine.id}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={t("ipam.sweep.intervalField")}>
                <input
                  className={inputClass}
                  type="number"
                  min={MIN_SWEEP_INTERVAL_HOURS}
                  value={interval}
                  onChange={(e) => setInterval(e.target.value)}
                />
              </Field>
              <label className="full" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => setEnabled(e.target.checked)}
                />
                {t("ipam.sweep.scheduledEnabled")}
              </label>
            </div>
            {intervalTooShort && (
              <p className="form-error" style={{ marginTop: 10 }}>
                {t("ipam.sweep.intervalTooShort", { count: MIN_SWEEP_INTERVAL_HOURS })}
              </p>
            )}
            <div className="form-actions">
              <Btn
                label={t("ipam.sweep.save")}
                icon="check"
                variant="primary"
                disabled={settings.isPending || intervalTooShort}
                onClick={() =>
                  settings.mutate({
                    subnetId: subnet.id,
                    body: {
                      sweep_enabled: enabled,
                      sweep_device_id: deviceId || null,
                      sweep_interval_hours: interval === "" ? null : Number(interval),
                    },
                  })
                }
              />
            </div>
            {settings.isError && <ErrorLine error={settings.error} />}
          </div>
        )}
      </Panel>

      <Panel title={t("ipam.sweep.runsTitle")} subtitle={t("ipam.sweep.runsSubtitle")} bare>
        {runs.data && runs.data.length === 0 ? (
          <div className="empty">
            <Icon name="search" size={26} />
            <b>{t("ipam.sweep.runsEmpty")}</b>
          </div>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>{t("ipam.sweep.colOutcome")}</Th>
                <Th>{t("ipam.sweep.colTrigger")}</Th>
                <Th>{t("ipam.sweep.colTargets")}</Th>
                <Th>{t("ipam.sweep.colAnswered")}</Th>
                <Th>{t("ipam.sweep.colStarted")}</Th>
              </tr>
            </thead>
            <tbody>
              {runs.data?.map((run) => {
                const presentation = sweepStatePresentation(run.state);
                const kind = variantKind(presentation.variant);
                return (
                  <tr key={run.id}>
                    <Td
                      lead={<Icon name="search" size={18} />}
                      gutter={kind === "crit" || kind === "warn" ? kind : undefined}
                    >
                      <span title={presentation.meaning}>
                        <StatusPill value={run.state} kind={kind} label={presentation.label} />
                      </span>
                      {run.error ? <div className="cell-sub">{run.error}</div> : null}
                    </Td>
                    <Td>{humaniseEnum(run.trigger, "SweepTrigger")}</Td>
                    <Td>
                      <span className="mono">{run.target_count}</span>
                    </Td>
                    <Td>
                      <span className="mono">{run.hosts_found}</span>
                      {run.truncated ? (
                        <span style={{ marginLeft: 6 }}>
                          <StatusPill value="truncated" kind="warn" label={t("ipam.sweep.truncated")} />
                        </span>
                      ) : null}
                    </Td>
                    <Td>
                      <span className="mono">{formatAge(run.started_at)}</span>
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Panel>
    </>
  );
}
