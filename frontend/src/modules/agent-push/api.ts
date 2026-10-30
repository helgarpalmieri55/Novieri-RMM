// TanStack Query hooks for the push-install control screens, built on the
// shared ApiClient (`useApi()`). Mirrors `modules/ipam/api.ts`: every mutation
// invalidates the query keys its SERVER-SIDE EFFECTS reach, and the UI
// refetches.
//
// **WHAT THE INVALIDATION LISTS ARE, AND WHY THEY ARE SHORT.** These two
// tables are independent: revoking an authorization does not change any
// credential scope, and unbinding a scope does not change any authorization.
// A wider invalidation would refetch lists nothing touched and imply a
// relationship the data does not have -- which on a control surface is a claim
// about the security model.
//
// **NOTHING HERE POLLS.** A push authorization changes when a person changes
// it, and a list that refreshed every thirty seconds would be pretending this
// is telemetry.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiError, Credential, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { mayCheck } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import {
  AGENT_PUSH_RUN,
  AGENT_PUSH_SCOPE_MANAGE,
  PUSHABLE_CREDENTIAL_TYPES,
  type HostKeyConfirm,
  type HostKeyOut,
  type PushAuthorizationCreate,
  type PushAuthorizationOut,
  type PushCredentialScopeBind,
  type PushCredentialScopeOut,
  type PushDispatchOut,
  type PushRequestCreate,
  type PushRunCreate,
  type PushRunOut,
  type PushRunPrefixOut,
  type PushRunPreviewOut,
  type PushRunTargetPage,
} from "./types";
import { runIsLive } from "./runState";

// A genuine 403 (the caller lacks the code) should settle immediately rather
// than retry against the same permission wall -- the app-wide QueryClient
// default (App.tsx) is `retry: 1`.
const NO_RETRY = { retry: false } as const;

export const PUSH_AUTHORIZATIONS_KEY = "agent-push-authorizations";
export const PUSH_CREDENTIAL_SCOPES_KEY = "agent-push-credential-scopes";

/**
 * The caller's permission codes, for deciding what RENDERS.
 *
 * INVARIANT #6: this gates nothing. Every one of the module's seven routes
 * carries its own `require_permission` dependency and the server re-checks on
 * every request; hiding a button here saves an operator a 403 and prevents
 * exactly nothing.
 */
export function usePushPermissions(): { run: boolean; scopeManage: boolean } {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  return {
    run: permissions.includes(AGENT_PUSH_RUN),
    scopeManage: permissions.includes(AGENT_PUSH_SCOPE_MANAGE),
  };
}

export function usePushAuthorizations(includeRevoked: boolean) {
  const api = useApi();
  return useQuery<PushAuthorizationOut[], ApiError>({
    queryKey: [PUSH_AUTHORIZATIONS_KEY, includeRevoked],
    queryFn: () => api.listPushAuthorizations(includeRevoked),
    ...NO_RETRY,
  });
}

/**
 * The body is BUILT when the mutation runs, not when the button is wired: its
 * `client_id` may not be known yet (`/me` in flight), and `requireKnown`
 * throwing there is a refusal the form shows, not an exception in a click
 * handler (`lib/internalClient.ts`).
 */
export function useCreatePushAuthorizationMutation() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation<PushAuthorizationOut, Error, () => PushAuthorizationCreate>({
    mutationFn: (build) => api.createPushAuthorization(build()),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [PUSH_AUTHORIZATIONS_KEY] });
    },
  });
}

export function useRevokePushAuthorizationMutation() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.revokePushAuthorization(id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [PUSH_AUTHORIZATIONS_KEY] });
    },
  });
}

export function usePushCredentialScopes(includeUnbound: boolean) {
  const api = useApi();
  return useQuery<PushCredentialScopeOut[], ApiError>({
    queryKey: [PUSH_CREDENTIAL_SCOPES_KEY, includeUnbound],
    queryFn: () => api.listPushCredentialScopes(includeUnbound),
    ...NO_RETRY,
  });
}

