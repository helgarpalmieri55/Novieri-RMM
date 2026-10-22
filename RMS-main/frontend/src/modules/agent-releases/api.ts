// TanStack Query hooks for publishing and promoting agent releases, built on
// the shared ApiClient (`useApi()`). Mirrors `modules/agent-push/api.ts`: every
// mutation invalidates the query keys its SERVER-SIDE effects reach.
//
// INVARIANT 6: nothing here gates anything. `usePublishPermission` decides what
// RENDERS; every route checks `agent.release.manage` itself, and a caller who
// lacks it is refused by the server whatever this returns.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { AgentReleasePromoted, AgentVersionRemoved, ApiError, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";

export const AGENT_RELEASES_KEY = "agent-releases";
/** The self-update section's "latest stable" reads the channel too. */
const FLEET_VERSIONS_KEY = "agent-fleet-versions";

export const RELEASE_MANAGE = "agent.release.manage";

export function usePublishPermission(): boolean {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  return me.data?.permissions?.includes(RELEASE_MANAGE) ?? false;
}

/**
 * Refetch the catalogue after a publish run. Called once when the run ends,
 * not per file: the dialog shows per-file progress itself, and six refetches
 * of the table behind it would buy nothing.
 */
export function useInvalidateAgentReleases(): () => void {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: [AGENT_RELEASES_KEY] });
    void client.invalidateQueries({ queryKey: [FLEET_VERSIONS_KEY] });
  };
}

export function usePromoteAgentVersionMutation() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation<
    AgentReleasePromoted,
    ApiError,
    { version: string; channel: "pilot" | "stable" }
  >({
    mutationFn: ({ version, channel }) => api.promoteAgentReleaseVersion(version, channel),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [AGENT_RELEASES_KEY] });
      void client.invalidateQueries({ queryKey: [FLEET_VERSIONS_KEY] });
    },
  });
}

/**
 * Remove every package of one version in one request: the server does it all
 * or none (`DELETE /agent/releases/versions/{version}`), clears the link from
 * past rollouts, and deletes the stored files after its commit. It replaced a
 * loop of per-package deletes that could stop half way through a version
 * (Codex review on #87). A 409 means an active rollout still uses it.
 */
export function useRemoveAgentVersionMutation() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation<AgentVersionRemoved, ApiError, { version: string }>({
    mutationFn: ({ version }) => api.deleteAgentReleaseVersion(version),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: [AGENT_RELEASES_KEY] });
      void client.invalidateQueries({ queryKey: [FLEET_VERSIONS_KEY] });
    },
  });
}
