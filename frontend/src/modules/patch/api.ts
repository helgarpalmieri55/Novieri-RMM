// TanStack Query hooks for the patch module, built on the shared ApiClient
// (useApi()). There is no live patch event stream — every mutation below
// invalidates the relevant query keys on success so the UI refetches, rather
// than subscribing to a websocket event that doesn't exist for this module
// (see DeviceDetailPage's inventory_updated handler for the pattern this
// module deliberately does NOT follow).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "../../lib/ApiContext";
import type { CurrentOrg, Me } from "../../lib/api";
import type {
  ApplyRequest,
  ApprovalListFilters,
  BulkApprovalCreateRequest,
  PatchDeviceRollupFilters,
  PatchPolicyInput,
  PatchUpdateFilters,
  ScanRequest,
} from "./types";

/** Explicit rather than left to the server's default, for the same reason the
 *  error monitor pins its own: the page can only tell the operator that a
 *  list was truncated at N if it knows N. */
export const PATCH_APPROVALS_LIST_LIMIT = 100;

export const PATCH_APPROVALS_QUERY_KEY = "patch-approvals";

/*
 * `usePatchCatalog` USED TO BE HERE, AND IS GONE. It fetched
 * `GET /patch/patches` at the route's 500-row ceiling on every visit to the
 * fleet page, rendered none of it, and existed for one purpose: a
 * `patch_id -> identifier` map so the Approvals panel could print what an
 * approval was for. `ApprovalOut.patch_identifier` now carries the name on the
 * row, so the request has no reason to exist — and the version that silently
 * missed past row 500, rendering a raw uuid with nothing on screen saying why,
 * has no reason to come back.
 *
 * `ApiClient.listPatches` HAS NOW GONE TOO. It survived one cycle as "the
 * client method for a shipped route", which described the state without
 * deciding it: no screen in this console wants the raw catalogue, and the only
 * one that ever did was the fetch above. The ROUTE stays -- it is the only
 * read that returns catalogue rows no device has ever reported a status for --
 * but the console can no longer call it, so re-adding the fetch is a
 * deliberate act against the type checker rather than one `useQuery` away.
 */

/**
 * Every ring, and how many devices each reaches (`patch.read`).
 *
 * Fetched on the catalogue page because the ring's size is what makes an
 * approval readable: "Approve 42 updates for pilot" says nothing until "pilot"
 * has a number of machines behind it. Long `staleTime`: ring membership moves
 * when a policy or a group changes, not while somebody is ticking boxes.
 */
export function usePatchRings() {
  const api = useApi();
  return useQuery({
    queryKey: ["patch-rings"],
    queryFn: () => api.listPatchRings(),
    staleTime: 60_000,
  });
}

/**
 * The page size for both rollups, and it is small on purpose.
 *
 * THE OPERATOR'S REPORT WAS ABOUT LENGTH: "still a super long list of missing
 * updates and that's just one device online". Aggregating to one row per patch
 * collapses the (device, patch) explosion, but a freshly-enrolled Ubuntu box
 * still has ~300 upgradable packages, so the catalogue itself is long and the
 * page has to page. 25 with a stated total beats 500 with a scrollbar: the
 * count is honest either way, and only one of them can be read.
 */
export const PATCH_ROLLUP_PAGE_SIZE = 25;

export const PATCH_UPDATES_QUERY_KEY = "patch-updates";
export const PATCH_DEVICE_ROLLUP_QUERY_KEY = "patch-device-rollup";
export const PATCH_RUNS_QUERY_KEY = "patch-runs";

/**
 * The fleet update list (`patch.read`) -- one row per PATCH.
 *
 * THE PRIMARY OBJECT OF THIS PAGE. Approving is a decision about a patch and a
 * ring, never about a device (`patch_approvals` has no device column), so the
 * list an operator works down is keyed the same way. Every filter is sent to
 * the server rather than applied to the fetched rows: the previous console
 * filtered a capped page, so it could answer "No patches match that filter"
 * about updates it had never asked for.
 */
export function usePatchUpdates(filters: PatchUpdateFilters = {}) {
  const api = useApi();
  const params: PatchUpdateFilters = { limit: PATCH_ROLLUP_PAGE_SIZE, ...filters };
  return useQuery({
    queryKey: [PATCH_UPDATES_QUERY_KEY, params],
    queryFn: () => api.listPatchUpdates(params),
  });
}

