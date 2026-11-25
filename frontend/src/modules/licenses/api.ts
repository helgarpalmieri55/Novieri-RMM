// TanStack Query hooks for the Licenses/SAM module, built on the shared
// ApiClient (useApi()). There is no live licenses event stream -- every
// mutation below invalidates the relevant query keys on success so the UI
// refetches, rather than subscribing to a websocket event that doesn't exist
// for this module (mirrors modules/bcp/api.ts's rationale).
//
// `useRevealLicenseKeyMutation` is deliberately a mutation, not a query: the
// revealed key must never be stored in a react-query cache keyed by license
// id (spec §5 / security invariant #3) -- the caller keeps the result in
// local component state only, shown once (mirrors CredentialsPage's
// checkoutCredential pattern).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "../../lib/ApiContext";
import { useNarrowedApi } from "../../shell/components";
import type {
  AllocationCreate,
  LicenseCreate,
  LicenseUpdate,
  ProductCreate,
  ProductUpdate,
  ReviewIgnoreRequest,
  ReviewMapRequest,
  VendorCreate,
  VendorUpdate,
} from "./types";
import { useSecretMutation } from "../../lib/secretMutation";

export function useVendors() {
  const api = useApi();
  return useQuery({ queryKey: ["license-vendors"], queryFn: () => api.listVendors() });
}

export function useCreateVendorMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: VendorCreate) => api.createVendor(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["license-vendors"] });
    },
  });
}

export function useVendor(id: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["license-vendor", id],
    queryFn: () => api.getVendor(id as string),
    enabled: id != null,
  });
}

export function useUpdateVendorMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: VendorUpdate }) => api.updateVendor(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["license-vendors"] });
      void queryClient.invalidateQueries({ queryKey: ["license-vendor", variables.id] });
    },
  });
}

export function useProducts() {
  const api = useApi();
  return useQuery({ queryKey: ["license-products"], queryFn: () => api.listProducts() });
}

export function useCreateProductMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ProductCreate) => api.createProduct(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["license-products"] });
    },
  });
}

export function useProduct(id: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["license-product", id],
    queryFn: () => api.getProduct(id as string),
    enabled: id != null,
  });
}

export function useUpdateProductMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ProductUpdate }) => api.updateProduct(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["license-products"] });
      void queryClient.invalidateQueries({ queryKey: ["license-product", variables.id] });
    },
  });
}

/**
 * THE REGISTER, NARROWED BY THE CLIENT CHIP (decision D6; redesign plan,
 * Receta L, point 7). `chip` is `?client=` ("" without one): the same call
 * with the same arguments, read through `useNarrowedApi` and keyed by the
 * client it narrowed to, so a change of chip reads again and never shows the
 * last client's rows under the new one. `VendorsPage` calls it without a chip.
 * Mutations still invalidate the `["licenses"]` prefix, which reaches every
 * client's key.
 */
export function useLicenses(chip = "") {
  const { api, client, ready } = useNarrowedApi(chip);
  return useQuery({
    queryKey: ["licenses", client],
    queryFn: () => api.listLicenses(),
    enabled: ready,
  });
}

export function useCreateLicenseMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  // A secret in its body: `useSecretMutation` drops it once it settles (`lib/secretMutation.ts`).
  return useSecretMutation({
    // The body is BUILT HERE, inside the mutation, not before it: a body that
    // cannot name its client yet throws `ClientNotChosen`
    // (`lib/clientSelection.ts`), which then fails the mutation -- so the
    // form's error surface says why and nothing is sent.
    mutationFn: (build: () => LicenseCreate) => api.createLicense(build()),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["licenses"] });
      void queryClient.invalidateQueries({ queryKey: ["license-compliance"] });
    },
  });
}

export function useLicense(id: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["license", id],
    queryFn: () => api.getLicense(id as string),
    enabled: id != null,
  });
}

export function useUpdateLicenseMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  // A secret in its body: `useSecretMutation` drops it once it settles (`lib/secretMutation.ts`).
  return useSecretMutation({
    mutationFn: ({ id, body }: { id: string; body: LicenseUpdate }) => api.updateLicense(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["licenses"] });
      void queryClient.invalidateQueries({ queryKey: ["license", variables.id] });
      void queryClient.invalidateQueries({ queryKey: ["license-compliance"] });
    },
  });
}

export function useDeleteLicenseMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteLicense(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["licenses"] });
      void queryClient.invalidateQueries({ queryKey: ["license-compliance"] });
    },
  });
}

// Deliberately a mutation, not a query -- see this file's module docstring.
export function useRevealLicenseKeyMutation() {
  const api = useApi();
  // A secret in its answer: `useSecretMutation` drops it once the page holds it (`lib/secretMutation.ts`).
  return useSecretMutation({ mutationFn: (id: string) => api.revealLicenseKey(id) });
}

// The org directory, for naming the person behind a user allocation. Same
// query key as subscriptions' `useOrgUsers`, so the two modules share one
// cached answer; `retry: false` because a caller without users.read gets a
// 403 that retrying will not change, and the page falls back to the id.
export function useOrgUsers() {
  const api = useApi();
  return useQuery({ queryKey: ["users"], queryFn: () => api.listUsers(), retry: false });
}

export function useAllocations(licenseId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["license-allocations", licenseId],
    queryFn: () => api.listAllocations(licenseId as string),
    enabled: licenseId != null,
  });
}

export function useCreateAllocationMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ licenseId, body }: { licenseId: string; body: AllocationCreate }) =>
      api.createAllocation(licenseId, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: ["license-allocations", variables.licenseId],
      });
    },
  });
}

export function useReleaseAllocationMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ allocationId }: { allocationId: string; licenseId: string }) =>
      api.releaseAllocation(allocationId),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: ["license-allocations", variables.licenseId],
      });
    },
  });
}

/** Narrowed like `useLicenses`: the register's tiles count the client its chip chose. */
export function useCompliance(chip = "") {
  const { api, client, ready } = useNarrowedApi(chip);
  return useQuery({
    queryKey: ["license-compliance", client],
    queryFn: () => api.licenseCompliance(),
    enabled: ready,
  });
}

export function useReviewQueue() {
  const api = useApi();
  return useQuery({ queryKey: ["license-review-queue"], queryFn: () => api.reviewQueue() });
}

export function useMapSoftwareMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ReviewMapRequest) => api.mapSoftware(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["license-review-queue"] });
      void queryClient.invalidateQueries({ queryKey: ["license-products"] });
      void queryClient.invalidateQueries({ queryKey: ["license-compliance"] });
    },
  });
}

export function useIgnoreSoftwareMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ReviewIgnoreRequest) => api.ignoreSoftware(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["license-review-queue"] });
    },
  });
}

/** Narrowed like `useLicenses`: the register's renewal tile counts the client its chip chose. */
export function useRenewals(withinDays?: number, chip = "") {
  const { api, client, ready } = useNarrowedApi(chip);
  return useQuery({
    queryKey: ["license-renewals", withinDays, client],
    queryFn: () => api.licenseRenewals(withinDays),
    enabled: ready,
  });
}

export function useCosts() {
  const api = useApi();
  return useQuery({ queryKey: ["license-costs"], queryFn: () => api.licenseCosts() });
}
