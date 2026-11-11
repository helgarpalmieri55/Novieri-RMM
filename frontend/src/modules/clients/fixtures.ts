/**
 * Fixtures for the Clients page: one client read by id, and its contacts. The
 * list (`listClients`) is the Settings fixtures', the sites (`listClientSites`)
 * IPAM's and the install link Agent packages', so the same client reads the
 * same on every screen; the records below match `settings/fixtures.ts`.
 *
 * A CONTACT'S PORTAL ACCESS (own-auth phase 2, Task 11): one who can sign in,
 * one with an open invitation (expiring within the 72 hours an invitation
 * lives), and two with none -- a turned-off contact is always `none`, as the
 * server says. Inviting answers as a server with mail configured does: the
 * link went by mail, so none comes back to show.
 */
import type { FixtureMap } from "../../fixtures/client";
import { CUSTOMER_ID } from "../../fixtures/core";
import type { ClientOut, ContactOut } from "../../lib/api";

const HOUR = 60 * 60_000;

const CLIENTS: ClientOut[] = [
  { id: "org-toeshee", name: "Toeshee", kind: "internal", status: "active", email_domains: ["toeshee.example"], phone: "", address: "", website: "", notes: "", enabled_modules: ["budget", "bcp", "change"], device_count: 42, site_count: 1 },
  { id: CUSTOMER_ID, name: "Andes Logistics", kind: "customer", status: "active", email_domains: ["andeslogistics.example"], phone: "+57 1 555 0100", address: "Calle 80 #10-20, Bogotá", website: "andeslogistics.example", notes: "", enabled_modules: [], device_count: 12, site_count: 2 },
  { id: "cli-unassigned", name: "Unassigned", kind: "unassigned", status: "active", email_domains: [], phone: "", address: "", website: "", notes: "", enabled_modules: [], device_count: 0, site_count: 0 },
];

const CONTACTS: ContactOut[] = [
  { id: "usr-contact-1", client_id: CUSTOMER_ID, kind: "contact", email: "ana.rojas@andeslogistics.example", display_name: "Ana Rojas", phone: "+57 1 555 0101", status: "active", access: "active", invite_expires_at: null },
  { id: "usr-contact-3", client_id: CUSTOMER_ID, kind: "contact", email: "luis.pardo@andeslogistics.example", display_name: "Luis Pardo", phone: null, status: "active", access: "invited", invite_expires_at: new Date(Date.now() + 48 * HOUR).toISOString() },
  { id: "usr-contact-4", client_id: CUSTOMER_ID, kind: "contact", email: "marta.gil@andeslogistics.example", display_name: "Marta Gil", phone: "+57 1 555 0104", status: "active", access: "none", invite_expires_at: null },
  { id: "usr-contact-2", client_id: CUSTOMER_ID, kind: "contact", email: "compras@andeslogistics.example", display_name: "Compras", phone: null, status: "disabled", access: "none", invite_expires_at: null },
];

const getClientRecord: FixtureMap["getClientRecord"] = (clientId) => {
  const found = CLIENTS.find((client) => client.id === clientId);
  if (!found) throw new Error(`no such client: ${clientId}`);
  return found;
};

const listContacts: FixtureMap["listContacts"] = (clientId) =>
  CONTACTS.filter((contact) => contact.client_id === clientId);

function contactOf(contactId: string): ContactOut {
  const found = CONTACTS.find((contact) => contact.id === contactId);
  if (!found) throw new Error(`no such contact: ${contactId}`);
  return found;
}

/** As the server answers with mail configured: the contact, invited, and no link to show. */
function invited(contactId: string): ContactOut {
  return { ...contactOf(contactId), access: "invited", invite_expires_at: new Date(Date.now() + 72 * HOUR).toISOString() };
}

const inviteContact: FixtureMap["inviteContact"] = (_clientId, contactId) => ({
  contact: invited(contactId),
  invite_link: null,
});

const cancelContactInvite: FixtureMap["cancelContactInvite"] = () => undefined;

const resetContactAccess: FixtureMap["resetContactAccess"] = (_clientId, contactId) => ({
  contact: invited(contactId),
  invite_link: null,
});

const fixtures: FixtureMap = { getClientRecord, listContacts, inviteContact, cancelContactInvite, resetContactAccess };

export default fixtures;
