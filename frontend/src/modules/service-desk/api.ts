// TanStack Query hooks for the Service Desk module, built on the shared
// ApiClient (useApi()). There is no live ticket event stream -- every
// mutation below invalidates the relevant query keys on success so the UI
// refetches, mirroring modules/budget/api.ts's rationale (this module
// dispatches no agent jobs either, per api.py's module docstring:
// control-plane only, no worker).
//
// Every hook defined here is wired into a page -- TicketListPage,
// TicketDetailPage, MyWorkPage, or ServiceDeskAdminPage. Budget shipped nine
// unwired hooks (including its headline action) and needed a fix round; none
// of these are left dangling.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "../../lib/ApiContext";
import type {
  ApprovalDecision,
  CannedResponseCreate,
  CannedResponseUpdate,
  CategoryCreate,
  CategoryUpdate,
  CommentCreate,
  QueueCreate,
  QueueUpdate,
  TicketAssign,
  TicketCreate,
  TicketListFilters,
  TicketStatusChange,
  TicketUpdate,
} from "./types";

// --------------------------------------------------------------------------
// Queues
// --------------------------------------------------------------------------

export function useTicketQueues(includeInactive = false) {
  const api = useApi();
  return useQuery({
    queryKey: ["service-desk-queues", includeInactive],
    queryFn: () => api.listTicketQueues(includeInactive),
  });
}

export function useCreateTicketQueueMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: QueueCreate) => api.createTicketQueue(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["service-desk-queues"] });
    },
  });
}

export function useUpdateTicketQueueMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: QueueUpdate }) =>
      api.updateTicketQueue(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["service-desk-queues"] });
    },
  });
}

// --------------------------------------------------------------------------
// Categories
// --------------------------------------------------------------------------

export function useTicketCategories(includeInactive = false) {
  const api = useApi();
  return useQuery({
    queryKey: ["service-desk-categories", includeInactive],
    queryFn: () => api.listTicketCategories(includeInactive),
  });
}

export function useCreateTicketCategoryMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: CategoryCreate) => api.createTicketCategory(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["service-desk-categories"] });
    },
  });
}

export function useUpdateTicketCategoryMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: CategoryUpdate }) =>
      api.updateTicketCategory(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["service-desk-categories"] });
    },
  });
}

// --------------------------------------------------------------------------
// Tickets
// --------------------------------------------------------------------------

export function useTickets(filters: TicketListFilters = {}, options: { enabled?: boolean } = {}) {
  const api = useApi();
  return useQuery({
    queryKey: ["service-desk-tickets", filters],
    queryFn: () => api.listTickets(filters),
    // C5 fix: `TicketListPage`'s default view now runs two of these side by
    // side (an "assigned to me" query and a wider "pool" query, see that
    // page's own comment) and toggles which one is live via `enabled` --
    // `options.enabled` left `undefined` (every other caller, e.g.
    // `MyWorkPage`) is TanStack Query's own "always enabled" default, so
    // this is backward compatible.
    enabled: options.enabled,
  });
}

export function useTicket(ticketId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["service-desk-ticket", ticketId],
    queryFn: () => api.getTicket(ticketId as string),
    enabled: ticketId != null,
  });
}

export function useCreateTicketMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    // The body is BUILT HERE, inside the mutation, not before it: a body that
    // cannot name its client yet throws `ClientNotChosen`
    // (`lib/clientSelection.ts`), which then fails the mutation -- so the
    // form's error surface says why and nothing is sent.
    mutationFn: (build: () => TicketCreate) => api.createTicket(build()),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["service-desk-tickets"] });
    },
  });
}

export function useUpdateTicketMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: TicketUpdate }) => api.updateTicket(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["service-desk-tickets"] });
      void queryClient.invalidateQueries({ queryKey: ["service-desk-ticket", variables.id] });
    },
  });
}

export function useChangeTicketStatusMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: TicketStatusChange }) =>
      api.changeTicketStatus(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["service-desk-tickets"] });
      void queryClient.invalidateQueries({ queryKey: ["service-desk-ticket", variables.id] });
      // Every status change writes a `ticket_status_history` row server-side
      // (transitions.change_status) -- the history panel must refetch too.
      void queryClient.invalidateQueries({
        queryKey: ["service-desk-ticket-history", variables.id],
      });
    },
  });
}

export function useAssignTicketMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: TicketAssign }) => api.assignTicket(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["service-desk-tickets"] });
      void queryClient.invalidateQueries({ queryKey: ["service-desk-ticket", variables.id] });
    },
  });
}

// A move re-resolves the ticket's SLA among the new client's policies
// (plan 2026-10-26 D2), so the ticket's SLA panel refetches too.
export function useMoveTicketMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, clientId }: { id: string; clientId: string }) =>
      api.moveTicket(id, clientId),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["service-desk-tickets"] });
      void queryClient.invalidateQueries({ queryKey: ["service-desk-ticket", variables.id] });
      void queryClient.invalidateQueries({ queryKey: ["sla-ticket", variables.id] });
    },
  });
}

export function useApproveTicketMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ApprovalDecision }) =>
      api.approveTicket(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["service-desk-tickets"] });
      void queryClient.invalidateQueries({ queryKey: ["service-desk-ticket", variables.id] });
    },
  });
}

export function useRejectTicketMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ApprovalDecision }) =>
      api.rejectTicket(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["service-desk-tickets"] });
      void queryClient.invalidateQueries({ queryKey: ["service-desk-ticket", variables.id] });
    },
  });
}

// --------------------------------------------------------------------------
// Status history (task 13's G1 fix -- spec §11's ticket-detail "status
// history"). Read-only, no mutation hook of its own -- `change_status`
// writes a `ticket_status_history` row server-side as a side effect, so
// `useChangeTicketStatusMutation` above is what invalidates this key.
// --------------------------------------------------------------------------

