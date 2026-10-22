// TanStack Query hooks for the AI admin agent, built on the shared ApiClient
// (useApi()). Mirrors modules/health/api.ts and modules/ipam/api.ts.
//
// **NOTHING HERE POLLS.** A turn is a request/response: the `POST` runs the turn
// inline and returns the whole answer (spec §6). Polling a chat panel would be
// pretending there is a background job, and there deliberately is not — no
// `ai_turn` table, no worker task, no state to watch.
//
// A 503 FROM `GET /ai/status` OR A TURN IS NOT RETRIED. §10.6: "no model
// provider is configured on this server" is a settled fact, not a transient
// failure, and retrying it makes an administrator wait to be told the same
// thing. Since §0.14 it is the only reason the assistant can be unavailable.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiError, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import type {
  AiInjectionFlagOut,
  AiProviderOut,
  AiSessionOut,
  AiSettingsOut,
  AiSettingsUpdate,
  AiStatus,
  AiTranscriptOut,
  AiTurnOut,
  AiTurnRequest,
} from "./types";

const NO_RETRY = { retry: false } as const;

export const AI_STATUS_KEY = "ai-status";
export const AI_SESSIONS_KEY = "ai-sessions";
export const AI_SESSION_KEY = "ai-session";
export const AI_SETTINGS_KEY = "ai-settings";
export const AI_PROVIDERS_KEY = "ai-providers";
export const AI_FLAGS_KEY = "ai-injection-flags";

/**
 * The caller's permission codes, for deciding what RENDERS.
 *
 * INVARIANT #6: this gates nothing. Every route carries its own
 * `require_permission` dependency and the server re-checks on every request.
 * Hiding the settings tab here saves an administrator a 403; it prevents
 * exactly nothing, and a caller who forges this list gets the same 403.
 */
export function useAiPermissions(): { use: boolean; admin: boolean } {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  return { use: permissions.includes("ai.use"), admin: permissions.includes("ai.admin") };
}

/**
 * The zone every date in this module is rendered in (cycle 5).
 *
 * READ FROM `/me`, NOT THE BROWSER — `lib/datetime.ts` records why the profile
 * zone is the authority. Same `["me"]` key and `staleTime` `useAiPermissions`
 * already uses, so this shares that cached response rather than issuing a
 * second request, and a null timezone falls the helpers back to the browser
 * zone rather than to a guess.
 */
export function useProfileZone(): string | null {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  return me.data?.timezone ?? null;
}

export function useAiStatus() {
  const api = useApi();
  return useQuery<AiStatus, ApiError>({
    queryKey: [AI_STATUS_KEY],
    queryFn: () => api.getAiStatus(),
    ...NO_RETRY,
  });
}

export function useAiSessions() {
  const api = useApi();
  return useQuery<AiSessionOut[], ApiError>({
    queryKey: [AI_SESSIONS_KEY],
    queryFn: () => api.listAiSessions(),
    ...NO_RETRY,
  });
}

export function useAiSession(sessionId: string | null) {
  const api = useApi();
  return useQuery<AiTranscriptOut, ApiError>({
    queryKey: [AI_SESSION_KEY, sessionId],
    queryFn: () => api.getAiSession(sessionId as string),
    enabled: sessionId != null,
    ...NO_RETRY,
  });
}

export function useCreateAiSession() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<AiSessionOut, ApiError, void>({
    mutationFn: () => api.createAiSession(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [AI_SESSIONS_KEY] });
    },
  });
}

/**
 * Ask a question.
 *
 * The transcript AND the session list are invalidated: a turn appends two
 * messages and moves `last_turn_at` and the token counters on the session row,
 * so a list rendered from a stale cache would show a conversation as older and
 * cheaper than it is.
 */
export function useCreateAiTurn(sessionId: string | null) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<AiTurnOut, ApiError, AiTurnRequest>({
    mutationFn: (body) => api.createAiTurn(sessionId as string, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [AI_SESSION_KEY, sessionId] });
      void queryClient.invalidateQueries({ queryKey: [AI_SESSIONS_KEY] });
    },
  });
}

export function useDeleteAiSession() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.deleteAiSession(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [AI_SESSIONS_KEY] });
    },
  });
}

export function useAiSettings() {
  const api = useApi();
  return useQuery<AiSettingsOut, ApiError>({
    queryKey: [AI_SETTINGS_KEY],
    queryFn: () => api.getAiSettings(),
    ...NO_RETRY,
  });
}

export function useAiProviders() {
  const api = useApi();
  return useQuery<AiProviderOut[], ApiError>({
    queryKey: [AI_PROVIDERS_KEY],
    queryFn: () => api.listAiProviders(),
    ...NO_RETRY,
  });
}

/**
 * Write the org's settings.
 *
 * `AI_STATUS_KEY` IS NO LONGER INVALIDATED ALONGSIDE, and that is the change
 * §0.14 makes here. It used to be, because a write could flip the per-org
 * `enabled` flag and the chat panel renders from `GET /ai/status`. No field on
 * this request can change availability any more — the deployment's `ITS_AI_*`
 * configuration decides it — so invalidating status after a retention edit
 * would be a refetch that can never return anything new.
 */
export function useUpdateAiSettings() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<AiSettingsOut, ApiError, AiSettingsUpdate>({
    mutationFn: (body) => api.updateAiSettings(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [AI_SETTINGS_KEY] });
    },
  });
}

export function useAiInjectionFlags() {
  const api = useApi();
  return useQuery<AiInjectionFlagOut[], ApiError>({
    queryKey: [AI_FLAGS_KEY],
    queryFn: () => api.listAiInjectionFlags(),
    ...NO_RETRY,
  });
}
