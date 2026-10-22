// TanStack Query hooks for the Email intake module, built on the shared
// ApiClient (useApi()). This module dispatches no agent job and registers no
// job type -- control-plane only, like notification and portal -- and there
// is no live event stream, so every mutation below invalidates the relevant
// query keys on success and the UI refetches.
//
// Every hook defined here is wired into EmailIntakeAdminPage's panels; none
// are left dangling.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "../../lib/ApiContext";
import type { ApiError } from "../../lib/api";
import type {
  MailboxCreate,
  MailboxOut,
  MailboxUpdate,
  MessageLogFilters,
  MessageLogOut,
  OAuthAppOut,
  OAuthAppUpdate,
} from "./types";

// --------------------------------------------------------------------------
// Mailboxes
// --------------------------------------------------------------------------

export function useEmailMailboxes() {
  const api = useApi();
  return useQuery<MailboxOut[], ApiError>({
    queryKey: ["email-intake-mailboxes"],
    queryFn: () => api.listEmailMailboxes(),
    // A genuine 403 (caller lacks email_intake.read) should settle
    // immediately rather than retry against the same permission wall.
    retry: false,
  });
}

export function useCreateEmailMailboxMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: MailboxCreate) => api.createEmailMailbox(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["email-intake-mailboxes"] });
    },
  });
}

export function useUpdateEmailMailboxMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: MailboxUpdate }) =>
      api.updateEmailMailbox(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["email-intake-mailboxes"] });
    },
  });
}

// Deleting a mailbox also drops its vault-held secret and its `Credential`
// row, server-side and in the same transaction -- and `inbound_message`
// rows for it go with it (`mailbox_id` is a NOT NULL FK). The dialog that
// calls this says so; that decision is the backend's, not the UI's.
export function useDeleteEmailMailboxMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteEmailMailbox(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["email-intake-mailboxes"] });
      // A deleted mailbox takes its messages with it.
      void queryClient.invalidateQueries({ queryKey: ["email-intake-messages"] });
    },
  });
}

// The connection test writes no mailbox row and deliberately does not touch
// `last_polled_at`/`last_error` (poller.test_mailbox: those describe the
// SCHEDULED poll's health, and a manual test overwriting them would erase
// the failure the operator is diagnosing). So there is nothing to
// invalidate -- the result is ephemeral and rendered inline where it was
// triggered, exactly as notification's test-send is.
export function useTestEmailMailboxMutation() {
  const api = useApi();
  return useMutation({
    mutationFn: (id: string) => api.testEmailMailbox(id),
  });
}

// --------------------------------------------------------------------------
// The message log
// --------------------------------------------------------------------------

// ONE QUERY SERVES BOTH THE LIST AND THE QUARANTINED COUNT, and that is a
// property of this endpoint rather than an economy: `MessageLogOut` carries
// `quarantined_count` computed over the whole SCOPE and deliberately NOT
// narrowed by `disposition` (messages.py). So unlike notification's
// dead-letter count -- which needs its own `status=dead` query precisely
// because `total` there IS narrowed -- the count here stays correct while
// the list is filtered, and a second request would only be able to disagree.
export function useEmailIntakeMessages(filters: MessageLogFilters = {}) {
  const api = useApi();
  return useQuery<MessageLogOut, ApiError>({
    queryKey: ["email-intake-messages", filters],
    queryFn: () => api.listEmailIntakeMessages(filters),
    retry: false,
  });
}

// --------------------------------------------------------------------------
// Microsoft 365 sign-in (mail-oauth design §4.4)
// --------------------------------------------------------------------------

const OAUTH_APP_KEY = ["email-intake-oauth-app"];
const MAILBOXES_KEY = ["email-intake-mailboxes"];

export function useEmailOAuthApp() {
  const api = useApi();
  return useQuery<OAuthAppOut, ApiError>({
    queryKey: OAUTH_APP_KEY,
    queryFn: () => api.getEmailOAuthApp(),
    retry: false,
  });
}

export function useSaveEmailOAuthAppMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: OAuthAppUpdate) => api.saveEmailOAuthApp(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: OAUTH_APP_KEY });
      // Saving the app gives a mailbox Microsoft refused another try.
      void queryClient.invalidateQueries({ queryKey: MAILBOXES_KEY });
    },
  });
}

export function useStartMailboxOAuthMutation() {
  const api = useApi();
  return useMutation({ mutationFn: (id: string) => api.startMailboxOAuth(id) });
}

export function useCompleteMailboxOAuthMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { state: string; code: string }) => api.completeMailboxOAuth(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: MAILBOXES_KEY });
    },
  });
}

export function useAbandonMailboxOAuthMutation() {
  const api = useApi();
  return useMutation({ mutationFn: (body: { state: string }) => api.abandonMailboxOAuth(body) });
}

export function useDisconnectMailboxOAuthMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.disconnectMailboxOAuth(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: MAILBOXES_KEY });
    },
  });
}
