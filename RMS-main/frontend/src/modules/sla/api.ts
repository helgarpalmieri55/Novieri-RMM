// TanStack Query hooks for the SLA module, built on the shared ApiClient
// (useApi()). There is no live SLA event stream -- the breach sweep runs on
// the worker's beat schedule -- so every mutation below invalidates the
// relevant query keys on success and the UI refetches, mirroring
// modules/notification/api.ts.
//
// Several server-side mutations have effects BEYOND the row they edit: a
// calendar timezone change, a window replacement, a holiday add/remove and a
// policy target/calendar change each recompute the deadlines of every open
// ticket on that calendar or policy, in the same transaction (spec §7,
// api.py). Those hooks therefore invalidate `["sla-ticket"]` as well, so a
// ticket view open in another tab cannot keep showing a deadline the server
// has already rewritten.
//
// Every hook defined here is wired into a panel; none are left dangling.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiError, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import type { QueueOut } from "../service-desk/types";
import type {
  CalendarCreate,
  CalendarDetail,
  CalendarOut,
  CalendarUpdate,
  HolidayCreate,
  HolidayOut,
  PolicyCreate,
  PolicyOut,
  PolicyUpdate,
  SettingsOut,
  SettingsUpdate,
  TicketSlaOut,
  WindowIn,
  WindowOut,
} from "./types";

// A genuine 403 (the caller lacks `sla.read`) should settle immediately
// rather than retry against the same permission wall -- the app-wide
// QueryClient default (App.tsx) is `retry: 1`.
const NO_RETRY = { retry: false } as const;

// --------------------------------------------------------------------------
// The reader's timezone (UX remediation cycle 5)
// --------------------------------------------------------------------------

