import { useState } from "react";
import { Link } from "react-router-dom";
import { ClientField } from "../../components/ClientField";
import { NoValue } from "../../components/emptystate";
import { attemptProblems, Form, FormActions, type Attempt, type FieldRule } from "../../components/form";
import { Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { useClientScope } from "../../lib/clientScope";
import { useNewRowClientField } from "../../lib/clientSelection";
import { useMePermissions } from "../../lib/mePermissions";
import { useClientName } from "../settings/api";
import {
  useCreateIpamVlanMutation,
  useCreateIpamVrfMutation,
  useIpamPermissions,
  useIpamSiteDirectory,
  useIpamSites,
  useIpamVlans,
  useIpamVrfs,
} from "./api";
import { IpamNav } from "./IpamNav";
import { clientRule } from "./NewRowClient";
import { UnassignedRefusal } from "../../components/UnassignedRefusal";
import { IPAM_MANAGE } from "./types";
import { Icon } from "../../shell/icons";
import {
  Btn,
  FormSection,
  PageHead,
  QueryState,
  StatusPill,
  useNewParam,
  useUnsavedGuard,
} from "../../shell/components";

// VRFs, VLANs and the client's sites: what everything else is scoped by.
//
// ON THE FORM KIT (redesign plan, Receta FO; Task 10, 5.9): three folding
// sections, each its sentence and its table, and for a caller with
// `ipam.manage` a New VRF / New VLAN that opens its dialog from `?new=vrf` /
// `?new=vlan` (decision D8), with the fields the add rows had. No save bar:
// each row is saved when its dialog is confirmed. The design's Registry
// screen lists addresses; this console's registry is the scoping tables, and
// the address list is the subnet detail's grid.
//
// EVERY ROW IS ONE CLIENT'S (MSP clients phase 2): a new one goes to its
// parent's client, else to the client chosen at the top, else to the one its
// form's Client field names (phase 3, `lib/clientSelection.ts`).
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
  const newVrf = useNewParam("vrf");
  const newVlan = useNewParam("vlan");
  return (
    <div>
      <PageHead
        title={t("ipam.registry.title")}
        description={t("ipam.registry.description")}
      />
      <IpamNav />
      <VrfSection canManage={permissions.manage} onNew={newVrf.start} />
      <SiteSection />
      <VlanSection canManage={permissions.manage} onNew={newVlan.start} />

      {/* Mounted only while open: each opening starts from an empty form. */}
      {permissions.manage && newVrf.open ? (
        <Dialog open onClose={newVrf.close} title={t("ipam.registry.newVrf")}>
          <VrfForm onClose={newVrf.close} />
        </Dialog>
      ) : null}
      {permissions.manage && newVlan.open ? (
        <Dialog open onClose={newVlan.close} title={t("ipam.registry.newVlan")}>
          <VlanForm onClose={newVlan.close} />
        </Dialog>
      ) : null}
    </div>
  );
}

/** A section's New button (or why there is none), above its table. */
function SectionAction({ children }: { children: React.ReactNode }) {
  return <div style={{ marginBottom: 12 }}>{children}</div>;
}

/**
 * The section's New button, or why there is none: with Unassigned chosen at
 * the top nothing new can be filed under it, and the section says so where
 * the button would be rather than opening a dialog that can only refuse.
 */
function NewRowAction({ canManage, label, onNew }: { canManage: boolean; label: string; onNew: () => void }) {
  // No parent above a VRF or a VLAN: the top bar's choice decides alone.
  const refused = useNewRowClientField(IPAM_MANAGE).refused === "unassigned";
  if (!canManage) return null;
  return refused ? <UnassignedRefusal /> : <Btn label={label} icon="plus" onClick={onNew} />;
}

