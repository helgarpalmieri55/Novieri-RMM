import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { NoValue } from "../../components/emptystate";
import { Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import { ApiError } from "../../lib/api";
import { AddressGrid } from "./AddressGrid";
import {
  useAllocateIpamAddressMutation,
  useIpamDrift,
  useIpamNextFree,
  useIpamPermissions,
  useIpamScanAuthorizations,
  useIpamSites,
  useIpamSubnet,
  useIpamSubnetUtilization,
  useIpamVlans,
} from "./api";
import { ErrorLine } from "./ErrorLine";
import { IpamNav } from "./IpamNav";
import {
  driftClassPresentation,
  effectiveSweepScope,
  formatAge,
  hasUtilization,
  isDriftFinding,
  lastSeenMeaning,
  severityVariant,
} from "./ipamState";
import { variantKind } from "./kinds";
import { RangesPanel } from "./RangesPanel";
import { SubnetActions } from "./SubnetActions";
import { SweepPanel } from "./SweepPanel";
import { UtilizationBar } from "./UtilizationBar";
import { MAX_NEXT_FREE } from "./types";
import type { DriftOut, SubnetOut } from "./types";
import { Icon } from "../../shell/icons";
import { Facts } from "../../shell/viz";
import { Btn, PageHead, Panel, StatusPill, Tabs } from "../../shell/components";

// One subnet: the facts strip, then the address grid, ranges, sweeps and drift
// behind v3's four tabs (`P['ipam/subnets/:subnetId']` in
// docs/design/itms-prototype-v3.html).
//
// THE FACTS ARE THE ONES THE API SERVES. v3 draws "Usable, Registered, Seen by
// sweep, Drift, Site server, Scan authorized"; the utilisation read answers
// the first three, the drift list the fourth, the sweep settings the fifth
// (a designated agent, or none) and the scan authorizations the sixth. A
// container prefix has no utilisation, so those cells say so rather than
// showing a zero.

const TABS: ReadonlyArray<{ key: string; label: MessageKey }> = [
  { key: "addresses", label: "ipam.subnet.tabAddresses" },
  { key: "ranges", label: "ipam.subnet.tabRanges" },
  { key: "sweeps", label: "ipam.subnet.tabSweeps" },
  { key: "drift", label: "ipam.subnet.tabDrift" },
];

export function SubnetDetailPage() {
  const t = useT();
  const { subnetId } = useParams<{ subnetId: string }>();
  const permissions = useIpamPermissions();
  const subnet = useIpamSubnet(subnetId);
  const assignable = subnet.data ? hasUtilization(subnet.data) : false;
  const utilization = useIpamSubnetUtilization(subnetId, assignable);
  const sites = useIpamSites();
  const vlans = useIpamVlans();
  const scopes = useIpamScanAuthorizations();
  const drift = useIpamDrift(subnetId ? { subnet: subnetId } : {});
  const [tab, setTab] = useState<string>("addresses");

  const findings = useMemo(
    () => (drift.data ?? []).filter((item) => isDriftFinding(item)),
    [drift.data],
  );

  if (subnet.error) {
    return (
      <div>
        <IpamNav />
        <ErrorLine error={subnet.error} />
      </div>
    );
  }
  if (!subnet.data || !subnetId) {
    return (
      <div>
        <IpamNav />
        <div className="empty">
          <Icon name="clock" size={26} />
          <b>{t("ipam.fields.loading")}</b>
        </div>
      </div>
    );
  }

  const row = subnet.data;
  // Worded here, on every render: a memoised list would keep the language it was built in.
  const tabs = TABS.map((item) => ({ key: item.key, label: t(item.label) }));
  // A 422 on utilisation is the enumeration ceiling, and the grid renders the
  // panel that explains it, so no error is shown twice.
  const utilizationTooLarge =
    utilization.error instanceof ApiError && utilization.error.status === 422;
  const util = utilization.data;
  const scope = effectiveSweepScope(row, scopes.data ?? []);
  const siteName = row.site_id
    ? (sites.data?.find((site) => site.id === row.site_id)?.name ?? row.site_id)
    : null;
  const vlan = row.vlan_id ? vlans.data?.find((item) => item.id === row.vlan_id) : undefined;
  const subline = [
    row.name || null,
    siteName,
    vlan
      ? vlan.name
        ? t("ipam.vlan.named", { vid: vlan.vid, name: vlan.name })
        : t("ipam.vlan.label", { vid: vlan.vid })
      : null,
    row.gateway ? t("ipam.subnet.gateway", { gateway: row.gateway }) : null,
    row.kind === "container" ? t("ipam.subnet.containerPrefix") : null,
  ].filter((part): part is string => part !== null);

  return (
    <div>
      <PageHead
        title={row.prefix}
        description={subline.length > 0 ? subline.join(" · ") : undefined}
        actions={
          <>
            <Link className="btn" to="/ipam">
              <Icon name="chevron-left" size={14} />
              <span>{t("ipam.subnet.allSubnets")}</span>
            </Link>
            {permissions.manage && <SubnetActions subnet={row} />}
          </>
        }
      />
      <IpamNav />

      <Facts
        items={[
          {
            k: t("ipam.subnet.usable"),
            v: assignable ? (
              util && util.capacity !== null ? (
                <span className="mono">{util.capacity}</span>
              ) : (
                <span className="mono">{t("ipam.utilization.na")}</span>
              )
            ) : (
              <NoValue label={t("ipam.subnet.containerNoAddresses")} />
            ),
          },
          {
            k: t("ipam.subnet.registered"),
            v: util ? (
              <span className="mono">{util.assigned + util.reserved}</span>
            ) : (
              <span className="mono">{t("ipam.utilization.na")}</span>
            ),
          },
          {
            k: t("ipam.subnet.seenBySweep"),
            v: util ? <span className="mono">{util.observed}</span> : <span className="mono">{t("ipam.utilization.na")}</span>,
          },
          {
            k: t("ipam.fields.drift"),
            v: drift.data ? (
              <Trans
                k="ipam.subnet.unresolved"
                vars={{ count: findings.length }}
                components={{ mono: <span className="mono" /> }}
              />
            ) : (
              <span className="mono">{t("ipam.utilization.na")}</span>
            ),
          },
          {
            k: t("ipam.fields.gateway"),
            v: row.gateway ? <span className="mono">{row.gateway}</span> : <NoValue label={t("ipam.subnet.noGateway")} />,
          },
          {
            k: t("ipam.subnet.dns"),
            v:
              row.dns_servers.length > 0 ? (
                <span className="mono">{row.dns_servers.join(", ")}</span>
              ) : (
                <NoValue label={t("ipam.subnet.noDns")} />
              ),
          },
          {
            k: t("ipam.subnet.sweeper"),
            v: row.sweep_device_id ? (
              <span className="mono">{row.sweep_device_id}</span>
            ) : (
              t("ipam.subnet.autoSelected")
            ),
          },
          {
            k: t("ipam.subnet.scanAuthorized"),
            v: scope.authorized ? (
              <StatusPill value="authorized" kind="ok" label={t("components.attributes.yes")} />
            ) : (
              <StatusPill value="not-authorized" kind="warn" label={t("components.attributes.no")} />
            ),
          },
          {
            k: t("ipam.fields.lastSwept"),
            v: (
              <span className="mono" title={lastSeenMeaning()}>
                {formatAge(row.last_swept_at)}
              </span>
            ),
          },
        ]}
      />

      {assignable && util && !utilizationTooLarge && (
        <Panel title={t("ipam.subnet.utilizationTitle")} subtitle={t("ipam.subnet.utilizationSubtitle")}>
          <UtilizationBar utilization={util} />
        </Panel>
      )}
      {!assignable && (
        <div className="notice" style={{ marginBottom: 16 }}>
          <Icon name="info" size={16} />
          <p>{t("ipam.subnet.containerNote")}</p>
        </div>
      )}

      <Tabs items={tabs} active={tab} onSelect={setTab} panelId="subnet-tabpanel" />

      <div id="subnet-tabpanel" role="tabpanel" aria-label={tabs.find((item) => item.key === tab)?.label} tabIndex={-1}>
        {tab === "addresses" && (
          <>
            {assignable && permissions.assign && <AllocatePanel subnetId={subnetId} />}
            {assignable ? (
              <AddressGrid
                subnetId={subnetId}
                subnetPrefix={row.prefix}
                utilization={util}
                canAssign={permissions.assign}
              />
            ) : (
              <Panel title={t("ipam.subnet.tabAddresses")}>
                <p className="note">{t("ipam.subnet.noGrid")}</p>
              </Panel>
            )}
          </>
        )}
        {tab === "ranges" && <RangesPanel subnetId={subnetId} canManage={permissions.manage} />}
        {tab === "sweeps" && (
          <SweepPanel
            subnet={row}
            canRun={permissions.sweepRun}
            canManage={permissions.sweepManage}
          />
        )}
        {tab === "drift" && <SubnetDriftPanel subnet={row} findings={findings} loading={drift.isLoading} error={drift.error} />}
      </div>
    </div>
  );
}

/** This subnet's unresolved findings, with the way to the page that handles them. */
function SubnetDriftPanel({
  subnet,
  findings,
  loading,
  error,
}: {
  subnet: SubnetOut;
  findings: readonly DriftOut[];
  loading: boolean;
  error: unknown;
}) {
  const t = useT();
  return (
    <Panel
      title={t("ipam.fields.drift")}
      subtitle={t("ipam.subnet.driftSubtitle")}
      actions={
        <Link className="btn small" to={`/ipam/drift?subnet=${subnet.id}`}>
          <Icon name="arrows-exchange" size={14} />
          <span>{t("ipam.subnet.driftLink")}</span>
        </Link>
      }
      bare
    >
      {loading ? (
        <div className="empty">
          <Icon name="clock" size={26} />
          <b>{t("ipam.fields.loading")}</b>
        </div>
      ) : error ? (
        <div className="panel-b">
          <ErrorLine error={error} />
        </div>
      ) : findings.length === 0 ? (
        <div className="empty">
          <Icon name="circle-check" size={26} />
          <b>{t("ipam.subnet.driftNone")}</b>
        </div>
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>{t("ipam.fields.address")}</Th>
              <Th>{t("ipam.fields.class")}</Th>
              <Th>{t("ipam.fields.severity")}</Th>
              <Th>{t("ipam.fields.firstDetected")}</Th>
            </tr>
          </thead>
          <tbody>
            {findings.map((item) => {
              const presentation = driftClassPresentation(item.drift_class);
              const severity = variantKind(severityVariant(item.severity));
              return (
                <tr key={item.id}>
                  <Td lead={<Icon name="arrows-exchange" size={18} />} gutter={severity}>
                    <div className="cell-title mono">{item.address}</div>
                  </Td>
                  <Td>
                    <StatusPill
                      value={item.drift_class}
                      kind={variantKind(presentation.variant)}
                      label={presentation.label}
                    />
                  </Td>
                  <Td>
                    <StatusPill value={item.severity} kind={severity} />
                  </Td>
                  <Td>
                    <span className="mono" title={lastSeenMeaning()}>
                      {formatAge(item.first_detected_at)}
                    </span>
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      )}
    </Panel>
  );
}

// Allocation, and the preview beside it.
//
// `next-free` IS ADVISORY and says so: two operators who both read it and then
// both assign race for the same address. `allocate` picks and claims in ONE
// transaction under an advisory lock and returns the address it took, which is
// why the button calls that and not "assign the address next-free showed me".
function AllocatePanel({ subnetId }: { subnetId: string }) {
  const t = useT();
  const [count, setCount] = useState(5);
  const [previewing, setPreviewing] = useState(false);
  const [assetLabel, setAssetLabel] = useState("");
  const [hostname, setHostname] = useState("");
  const nextFree = useIpamNextFree(subnetId, count, previewing);
  const allocate = useAllocateIpamAddressMutation();

  return (
    <Panel
      title={t("ipam.subnet.allocateTitle")}
      subtitle={t("ipam.subnet.allocateSubtitle")}
    >
      <p className="note" style={{ marginBottom: 14 }}>
        {t("ipam.subnet.allocateNote")}
      </p>
      <div className="form-grid">
        <Field label={t("ipam.subnet.previewCount")}>
          <input
            className={inputClass}
            type="number"
            min={1}
            max={MAX_NEXT_FREE}
            value={count}
            onChange={(e) => setCount(Number(e.target.value))}
          />
        </Field>
        <Field label={t("ipam.fields.assetLabel")}>
          <input
            className={inputClass}
            value={assetLabel}
            onChange={(e) => setAssetLabel(e.target.value)}
          />
        </Field>
        <Field label={t("ipam.fields.hostname")}>
          <input
            className={inputClass}
            value={hostname}
            onChange={(e) => setHostname(e.target.value)}
          />
        </Field>
      </div>
      <div className="form-actions">
        <Btn label={t("ipam.subnet.showNextFree")} icon="eye" onClick={() => setPreviewing(true)} />
        <Btn
          label={allocate.isPending ? t("ipam.subnet.allocating") : t("ipam.subnet.allocateNext")}
          icon="plus"
          variant="primary"
          disabled={allocate.isPending}
          onClick={() =>
            allocate.mutate({
              subnetId,
              body: { asset_label: assetLabel, hostname, allocation_method: "static" },
            })
          }
        />
        {previewing && nextFree.data && (
          <span className="form-hint">
            <Trans
              k="ipam.subnet.nextFree"
              vars={{ list: nextFree.data.join(", ") || t("ipam.subnet.nextFreeNone") }}
              components={{ mono: <span className="mono" /> }}
            />
          </span>
        )}
        {allocate.isSuccess && (
          <span className="form-saved" role="status">
            {t("ipam.subnet.allocated", { address: allocate.data.address })}
          </span>
        )}
      </div>
      {previewing && nextFree.error && <ErrorLine error={nextFree.error} />}
      {allocate.isError && <ErrorLine error={allocate.error} />}
    </Panel>
  );
}