/**
 * Which machines need attention (`patch.read`) -- one row per DEVICE.
 *
 * Replaces grouping `FleetReport.exposures` in the browser. That list is one
 * entry per (device, patch) pair and uncapped, so the grouping was correct and
 * the fetch behind it was 300 rows to render one.
 */
/**
 * The apply RUNS for one machine (`patch.read`) — newest first.
 *
 * WHAT IT ANSWERS that nothing else did: "I applied the updates, did they
 * install?" `usePatchUpdates` states the end position per patch, which is the
 * durable truth and cannot say that a run happened at all; a run that failed
 * on Tuesday and succeeded on Wednesday leaves an `installed` row and no trace
 * of Tuesday. `patch_runs` has been written since the module shipped and no
 * route read it.
 */
export function usePatchRuns(deviceId: string | null, limit = 5) {
  const api = useApi();
  return useQuery({
    queryKey: [PATCH_RUNS_QUERY_KEY, deviceId, limit],
    queryFn: () => api.listPatchRuns({ device_id: deviceId ?? undefined, limit }),
    enabled: deviceId != null,
  });
}

export function usePatchDeviceRollup(filters: PatchDeviceRollupFilters = {}) {
  const api = useApi();
  const params: PatchDeviceRollupFilters = { limit: PATCH_ROLLUP_PAGE_SIZE, ...filters };
  return useQuery({
    queryKey: [PATCH_DEVICE_ROLLUP_QUERY_KEY, params],
    queryFn: () => api.listPatchDeviceRollup(params),
  });
}

/**
 * ONE PLATFORM'S OUTSTANDING TOTAL, FOR THE TAB STRIP.
 *
 * The strip carries a count per tab, and that count has to be the same
 * arithmetic as the tiles and the table inside the tab or the page is back to
 * the tile/table disagreement the rollup rebuild existed to end. So it is
 * `GET /patch/updates` again -- one row asked for, `total_unfiltered` read off
 * the response -- rather than a fourth number counted somewhere else.
 *
 * `total_unfiltered` and not `total`: the field applies every filter EXCEPT
 * severity, so the strip states what the platform is missing rather than what
 * the currently-selected severity happens to match. A tab reading "0" because
 * of a filter set on a different tab would be the worst possible label on a
 * navigation control.
 *
 * NO SEARCH TERM IS PASSED, deliberately. The strip is a claim about the
 * platform; a count that shrank while somebody typed in the filter box would be
 * the `fleet`-vs-`state_counts` distinction the devices page already records,
 * on a control the operator uses to decide where to look.
 */
export function usePatchFamilyTotal(osFamily: string) {
  const api = useApi();
  const params: PatchUpdateFilters = { os_family: osFamily, limit: 1 };
  return useQuery({
    queryKey: [PATCH_UPDATES_QUERY_KEY, params],
    queryFn: () => api.listPatchUpdates(params),
    // The strip is navigation. It does not need to be fresher than the page.
    staleTime: 30_000,
  });
}

/**
 * HOW MANY MACHINES OF THIS PLATFORM CARRY AN AGENT.
 *
 * THE NUMBER THAT SEPARATES THREE EMPTY STATES. "No Windows machine is
 * enrolled", "no scan has run" and "every Windows machine is up to date" are
 * the same empty table, and the patch API cannot tell them apart:
 * `fleet_scan_coverage` computes `devices_total`/`devices_scanned` for the
 * whole ORG, so on a per-platform tab neither is a statement about the platform.
 * `GET /devices?os_family=…` is, and it already exists.
 *
 * `all - assets` and not `all`: `assets` is the server's own name for a row
 * with no agent (`app/services/device_list.py`), and a machine with no agent
 * cannot be patch-scanned at all, so it is not part of the denominator this tab
 * is talking about.
 *
 * GATED ON `devices.read`, and the caller must handle `null`. An operator with
 * `patch.read` alone is a real configuration, and inventing a count for them
 * would put an unmeasured number on the one screen whose whole discipline is
 * that a zero states its provenance.
 */
