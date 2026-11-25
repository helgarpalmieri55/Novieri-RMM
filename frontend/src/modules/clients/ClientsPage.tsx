/**
 * Clients (MSP clients phase 3, design §4.1): the companies this organisation
 * serves, and its two system clients -- its own IT (`internal`) and
 * `unassigned`, which `GET /clients` lists only to a person with every
 * client. Archived clients are hidden unless asked for.
 *
 * ON THE LIST KIT (redesign design §2, Receta L; the model the waves copy):
 * the search and the archived chip live in the URL (`?q=`, and `?archived=1`,
 * the address it always had), "Showing x of y", an empty list that says its
 * cause, and the two create dialogs open from `?new=` (decision D8) so the New
 * menu links to them.
 *
 * "NEW CLIENT" NEEDS `clients.manage` ON EVERY CLIENT (`POST /clients` is a
 * global route), so it is offered on `permissions_everywhere`. Offering it is
 * a courtesy: the server checks the code on every request (invariant 6).
 */
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Form } from "../../components/form";
import { Button, Dialog, Field, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { useClientScope } from "../../lib/clientScope";
import { useEverywherePermissions } from "../../lib/mePermissions";
import {
  Btn,
  FilterChips,
  ListEmpty,
  ListToolbar,
  PageHead,
  Panel,
  QueryState,
  SearchField,
  ShowingCount,
  StatusPill,
  useNewParam,
  useUrlFilters,
} from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useClientList } from "./api";
import { ClientFormDialog } from "./ClientForm";
import { clientKindLabel, clientStatusKind, clientStatusLabel } from "./labels";

/** For the search: case and accents do not count ("bogota" finds "Bogotá"), the Admin panel's rule. */
function fold(text: string): string {
  return text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().trim();
}

export function ClientsPage() {
  const t = useT();
  const navigate = useNavigate();
  const filters = useUrlFilters(["q", "archived"] as const);
  const archived = filters.values.archived === "1";
  const everywhere = useEverywherePermissions();
  const canCreate = everywhere.has("clients.manage");
  const clients = useClientList(archived);
  // New → Client and New → Contact (redesign decision D8): `?new=client`
  // opens New client; `?new=contact` asks which client first.
  const created = useNewParam("client");
  const contact = useNewParam("contact");
  // Asked only of someone with a client whose contacts they may manage, and
  // so only once `/me` has answered: the dialog takes its first choice when it
  // opens, and a bare `?new=contact` opens nothing for a reader.
  const { clients: mine } = useClientScope();
  const contactable = mine.some((client) => client.kind !== "unassigned" && client.permissions.includes("clients.manage"));
  const all = clients.data ?? [];
  const wanted = fold(filters.values.q);
  // `GET /clients` answers every client at once, so the search filters here,
  // by name and by e-mail domain, and the total is the server's whole answer.
  const rows =
    wanted === ""
      ? all
      : all.filter(
          (client) => fold(client.name).includes(wanted) || client.email_domains.some((domain) => fold(domain).includes(wanted)),
        );
  const newClient = canCreate ? (
    <Btn label={t("clients.list.newClient")} icon="plus" variant="primary" onClick={created.start} />
  ) : undefined;

  return (
    <>
      <PageHead title={t("clients.list.title")} description={t("clients.list.description")} actions={newClient} />
      <Panel bare>
        <ListToolbar
          search={
            <SearchField
              aria-label={t("clients.list.search")}
              placeholder={t("clients.list.search")}
              value={filters.values.q}
              onChange={(event) => filters.set("q", event.target.value)}
            />
          }
          chips={
            <FilterChips
              label={t("clients.list.archivedLabel")}
              options={[
                { value: "", label: t("clients.list.chipActive") },
                { value: "1", label: t("clients.list.chipArchived") },
              ]}
              value={filters.values.archived}
              onChange={(value) => filters.set("archived", value)}
            />
          }
          // No count before the list has answered: "Showing 0 of 0" would be an invented zero.
          end={clients.data ? <ShowingCount shown={rows.length} total={all.length} /> : null}
        />
        <QueryState loading={clients.isLoading} error={clients.error} empty={false} emptyLabel="">
          {rows.length === 0 ? (
            <ListEmpty
              filtered={filters.active}
              onClear={filters.clear}
              nothingYet={t("clients.list.emptyYet")}
              icon="building"
              action={newClient}
            />
          ) : (
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
          )}
        </QueryState>
      </Panel>
      {/* KEYED so a refusal cannot outlive the dialog that produced it. Open
          only for someone who may create one, whatever the URL asks. */}
      <ClientFormDialog
        key={created.open && canCreate ? "open" : "closed"}
        open={created.open && canCreate}
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
