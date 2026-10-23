// TanStack Query hooks for act mode (docs/modules/ai-automation.md §6, §12),
// built on the shared ApiClient. Mirrors modules/ai-admin/api.ts.
//
// **NOTHING HERE DECIDES ANYTHING** (invariant #6). Every route carries its own
// server-side gate, and the confirm carries THREE checks the browser cannot see:
// `ai.act.confirm`, the permission the job type's own module dispatch route
// requires, and a loopback read of the target device. A `canConfirm` flag below
// decides only what RENDERS, and a caller who forges it gets a 403 with no job
// created — which `test_ai_act_api.py` asserts on the `jobs` table, not on the
// status code.
//
// A 409 IS NOT RETRIED AND MUST NOT BE. It means the world moved between the
// preview and the click — the catalogue was edited, the dial was lowered, a
// script version was bumped, a fuse tripped, or somebody already answered this
// action. Retrying re-sends a digest that is stale by definition; the correct
// response is to re-fetch the preview and let the human look again.

import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiError, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import type {
  AiActionDispatched,
  AiActionOut,
  AiActionPreviewOut,
  AiCeilingOut,
  AiCeilingUpdate,
  AiRejectionReason,
} from "./types";

const NO_RETRY = { retry: false } as const;

export const AI_ACTIONS_KEY = "ai-actions";
export const AI_ACTION_KEY = "ai-action";
export const AI_CEILINGS_KEY = "ai-ceilings";

/** What the console RENDERS. Not what the server allows. */
export function useAiActPermissions(): {
  propose: boolean;
  confirm: boolean;
  admin: boolean;
} {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  return {
    propose: permissions.includes("ai.act.propose"),
    confirm: permissions.includes("ai.act.confirm"),
    admin: permissions.includes("ai.admin"),
  };
}

export function useAiActions(sessionId?: string) {
  const api = useApi();
  return useQuery<AiActionOut[], ApiError>({
    queryKey: [AI_ACTIONS_KEY, sessionId ?? null],
    queryFn: () => api.listAiActions(sessionId),
    ...NO_RETRY,
  });
}

export function useAiAction(id: string | null) {
  const api = useApi();
  return useQuery<AiActionPreviewOut, ApiError>({
    queryKey: [AI_ACTION_KEY, id],
    queryFn: () => api.getAiAction(id as string),
    enabled: id !== null,
    ...NO_RETRY,
  });
}

export function useConfirmAiAction() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation<AiActionDispatched, ApiError, { id: string; digest: string }>({
    mutationFn: ({ id, digest }) => api.confirmAiAction(id, { digest }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [AI_ACTIONS_KEY] });
      void client.invalidateQueries({ queryKey: [AI_ACTION_KEY] });
    },
    ...NO_RETRY,
  });
}

export function useRejectAiAction() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation<AiActionOut, ApiError, { id: string; reason: AiRejectionReason }>({
    mutationFn: ({ id, reason }) => api.rejectAiAction(id, { reason }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [AI_ACTIONS_KEY] });
      void client.invalidateQueries({ queryKey: [AI_ACTION_KEY] });
    },
    ...NO_RETRY,
  });
}

export function useAiCeilings() {
  const api = useApi();
  return useQuery<AiCeilingOut[], ApiError>({
    queryKey: [AI_CEILINGS_KEY],
    queryFn: () => api.listAiCeilings(),
    ...NO_RETRY,
  });
}

export function useSetAiCeiling() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation<AiCeilingOut, ApiError, AiCeilingUpdate>({
    mutationFn: (body) => api.setAiCeiling(body),
    onSuccess: () => void client.invalidateQueries({ queryKey: [AI_CEILINGS_KEY] }),
    ...NO_RETRY,
  });
}

/**
 * The CATALOGUE NAME of each of a handful of actions, keyed by action id.
 *
 * WHY A SECOND REQUEST PER ROW EXISTS AT ALL. `ActionOut` — the list shape —
 * carries `approved_action_key` and `job_type` and no prose, so a row built
 * from it alone is titled `patch.approve.ring` and sub-titled `patch.approve`:
 * an API key and a permission code, which is not a sentence anybody can
 * review. The name an org admin wrote for the catalogue row
 * (`ActionPreviewOut.name`, "Approve KB5041585 into ring test") is served by
 * the DETAIL route and nowhere else.
 *
 * IT IS THE SAME READ, AT THE SAME PERMISSION. `GET /ai/actions/{id}` is
 * `ai.use` plus `devices.read` in the handler — exactly what the list this
 * feeds already cost — and it mutates nothing. The query key is
 * `useAiAction`'s own, so the preview a row's "Review" link opens is already
 * in the cache when it is clicked; the request is not spent twice.
 *
 * **THE RATIONALE IS NOT READ HERE AND MUST NOT BE.** §6.3 keeps the model's
 * argument off a one-region row; only the catalogue-derived `name` crosses
 * over, which is the field the confirmation screen makes its h1 for the same
 * reason: an org admin authored it through the approved-action write path.
 *
 * A preview that does not load (expired between the list and this, or a
 * principal who may list but not read a device) leaves the id absent from the
 * map and the caller falls back to the key. Nothing here decides anything.
 */
export function useAiActionNames(ids: readonly string[]): Readonly<Record<string, string>> {
  const api = useApi();
  const results = useQueries({
    queries: ids.map((id) => ({
      queryKey: [AI_ACTION_KEY, id],
      queryFn: () => api.getAiAction(id),
      staleTime: 30_000,
      ...NO_RETRY,
    })),
  });
  const names: Record<string, string> = {};
  results.forEach((result, at) => {
    const id = ids[at];
    const name = (result.data as AiActionPreviewOut | undefined)?.name;
    if (id !== undefined && name !== undefined && name !== "") names[id] = name;
  });
  return names;
}
