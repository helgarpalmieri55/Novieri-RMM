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
  ContactOut,
  ContactUpdate,
  SiteCreate,
  SiteUpdate,
} from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
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

export function useUpdateClientMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ClientOut, ApiError, ClientUpdate>({
    mutationFn: (body) => api.updateClient(clientId, body),
    onSuccess: () => afterClientChange(queryClient, clientId),
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
