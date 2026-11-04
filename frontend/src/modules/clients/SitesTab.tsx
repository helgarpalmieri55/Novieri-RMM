/**
 * A client's sites: list (archived ones too), add, edit and archive with
 * `sites.manage` on this client, and each site's install link with
 * `enrollment.link.manage` there -- the same panel Agent packages shows
 * (`agent-releases/InstallLinkPanel.tsx`). The internal client's Principal
 * site (its id is the organisation's) is never archived, so it is not offered;
 * a site holding devices is refused with a 409 the page explains.
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { ClientOut, ClientSiteOut, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useClientScope } from "../../lib/clientScope";
import { formatDate } from "../../lib/datetime";
import { Btn, Panel, QueryState, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { InstallLinkPanel } from "../agent-releases/InstallLinkPanel";
import { useArchiveSiteMutation, useClientSitesWithArchived, useCreateSiteMutation, useUpdateSiteMutation } from "./api";

const siteRules = (): readonly FieldRule[] => [{ name: "name", label: translate("clients.sites.name") }];

export function SitesTab({ client }: { client: ClientOut }) {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const { clients } = useClientScope();
  const mine = clients.find((item) => item.id === client.id);
  const open = client.status === "active" && client.kind !== "unassigned";
  const canManage = open && (mine?.permissions.includes("sites.manage") ?? false);
  const canLink = open && (mine?.permissions.includes("enrollment.link.manage") ?? false);
  const sites = useClientSitesWithArchived(client.id);
  const archive = useArchiveSiteMutation(client.id);
  const [editing, setEditing] = useState<ClientSiteOut | "new" | null>(null);
  const [archiving, setArchiving] = useState<ClientSiteOut | null>(null);
  const [linkFor, setLinkFor] = useState<string | null>(null);
  const rows = sites.data ?? [];

  if (client.kind === "unassigned") {
    return (
      <Panel title={t("clients.sites.title")}>
        <p className="note">{t("clients.sites.unassigned")}</p>
      </Panel>
    );
  }

  return (
    <>
      <Panel
        title={t("clients.sites.title")}
        subtitle={t("clients.sites.subtitle")}
        actions={canManage ? <Btn label={t("clients.sites.add")} icon="plus" onClick={() => setEditing("new")} /> : undefined}
        bare
      >
        <QueryState
          loading={sites.isLoading}
          error={sites.error}
          empty={rows.length === 0}
          emptyLabel={t("clients.sites.empty")}
          emptyIcon="building"
        >
          <Table label={t("clients.sites.tableLabel")}>
            <thead>
              <tr>
                <Th>{t("clients.sites.colSite")}</Th>
                <Th>{t("clients.sites.colDevices")}</Th>
                <Th>{t("clients.sites.colStatus")}</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {rows.map((site) => {
                const live = site.archived_at === null;
                const principalOfInternal = client.kind === "internal" && site.id === client.id;
                return (
                  <tr key={site.id}>
                    <Td lead={<Icon name="map-pin" size={18} />}>
                      <div className="cell-title">{site.name}</div>
                      {site.address || site.description ? (
                        <div className="cell-sub">{site.address || site.description}</div>
                      ) : null}
                    </Td>
                    <Td>
                      <span className="mono">{site.device_count}</span>
                    </Td>
                    <Td>
                      {live ? (
                        <StatusPill value="active" kind="ok" label={t("clients.sites.inUse")} />
                      ) : (
                        <StatusPill
                          value="archived"
                          kind="mut"
                          label={t("clients.sites.archivedOn", { date: formatDate(site.archived_at, me.data?.timezone) })}
                        />
                      )}
                    </Td>
                    <Td>
                      {live && canManage ? (
                        <Btn
                          small
                          label={t("clients.sites.edit")}
                          ariaLabel={t("clients.sites.editAria", { name: site.name })}
                          onClick={() => setEditing(site)}
                        />
                      ) : null}{" "}
                      {live && canLink ? (
                        <Btn
                          small
                          label={t("clients.sites.installLink")}
                          ariaLabel={t("clients.sites.installLinkAria", { name: site.name })}
                          pressed={linkFor === site.id}
                          onClick={() => setLinkFor(linkFor === site.id ? null : site.id)}
                        />
                      ) : null}{" "}
                      {live && canManage && !principalOfInternal ? (
                        <Btn
                          small
                          variant="danger"
                          label={t("clients.sites.archive")}
                          ariaLabel={t("clients.sites.archiveAria", { name: site.name })}
                          onClick={() => {
                            archive.reset();
                            setArchiving(site);
                          }}
                        />
                      ) : null}
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </QueryState>
        {archive.isError ? (
          <p role="alert" className="form-error">
            {t("clients.sites.archiveFailed")} {describeSubmitError(archive.error)}
          </p>
        ) : null}
      </Panel>

      {linkFor !== null ? <InstallLinkPanel zone={me.data?.timezone} siteId={linkFor} /> : null}

      {editing !== null ? (
        <SiteFormDialog
          clientId={client.id}
          site={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
        />
      ) : null}
      <ConfirmDialog
        open={archiving !== null}
        title={t("clients.sites.archiveTitle")}
        subject={archiving?.name ?? ""}
        body={t("clients.sites.archiveBody")}
        confirmLabel={t("clients.sites.archiveConfirm")}
        destructive
        onCancel={() => setArchiving(null)}
        onConfirm={() => {
          const site = archiving;
          setArchiving(null);
          if (site) archive.mutate(site.id);
        }}
      />
    </>
  );
}

function SiteFormDialog({
  clientId,
  site,
  onClose,
}: {
  clientId: string;
  site: ClientSiteOut | null;
  onClose: () => void;
}) {
  const t = useT();
  const create = useCreateSiteMutation(clientId);
  const update = useUpdateSiteMutation(clientId);
  const mutation = site ? update : create;
  const [name, setName] = useState(site?.name ?? "");
  const [address, setAddress] = useState(site?.address ?? "");
  const [description, setDescription] = useState(site?.description ?? "");
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function submit() {
    mutation.reset();
    const next: Attempt = { rules: siteRules, values: { name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    const body = { name: name.trim(), address: address.trim(), description: description.trim() };
    if (site) update.mutate({ siteId: site.id, body }, { onSuccess: onClose });
    else create.mutate(body, { onSuccess: onClose });
  }

  return (
    <Dialog
      open
      onClose={onClose}
      title={site ? t("clients.sites.formEditTitle") : t("clients.sites.formAddTitle")}
    >
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("clients.sites.name")}>
            <input className={inputClass} value={name} onChange={(event) => setName(event.target.value)} />
          </Field>
          <Field label={t("clients.sites.address")}>
            <input className={inputClass} value={address} onChange={(event) => setAddress(event.target.value)} />
          </Field>
          <Field label={t("clients.sites.description")} full>
            <textarea
              className={inputClass}
              rows={3}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </Field>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          <FormActions
            label={t("clients.sites.save")}
            pendingLabel={t("clients.sites.saving")}
            pending={mutation.isPending}
            error={mutation.error}
            failedMessage={t("clients.sites.saveFailed")}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
