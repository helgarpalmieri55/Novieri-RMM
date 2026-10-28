// TanStack Query hooks for the IPAM module, built on the shared ApiClient
// (useApi()). Mirrors modules/sla/api.ts and modules/health/api.ts: every
// mutation invalidates the query keys its SERVER-SIDE EFFECTS reach, and the UI
// refetches.
//
// THE INVALIDATION LISTS BELOW ARE THOSE EFFECTS, NOT A GUESS. Four writes in
// this module reach well past the row they touch:
//
//   * CREATING OR DELETING A SUBNET RE-PARENTS OTHERS. `parent_id` is a cache
//     of "the smallest other prefix in the same VRF containing this one",
//     recomputed on every write — inserting a supernet between existing rows
//     re-parents exactly the contained children. So the whole subnet LIST is
//     invalidated, never just the new row: a tree rendered from a stale list
//     would show the old hierarchy.
//   * ASSIGN / RESERVE / RELEASE / ALLOCATE CHANGE THE GRID AND UTILISATION of
//     whichever subnet contains the address — and the caller does not always
//     know which, since containment is derived. Those mutations invalidate the
//     grid and utilisation keys wholesale.
//   * CREATING OR DELETING A RANGE changes the DERIVED STATUS of every address
//     inside it (a `dhcp_pool` band outranks everything in §3.1's table) and
//     changes next-free. The grid is invalidated for that reason, not because
//     the range list changed.
//   * REVOKING A SCAN AUTHORIZATION can make a subnet unsweepable. The subnet
//     pages read the authorization list to say so, so it is invalidated
//     alongside.
//
// There is no live IPAM event stream — the reconcile sweep runs daily on the
// worker's beat schedule and sweeps are dispatched jobs — so nothing here
// polls. A drift list that refreshed every 30 seconds would be pretending to be
// telemetry, which is precisely the boundary this module does not cross.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiError, ClientSiteOut, Device, DevicePage, Me, Page } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { internalClientId, loadingUntilClient, requireKnown } from "../../lib/internalClient";
import type {
  AddressCreate,
  AddressOut,
  AddressUpdate,
  AllocateRequest,
  DocRecordConversionOut,
  DriftFilters,
  DriftOut,
  GridEntryOut,
  HistoryEntryOut,
  ImportPreviewOut,
  ImportResultOut,
  OrphanOut,
  RangeCreate,
  RangeOut,
  ScopeCreate,
  ScopeOut,
  SearchOut,
  SubnetCreate,
  SubnetFilters,
  SubnetOut,
  SubnetUpdate,
  SweepRunOut,
  SweepSettings,
  UtilizationOut,
  VlanCreate,
  VlanOut,
  VrfCreate,
  VrfOut,
} from "./types";
import {
  IPAM_ASSIGN,
  IPAM_MANAGE,
  IPAM_READ,
  IPAM_SCOPE_MANAGE,
  IPAM_SWEEP_MANAGE,
  IPAM_SWEEP_RUN,
} from "./types";

// A genuine 403 (the caller lacks the code) should settle immediately rather
// than retry against the same permission wall — the app-wide QueryClient
// default (App.tsx) is `retry: 1`.
const NO_RETRY = { retry: false } as const;

// A 422 from the grid or next-free is the ENUMERATION CEILING, which is a
// permanent property of the prefix, not a transient failure. Retrying it just
// makes the operator wait longer to be told the same thing.
const NO_RETRY_ENUMERATION = { retry: false } as const;

/**
 * The caller's permission codes, for deciding what RENDERS.
 *
 * INVARIANT #6: this gates nothing. Every one of the module's 38 routes carries
 * its own `require_permission` dependency and the server re-checks on every
 * request; hiding a button here is a usability affordance that saves an
 * operator a 403, and it prevents exactly nothing. A caller who forges this
 * list gets the same 403 they would have got anyway.
 */