export function usePatchFamilyManagedCount(osFamily: string, enabled: boolean) {
  const api = useApi();
  return useQuery({
    queryKey: ["patch-family-managed", osFamily],
    queryFn: async () => {
      const page = await api.listDevices({
        osFamily: osFamily as "windows" | "linux" | "macos",
        limit: 1,
      });
      return page.state_counts.all - page.state_counts.assets;
    },
    enabled,
    staleTime: 60_000,
  });
}

export function usePatchFleetReport() {
  const api = useApi();
  return useQuery({
    queryKey: ["patch-fleet"],
    queryFn: () => api.getPatchFleetReport(),
  });
}

export const PATCH_RESTARTS_QUERY_KEY = "patch-restarts";

/** Changes an approval can be linked to: approved, or being implemented. An
 *  operator who may not read changes gets none, and the approval proceeds
 *  without one (the server records it, or refuses under a gate). */
export function useAuthorizingChanges() {
  const api = useApi();
  return useQuery({
    queryKey: ["patch-authorizing-changes"],
    queryFn: async () =>
      (await api.listChanges()).filter((c) => c.state === "approved" || c.state === "implementing"),
    retry: false,
    staleTime: 30_000,
  });
}

/** Whether this platform's app package manager could be scanned, fleet-wide. */
export function usePatchAppSources(osFamily: string) {
  const api = useApi();
  return useQuery({
    queryKey: ["patch-app-sources", osFamily],
    queryFn: () => api.listPatchAppSources(osFamily),
  });
}

/** Devices still owed a restart after patching. */
export function usePatchRestarts(enabled = true) {
  const api = useApi();
  return useQuery({
    queryKey: [PATCH_RESTARTS_QUERY_KEY],
    queryFn: () => api.listPatchRestarts(),
    enabled,
  });
}

/**
 * The zone patch times are shown in: the organization's, then the operator's
 * profile, then the browser. The org zone comes first because a restart time
 * is about the fleet's working day, not the reader's.
 */
export function usePatchZone(): string | null {
  const api = useApi();
  const org = useQuery<CurrentOrg>({
    queryKey: ["patch-org-zone"],
    queryFn: () => api.getCurrentOrg(),
    staleTime: 300_000,
    retry: false,
  });
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  return org.data?.settings.timezone || me.data?.timezone || null;
}

export function useDevicePatchStatus(deviceId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["patch-device", deviceId],
    queryFn: () => api.getDevicePatchStatus(deviceId as string),
    enabled: deviceId != null,
  });
}

export function usePatchPolicies() {
  const api = useApi();
  return useQuery({
    queryKey: ["patch-policies"],
    queryFn: () => api.listPatchPolicies(),
  });
}

export function usePatchScanMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ScanRequest = {}) => api.triggerPatchScan(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["patch-fleet"] });
      void queryClient.invalidateQueries({ queryKey: ["patch-catalog"] });
      void queryClient.invalidateQueries({ queryKey: [PATCH_UPDATES_QUERY_KEY] });
      void queryClient.invalidateQueries({ queryKey: [PATCH_DEVICE_ROLLUP_QUERY_KEY] });
    },
  });
}

/**
 * This org's approvals (`patch.read`).
 *
 * NO `revoked` FILTER IS PASSED, and that is the decision rather than an
 * omission: the route includes revoked approvals by default because they are
 * soft-revoked so the trail holds, and a console that asked for `revoked=false`
 * would show a shorter history than the audit log does. The list marks them
 * instead.
 */
export function usePatchApprovals(filters: ApprovalListFilters = {}) {
  const api = useApi();
  const params: ApprovalListFilters = { limit: PATCH_APPROVALS_LIST_LIMIT, ...filters };
  return useQuery({
    queryKey: [PATCH_APPROVALS_QUERY_KEY, params],
    queryFn: () => api.listPatchApprovals(params),
  });
}

