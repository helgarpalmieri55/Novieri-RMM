// TanStack Query hooks for the Reporting module, built on the shared ApiClient
// (useApi()), matching modules/notification/api.ts's conventions.
//
// EVERYTHING HERE IS A READ. Reporting stores nothing, so there is no mutation
// hook and no cache to invalidate -- with ONE exception that is not a mutation
// of server state at all: `useExportReport` is a `useMutation` purely because
// an export is user-initiated and must not run on render (it writes a
// `report.exported` audit event server-side, spec §0.7, so a `useQuery` that
// refetched on focus would append to the audit chain every time the tab
// regained focus).
//
// `retry: false` throughout, for the reason notification/api.ts gives: a 403
// from the source conjunction is a permission wall, not a transient failure,
// and retrying it just delays the message that names the missing codes.

import { useMutation, useQuery } from "@tanstack/react-query";
import { useApi } from "../../lib/ApiContext";
import type { Me } from "../../lib/api";
import type { ReportParams } from "./types";

// The signed-in operator's own zone (`/me`, cycle 2), which is what every
// datetime on a report page is entered in and displayed in.
//
// SAME QUERY KEY AS THE CHROME'S, so this is the cached `/me` the shell has
// usually already fetched rather than a second request. `null` when the
// profile carries no zone or the request has not landed -- `formatDateTime`
// and `toInstant` both read that as "fall back to the browser", which is the
// old behaviour and never a wrong-but-confident time.
export function useProfileZone(): string | null {
  const api = useApi();
  const me = useQuery<Me>({
    queryKey: ["me"],
    queryFn: () => api.me(),
    staleTime: 60_000,
    retry: false,
  });
  return me.data?.timezone ?? null;
}

export function useReportCatalogue() {
  const api = useApi();
  return useQuery({
    queryKey: ["report-catalogue"],
    queryFn: () => api.listReports(),
    retry: false,
  });
}

export function useReport(key: string, params: ReportParams, enabled: boolean) {
  const api = useApi();
  return useQuery({
    // The params are part of the key: a report is computed live, so two
    // parameter sets are two different answers and must not share a cache
    // entry.
    queryKey: ["report", key, params],
    queryFn: () => api.runReport(key, params),
    enabled,
    retry: false,
  });
}

export function useExportReport(key: string) {
  const api = useApi();
  return useMutation({
    mutationFn: (params: ReportParams) => api.exportReport(key, params),
  });
}

/**
 * The organisation's ticket queues, for the ONE report parameter that is a
 * queue's UUID (`sla-attainment`'s `queue_id`).
 *
 * AUDIT [MEDIUM], /reports/sla-attainment: "Queue Id repeats the /changes
 * 'Action ID' defect -- the user must find a UUID in another module and
 * transcribe it, when a queue picker already exists elsewhere in the product
 * (/sla has a populated queue dropdown)."
 *
 * `enabled` is passed by the caller and is false unless a control actually
 * needs it: a report with no
 * queue parameter must not fetch another module's data, and `ticket.read` is
 * exactly the permission `sla-attainment` already requires -- a report that
 * does not require it would get a 403 for a list it never shows.
 *
 * `retry: false` for this module's stated reason: a 403 is a permission wall,
 * not a transient failure.
 */
export function useTicketQueues(enabled: boolean) {
  const api = useApi();
  return useQuery({
    queryKey: ["reporting-ticket-queues"],
    queryFn: () => api.listTicketQueues(),
    enabled,
    retry: false,
  });
}
