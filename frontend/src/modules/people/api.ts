// TanStack Query hooks for People. Every mutation refreshes the person it
// touched and the list; release and sign-in changes also refresh the owning
// module's own caches, since those pages show the same rows.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import type { AccountOut, ChecklistKind, PersonCreate, PersonUpdate, StepState } from "./types";

export function useMe() {
  const api = useApi();
  return useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
}

export function useCan(code: string): boolean {
  const me = useMe();
  return (me.data?.permissions ?? []).includes(code);
}

export function usePeople(params: { q?: string; status?: string } = {}, enabled = true) {
  const api = useApi();
  return useQuery({
    queryKey: ["people", params.q ?? "", params.status ?? ""],
    queryFn: () => api.listPeople(params),
    enabled,
  });
}

export function usePerson(personId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["person", personId],
    queryFn: () => api.getPerson(personId as string),
    enabled: personId != null,
  });
}

export function useUnmatched(enabled = true) {
  const api = useApi();
  return useQuery({
    queryKey: ["people-unmatched"],
    queryFn: () => api.listUnmatched(),
    enabled,
    retry: false,
  });
}

export function useDevicePerson(deviceId: string | undefined, enabled: boolean) {
  const api = useApi();
  return useQuery({
    queryKey: ["device-person", deviceId],
    queryFn: () => api.getDevicePerson(deviceId as string),
    enabled: enabled && deviceId != null,
    retry: false,
  });
}

export function usePersonForUser(userId: string | null | undefined, enabled: boolean) {
  const api = useApi();
  return useQuery({
    queryKey: ["person-for-user", userId],
    queryFn: () => api.personForUser(userId as string),
    enabled: enabled && userId != null,
    retry: false,
  });
}

function useRefresh() {
  const queryClient = useQueryClient();
  return (personId?: string) => {
    void queryClient.invalidateQueries({ queryKey: ["people"] });
    void queryClient.invalidateQueries({ queryKey: ["people-unmatched"] });
    if (personId) void queryClient.invalidateQueries({ queryKey: ["person", personId] });
  };
}

export function useCreatePerson() {
  const api = useApi();
  const refresh = useRefresh();
  return useMutation({
    // The body is BUILT HERE, inside the mutation, not before it: a body that
    // cannot name its client yet throws `InternalScopeUnknown`
    // (`lib/internalClient.ts`), which then fails the mutation -- so the
    // form's error surface says why and nothing is sent.
    mutationFn: (build: () => PersonCreate) => api.createPerson(build()),
    onSuccess: () => refresh(),
  });
}

export function useUpdatePerson(personId: string) {
  const api = useApi();
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (body: PersonUpdate) => api.updatePerson(personId, body),
    onSuccess: () => refresh(personId),
  });
}

export function useSetDevicePerson(deviceId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (personId: string | null) => api.setDevicePerson(deviceId, personId),
    onSuccess: () => {
      refresh();
      void queryClient.invalidateQueries({ queryKey: ["device-person", deviceId] });
      void queryClient.invalidateQueries({ queryKey: ["person"] });
      // The fleet list's "Assigned to" column shows the same link.
      void queryClient.invalidateQueries({ queryKey: ["devices"] });
    },
  });
}

export function useClearDeviceOverride(deviceId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  const refresh = useRefresh();
  return useMutation({
    mutationFn: () => api.clearDevicePersonOverride(deviceId),
    onSuccess: () => {
      refresh();
      void queryClient.invalidateQueries({ queryKey: ["device-person", deviceId] });
      void queryClient.invalidateQueries({ queryKey: ["person"] });
      // The fleet list's "Assigned to" column shows the same link.
      void queryClient.invalidateQueries({ queryKey: ["devices"] });
    },
  });
}

export function useLinkSeat() {
  const api = useApi();
  const refresh = useRefresh();
  return useMutation({
    mutationFn: ({ seatId, personId }: { seatId: string; personId: string | null }) =>
      api.linkSeatToPerson(seatId, personId),
    onSuccess: (person) => refresh(person.id),
  });
}

export function useStartChecklist(personId: string) {
  const api = useApi();
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (kind: ChecklistKind) => api.startChecklist(personId, kind),
    onSuccess: () => refresh(personId),
  });
}

export function useUpdateStep(personId: string, checklistId: string) {
  const api = useApi();
  const refresh = useRefresh();
  return useMutation({
    mutationFn: ({ key, state, note }: { key: string; state: StepState; note: string }) =>
      api.updateChecklistStep(personId, checklistId, key, { state, note }),
    onSuccess: () => refresh(personId),
  });
}

export function useCloseChecklist(personId: string, checklistId: string) {
  const api = useApi();
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (outcome: "complete" | "cancel") =>
      api.closeChecklist(personId, checklistId, outcome),
    onSuccess: () => refresh(personId),
  });
}

/** Release a seat through the subscriptions module's own endpoint. */
export function useReleaseSeat(personId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (seatId: string) => api.releaseSeat(seatId),
    onSuccess: () => {
      refresh(personId);
      void queryClient.invalidateQueries({ queryKey: ["subscription-seats"] });
      void queryClient.invalidateQueries({ queryKey: ["subscription-idle-seats"] });
    },
  });
}

/** Release a licence through the licences module's own endpoint. */
export function useReleaseLicence(personId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (allocationId: string) => api.releaseAllocation(allocationId),
    onSuccess: () => {
      refresh(personId);
      void queryClient.invalidateQueries({ queryKey: ["license-allocations"] });
    },
  });
}

/**
 * Turn a sign-in off through the owning module's own endpoint: Settings'
 * user endpoint for staff, which refuses a contact; a client's contact is
 * switched off as a contact (`PATCH /contacts/{id}`, MSP clients phase 1).
 */
export function useTurnOffSignIn(personId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async (account: Pick<AccountOut, "user_id" | "kind">): Promise<void> => {
      if (account.kind === "contact") {
        await api.updateContact(account.user_id, { status: "disabled" });
      } else {
        await api.setUserEnabled(account.user_id, false);
      }
    },
    onSuccess: () => {
      refresh(personId);
      void queryClient.invalidateQueries({ queryKey: ["users"] });
    },
  });
}

/** Who may turn this account's sign-in off: `users.manage` for staff, `clients.manage` for a contact. */
export function turnOffPermission(account: Pick<AccountOut, "kind">): string {
  return account.kind === "contact" ? "clients.manage" : "users.manage";
}