/**
 * The org's user directory, read ONLY to put a name on an approval's actor id
 * (`users.read`).
 *
 * REVIEW, 2026-09-08 [MINOR, copy]: the Approvals table printed "usr-helgar"
 * where the rest of the console prints "Helgar Palmieri". `ApprovalOut` carries
 * `approved_by`/`revoked_by` as uuids and no name, so a formatter cannot fix
 * it; the name has to be looked up.
 *
 * AND THIS IS NOT `usePatchCatalog` COMING BACK. That fetch was a 500-row page
 * of a growing table, pulled on every visit, to name a value the row could have
 * carried itself -- and the fix was to put `patch_identifier` on the row. This
 * one is the org's people, a small list that every other screen naming a person
 * already reads (`subscriptions/api.useOrgUsers`, same `["users"]` key, so the
 * two share one cache entry), and it is asked for only when this panel actually
 * has an actor id to resolve.
 *
 * `enabled` RATHER THAN A HOPEFUL REQUEST. `users.read` is not implied by
 * `patch.read` or `patch.approve`, so for many operators this call can only
 * 403; asking anyway would put a guaranteed failure on the page's request list
 * on every visit. `retry: false` for the same reason a 403 is not a blip, and
 * a failure is silent here by design -- the id is still printed, and an
 * approvals table that announced "could not list users" would be reporting a
 * permission it does not need in order to do its job.
 */
export function useApprovalActorDirectory(enabled: boolean) {
  const api = useApi();
  return useQuery({
    queryKey: ["users"],
    queryFn: () => api.listUsers(),
    enabled,
    retry: false,
    // People are added to an org far less often than approvals are made.
    staleTime: 5 * 60_000,
  });
}

/**
 * Clear a selection of patches for one ring (`patch.approve`).
 *
 * THIS REPLACED THE PER-PATCH MUTATION RATHER THAN JOINING IT. The approval
 * model is `(org_id, patch_id, ring_name)` — no device column — so one approval
 * has always cleared a patch for a whole ring, and a console offering a button
 * and a confirmation dialog per catalogue row was asking for N decisions where
 * the model holds one shape. A selection of one is the same call.
 */
export function usePatchBulkApproveMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: BulkApprovalCreateRequest) => api.bulkApprovePatches(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["patch-catalog"] });
      void queryClient.invalidateQueries({ queryKey: ["patch-fleet"] });
      // The approvals that were just created are rows in that list; without
      // this the operator has to reload to see what they just made.
      void queryClient.invalidateQueries({ queryKey: [PATCH_APPROVALS_QUERY_KEY] });
      // ...and the "Cleared for" column is a property of the update row now,
      // not a browser-side join against that list, so the update page has to
      // be refetched or it keeps showing the rows as undecided.
      void queryClient.invalidateQueries({ queryKey: [PATCH_UPDATES_QUERY_KEY] });
    },
  });
}

/**
 * Install what is already approved, on named machines (`patch.apply`).
 *
 * WHAT IT INVALIDATES AND WHY IT IS NOT MORE. A dispatched `patch.apply` does
 * not change `device_patch_status` — the agent does, when the job comes back —
 * so refetching the update and device rollups here would repaint the same
 * numbers and teach the operator that the counts had already moved. What HAS
 * changed the moment this returns is the job list, which is where an operator
 * goes to watch an install run.
 *
 * The rollups still have to catch up eventually, and they do: this module has
 * no live patch event stream (see the file header), so the honest mechanism is
 * the operator's next visit or refetch, not an invalidation that pretends the
 * endpoint reported a result it cannot have.
 */
/** Book a restart for the device's next maintenance window (`patch.apply`). */
export function useDeviceRestartMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (deviceId: string) => api.requestDeviceRestart(deviceId),
    onSuccess: (_data, deviceId) => {
      void queryClient.invalidateQueries({ queryKey: ["patch-device", deviceId] });
      void queryClient.invalidateQueries({ queryKey: [PATCH_RESTARTS_QUERY_KEY] });
    },
  });
}

export function usePatchApplyMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ApplyRequest) => api.applyPatches(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
}

/**
 * Withdraw an approval (`patch.approve`).
 *
 * The list is invalidated because the revoke is what the row is FOR: the
 * server soft-revokes, so the row comes back marked rather than disappearing,
 * and a page that did not refetch would keep showing a withdrawn clearance as
 * being in force. The fleet report goes with it — what a ring is cleared to
 * install has changed.
 */
export function usePatchRevokeApprovalMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (approvalId: string) => api.revokePatchApproval(approvalId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [PATCH_APPROVALS_QUERY_KEY] });
      void queryClient.invalidateQueries({ queryKey: ["patch-fleet"] });
      void queryClient.invalidateQueries({ queryKey: [PATCH_UPDATES_QUERY_KEY] });
    },
  });
}

export function usePatchPolicyMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: PatchPolicyInput) => api.upsertPatchPolicy(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["patch-policies"] });
    },
  });
}