export function useIpamPermissions(): {
  read: boolean;
  assign: boolean;
  manage: boolean;
  sweepRun: boolean;
  sweepManage: boolean;
  scopeManage: boolean;
} {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  return {
    read: permissions.includes(IPAM_READ),
    assign: permissions.includes(IPAM_ASSIGN),
    manage: permissions.includes(IPAM_MANAGE),
    sweepRun: permissions.includes(IPAM_SWEEP_RUN),
    sweepManage: permissions.includes(IPAM_SWEEP_MANAGE),
    scopeManage: permissions.includes(IPAM_SCOPE_MANAGE),
  };
}

/**
 * The zone the signed-in operator works in, for `toInstant` (UX cycle 5).
 *
 * WHY A HOOK AND NOT A PROP. The two date fields in this module — "Suppress
 * until" and a scan authorization's "Expires" — sit inside table rows and a
 * grant form, several components below any page that already holds `/me`.
 * Threading `timezone` down that chain means touching components that have
 * nothing to do with dates. This reads the SAME `["me"]` query the permission
 * hook above already keeps warm, so it costs no extra request.
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

export const SUBNETS_KEY = "ipam-subnets";
export const GRID_KEY = "ipam-grid";
export const UTILIZATION_KEY = "ipam-utilization";
export const DRIFT_KEY = "ipam-drift";
export const SCOPES_KEY = "ipam-scan-authorizations";

// --------------------------------------------------------------------------
// The client — every IPAM row is one's (MSP clients phase 2)
// --------------------------------------------------------------------------

/**
 * The client this console files new IPAM rows under, and whose sites its
 * pickers list. `null` until `/me` has answered: a sites read waits for it, and
 * a create that would have to name it throws `InternalScopeUnknown` inside its
 * mutation (`requireKnown`), so NOTHING is sent.
 */
export function useIpamClient(): string | null {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  // PHASE 3: the client/site selector. Until it exists that is the
  // organisation's internal client, whose id is the organisation's --
  // `/me`'s `org_id` (`lib/internalClient.ts`).
  return internalClientId(me.data);
}

/**
 * A create body names its client only when nothing above it does -- no VRF,
 * or for a VLAN no site -- because the server takes the client from the parent
 * and refuses a body that contradicts it. `parent` is that VRF or site.
 */
function namingClient<B extends { client_id?: string | null }>(
  body: B,
  parent: string | null | undefined,
  client: string | null,
): B {
  return parent ? body : { ...body, client_id: requireKnown(client) };
}

// --------------------------------------------------------------------------
// Registry — VRFs, VLANs, and the client's sites they name
// --------------------------------------------------------------------------

export function useIpamVrfs() {
  const api = useApi();
  return useQuery<VrfOut[], ApiError>({
    queryKey: ["ipam-vrfs"],
    queryFn: () => api.listIpamVrfs(),
    ...NO_RETRY,
  });
}

export function useCreateIpamVrfMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  const client = useIpamClient();
  return useMutation<VrfOut, ApiError, Omit<VrfCreate, "client_id">>({
    // PHASE 3: the selected client; until then the internal one (`useIpamClient`).
    mutationFn: (body) => api.createIpamVrf({ ...body, client_id: requireKnown(client) }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ipam-vrfs"] });
    },
  });
}

/**
 * The sites a VLAN or a subnet may name: the client's own (`GET
 * /clients/{id}/sites`, phase 1). IPAM keeps no sites of its own since
 * migration 0240, and creates none -- a site is a client's office, managed
 * with the client. LOADING until the client is known, not empty.
 */
export function useIpamSites() {
  const api = useApi();
  const client = useIpamClient();
  return loadingUntilClient(
    useQuery<ClientSiteOut[], ApiError>({
      queryKey: ["client-sites", client],
      queryFn: () => api.listClientSites(requireKnown(client)),
      enabled: client != null,
      ...NO_RETRY,
    }),
    client,
  );
}

export function useIpamVlans(siteId?: string) {
  const api = useApi();
  return useQuery<VlanOut[], ApiError>({
    queryKey: ["ipam-vlans", siteId ?? null],
    queryFn: () => api.listIpamVlans(siteId),
    ...NO_RETRY,
  });
}