function VrfSection({ canManage, onNew }: { canManage: boolean; onNew: () => void }) {
  const t = useT();
  const vrfs = useIpamVrfs();
  // A VRF is one client's, and decides the client of what is filed in it.
  const clientName = useClientName();

  return (
    <FormSection title={t("ipam.registry.vrfsTitle")} icon="topology-star-3" description={t("ipam.registry.vrfsSubtitle")}>
      <SectionAction>
        <NewRowAction canManage={canManage} label={t("ipam.registry.newVrf")} onNew={onNew} />
      </SectionAction>
      <QueryState
        loading={vrfs.isLoading}
        error={vrfs.error}
        empty={(vrfs.data ?? []).length === 0}
        emptyLabel={t("ipam.registry.vrfsEmpty")}
        emptyIcon="network"
      >
        <Table label={t("ipam.registry.vrfsTitle")}>
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
                  <div className="cell-title">
                    {t("ipam.fields.vrfOfClient", { vrf: vrf.name, client: clientName(vrf.client_id) })}
                  </div>
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
    </FormSection>
  );
}

/** What a VRF may not be added without, named on Add (it used to sit disabled, saying nothing). */
const vrfRules = (): readonly FieldRule[] => [{ name: "name", label: translate("ipam.fields.name") }];

/** New VRF, in its dialog: the add row's fields and the same body. */
function VrfForm({ onClose }: { onClose: () => void }) {
  const t = useT();
  const create = useCreateIpamVrfMutation();
  // No parent: the client chosen at the top, or the Client field's (§3.1).
  const client = useNewRowClientField(IPAM_MANAGE);
  const [name, setName] = useState("");
  const [rd, setRd] = useState("");
  // Populated on Add, never while typing (the form kernel's rule).
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  // `?new=vrf` holds the form: a change asks before it is left, a saved one leaves.
  const { guard, release } = useUnsavedGuard(name !== "" || rd !== "" || client.picked !== "", { search: ["new"] });

  function add() {
    const next: Attempt = {
      rules: () => [...clientRule(client), ...vrfRules()],
      values: { client: client.clientId, name },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) {
      create.reset();
      return;
    }
    create.mutate(
      { name, rd: rd || null, client_id: client.clientId },
      {
        onSuccess: () => {
          release();
          onClose();
        },
      },
    );
  }

  if (client.refused === "unassigned") return <UnassignedRefusal />;
  return (
    <Form onSubmit={add}>
      {guard}
      <div className="form-grid">
        {client.needsField ? <ClientField field={client} /> : null}
        <Field label={t("ipam.fields.name")}>
          <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={t("ipam.registry.routeDistinguisher")}>
          <input className={inputClass} value={rd} onChange={(e) => setRd(e.target.value)} />
        </Field>
      </div>
      <FormActions
        label={t("ipam.registry.addVrf")}
        pending={create.isPending}
        error={create.error}
        problems={attemptProblems(attempt)}
      />
    </Form>
  );
}

