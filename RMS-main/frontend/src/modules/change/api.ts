// TanStack Query hooks for the change-management module, built on the
// shared ApiClient (useApi()). There is no live change-management event
// stream — every mutation below invalidates the relevant query keys on
// success so the UI refetches, rather than subscribing to a websocket event
// that doesn't exist for this module (mirrors modules/provisioning/api.ts's
// rationale, itself mirroring modules/backup/api.ts).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "../../lib/ApiContext";
import type { Me } from "../../lib/api";
import type {
  ApprovalDecisionRequest,
  ChangeCreateRequest,
  ChangeGateSetRequest,
  ChangeUpdateRequest,
  LinkCreateRequest,
  TransitionRequest,
} from "./types";

// --------------------------------------------------------------------------
// The reader's timezone (UX remediation cycle 5)
// --------------------------------------------------------------------------

/**
 * The zone the signed-in operator works in, for `lib/datetime`.
 *
 * A HOOK AND NOT A PROP, the same reasoning as `modules/health/api.ts` and
 * `modules/sla/api.ts`. Every instant this module renders sits well below the
 * page component -- `ApprovalQueue`'s rows are components of their own, and
 * `ChangeCalendar` renders windows two tables deep. Threading `timezone` down
 * those chains means editing files with nothing to do with this module. This
 * reads the SAME `["me"]` query the pages already keep warm
 * (`staleTime: 60_000`), so it costs no extra request.
 *
 * WHOSE ZONE THIS IS: the READER's. `created_at`, `decided_at`, `linked_at`
 * and a maintenance window's `window_start`/`window_end` are all INSTANTS --
 * single moments on a server clock, stored as `datetime` by
 * `change_mgmt/schema.py` -- not wall-clock times belonging to some other
 * zone the way an SLA calendar's working windows are.
 *
 * `null` when the profile carries no preference; every helper in
 * `lib/datetime` falls back to the browser zone on `null`, which is the old
 * behaviour and is right for someone who never set one.
 */
export function useUserTimezone(): string | null {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  return me.data?.timezone ?? null;
}

export function useChanges() {
  const api = useApi();
  return useQuery({ queryKey: ["changes"], queryFn: () => api.listChanges() });
}

export function useCreateChangeMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ChangeCreateRequest) => api.createChange(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["changes"] });
    },
  });
}

export function useChange(id: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["change", id],
    queryFn: () => api.getChange(id as string),
    enabled: id != null,
  });
}

export function useUpdateChangeMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ChangeUpdateRequest }) =>
      api.updateChange(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["changes"] });
      void queryClient.invalidateQueries({ queryKey: ["change", variables.id] });
    },
  });
}

export function useSubmitChangeMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.submitChange(id),
    onSuccess: (_data, id) => {
      void queryClient.invalidateQueries({ queryKey: ["changes"] });
      void queryClient.invalidateQueries({ queryKey: ["change", id] });
    },
  });
}

export function useApproveChangeMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ApprovalDecisionRequest }) =>
      api.approveChange(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["changes"] });
      void queryClient.invalidateQueries({ queryKey: ["change", variables.id] });
      void queryClient.invalidateQueries({ queryKey: ["change", variables.id, "approvals"] });
    },
  });
}

export function useRejectChangeMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ApprovalDecisionRequest }) =>
      api.rejectChange(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["changes"] });
      void queryClient.invalidateQueries({ queryKey: ["change", variables.id] });
      void queryClient.invalidateQueries({ queryKey: ["change", variables.id, "approvals"] });
    },
  });
}

export function useTransitionChangeMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: TransitionRequest }) =>
      api.transitionChange(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["changes"] });
      void queryClient.invalidateQueries({ queryKey: ["change", variables.id] });
    },
  });
}

export function useChangeLinks(id: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["change-links", id],
    queryFn: () => api.listChangeLinks(id as string),
    enabled: id != null,
  });
}

export function useAddChangeLinkMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: LinkCreateRequest }) =>
      api.addChangeLink(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["change-links", variables.id] });
    },
  });
}

export function useRemoveChangeLinkMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, linkId }: { id: string; linkId: string }) =>
      api.removeChangeLink(id, linkId),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["change-links", variables.id] });
    },
  });
}

// --------------------------------------------------------------------------
// The lists behind "Link action" (UX remediation cycle 6)
// --------------------------------------------------------------------------

/**
 * The deployments, and the packages that name them, for the link picker.
 *
 * FETCHED ONLY WHEN THE PICKER IS ON SCREEN (`enabled`). The link form offers
 * four action types and shows one control at a time; loading every module's
 * list on every visit to a change detail page would spend four requests to
 * populate a control the reader may never open. `staleTime` matches the other
 * reference reads in this file so switching the type back and forth does not
 * refetch.
 *
 * `retry: false` because a 403 -- an operator holding `change.manage` but not
 * `deploy.read` -- is an answer, not a flake, and retrying it three times only
 * delays the empty hint that explains it.
 */
export function useLinkableDeployments(enabled: boolean) {
  const api = useApi();
  return useQuery({
    queryKey: ["change-linkable-deployments"],
    queryFn: () => api.listDeployments(),
    enabled,
    retry: false,
    staleTime: 60_000,
  });
}

export function useLinkablePackages(enabled: boolean) {
  const api = useApi();
  return useQuery({
    queryKey: ["change-linkable-packages"],
    queryFn: () => api.listPackages(),
    enabled,
    retry: false,
    staleTime: 60_000,
  });
}

export function useLinkableProvisionRuns(enabled: boolean) {
  const api = useApi();
  return useQuery({
    queryKey: ["change-linkable-provision-runs"],
    queryFn: () => api.listProvisionRuns(),
    enabled,
    retry: false,
    staleTime: 60_000,
  });
}

export function useLinkableProvisionProfiles(enabled: boolean) {
  const api = useApi();
  return useQuery({
    queryKey: ["change-linkable-provision-profiles"],
    queryFn: () => api.listProvisionProfiles(),
    enabled,
    retry: false,
    staleTime: 60_000,
  });
}

export function useChangeCalendar(from: string | undefined, to: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["change-calendar", from, to],
    queryFn: () => api.getChangeCalendar(from as string, to as string),
    enabled: from != null && to != null,
  });
}

export function useChangeApprovals(id: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["change", id, "approvals"],
    queryFn: () => api.listChangeApprovals(id as string),
    enabled: id != null,
  });
}

export function useChangeEvidence(id: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["change-evidence", id],
    queryFn: () => api.getChangeEvidence(id as string),
    enabled: id != null,
  });
}

export const CHANGE_GATES_KEY = "change-gates";

export function useChangeGates(enabled = true) {
  const api = useApi();
  return useQuery({ queryKey: [CHANGE_GATES_KEY], queryFn: () => api.listChangeGates(), enabled });
}

/** Device groups a gate can name. */
export function useGateGroups(enabled = true) {
  const api = useApi();
  return useQuery({
    queryKey: ["change-gate-groups"],
    queryFn: async () => (await api.listDeviceGroups()).items,
    enabled,
    retry: false,
    staleTime: 60_000,
  });
}

export function useSetChangeGateMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ChangeGateSetRequest) => api.setChangeGate(body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [CHANGE_GATES_KEY] }),
  });
}

export function useRemoveChangeGateMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.removeChangeGate(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [CHANGE_GATES_KEY] }),
  });
}