export function useCreateIpamVlanMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  const client = useIpamClient();
  return useMutation<VlanOut, ApiError, VlanCreate>({
    // PHASE 3: a VLAN with no site is the selected client's; until then the
    // internal one's. With a site, the site's client is the VLAN's.
    mutationFn: (body) => api.createIpamVlan(namingClient(body, body.site_id, client)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ipam-vlans"] });
    },
  });
}

// --------------------------------------------------------------------------
// Subnets and the derived hierarchy
// --------------------------------------------------------------------------

export function useIpamSubnets(filters: SubnetFilters = {}) {
  const api = useApi();
  return useQuery<SubnetOut[], ApiError>({
    queryKey: [SUBNETS_KEY, filters],
    queryFn: () => api.listIpamSubnets(filters),
    ...NO_RETRY,
  });
}

/**
 * The fleet's devices, for the one thing this module needs them for: turning
 * the record ids in a drift finding's evidence into hostnames.
 *
 * THE SHARED `["devices"]` KEY, deliberately. Half the console already reads
 * the device list under exactly this key with exactly this call (site servers,
 * remote control, jobs, deploy), so a drift page opened after any of them
 * costs no request at all, and one opened first warms the same cache for them.
 * A per-id `getDevice` fan-out would be four requests for four ids and a
 * fifth the moment a class grows a key.
 *
 * A MISS IS NOT AN ERROR. The names are a courtesy over ids the page can
 * already render; `describeDriftDetail` falls back to the id, so a failed or
 * still-loading read costs the evidence nothing.
 */
export function useIpamDeviceNames() {
  const api = useApi();
  return useQuery<Page<Device>, ApiError>({
    queryKey: ["devices"],
    queryFn: () => api.listDevices(),
    ...NO_RETRY,
  });
}

export function useIpamSubnet(subnetId: string | undefined) {
  const api = useApi();
  return useQuery<SubnetOut, ApiError>({
    queryKey: ["ipam-subnet", subnetId],
    queryFn: () => api.getIpamSubnet(subnetId as string),
    enabled: subnetId != null && subnetId !== "",
    ...NO_RETRY,
  });
}

// A new prefix RE-PARENTS existing rows (`parent_id` is derived), so the whole
// list is stale, not just this row.
export function useCreateIpamSubnetMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  const client = useIpamClient();
  return useMutation<SubnetOut, ApiError, SubnetCreate>({
    // PHASE 3: with no VRF, the selected client's `global` VRF; until then the
    // internal client's. With a VRF, the VRF's client is the subnet's.
    mutationFn: (body) => api.createIpamSubnet(namingClient(body, body.vrf_id, client)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [SUBNETS_KEY] });
    },
  });
}

export function useUpdateIpamSubnetMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<SubnetOut, ApiError, { id: string; body: SubnetUpdate }>({
    mutationFn: ({ id, body }) => api.updateIpamSubnet(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: [SUBNETS_KEY] });
      void queryClient.invalidateQueries({ queryKey: ["ipam-subnet", variables.id] });
    },
  });
}

// Deletion re-parents the deleted row's children onto ITS parent, so again the
// whole list is stale.
export function useDeleteIpamSubnetMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, { id: string; cascade?: boolean }>({
    mutationFn: ({ id, cascade }) => api.deleteIpamSubnet(id, cascade ?? false),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [SUBNETS_KEY] });
    },
  });
}

/**
 * The address grid. 422 above the enumeration ceiling and for every IPv6
 * prefix — the caller must render that refusal as the panel it is, not as an
 * empty grid.
 */
export function useIpamSubnetAddresses(
  subnetId: string | undefined,
  offset: number,
  limit: number,
  enabled = true,
) {
  const api = useApi();
  return useQuery<GridEntryOut[], ApiError>({
    queryKey: [GRID_KEY, subnetId, offset, limit],
    queryFn: () => api.listIpamSubnetAddresses(subnetId as string, offset, limit),
    enabled: enabled && subnetId != null && subnetId !== "",
    ...NO_RETRY_ENUMERATION,
  });
}