/**
 * The zone the signed-in operator works in, for `lib/datetime`.
 *
 * WHY A HOOK AND NOT A PROP, mirroring `modules/ipam/api.ts#useUserTimezone`.
 * The instants this module renders sit deep: a calendar's `created_at` is in
 * a table row, and every deadline is inside `TicketSlaPanel`, which is
 * mounted by the SERVICE DESK's ticket page -- a component chain this module
 * does not own. Threading `timezone` down either chain means editing files
 * that have nothing to do with dates. This reads the SAME `["me"]` query
 * every page in the product already keeps warm (`staleTime: 60_000`), so it
 * costs no extra request.
 *
 * CAREFUL ABOUT WHOSE ZONE THIS IS. It is the READER's, and it is right for
 * an instant -- a deadline is one moment, and two people reading one ticket
 * should each see it where they are. It is emphatically NOT the calendar's:
 * a working window and a holiday date are wall-clock values belonging to
 * `calendar.timezone`, which is why those are rendered as typed and the
 * calendar's zone is badged beside them rather than converted.
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

// --------------------------------------------------------------------------
// Calendars
// --------------------------------------------------------------------------

export function useSlaCalendars() {
  const api = useApi();
  return useQuery<CalendarOut[], ApiError>({
    queryKey: ["sla-calendars"],
    queryFn: () => api.listSlaCalendars(),
    ...NO_RETRY,
  });
}

export function useSlaCalendar(calendarId: string | undefined) {
  const api = useApi();
  return useQuery<CalendarDetail, ApiError>({
    queryKey: ["sla-calendar", calendarId],
    queryFn: () => api.getSlaCalendar(calendarId as string),
    enabled: calendarId != null && calendarId !== "",
    ...NO_RETRY,
  });
}

export function useCreateSlaCalendarMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: CalendarCreate) => api.createSlaCalendar(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["sla-calendars"] });
    },
  });
}

// A timezone change recomputes every still-running clock on this calendar
// (spec §7) -- hence the ticket-state invalidation alongside the calendar's.
export function useUpdateSlaCalendarMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: CalendarUpdate }) =>
      api.updateSlaCalendar(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["sla-calendars"] });
      void queryClient.invalidateQueries({ queryKey: ["sla-calendar", variables.id] });
      void queryClient.invalidateQueries({ queryKey: ["sla-ticket"] });
    },
  });
}

export function useDeleteSlaCalendarMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteSlaCalendar(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["sla-calendars"] });
    },
  });
}

export function useReplaceSlaWindowsMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<WindowOut[], ApiError, { id: string; windows: WindowIn[] }>({
    mutationFn: ({ id, windows }) => api.replaceSlaCalendarWindows(id, windows),
    onSuccess: (_data, variables) => {
      // The window count decides `calendar_health`, so the LIST is stale too,
      // not just the detail.
      void queryClient.invalidateQueries({ queryKey: ["sla-calendars"] });
      void queryClient.invalidateQueries({ queryKey: ["sla-calendar", variables.id] });
      void queryClient.invalidateQueries({ queryKey: ["sla-ticket"] });
    },
  });
}

// --------------------------------------------------------------------------
// Holidays
// --------------------------------------------------------------------------

export function useSlaHolidays(calendarId: string | undefined) {
  const api = useApi();
  return useQuery<HolidayOut[], ApiError>({
    queryKey: ["sla-holidays", calendarId],
    queryFn: () => api.listSlaCalendarHolidays(calendarId as string),
    enabled: calendarId != null && calendarId !== "",
    ...NO_RETRY,
  });
}

export function useAddSlaHolidayMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<HolidayOut, ApiError, { id: string; body: HolidayCreate }>({
    mutationFn: ({ id, body }) => api.addSlaCalendarHoliday(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["sla-holidays", variables.id] });
      void queryClient.invalidateQueries({ queryKey: ["sla-ticket"] });
    },
  });
}

export function useRemoveSlaHolidayMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, { id: string; holidayId: string }>({
    mutationFn: ({ id, holidayId }) => api.removeSlaCalendarHoliday(id, holidayId),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["sla-holidays", variables.id] });
      void queryClient.invalidateQueries({ queryKey: ["sla-ticket"] });
    },
  });
}

// --------------------------------------------------------------------------
// Policies
// --------------------------------------------------------------------------

export function useSlaPolicies() {
  const api = useApi();
  return useQuery<PolicyOut[], ApiError>({
    queryKey: ["sla-policies"],
    queryFn: () => api.listSlaPolicies(),
    ...NO_RETRY,
  });
}

export function useCreateSlaPolicyMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<PolicyOut, ApiError, PolicyCreate>({
    mutationFn: (body) => api.createSlaPolicy(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["sla-policies"] });
    },
  });
}

// A change to either target or to `calendar_id` recomputes every
// still-running clock under this policy, in the same transaction (spec §7).
export function useUpdateSlaPolicyMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<PolicyOut, ApiError, { id: string; body: PolicyUpdate }>({
    mutationFn: ({ id, body }) => api.updateSlaPolicy(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["sla-policies"] });
      void queryClient.invalidateQueries({ queryKey: ["sla-ticket"] });
    },
  });
}

// Deleting a policy still referenced by any ticket_sla row is REFUSED with a
// 400 naming the tickets (api.py) -- `active: false` is the retirement path.
export function useDeleteSlaPolicyMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.deleteSlaPolicy(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["sla-policies"] });
    },
  });
}

// --------------------------------------------------------------------------
// Org settings
// --------------------------------------------------------------------------

export function useSlaSettings() {
  const api = useApi();
  return useQuery<SettingsOut, ApiError>({
    queryKey: ["sla-settings"],
    queryFn: () => api.getSlaSettings(),
    ...NO_RETRY,
  });
}

export function useUpdateSlaSettingsMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<SettingsOut, ApiError, SettingsUpdate>({
    mutationFn: (body) => api.updateSlaSettings(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["sla-settings"] });
    },
  });
}

// --------------------------------------------------------------------------
// Ticket queues -- a CROSS-MODULE read
// --------------------------------------------------------------------------
//
// `GET /tickets/queues` is gated on `ticket.read`, independent of this
// module's `sla.read`/`sla.admin`: an SLA admin is not guaranteed it. Every
// consumer degrades to a free-text queue-id field on a 403 rather than
// blocking on a permission this module does not itself require (the
// precedent notification's RulesPanel set, and the reverse direction of
// service-desk's DeviceSummaryPanel). One shared query key, so the four
// pickers on this page are one request.

export function useSlaTicketQueues() {
  const api = useApi();
  return useQuery<QueueOut[], ApiError>({
    queryKey: ["sla-ticket-queues"],
    queryFn: () => api.listTicketQueues(),
    ...NO_RETRY,
  });
}

// --------------------------------------------------------------------------
// Per-ticket state (gated on `ticket.read`)
// --------------------------------------------------------------------------
//
// `has_sla: false` is a successful 200, so this query does NOT treat "no SLA"
// as an error -- the panel renders it as the legitimate state it is.

export function useTicketSla(ticketId: string | undefined) {
  const api = useApi();
  return useQuery<TicketSlaOut, ApiError>({
    queryKey: ["sla-ticket", ticketId],
    queryFn: () => api.getTicketSla(ticketId as string),
    enabled: ticketId != null && ticketId !== "",
    ...NO_RETRY,
  });
}
