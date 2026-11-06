/**
 * Fixtures for the Clients page: one client read by id, and its contacts. The
 * list (`listClients`) is the Settings fixtures', the sites (`listClientSites`)
 * IPAM's and the install link Agent packages', so the same client reads the
 * same on every screen; the records below match `settings/fixtures.ts`.
 */
import type { FixtureMap } from "../../fixtures/client";
import { CUSTOMER_ID } from "../../fixtures/core";
import type { ClientOut, ContactOut } from "../../lib/api";

const CLIENTS: ClientOut[] = [
  { id: "org-toeshee", name: "Toeshee", kind: "internal", status: "active", email_domains: ["toeshee.example"], phone: "", address: "", website: "", notes: "", enabled_modules: ["budget", "bcp", "change"], device_count: 42, site_count: 1 },
  { id: CUSTOMER_ID, name: "Andes Logistics", kind: "customer", status: "active", email_domains: ["andeslogistics.example"], phone: "+57 1 555 0100", address: "Calle 80 #10-20, Bogotá", website: "andeslogistics.example", notes: "", enabled_modules: [], device_count: 12, site_count: 2 },
  { id: "cli-unassigned", name: "Unassigned", kind: "unassigned", status: "active", email_domains: [], phone: "", address: "", website: "", notes: "", enabled_modules: [], device_count: 0, site_count: 0 },
];

const CONTACTS: ContactOut[] = [
  { id: "usr-contact-1", client_id: CUSTOMER_ID, kind: "contact", email: "ana.rojas@andeslogistics.example", display_name: "Ana Rojas", phone: "+57 1 555 0101", status: "active" },
  { id: "usr-contact-2", client_id: CUSTOMER_ID, kind: "contact", email: "compras@andeslogistics.example", display_name: "Compras", phone: null, status: "disabled" },
];

const getClientRecord: FixtureMap["getClientRecord"] = (clientId) => {
  const found = CLIENTS.find((client) => client.id === clientId);
  if (!found) throw new Error(`no such client: ${clientId}`);
  return found;
};

const listContacts: FixtureMap["listContacts"] = (clientId) =>
  CONTACTS.filter((contact) => contact.client_id === clientId);

const fixtures: FixtureMap = { getClientRecord, listContacts };

export default fixtures;