export function useIpamSubnetUtilization(subnetId: string | undefined, enabled = true) {
  const api = useApi();
  return useQuery<UtilizationOut, ApiError>({
    queryKey: [UTILIZATION_KEY, subnetId],
    queryFn: () => api.getIpamSubnetUtilization(subnetId as string),
    enabled: enabled && subnetId != null && subnetId !== "",
    ...NO_RETRY_ENUMERATION,
  });
}

/** ADVISORY (§3.3). Two operators who both read this race for the same
 * address; `useAllocateIpamAddressMutation` is the non-racing path. */
export function useIpamNextFree(subnetId: string | undefined, count: number, enabled: boolean) {
  const api = useApi();
  return useQuery<string[], ApiError>({
    queryKey: ["ipam-next-free", subnetId, count],
    queryFn: () => api.getIpamSubnetNextFree(subnetId as string, count),
    enabled: enabled && subnetId != null && subnetId !== "",
    ...NO_RETRY_ENUMERATION,
  });
}

// --------------------------------------------------------------------------
// Ranges
// --------------------------------------------------------------------------

export function useIpamRanges(subnetId: string | undefined) {
  const api = useApi();
  return useQuery<RangeOut[], ApiError>({
    queryKey: ["ipam-ranges", subnetId],
    queryFn: () => api.listIpamRanges(subnetId as string),
    enabled: subnetId != null && subnetId !== "",
    ...NO_RETRY,
  });
}

// A range changes the DERIVED STATUS of every address inside it — a `dhcp_pool`
// band outranks every other row of §3.1's table — and changes what next-free
// will hand out. Hence the grid and utilisation invalidation.
export function useCreateIpamRangeMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<RangeOut, ApiError, { subnetId: string; body: RangeCreate }>({
    mutationFn: ({ subnetId, body }) => api.createIpamRange(subnetId, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["ipam-ranges", variables.subnetId] });
      void queryClient.invalidateQueries({ queryKey: [GRID_KEY] });
      void queryClient.invalidateQueries({ queryKey: [UTILIZATION_KEY] });
      void queryClient.invalidateQueries({ queryKey: ["ipam-next-free"] });
    },
  });
}

export function useDeleteIpamRangeMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, { subnetId: string; rangeId: string }>({
    mutationFn: ({ subnetId, rangeId }) => api.deleteIpamRange(subnetId, rangeId),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["ipam-ranges", variables.subnetId] });
      void queryClient.invalidateQueries({ queryKey: [GRID_KEY] });
      void queryClient.invalidateQueries({ queryKey: [UTILIZATION_KEY] });
      void queryClient.invalidateQueries({ queryKey: ["ipam-next-free"] });
    },
  });
}

// --------------------------------------------------------------------------
// Addresses
// --------------------------------------------------------------------------

export function useIpamAddress(addressId: string | undefined) {
  const api = useApi();
  return useQuery<AddressOut, ApiError>({
    queryKey: ["ipam-address", addressId],
    queryFn: () => api.getIpamAddress(addressId as string),
    enabled: addressId != null && addressId !== "",
    ...NO_RETRY,
  });
}

/** The audit-log projection (§0.13), reachable with `ipam.read` alone. */
export function useIpamAddressHistory(addressId: string | undefined) {
  const api = useApi();
  return useQuery<HistoryEntryOut[], ApiError>({
    queryKey: ["ipam-address-history", addressId],
    queryFn: () => api.getIpamAddressHistory(addressId as string),
    enabled: addressId != null && addressId !== "",
    ...NO_RETRY,
  });
}

// Every address write changes the grid and the utilisation of whichever subnet
// contains it — and containment is DERIVED, so the caller cannot reliably name
// that subnet. Both keys are invalidated wholesale rather than by id.
function invalidateAddressEffects(
  queryClient: ReturnType<typeof useQueryClient>,
  addressId?: string,
): void {
  void queryClient.invalidateQueries({ queryKey: [GRID_KEY] });
  void queryClient.invalidateQueries({ queryKey: [UTILIZATION_KEY] });
  void queryClient.invalidateQueries({ queryKey: ["ipam-next-free"] });
  if (addressId !== undefined) {
    void queryClient.invalidateQueries({ queryKey: ["ipam-address", addressId] });
    void queryClient.invalidateQueries({ queryKey: ["ipam-address-history", addressId] });
  }
}

