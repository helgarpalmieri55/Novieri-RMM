/**
 * The Clients page's reads and writes (MSP clients phase 3, design §4.1),
 * over `backend/app/api/v1/clients.py`. Every write refreshes what names a
 * client elsewhere: the Users tab's list (`CLIENTS_KEY`) and `/me`, whose
 * `clients` carry the names and the optional modules the menu follows.
 */
import { type QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type {
  ApiError,
  ClientCreate,
  ClientOut,
  ClientSiteOut,
  ClientUpdate,
  ContactCreate,
  ContactInviteOut,
  ContactOut,
  ContactUpdate,
  SiteCreate,
  SiteUpdate,
} from "../../lib/api";
import { sendOnce, type StepUpRun } from "../../components/stepUp";
import { useApi } from "../../lib/ApiContext";
import { useSecretMutation } from "../../lib/secretMutation";
import { CLIENTS_KEY } from "../settings/api";

export const CLIENT_LIST_KEY = "clients-page";
export const CLIENT_KEY = "client";
/** The same key the site pickers and IPAM read a client's sites under. */
export const CLIENT_SITES_KEY = "client-sites";
export const CONTACTS_KEY = "client-contacts";

const NO_RETRY = { retry: false } as const;

function afterClientChange(queryClient: QueryClient, clientId: string | null): void {
  void queryClient.invalidateQueries({ queryKey: [CLIENT_LIST_KEY] });
  void queryClient.invalidateQueries({ queryKey: [CLIENTS_KEY] });
  void queryClient.invalidateQueries({ queryKey: ["me"] });
  if (clientId !== null) void queryClient.invalidateQueries({ queryKey: [CLIENT_KEY, clientId] });
}

export function useClientList(includeArchived: boolean) {
  const api = useApi();
  return useQuery<ClientOut[], ApiError>({
    queryKey: [CLIENT_LIST_KEY, { includeArchived }],
    queryFn: () => api.listClients(includeArchived ? { include_archived: true } : {}),
    ...NO_RETRY,
  });
}

/** `enabled: false` while the console works on another client (the read would be a 404). */
export function useClientRecord(clientId: string, enabled: boolean) {
  const api = useApi();
  return useQuery<ClientOut, ApiError>({
    queryKey: [CLIENT_KEY, clientId],
    queryFn: () => api.getClientRecord(clientId),
    enabled,
    ...NO_RETRY,
  });
}

/** Archived sites too: the Sites tab lists them, the pickers never do. */
export function useClientSitesWithArchived(clientId: string) {
  const api = useApi();
  return useQuery<ClientSiteOut[], ApiError>({
    queryKey: [CLIENT_SITES_KEY, clientId, "with-archived"],
    queryFn: () => api.listClientSites(clientId, { include_archived: true }),
    ...NO_RETRY,
  });
}

export function useClientContacts(clientId: string) {
  const api = useApi();
  return useQuery<ContactOut[], ApiError>({
    queryKey: [CONTACTS_KEY, clientId],
    queryFn: () => api.listContacts(clientId),
    ...NO_RETRY,
  });
}

export function useCreateClientMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ClientOut, ApiError, ClientCreate>({
    mutationFn: (body) => api.createClient(body),
    onSuccess: (saved) => afterClientChange(queryClient, saved.id),
  });
}

/**
 * THE SAVED ANSWER IS THE RECORD AT ONCE. The save is answered before the
 * record is asked again, and the form re-enables on that answer: a page
 * still drawing the record from before would build the next change on it --
 * the Modules tab's second switch sent the old `enabled_modules` and undid
 * the first on the server. So the server's answer goes into the record's
 * cache before anything is refreshed.
 *
 * `run` is the edit form's `useStepUp().run`: a change to the client's
 * Microsoft tenants needs the second factor confirmed in the last 10 minutes
 * (design §2.5, S1). The Modules tab sends only `enabled_modules`, which
 * never does, and sends once.
 */
export function useUpdateClientMutation(clientId: string, run: StepUpRun = sendOnce) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ClientOut, Error, ClientUpdate>({
    mutationFn: (body) => run(() => api.updateClient(clientId, body)),
    onSuccess: (saved) => {
      queryClient.setQueryData<ClientOut>([CLIENT_KEY, clientId], saved);
      afterClientChange(queryClient, clientId);
    },
  });
}

export function useArchiveClientMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ClientOut, ApiError, void>({
    mutationFn: () => api.archiveClient(clientId),
    onSuccess: () => afterClientChange(queryClient, clientId),
  });
}

function afterSiteChange(queryClient: QueryClient, clientId: string): void {
  // Every reader of a client's sites (pickers, IPAM, this page), and the
  // client's site count.
  void queryClient.invalidateQueries({ queryKey: [CLIENT_SITES_KEY] });
  afterClientChange(queryClient, clientId);
}

export function useCreateSiteMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ClientSiteOut, ApiError, SiteCreate>({
    mutationFn: (body) => api.createClientSite(clientId, body),
    onSuccess: () => afterSiteChange(queryClient, clientId),
  });
}

export function useUpdateSiteMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ClientSiteOut, ApiError, { siteId: string; body: SiteUpdate }>({
    mutationFn: ({ siteId, body }) => api.updateSite(siteId, body),
    onSuccess: () => afterSiteChange(queryClient, clientId),
  });
}

export function useArchiveSiteMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ClientSiteOut, ApiError, string>({
    mutationFn: (siteId) => api.archiveSite(siteId),
    onSuccess: () => afterSiteChange(queryClient, clientId),
  });
}

export function useCreateContactMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ContactOut, ApiError, ContactCreate>({
    mutationFn: (body) => api.createContact(clientId, body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [CONTACTS_KEY, clientId] }),
  });
}

export function useUpdateContactMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ContactOut, ApiError, { contactId: string; body: ContactUpdate }>({
    mutationFn: ({ contactId, body }) => api.updateContact(contactId, body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [CONTACTS_KEY, clientId] }),
  });
}

/**
 * Invite (or invite again) a contact. THE ANSWER CAN CARRY A ONE-TIME LINK:
 * `useSecretMutation` (`lib/secretMutation.ts`) -- the page's own callbacks
 * take it into the page's state, then the mutation forgets body and answer
 * (A2-69; `test/secretMutations.test.ts`).
 */
export function useInviteContactMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useSecretMutation<ContactInviteOut, ApiError, string>({
    mutationFn: (contactId) => api.inviteContact(clientId, contactId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [CONTACTS_KEY, clientId] }),
  });
}

export function useCancelContactInviteMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (contactId) => api.cancelContactInvite(clientId, contactId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [CONTACTS_KEY, clientId] }),
  });
}

/**
 * Through the tab's `useStepUp().run`: the server asks for the technician's
 * recent confirmation. Its answer can carry the new one-time link, as an
 * invitation's: `useSecretMutation` (A2-69).
 */
export function useResetContactAccessMutation(clientId: string, run: StepUpRun) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useSecretMutation<ContactInviteOut, Error, string>({
    mutationFn: (contactId) => run(() => api.resetContactAccess(clientId, contactId)),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [CONTACTS_KEY, clientId] }),
  });
}