export function useBindPushCredentialScopeMutation() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation<PushCredentialScopeOut, ApiError, PushCredentialScopeBind>({
    mutationFn: (body) => api.bindPushCredentialScope(body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [PUSH_CREDENTIAL_SCOPES_KEY] });
    },
  });
}

export function useUnbindPushCredentialScopeMutation() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.unbindPushCredentialScope(id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [PUSH_CREDENTIAL_SCOPES_KEY] });
    },
  });
}

/**
 * The org's push credentials -- `push_install` (SSH) and `windows_domain`
 * (WinRM, Kerberos only; D10 overturned 2026-09-28) -- for the bind form's
 * picker and the readiness panel.
 *
 * FILTERED BY TYPE HERE AS WELL AS REFUSED BY THE SERVER, and the two are not
 * redundant: the server's refusal is the control, and this filter is what stops
 * the form offering a choice that can only end in an error. A picker listing
 * every credential in the vault would invite an operator to bind their domain
 * remote-control account to a prefix and be told no -- teaching them that the
 * dedication rule is an obstacle rather than the point.
 *
 * `credentials.read` gates the underlying route, which
 * `agent.push.scope.manage` does not imply; a caller without it sees the
 * empty-picker explanation rather than a broken form.
 */
export function usePushCredentials() {
  const api = useApi();
  return useQuery<Credential[], ApiError>({
    queryKey: ["credentials", "pushable"],
    queryFn: async () =>
      (await api.listCredentials()).filter((credential) =>
        PUSHABLE_CREDENTIAL_TYPES.includes(credential.type),
      ),
    ...NO_RETRY,
  });
}


// --------------------------------------------------------------------------
// Cycle B2 — the push request and the host-key crux
// --------------------------------------------------------------------------
//
// **THROUGH THE SHARED `ApiClient`, SINCE 2026-09-29.** These three routes
// (`POST /requests`, `GET /host-keys`, `POST /host-keys/confirm`) used to go
// through a thin fetch of this module's own, because `lib/api.ts` belonged to
// another cycle. That kept them out of fixture mode (so no screen could be
// walked without a backend) and out of the mocked client every other test
// uses. They are methods on `ApiClient` now, on the same bearer token and the
// same `/api/v1` base; the server's `require_permission` on every route is the
// only authority regardless.
//
// **NOTHING HERE POLLS, AND A DISPATCH IS NEVER RETRIED.** A push presents an
// administrative credential; an automatic retry across a batch is how an AD
// account gets locked out (spec §5.3), and a UI that re-fired one would be
// doing exactly what the server refuses to.

export const PUSH_HOST_KEYS_KEY = "agent-push-host-keys";

/** Every host key (SSH) and WinRM readiness row this org has observed. */
export function usePushHostKeys() {
  const api = useApi();
  return useQuery<HostKeyOut[], ApiError>({
    queryKey: [PUSH_HOST_KEYS_KEY],
    queryFn: () => api.listPushHostKeys(),
    ...NO_RETRY,
  });
}

/**
 * The same list, for the "Before a push can run" panel. Keyed UNDER
 * `PUSH_HOST_KEYS_KEY`, so every mutation below that invalidates the host keys
 * -- a probe, a confirmation -- refreshes the panel as well.
 *
 * Asked only of an account that holds `agent.push.run`, the route's own code:
 * anyone else is told the check cannot be made, not sent a request that can
 * only be refused.
 */
export function usePushProbeReadiness() {
  const api = useApi();
  const me = useMePermissions();
  return useQuery<HostKeyOut[], ApiError>({
    queryKey: [PUSH_HOST_KEYS_KEY, "readiness"],
    queryFn: () => api.listPushHostKeys(),
    enabled: mayCheck(me, AGENT_PUSH_RUN),
    ...NO_RETRY,
  });
}

/** Confirm the fingerprint a probe observed. HIGH CONSEQUENCE: it becomes the
 * trust anchor every later push to this host is verified against (§4.4, §7.3). */
export function useConfirmHostKeyMutation() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation<HostKeyOut, ApiError, HostKeyConfirm>({
    mutationFn: (body) => api.confirmPushHostKey(body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [PUSH_HOST_KEYS_KEY] });
    },
  });
}

