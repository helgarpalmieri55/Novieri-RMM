/**
 * Clients (MSP clients phase 3, design §4.1): the companies this organisation
 * serves, and its two system clients -- its own IT (`internal`) and
 * `unassigned`, which `GET /clients` lists only to a person with every
 * client. Archived clients are hidden unless asked for.
 *
 * "NEW CLIENT" NEEDS `clients.manage` ON EVERY CLIENT (`POST /clients` is a
 * global route), so it is offered on `permissions_everywhere`. Offering it is
 * a courtesy: the server checks the code on every request (invariant 6).
 */
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { Form } from "../../components/form";
import { Button, Dialog, Field, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { useClientScope } from "../../lib/clientScope";
import { useEverywherePermissions } from "../../lib/mePermissions";
import { Btn, FilterBar, PageHead, Panel, QueryState, StatusPill, Toggle, useNewParam } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useClientList } from "./api";
import { ClientFormDialog } from "./ClientForm";
import { clientKindLabel, clientStatusKind, clientStatusLabel } from "./labels";

export function ClientsPage() {
  const t = useT();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const archived = params.get("archived") === "1";
  const everywhere = useEverywherePermissions();
  const clients = useClientList(archived);
  // New → Client and New → Contact (redesign decision D8): `?new=client`
  // opens New client; `?new=contact` asks which client first.
  const created = useNewParam("client");
  const contact = useNewParam("contact");
  const creating = created.open && everywhere.has("clients.manage");
  // Asked only of someone with a client whose contacts they may manage, and
  // so only once `/me` has answered: the dialog takes its first choice when it
  // opens, and a bare `?new=contact` opens nothing for a reader.
  const { clients: mine } = useClientScope();
  const contactable = mine.some((client) => client.kind !== "unassigned" && client.permissions.includes("clients.manage"));
  const rows = clients.data ?? [];

  function showArchived(next: boolean) {
    const query = new URLSearchParams(params);
    if (next) query.set("archived", "1");
    else query.delete("archived");
    setParams(query, { replace: true });
  }

  return (
    <>
      <PageHead
        title={t("clients.list.title")}
        description={t("clients.list.description")}
        actions={
          everywhere.has("clients.manage") ? (
            <Btn
              label={t("clients.list.newClient")}
              icon="plus"
              variant="primary"
              onClick={created.start}
            />
          ) : undefined
        }
      />
      <Panel bare>
        <FilterBar>
          <Toggle checked={archived} onChange={showArchived} label={t("clients.list.showArchived")} />
          <span className="filter-spacer" />
        </FilterBar>
        <QueryState
          loading={clients.isLoading}
          error={clients.error}
          empty={rows.length === 0}
          emptyLabel={t("clients.list.empty")}
          emptyIcon="building"
        >
          <Table label={t("clients.list.tableLabel")}>
            <thead>
              <tr>
                <Th>{t("clients.list.colClient")}</Th>
                <Th>{t("clients.list.colKind")}</Th>
                <Th>{t("clients.list.colDevices")}</Th>
                <Th>{t("clients.list.colSites")}</Th>
                <Th>{t("clients.list.colStatus")}</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((client) => (
                <tr key={client.id}>
                  <Td lead={<Icon name="building" size={18} />}>
                    <div className="cell-title">
                      <Link to={`/clients/${client.id}`}>{client.name}</Link>
                    </div>
                    {client.email_domains.length > 0 ? (
                      <div className="cell-sub">{client.email_domains.join(", ")}</div>
                    ) : null}
                  </Td>
                  <Td>{clientKindLabel(client.kind)}</Td>
                  <Td>
                    <span className="mono">{client.device_count}</span>
                  </Td>
                  <Td>
                    <span className="mono">{client.site_count}</span>
                  </Td>
                  <Td>
                    <StatusPill
                      value={client.status}
                      kind={clientStatusKind(client.status)}
                      label={clientStatusLabel(client.status)}
                    />
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>
      {/* KEYED so a refusal cannot outlive the dialog that produced it. */}
      <ClientFormDialog
        key={creating ? "open" : "closed"}
        open={creating}
        client={null}
        onClose={created.close}
        onSaved={(saved) => {
          // A replacing navigation: the new client's page takes the place of
          // the list with its dialog open, so Back returns to the list.
          navigate(`/clients/${saved.id}`, { replace: true });
        }}
      />
      {contact.open && contactable ? <ContactClientDialog onClose={contact.close} /> : null}
    </>
  );
}

/**
 * New → Contact with every client shown (redesign design §1.2): a contact
 * belongs to a client, so the first question is which one. Only clients
 * where the person may manage contacts, never Unassigned -- the same rule
 * ContactsTab applies -- and the answer goes to that client's contacts tab,
 * where the form opens.
 */
function ContactClientDialog({ onClose }: { onClose: () => void }) {
  const t = useT();
  const navigate = useNavigate();
  const { clients } = useClientScope();
  const choices = clients.filter((client) => client.kind !== "unassigned" && client.permissions.includes("clients.manage"));
  const [picked, setPicked] = useState(choices[0]?.id ?? "");
  return (
    <Dialog open onClose={onClose} title={t("clients.list.newContactTitle")}>
      {/* REPLACING: the client's contacts take the question's place, so Back
          does not ask it again (Task 4 review, fix round 1). */}
      <Form onSubmit={() => picked && navigate(`/clients/${picked}?tab=contacts&new=contact`, { replace: true })}>
        <Field label={t("shell.kit.client")}>
          <select className="select-field" value={picked} onChange={(event) => setPicked(event.target.value)}>
            {choices.map((client) => (
              <option key={client.id} value={client.id}>
                {client.name}
              </option>
            ))}
          </select>
        </Field>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          <Button type="submit" disabled={!picked}>
            {t("clients.list.newContactContinue")}
          </Button>
        </div>
      </Form>
    </Dialog>
  );
}