/** The sites in view, read-only: IPAM names them and no longer creates them. */
function SiteSection() {
  const t = useT();
  const { selected } = useClientScope();
  const clientName = useClientName();
  const directory = useIpamSiteDirectory();
  // NOT A PERMISSION GATE: the Clients page reads `clients.read` itself.
  const canReadClients = useMePermissions().has("clients.read");

  return (
    <FormSection title={t("ipam.registry.sitesTitle")} icon="building" description={t("ipam.registry.sitesSubtitle")}>
      {canReadClients ? (
        <SectionAction>
          <Link className="btn" to="/clients">
            <Icon name="external-link" size={14} />
            <span>{t("ipam.registry.manageSites")}</span>
          </Link>
        </SectionAction>
      ) : null}
      <QueryState
        loading={directory.isLoading}
        error={directory.error}
        empty={directory.sites.length === 0}
        emptyLabel={t("ipam.registry.sitesEmpty")}
        emptyIcon="building"
      >
        <Table label={t("ipam.registry.sitesTitle")}>
          <thead>
            <tr>
              <Th>{t("ipam.fields.name")}</Th>
            </tr>
          </thead>
          <tbody>
            {/* One column: the address -- and, with every client chosen,
                whose office it is -- rides in the sub-line. */}
            {directory.sites.map((site) => {
              const sub = [
                selected === null ? clientName(site.client_id) : "",
                site.description || site.address,
              ].filter((part) => part !== "");
              return (
                <tr key={site.id}>
                  <Td lead={<Icon name="building" size={18} />}>
                    <div className="cell-title">{site.name}</div>
                    {sub.length > 0 ? <div className="cell-sub">{sub.join(" · ")}</div> : null}
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      </QueryState>
    </FormSection>
  );
}

function VlanSection({ canManage, onNew }: { canManage: boolean; onNew: () => void }) {
  const t = useT();
  const vlans = useIpamVlans();
  const { selected } = useClientScope();
  const clientName = useClientName();
  const directory = useIpamSiteDirectory();
  const showClient = selected === null;

  return (
    <FormSection title={t("ipam.registry.vlansTitle")} icon="router" description={t("ipam.registry.vlansSubtitle")}>
      <SectionAction>
        <NewRowAction canManage={canManage} label={t("ipam.registry.newVlan")} onNew={onNew} />
      </SectionAction>
      <QueryState
        loading={vlans.isLoading}
        error={vlans.error}
        empty={(vlans.data ?? []).length === 0}
        emptyLabel={t("ipam.registry.vlansEmpty")}
        emptyIcon="network"
      >
        <Table label={t("ipam.registry.vlansTitle")}>
          <thead>
            <tr>
              <Th>{t("ipam.fields.vlan")}</Th>
              {showClient ? <Th>{t("ipam.fields.client")}</Th> : null}
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
                {showClient ? <Td>{clientName(vlan.client_id)}</Td> : null}
                <Td>
                  {vlan.site_id === null ? (
                    <StatusPill value="client-wide" kind="info" label={t("ipam.registry.clientWide")} />
                  ) : (
                    <StatusPill value="site" kind="mut" label={directory.siteName(vlan.site_id)} />
                  )}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
    </FormSection>
  );
}

/** What a VLAN may not be added without, named on Add (it used to sit disabled, saying nothing). */
const vlanRules = (): readonly FieldRule[] => [{ name: "vid", label: translate("ipam.registry.vid") }];

/** New VLAN, in its dialog: the add row's fields, its client and site chosen as before, and the same body. */
function VlanForm({ onClose }: { onClose: () => void }) {
  const t = useT();
  // No parent above the form: the client chosen at the top, or the Client
  // field's. Its sites are the only ones a new VLAN may name; a site, once
  // picked, decides the VLAN's client (the body then names none).
  const [siteId, setSiteId] = useState("");
  // Another client is another set of sites.
  const client = useNewRowClientField(IPAM_MANAGE, undefined, { onPick: () => setSiteId("") });
  const vlanClient = client.clientId;
  const sites = useIpamSites(vlanClient);
  const create = useCreateIpamVlanMutation();
  const [vid, setVid] = useState("");
  const [name, setName] = useState("");
  // Populated on Add, never while typing (the form kernel's rule).
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  // `?new=vlan` holds the form: a change asks before it is left, a saved one leaves.
  const { guard, release } = useUnsavedGuard(
    vid !== "" || name !== "" || siteId !== "" || client.picked !== "",
    { search: ["new"] },
  );

  function add() {
    const next: Attempt = {
      rules: () => [...clientRule(client), ...vlanRules()],
      values: { client: vlanClient, vid },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) {
      create.reset();
      return;
    }
    create.mutate(
      { vid: Number(vid), site_id: siteId || null, client_id: vlanClient, name },
      {
        onSuccess: () => {
          release();
          onClose();
        },
      },
    );
  }

  if (client.refused === "unassigned") return <UnassignedRefusal />;
  return (
    <Form onSubmit={add}>
      {guard}
      <div className="form-grid">
        {client.needsField ? <ClientField field={client} /> : null}
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
          <select className={inputClass} value={siteId} onChange={(e) => setSiteId(e.target.value)}>
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
      <FormActions
        label={t("ipam.registry.addVlan")}
        pending={create.isPending}
        error={create.error}
        problems={attemptProblems(attempt)}
      />
    </Form>
  );
}