/** Dispatch a probe or an install. Deliberately NOT retried (see the header). */
export function usePushRequestMutation() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation<PushDispatchOut, ApiError, PushRequestCreate>({
    mutationFn: (body) => api.requestPush(body),
    retry: false,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [PUSH_HOST_KEYS_KEY] });
    },
  });
}

// --------------------------------------------------------------------------
// Push runs (design 2026-09-30)
// --------------------------------------------------------------------------
//
// **THE PREVIEW IS NEVER REFETCHED BEHIND THE OPERATOR'S BACK.** The list on
// screen is what they confirm, bound by its digest; a background refetch that
// swapped rows under them would make the screen and the confirmation describe
// different machines. It refetches only when asked (a changed-list 409).
//
// **A LIVE RUN'S PAGE POLLS EVERY FIVE SECONDS**, and nothing else here does. A
// run in flight is telemetry; once it is paused with nothing in flight,
// cancelled or finished, polling stops.

export const PUSH_RUNS_KEY = "agent-push-runs";

/**
 * The subnets a run with `credentialId` may be built from: its CLIENT's active
 * authorizations (MSP clients phase 2, Task 12). A run is made in its
 * credential's client, so the credential is chosen first and only then are
 * subnets offered; nothing is asked for before.
 */
export function usePushRunPrefixes(credentialId: string) {
  const api = useApi();
  return useQuery<PushRunPrefixOut[], ApiError>({
    queryKey: [PUSH_RUNS_KEY, "prefixes", credentialId],
    queryFn: () => api.listPushRunPrefixes(credentialId),
    enabled: credentialId !== "",
    ...NO_RETRY,
  });
}

export function usePushRunPreview(prefix: string, credentialId: string) {
  const api = useApi();
  return useQuery<PushRunPreviewOut, ApiError>({
    queryKey: [PUSH_RUNS_KEY, "preview", prefix, credentialId],
    queryFn: () => api.previewPushRun(prefix, credentialId),
    enabled: prefix !== "" && credentialId !== "",
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    ...NO_RETRY,
  });
}

/** Confirm a list. Deliberately NOT retried: a retry is a second confirmation. */
export function useCreatePushRunMutation() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation<PushRunOut, ApiError, PushRunCreate>({
    mutationFn: (body) => api.createPushRun(body),
    retry: false,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [PUSH_RUNS_KEY] });
    },
  });
}

export function usePushRuns() {
  const api = useApi();
  return useQuery<PushRunOut[], ApiError>({
    queryKey: [PUSH_RUNS_KEY, "list"],
    queryFn: () => api.listPushRuns(),
    ...NO_RETRY,
  });
}

export function usePushRun(id: string) {
  const api = useApi();
  return useQuery<PushRunOut, ApiError>({
    queryKey: [PUSH_RUNS_KEY, "run", id],
    queryFn: () => api.getPushRun(id),
    enabled: id !== "",
    refetchInterval: (query) => (query.state.data && runIsLive(query.state.data) ? 5_000 : false),
    ...NO_RETRY,
  });
}

export function usePushRunTargets(id: string, offset: number, limit: number, live: boolean) {
  const api = useApi();
  return useQuery<PushRunTargetPage, ApiError>({
    queryKey: [PUSH_RUNS_KEY, "run", id, "targets", offset, limit],
    queryFn: () => api.listPushRunTargets(id, { offset, limit }),
    enabled: id !== "",
    refetchInterval: live ? 5_000 : false,
    ...NO_RETRY,
  });
}

/** Pause, Resume or Cancel: each a person's act, never retried. */
export function usePushRunControl() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation<PushRunOut, ApiError, { runId: string; action: "pause" | "resume" | "cancel" }>({
    mutationFn: ({ runId, action }) =>
      action === "pause"
        ? api.pausePushRun(runId)
        : action === "resume"
          ? api.resumePushRun(runId)
          : api.cancelPushRun(runId),
    retry: false,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [PUSH_RUNS_KEY] });
    },
  });
}