export function useTicketStatusHistory(ticketId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["service-desk-ticket-history", ticketId],
    queryFn: () => api.listTicketStatusHistory(ticketId as string),
    enabled: ticketId != null,
  });
}

// --------------------------------------------------------------------------
// Comments
// --------------------------------------------------------------------------

export function useTicketComments(ticketId: string | undefined, includeInternal = true) {
  const api = useApi();
  return useQuery({
    queryKey: ["service-desk-comments", ticketId, includeInternal],
    queryFn: () => api.listTicketComments(ticketId as string, includeInternal),
    enabled: ticketId != null,
  });
}

export function useCreateTicketCommentMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: CommentCreate }) =>
      api.createTicketComment(id, body),
    onSuccess: (_data, variables) => {
      // Prefix invalidation: matches both the include_internal=true and
      // include_internal=false variants of the comment list for this ticket.
      void queryClient.invalidateQueries({ queryKey: ["service-desk-comments", variables.id] });
    },
  });
}

// --------------------------------------------------------------------------
// Attachments
// --------------------------------------------------------------------------

export function useTicketAttachments(ticketId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["service-desk-attachments", ticketId],
    queryFn: () => api.listTicketAttachments(ticketId as string),
    enabled: ticketId != null,
  });
}

export function useUploadTicketAttachmentMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, file }: { id: string; file: File }) => api.uploadTicketAttachment(id, file),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["service-desk-attachments", variables.id] });
      // `has_attachments` on both the detail row and every list page that
      // includes this ticket needs to flip too.
      void queryClient.invalidateQueries({ queryKey: ["service-desk-ticket", variables.id] });
      void queryClient.invalidateQueries({ queryKey: ["service-desk-tickets"] });
    },
  });
}

// --------------------------------------------------------------------------
// Directory reads that back the pickers (UX remediation cycle 6).
//
// NOT service-desk routes. `GET /users` and `GET /devices` carry their own
// server-side permissions, entirely independent of `ticket.write` -- exactly
// the arrangement `DeviceSummaryPanel` already documents on the ticket
// detail page, and invariant #6 keeps them there rather than proxying either
// behind a service-desk route to dodge the check.
//
// `retry: false` so a genuine 403 settles at once instead of retrying
// against the same permission wall, and the picker falls through to its
// `emptyHint` rather than spinning. A refused directory must never break the
// form it decorates: `Assign to` and `Device` are optional, and Requester is
// still typeable through the picker's own search box.
// --------------------------------------------------------------------------

export function useDirectoryUsers() {
  const api = useApi();
  return useQuery({
    queryKey: ["service-desk-directory-users"],
    queryFn: async () => (await api.listUsers()).users,
    retry: false,
    staleTime: 60_000,
  });
}

export function useDirectoryDevices() {
  const api = useApi();
  return useQuery({
    queryKey: ["service-desk-directory-devices"],
    queryFn: async () => (await api.listDevices()).items,
    retry: false,
    staleTime: 60_000,
  });
}

// --------------------------------------------------------------------------
// Quick actions on the ticket page
// --------------------------------------------------------------------------

/**
 * Resolve in one click, optionally with a reply. One server call and one
 * transaction: the reply and the resolution land together or not at all.
 */
export function useResolveTicketMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, reply }: { id: string; reply?: CommentCreate }) =>
      api.resolveTicket(id, reply ? { reply } : {}),
    onSettled: (_data, _error, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["service-desk-tickets"] });
      void queryClient.invalidateQueries({ queryKey: ["service-desk-ticket", variables.id] });
      void queryClient.invalidateQueries({ queryKey: ["service-desk-comments", variables.id] });
      void queryClient.invalidateQueries({
        queryKey: ["service-desk-ticket-history", variables.id],
      });
      void queryClient.invalidateQueries({ queryKey: ["sla-ticket", variables.id] });
    },
  });
}

// --------------------------------------------------------------------------
// Saved replies (canned responses)
// --------------------------------------------------------------------------

export function useCannedResponses(options: { enabled?: boolean } = {}) {
  const api = useApi();
  return useQuery({
    queryKey: ["service-desk-canned-responses"],
    queryFn: () => api.listCannedResponses(),
    enabled: options.enabled,
    retry: false,
    staleTime: 60_000,
  });
}

function useCannedInvalidation() {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["service-desk-canned-responses"] });
  };
}

export function useCreateCannedResponseMutation() {
  const api = useApi();
  const invalidate = useCannedInvalidation();
  return useMutation({
    mutationFn: (body: CannedResponseCreate) => api.createCannedResponse(body),
    onSuccess: invalidate,
  });
}

export function useUpdateCannedResponseMutation() {
  const api = useApi();
  const invalidate = useCannedInvalidation();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: CannedResponseUpdate }) =>
      api.updateCannedResponse(id, body),
    onSuccess: invalidate,
  });
}

export function useDeleteCannedResponseMutation() {
  const api = useApi();
  const invalidate = useCannedInvalidation();
  return useMutation({
    mutationFn: (id: string) => api.deleteCannedResponse(id),
    onSuccess: invalidate,
  });
}

// --------------------------------------------------------------------------
// Knowledge-base suggestions for a ticket subject. `/docs/suggest` needs
// `docs.read`; callers enable this only for holders, and a refusal shows
// nothing.
// --------------------------------------------------------------------------

export function useArticleSuggestions(subject: string, enabled: boolean) {
  const api = useApi();
  const q = subject.trim();
  return useQuery({
    queryKey: ["service-desk-article-suggestions", q],
    queryFn: () => api.suggestDocArticles(q),
    enabled: enabled && q.length > 0,
    retry: false,
    staleTime: 5 * 60_000,
  });
}
