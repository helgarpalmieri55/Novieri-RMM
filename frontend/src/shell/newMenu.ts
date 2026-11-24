/**
 * THE "NEW" MENU'S ENTRIES (redesign design §1.2): Ticket, Client, Contact,
 * Computer, Document and Change, each a real destination (decision D8:
 * `?new=` opens the page's own create dialog) and each shown only with the
 * code that page's own create button checks. Cosmetic, like the rail
 * (invariant 6): the server refuses the write either way.
 */
import type { MeClient } from "../lib/api";
import { ADD_COMPUTER_TO } from "./nav";

export type NewMenuKind = "ticket" | "client" | "contact" | "device" | "document" | "change";

export interface NewMenuEntry {
  readonly kind: NewMenuKind;
  readonly to: string;
  readonly icon: string;
}

export interface NewMenuInput {
  /** `/me.permissions`: codes held in at least one client of the current choice. */
  readonly permissions: readonly string[];
  /** `/me.permissions_everywhere`: codes held in every client. */
  readonly everywhere: readonly string[];
  /** `/me.clients`, each with the codes the person holds there. */
  readonly clients: readonly MeClient[];
  /** The client chosen in the top bar; `null` is every client. */
  readonly selected: string | null;
  /** The optional modules on for the current choice. */
  readonly modules: ReadonlySet<string>;
}

export function newMenuEntries(input: NewMenuInput): NewMenuEntry[] {
  const held = new Set(input.permissions);
  const entries: NewMenuEntry[] = [];
  // TicketListPage's New ticket: `ticket.write`.
  if (held.has("ticket.write")) entries.push({ kind: "ticket", to: "/service-desk?new=ticket", icon: "ticket" });
  // ClientsPage's New client: `clients.manage` in every client (a global route).
  if (input.everywhere.includes("clients.manage")) entries.push({ kind: "client", to: "/clients?new=client", icon: "building" });
  // ContactsTab's Add: `clients.manage` in THAT client, never Unassigned.
  const contactable = input.clients.filter(
    (client) => client.kind !== "unassigned" && client.permissions.includes("clients.manage"),
  );
  if (input.selected !== null) {
    const chosen = contactable.find((client) => client.id === input.selected);
    if (chosen) entries.push({ kind: "contact", to: `/clients/${chosen.id}?tab=contacts&new=contact`, icon: "user" });
  } else if (contactable.length > 0) {
    entries.push({ kind: "contact", to: "/clients?new=contact", icon: "user" });
  }
  // The single "Add a computer" entry (nav.ts): `devices.read`, as the top bar had it.
  if (held.has("devices.read")) entries.push({ kind: "device", to: ADD_COMPUTER_TO, icon: "devices" });
  // DocsPage's New article: `docs.manage`.
  if (held.has("docs.manage")) entries.push({ kind: "document", to: "/docs?new=article", icon: "book" });
  // ChangesPage's New change: `change.request`, and the module on for the choice.
  if (input.modules.has("change") && held.has("change.request")) {
    entries.push({ kind: "change", to: "/changes?new=change", icon: "changes" });
  }
  return entries;
}