export function useCreateIpamAddressMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  const client = useIpamClient();
  return useMutation<AddressOut, ApiError, AddressCreate>({
    // PHASE 3: with no VRF (the address dialog names none), the selected
    // client's `global` VRF; until then the internal client's.
    mutationFn: (body) => api.createIpamAddress(namingClient(body, body.vrf_id, client)),
    onSuccess: (data) => invalidateAddressEffects(queryClient, data.id),
  });
}

export function useUpdateIpamAddressMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<AddressOut, ApiError, { id: string; body: AddressUpdate }>({
    mutationFn: ({ id, body }) => api.updateIpamAddress(id, body),
    onSuccess: (data) => invalidateAddressEffects(queryClient, data.id),
  });
}

export function useReleaseIpamAddressMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<AddressOut, ApiError, string>({
    mutationFn: (id) => api.releaseIpamAddress(id),
    onSuccess: (data) => invalidateAddressEffects(queryClient, data.id),
  });
}

/** THE NON-RACING PATH (§3.3): picks and claims in one transaction under an
 * advisory lock and returns the address it took. */
export function useAllocateIpamAddressMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<AddressOut, ApiError, { subnetId: string; body: AllocateRequest }>({
    mutationFn: ({ subnetId, body }) => api.allocateIpamAddress(subnetId, body),
    onSuccess: (data) => invalidateAddressEffects(queryClient, data.id),
  });
}

export function useIpamSearch(q: string) {
  const api = useApi();
  return useQuery<SearchOut, ApiError>({
    queryKey: ["ipam-search", q],
    queryFn: () => api.searchIpam(q),
    enabled: q.trim().length > 0,
    ...NO_RETRY,
  });
}

// --------------------------------------------------------------------------
// Drift and orphans
// --------------------------------------------------------------------------

export function useIpamDrift(filters: DriftFilters = {}) {
  const api = useApi();
  return useQuery<DriftOut[], ApiError>({
    queryKey: [DRIFT_KEY, filters],
    queryFn: () => api.listIpamDrift(filters),
    ...NO_RETRY,
  });
}

/** Orphans are a SEPARATE query because they are separate information (§4.2):
 * observed addresses inside no registered subnet. Nothing is wrong with the
 * plan; the plan is absent. */
export function useIpamOrphans() {
  const api = useApi();
  return useQuery<OrphanOut[], ApiError>({
    queryKey: ["ipam-orphans"],
    queryFn: () => api.listIpamOrphans(),
    ...NO_RETRY,
  });
}

export function useAcknowledgeIpamDriftMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<DriftOut, ApiError, { id: string; note: string }>({
    mutationFn: ({ id, note }) => api.acknowledgeIpamDrift(id, note),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [DRIFT_KEY] });
    },
  });
}

export function useSuppressIpamDriftMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<DriftOut, ApiError, { id: string; until: string; note: string }>({
    mutationFn: ({ id, until, note }) => api.suppressIpamDrift(id, until, note),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [DRIFT_KEY] });
    },
  });
}

// --------------------------------------------------------------------------
// Sweep governance
// --------------------------------------------------------------------------

export function useIpamScanAuthorizations(includeRevoked = false) {
  const api = useApi();
  return useQuery<ScopeOut[], ApiError>({
    queryKey: [SCOPES_KEY, includeRevoked],
    queryFn: () => api.listIpamScanAuthorizations(includeRevoked),
    ...NO_RETRY,
  });
}

/** HIGH PRIVILEGE (`ipam.scope.manage`). Granting this decides where the
 * platform emits network traffic. */
export function useCreateIpamScanAuthorizationMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  const client = useIpamClient();
  return useMutation<ScopeOut, ApiError, ScopeCreate>({
    // PHASE 3: with no VRF, the selected client's `global` VRF; until then the
    // internal client's. With a VRF, the VRF's client is the authorization's.
    mutationFn: (body) =>
      api.createIpamScanAuthorization(namingClient(body, body.vrf_id, client)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [SCOPES_KEY] });
    },
  });
}

export function useRevokeIpamScanAuthorizationMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.revokeIpamScanAuthorization(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [SCOPES_KEY] });
    },
  });
}

export function useIpamSweepRuns(subnetId: string | undefined, limit = 20) {
  const api = useApi();
  return useQuery<SweepRunOut[], ApiError>({
    queryKey: ["ipam-sweep-runs", subnetId, limit],
    queryFn: () => api.listIpamSweepRuns(subnetId as string, limit),
    enabled: subnetId != null && subnetId !== "",
    ...NO_RETRY,
  });
}

/** 202 on success; 403 when nothing authorises the targets — and in that case
 * NO JOB ROW IS CREATED, so the refusal is complete rather than partial. */
export function useDispatchIpamSweepMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<SweepRunOut, ApiError, string>({
    mutationFn: (subnetId) => api.dispatchIpamSweep(subnetId),
    onSuccess: (_data, subnetId) => {
      void queryClient.invalidateQueries({ queryKey: ["ipam-sweep-runs", subnetId] });
      void queryClient.invalidateQueries({ queryKey: ["ipam-subnet", subnetId] });
    },
  });
}

/**
 * The machines a subnet's designated sweeper may be: its CLIENT'S, with an
 * agent (`GET /devices?client_id=`). The server refuses another client's
 * machine (422) and the sweeper never borrows one; this is what keeps the
 * picker from offering it.
 */
export function useSweeperChoices(clientId: string) {
  const api = useApi();
  return useQuery<DevicePage, ApiError, Device[]>({
    queryKey: ["devices", { clientId, limit: SWEEPER_CHOICES }],
    queryFn: () => api.listDevices({ clientId, limit: SWEEPER_CHOICES }),
    select: (page) => page.items.filter((device) => device.agent_status !== null),
    ...NO_RETRY,
  });
}

/** The fleet list's page ceiling: a picker, not a report. */
const SWEEPER_CHOICES = 500;

export function useUpdateIpamSweepSettingsMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<SubnetOut, ApiError, { subnetId: string; body: SweepSettings }>({
    mutationFn: ({ subnetId, body }) => api.updateIpamSweepSettings(subnetId, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["ipam-subnet", variables.subnetId] });
      void queryClient.invalidateQueries({ queryKey: [SUBNETS_KEY] });
    },
  });
}

// --------------------------------------------------------------------------
// Import / export
// --------------------------------------------------------------------------
//
// Preview and apply are deliberately NOT one hook. They are two steps bound by
// a digest, and a single hook that ran both would be the exact defeat of that
// guard.

//
// An import NAMES ITS CLIENT: the sheet goes into that client's `global` VRF,
// and the server refuses one that names none.

export function usePreviewIpamImportMutation() {
  const api = useApi();
  const client = useIpamClient();
  return useMutation<
    ImportPreviewOut,
    ApiError,
    { file: File; mapping: Record<string, string> }
  >({
    // PHASE 3: the selected client; until then the internal one (`useIpamClient`).
    mutationFn: ({ file, mapping }) => api.previewIpamImport(file, mapping, requireKnown(client)),
  });
}

export function useApplyIpamImportMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  const client = useIpamClient();
  return useMutation<
    ImportResultOut,
    ApiError,
    { file: File; mapping: Record<string, string>; digest: string }
  >({
    // PHASE 3: the selected client -- the one previewed; until then the internal one.
    mutationFn: ({ file, mapping, digest }) =>
      api.applyIpamImport(file, mapping, digest, requireKnown(client)),
    onSuccess: () => {
      invalidateAddressEffects(queryClient);
    },
  });
}

/** `dry_run` defaults to TRUE, and the default is the safe one (§8.3). */
export function useConvertIpamDocRecordsMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<DocRecordConversionOut, ApiError, boolean>({
    mutationFn: (dryRun) => api.convertIpamDocRecords(dryRun),
    onSuccess: (data) => {
      // A dry run wrote nothing, so nothing is stale.
      if (!data.dry_run) void queryClient.invalidateQueries({ queryKey: [SUBNETS_KEY] });
    },
  });
}
