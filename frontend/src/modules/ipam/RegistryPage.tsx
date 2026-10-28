import { useState } from "react";
import { NoValue } from "../../components/emptystate";
import { Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import {
  useCreateIpamVlanMutation,
  useCreateIpamVrfMutation,
  useIpamPermissions,
  useIpamSites,
  useIpamVlans,
  useIpamVrfs,
} from "./api";
import { ErrorLine } from "./ErrorLine";
import { IpamNav } from "./IpamNav";
import { Icon } from "../../shell/icons";
import { Btn, PageHead, Panel, QueryState, StatusPill } from "../../shell/components";

// VRFs, VLANs and the client's sites: what everything else is scoped by.
//
// ON THE v3 KIT: three bare panels of rows, each led by its glyph, with the
// panel's sentence in its head and the add form flush under the list for a
// caller with `ipam.manage`. The design's Registry screen lists addresses;
// this console's registry is the scoping tables, and the address list is the
// subnet detail's grid.
//
// EVERY ROW IS ONE CLIENT'S (MSP clients phase 2): this console files them under
// the internal client until phase 3's selector (`api.ts::useIpamClient`).
// IPAM HAS NO SITES OF ITS OWN ANY MORE: the sites panel lists the client's
// sites read-only -- an office is the client's, managed with the client -- and
// the VLAN and subnet pickers name them.
//
// Two things here are easy to get wrong and are stated on the page rather than
// left to be discovered:
//
//   * A VLAN WITH NO SITE IS THE WHOLE CLIENT'S, not a VLAN whose site somebody
//     forgot to fill in. VLAN 10 legitimately exists at every site AND once
//     without one, and those are different rows.
//   * EVERY SUBNET HAS A VRF. There is no "no VRF": a subnet created without
//     one lands in the client's lazily created `global` VRF, because a
//     nullable scoping column would let the same prefix be registered twice.
export function RegistryPage() {
  const t = useT();
  const permissions = useIpamPermissions();
  return (
    <div>
      <PageHead
        title={t("ipam.registry.title")}
        description={t("ipam.registry.description")}
      />
      <IpamNav />
      <div className="grid grid-2">
        <VrfPanel canManage={permissions.manage} />
        <SitePanel />
      </div>
      <VlanPanel canManage={permissions.manage} />
    </div>
  );
}

/** The add form, flush under a bare panel's list. */
function AddRow({ children }: { children: React.ReactNode }) {
  return (
    <div className="panel-b" style={{ borderTop: "1px solid var(--line)" }}>
      {children}
    </div>
  );
}

function VrfPanel({ canManage }: { canManage: boolean }) {
  const t = useT();
  const vrfs = useIpamVrfs();
  const create = useCreateIpamVrfMutation();
  const [name, setName] = useState("");
  const [rd, setRd] = useState("");

  return (
    <Panel
      title={t("ipam.registry.vrfsTitle")}
      subtitle={t("ipam.registry.vrfsSubtitle")}
      bare
    >
      <QueryState
        loading={vrfs.isLoading}
        error={vrfs.error}
        empty={(vrfs.data ?? []).length === 0}
        emptyLabel={t("ipam.registry.vrfsEmpty")}
        emptyIcon="network"
      >
        <Table>
          <thead>
            <tr>
              <Th>{t("ipam.fields.name")}</Th>
              <Th>{t("ipam.registry.colRd")}</Th>
            </tr>
          </thead>
          <tbody>
            {vrfs.data?.map((vrf) => (
              <tr key={vrf.id}>
                <Td lead={<Icon name="topology-star-3" size={18} />}>
                  <div className="cell-title">{vrf.name}</div>
                  {vrf.description ? <div className="cell-sub">{vrf.description}</div> : null}
                </Td>
                <Td>
                  {vrf.rd ? (
                    <span className="mono">{vrf.rd}</span>
                  ) : (
                    <NoValue label={t("ipam.registry.noRd")} />
                  )}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
      {canManage && (
        <AddRow>
          <div className="form-grid">
            <Field label={t("ipam.fields.name")}>
              <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
            <Field label={t("ipam.registry.routeDistinguisher")}>
              <input className={inputClass} value={rd} onChange={(e) => setRd(e.target.value)} />
            </Field>
          </div>
          <div className="form-actions">
            <Btn
              label={t("ipam.registry.addVrf")}
              icon="plus"
              disabled={create.isPending || name.trim() === ""}
              onClick={() =>
                create.mutate(
                  { name, rd: rd || null },
                  {
                    onSuccess: () => {
                      setName("");
                      setRd("");
                    },
                  },
                )
              }
            />
            {create.isError && <ErrorLine error={create.error} />}
          </div>
        </AddRow>
      )}
    </Panel>
  );
}

/** The client's sites, read-only: IPAM names them and no longer creates them. */
function SitePanel() {
  const t = useT();
  const sites = useIpamSites();

  return (
    <Panel
      title={t("ipam.registry.sitesTitle")}
      subtitle={t("ipam.registry.sitesSubtitle")}
      bare
    >
      <QueryState
        loading={sites.isLoading}
        error={sites.error}
        empty={(sites.data ?? []).length === 0}
        emptyLabel={t("ipam.registry.sitesEmpty")}
        emptyIcon="building"
      >
        <Table>
          <thead>
            <tr>
              <Th>{t("ipam.fields.name")}</Th>
            </tr>
          </thead>
          <tbody>
            {/* One column: in the two-column grid a second column clipped at
                the panel edge at 1440, so the address rides in the sub-line. */}
            {sites.data?.map((site) => (
              <tr key={site.id}>
                <Td lead={<Icon name="building" size={18} />}>
                  <div className="cell-title">{site.name}</div>
                  {site.description || site.address ? (
                    <div className="cell-sub">{site.description || site.address}</div>
                  ) : null}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
    </Panel>
  );
}

function VlanPanel({ canManage }: { canManage: boolean }) {
  const t = useT();
  const vlans = useIpamVlans();
  const sites = useIpamSites();
  const create = useCreateIpamVlanMutation();
  const [vid, setVid] = useState("");
  const [siteId, setSiteId] = useState("");
  const [name, setName] = useState("");

  return (
    <Panel
      title={t("ipam.registry.vlansTitle")}
      subtitle={t("ipam.registry.vlansSubtitle")}
      bare
    >
      <QueryState
        loading={vlans.isLoading}
        error={vlans.error}
        empty={(vlans.data ?? []).length === 0}
        emptyLabel={t("ipam.registry.vlansEmpty")}
        emptyIcon="network"
      >
        <Table>
          <thead>
            <tr>
              <Th>{t("ipam.fields.vlan")}</Th>
              <Th>{t("ipam.registry.colScope")}</Th>
            </tr>
          </thead>
          <tbody>
            {vlans.data?.map((vlan) => (
              <tr key={vlan.id}>
                <Td lead={<Icon name="router" size={18} />}>
                  <div className="cell-title">
                    <span className="mono">VLAN {vlan.vid}</span>
                    {vlan.name ? ` ${vlan.name}` : ""}
                  </div>
                  {vlan.description ? <div className="cell-sub">{vlan.description}</div> : null}
                </Td>
                <Td>
                  {vlan.site_id === null ? (
                    <StatusPill
                      value="client-wide"
                      kind="info"
                      label={t("ipam.registry.clientWide")}
                    />
                  ) : (
                    <StatusPill
                      value="site"
                      kind="mut"
                      label={sites.data?.find((site) => site.id === vlan.site_id)?.name ?? vlan.site_id}
                    />
                  )}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
      {canManage && (
        <AddRow>
          <div className="form-grid">
            <Field label={t("ipam.registry.vid")}>
              <input
                className={inputClass}
                type="number"
                min={1}
                max={4094}
                value={vid}
                onChange={(e) => setVid(e.target.value)}
              />
            </Field>
            <Field label={t("ipam.fields.site")}>
              <select
                className={inputClass}
                value={siteId}
                onChange={(e) => setSiteId(e.target.value)}
              >
                <option value="">{t("ipam.registry.clientWideNoSite")}</option>
                {sites.data?.map((site) => (
                  <option key={site.id} value={site.id}>
                    {site.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t("ipam.fields.name")}>
              <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
          </div>
          <div className="form-actions">
            <Btn
              label={t("ipam.registry.addVlan")}
              icon="plus"
              disabled={create.isPending || vid === ""}
              onClick={() =>
                create.mutate(
                  { vid: Number(vid), site_id: siteId || null, name },
                  {
                    onSuccess: () => {
                      setVid("");
                      setName("");
                    },
                  },
                )
              }
            />
            {create.isError && <ErrorLine error={create.error} />}
          </div>
        </AddRow>
      )}
    </Panel>
  );
}
