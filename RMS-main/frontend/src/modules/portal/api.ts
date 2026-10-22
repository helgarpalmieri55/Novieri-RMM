// TanStack Query hooks for the end-user portal, built on the shared
// ApiClient (useApi()) -- mirrors modules/service-desk/api.ts's shape
// exactly. Every hook here is wired into a page (MyTicketsPage,
// PortalTicketDetailPage, or NewTicketPage); none are left dangling
// (the Budget module's nine-unwired-hooks lesson).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useApi } from "../../lib/ApiContext";
import type { PortalCommentCreate, PortalTicketCreate, PortalTicketListFilters } from "./types";

// --------------------------------------------------------------------------
// Categories (spec §2.1: read-only org refdata, needed to file a ticket)
// --------------------------------------------------------------------------

// `retry: false` -- mirrors service-desk's `DeviceSummaryPanel` precedent:
// a portal caller may hold `portal.ticket.write` without `portal.ticket.read`
// in theory (both codes are granted together to the seeded `requester` role,
// but nothing about this client assumes that will always be true), and a
// genuine 403 should settle immediately rather than retry against the same
// permission wall.
export function usePortalCategories() {
  const api = useApi();
  return useQuery({
    queryKey: ["portal-categories"],
    queryFn: () => api.listPortalCategories(),
    retry: false,
  });
}

// --------------------------------------------------------------------------
// Tickets
// --------------------------------------------------------------------------

export function usePortalTickets(filters: PortalTicketListFilters = {}) {
  const api = useApi();
  return useQuery({
    queryKey: ["portal-tickets", filters],
    queryFn: () => api.listPortalTickets(filters),
  });
}

export function usePortalTicket(ticketId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["portal-ticket", ticketId],
    queryFn: () => api.getPortalTicket(ticketId as string),
    enabled: ticketId != null,
  });
}

export function useCreatePortalTicketMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: PortalTicketCreate) => api.createPortalTicket(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["portal-tickets"] });
    },
  });
}

// --------------------------------------------------------------------------
// Comments
// --------------------------------------------------------------------------

export function useCreatePortalCommentMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: PortalCommentCreate }) =>
      api.createPortalComment(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["portal-ticket", variables.id] });
    },
  });
}

// --------------------------------------------------------------------------
// Attachments -- upload only. There is no separate `GET .../attachments`
// list route in the real API; the list lives on `PortalTicketDetail.
// attachments` (I2 fix) instead, returned by the SAME `GET /portal/tickets/
// {id}` call `usePortalTicket` already uses -- so a successful upload
// invalidates that query (mirrors `useCreatePortalCommentMutation` exactly)
// rather than being tracked in local component state, and the uploaded file
// survives a page reload.
// --------------------------------------------------------------------------

export function useUploadPortalAttachmentMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, file }: { id: string; file: File }) => api.uploadPortalAttachment(id, file),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["portal-ticket", variables.id] });
    },
  });
}

// --------------------------------------------------------------------------
// Help articles staff published to the portal (GET /portal/help/...)
// --------------------------------------------------------------------------

/** How long typing must pause before the subject is used to look for help. */
export const HELP_SUGGEST_DEBOUNCE_MS = 400;
// The server ignores words shorter than this, so asking earlier finds nothing.
const MIN_HELP_QUERY = 3;

function useDebounced(value: string, ms: number): string {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

// Secondary to filing the ticket: a failure shows nothing rather than an
// error, and there is no retry.
export function usePortalHelpSuggestions(subject: string, enabled: boolean) {
  const api = useApi();
  const q = useDebounced(subject.trim(), HELP_SUGGEST_DEBOUNCE_MS);
  return useQuery({
    queryKey: ["portal-help-suggest", q],
    queryFn: () => api.suggestPortalHelp(q),
    enabled: enabled && q.length >= MIN_HELP_QUERY,
    retry: false,
    staleTime: 60_000,
  });
}

export function usePortalHelpArticle(articleId: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: ["portal-help-article", articleId],
    queryFn: () => api.getPortalHelpArticle(articleId as string),
    enabled: articleId != null,
    retry: false,
  });
}
