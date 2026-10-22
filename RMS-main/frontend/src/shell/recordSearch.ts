/**
 * Records the top-bar search can find: devices by name or address, tickets by
 * number or subject, and a requester's own tickets.
 *
 * Each source is queried only when the account holds the code its list route
 * requires, so typing never fires a request that answers 403. That is
 * courtesy, not control: the server scopes every list by org and permission
 * whatever this file asks for (invariants 5 and 6).
 */

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { useT } from "../i18n/context";
import { translate } from "../i18n/core";
import { useApi } from "../lib/ApiContext";
import { joinWords } from "../lib/joinWords";

export interface RecordHit {
  id: string;
  label: string;
  sub?: string;
  type: string;
  icon: string;
  to: string;
}

/** Below this, a query matches too much to be worth a round trip. */
export const MIN_RECORD_QUERY = 2;
/** Wait this long after the last keystroke before asking the server. */
export const RECORD_SEARCH_DEBOUNCE_MS = 250;
const PER_SOURCE = 5;
/**
 * The bounded page the client-side matches look through. Neither the ticket
 * list's `q` (subject and description only) nor the portal's list (no search
 * parameter at all) can match a ticket number, so those matches are made in
 * the browser over the newest page, and older tickets are missed.
 */
const CLIENT_FILTER_PAGE = 100;

/**
 * What the field promises and what its empty state explains, built from the
 * same permission checks `useRecordSearch` uses, so the prompt never names a
 * record kind this account's search will not look up.
 */
export function searchPrompt(
  permissions: readonly string[],
  portalOnly: boolean,
): { placeholder: string; hint: string } {
  if (portalOnly) {
    return {
      placeholder: translate("shell.search.portalPlaceholder"),
      hint: translate("shell.search.portalHint"),
    };
  }
  const kinds: string[] = [];
  if (permissions.includes("devices.read")) {
    kinds.push(translate("shell.search.kindDevice"), translate("shell.search.kindAddress"));
  }
  if (permissions.includes("ticket.read")) kinds.push(translate("shell.search.kindTicket"));
  kinds.push(translate("shell.search.kindPage"));
  const list = joinWords(kinds, "or");
  const ask = permissions.includes("ai.use") ? translate("shell.search.askSuffix") : "";
  const matched: string[] = [];
  if (permissions.includes("devices.read")) matched.push(translate("shell.search.matchDevices"));
  if (permissions.includes("ticket.read")) matched.push(translate("shell.search.matchTickets"));
  matched.push(translate("shell.search.matchPages"));
  return {
    placeholder: translate("shell.search.placeholder", { list, ask }),
    hint: translate("shell.search.hint", { list: joinWords(matched) }),
  };
}

function useDebounced(value: string, ms: number): string {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

const includes = (haystack: string | null | undefined, needle: string) =>
  (haystack ?? "").toLowerCase().includes(needle.toLowerCase());

export function useRecordSearch(
  rawQuery: string,
  permissions: readonly string[],
): { hits: RecordHit[]; pending: boolean } {
  useT();
  const api = useApi();
  const typed = rawQuery.trim();
  const q = useDebounced(typed, RECORD_SEARCH_DEBOUNCE_MS);
  const long = q.length >= MIN_RECORD_QUERY;
  const canDevices = permissions.includes("devices.read");
  const canTickets = permissions.includes("ticket.read");
  // A requester sees their own tickets through the portal's routes; an
  // operator who also holds the portal code already has the full desk.
  const canPortal = !canTickets && permissions.includes("portal.ticket.read");
  const wantsNumber = /\d/.test(q);

  const devices = useQuery({
    queryKey: ["search", "devices", q],
    queryFn: () => api.listDevices({ q, limit: PER_SOURCE }),
    enabled: long && canDevices,
    staleTime: 30_000,
  });
  const ticketsBySubject = useQuery({
    queryKey: ["search", "tickets", q],
    queryFn: () => api.listTickets({ q, limit: PER_SOURCE }),
    enabled: long && canTickets,
    staleTime: 30_000,
  });
  const ticketPage = useQuery({
    queryKey: ["search", "tickets", "recent"],
    queryFn: () => api.listTickets({ limit: CLIENT_FILTER_PAGE }),
    enabled: long && canTickets && wantsNumber,
    staleTime: 30_000,
  });
  const portalPage = useQuery({
    queryKey: ["search", "portal-tickets"],
    queryFn: () => api.listPortalTickets({ limit: CLIENT_FILTER_PAGE }),
    enabled: long && canPortal,
    staleTime: 30_000,
  });

  if (!long || typed !== q) {
    return { hits: [], pending: typed.length >= MIN_RECORD_QUERY };
  }

  const hits: RecordHit[] = [];
  for (const device of devices.data?.items ?? []) {
    hits.push({
      id: `device:${device.id}`,
      label: device.hostname ?? device.primary_ip ?? translate("shell.search.unnamedDevice"),
      sub: [device.primary_ip, device.os_version].filter(Boolean).join(" · ") || undefined,
      type: translate("shell.search.typeDevice"),
      icon: "devices",
      to: `/devices/${device.id}`,
    });
  }

  const tickets = new Map<string, { id: string; number: string; subject: string }>();
  for (const ticket of ticketPage.data?.items ?? []) {
    if (includes(ticket.number, q)) tickets.set(ticket.id, ticket);
  }
  for (const ticket of ticketsBySubject.data?.items ?? []) tickets.set(ticket.id, ticket);
  for (const ticket of [...tickets.values()].slice(0, PER_SOURCE)) {
    hits.push({
      id: `ticket:${ticket.id}`,
      label: `${ticket.number} · ${ticket.subject}`,
      type: translate("shell.search.typeTicket"),
      icon: "ticket",
      to: `/service-desk/tickets/${ticket.id}`,
    });
  }

  const mine = (portalPage.data ?? []).filter(
    (ticket) => includes(ticket.number, q) || includes(ticket.subject, q),
  );
  for (const ticket of mine.slice(0, PER_SOURCE)) {
    hits.push({
      id: `portal-ticket:${ticket.id}`,
      label: `${ticket.number} · ${ticket.subject}`,
      type: translate("shell.search.typeTicket"),
      icon: "ticket",
      to: `/portal/tickets/${ticket.id}`,
    });
  }

  const pending = [devices, ticketsBySubject, ticketPage, portalPage].some(
    (query) => query.isFetching,
  );
  return { hits, pending };
}
