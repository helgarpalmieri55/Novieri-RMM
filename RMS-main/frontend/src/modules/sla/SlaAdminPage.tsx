import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { Me } from "../../lib/api";
import { useT } from "../../i18n/context";
import { ApiError } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useSlaCalendars, useSlaPolicies, useSlaSettings, useSlaTicketQueues } from "./api";
import { CalendarsPanel } from "./CalendarsPanel";
import { PoliciesPanel } from "./PoliciesPanel";
import { PolicyResolverPanel } from "./PolicyResolverPanel";
import { SlaPrerequisites } from "./SlaPrerequisites";
import { SlaSettingsPanel } from "./SlaSettingsPanel";
import { PageHead, StatCard, StatsGrid } from "../../shell/components";

// Spec §9.1's admin surface: business calendars (with the weekly window grid
// and holidays), SLA policies, the queue/priority resolution preview, and the
// org settings.
//
// `canRead`/`canAdmin` below only decide what RENDERS. Every route is gated
// server-side -- `sla.read` on reads, `sla.admin` on every mutation -- and
// that check is the only authority (invariant #6). Hiding a control here is a
// usability affordance; it prevents nothing.
//
// WHAT THE DESIGN ASKS FOR THAT NO ENDPOINT SERVES (UI port spec §7). The
// prototype's stat row reads "SLA attainment 93%", "At risk 3", "Breached
// this month 3" -- none of them are backed here. Attainment and breach counts
// are `reporting`'s `sla-attainment` report (spec §5.4), which is grouped by
// `(queue, priority)` over an OPERATOR-CHOSEN date window and additionally
// requires `reporting.read` plus `ticket.read` -- it is not a number this
// admin page can compute for free, and faking a window would misstate it.
// What this page already fetches honestly are the two consequence counts
// `GET /sla/settings` was built to surface (spec §9.2): tickets with no SLA
// row at all, and tickets whose stored deadline no longer matches the
// configuration that produced it -- plus the calendar and policy counts.
export function SlaAdminPage() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canAdmin = permissions.includes("sla.admin");

  // One shared queue read for every panel that names a queue (TanStack dedupes
  // on the key). Gated on `ticket.read`, which an SLA admin is not guaranteed
  // -- so a queue that cannot be named falls back to its id rather than
  // blanking the cell.
  const queues = useSlaTicketQueues();
  const queuesForbidden = queues.error instanceof ApiError && queues.error.status === 403;
  const queueName = (id: string | null): string => {
    if (id === null) return t("sla.common.anyQueue");
    if (queuesForbidden) return id;
    return queues.data?.find((queue) => queue.id === id)?.name ?? id;
  };

  // Each of these is the SAME query a panel below already issues, served from
  // the same cache entry rather than doubling the request count.
  const calendars = useSlaCalendars();
  const policies = useSlaPolicies();
  const settings = useSlaSettings();
  // The policy form's escape hatch out of the first-run dead end (cycle 7):
  // "Add a business calendar" inside the Add-policy dialog bumps this, and the
  // calendars panel below opens its own create dialog. A COUNTER, not a flag,
  // so a user who dismisses the calendar dialog and asks again gets it back.
  const [calendarCreateNonce, setCalendarCreateNonce] = useState(0);
  const activePolicyCount = (policies.data ?? []).filter((policy) => policy.active).length;
  // WHAT THE TWO CONFIGURATION FIGURES ARE MADE OF. The two consequence tiles
  // beside them carry a sentence at a non-zero count and these carried
  // nothing, so half the row sat with an empty band under the number -- the
  // same raggedness the review found on /notifications and /email-intake.
  // Both lines below are read off rows this page already holds, in this
  // module's own vocabulary, and both are `undefined` when there is nothing
  // loaded or nothing to break down, so a zero label keeps the line.
  const calendarFoot = ((): string | undefined => {
    const rows = calendars.data;
    if (rows === undefined || rows.length === 0) return undefined;
    // A calendar with no working windows or an unresolvable timezone produces
    // no deadline at all (`calendarHealthExplanation`), so "3 calendars" with
    // one of them broken is a materially different configuration from three
    // that work -- and it is the fault this panel exists to surface.
    const broken = rows.filter((calendar) => calendar.health !== "ok").length;
    return broken === 0
      ? t("sla.admin.allCanProduce")
      : t("sla.admin.brokenCalendars", { count: broken });
  })();
  const policyFoot =
    policies.data === undefined || activePolicyCount === 0
      ? undefined
      : t("sla.admin.policiesFoot", { count: policies.data.length, active: activePolicyCount });
  const ticketsWithoutSla = settings.data?.tickets_without_sla ?? 0;
  const staleTicketSla = settings.data?.stale_ticket_sla ?? 0;

  /**
   * "NOT MEASURED" AND "MEASURED ZERO" ARE DIFFERENT ANSWERS, and until now
   * this row gave the reassuring one for both: `?? 0` renders a confident
   * "0 tickets with no SLA" while `GET /sla/settings` is still in flight, or
   * has failed, or was refused -- the audit's finding of nine cards reading 0
   * on a page where nothing had been measured, on the page whose whole
   * subject is unkept promises. An em dash says "no answer yet"; the
   * `zeroLabel` on each card below says what a real 0 means.
   */
  const count = (loaded: boolean, value: number): string => (loaded ? String(value) : "\u2014");
  const settingsLoaded = settings.data !== undefined;

  return (
    <div>
      <PageHead
        title={t("sla.admin.title")}
        description={t("sla.admin.description")}
      />

      <SlaPrerequisites />

      {/* WHERE EACH NUMBER LIVES. The two configuration counts are counts of
          the tables further down THIS page -- the calendars panel and the
          policies panel are the detail behind them, and there is no other
          route that holds either, so /sla is the honest destination rather
          than an invented one. The two consequence counts are counts of
          TICKETS, which live in the service desk; `list_tickets` has no "no
          SLA row" or "stale deadline" filter to narrow to (those numbers are
          computed by `GET /sla/settings`, not by a ticket query), so they
          land on the ticket list itself rather than on a URL that would
          claim a filter this API cannot honour.

          Gold is "a promise is missing", orange is "a promise is stale", and
          only the first is loud: a ticket with no SLA row has no clock at
          all, while a stale one is still counting, just from a configuration
          that has moved on. Blue and neutral are configuration facts, which
          nobody is being asked to act on. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("sla.admin.calendarsLabel")}
          value={count(calendars.data !== undefined, calendars.data?.length ?? 0)}
          icon="calendar"
          to="/sla"
          tone="neutral"
          foot={calendarFoot}
          zeroLabel={t("sla.admin.calendarsZero")}
        />
        <StatCard
          label={t("sla.admin.policiesLabel")}
          value={count(policies.data !== undefined, activePolicyCount)}
          icon="shield-check"
          to="/sla"
          tone="blue"
          foot={policyFoot}
          zeroLabel={t("sla.admin.policiesZero")}
        />
        <StatCard
          label={t("sla.admin.noSlaLabel")}
          value={count(settingsLoaded, ticketsWithoutSla)}
          icon="alert-triangle"
          to="/service-desk"
          tone="gold"
          // Never loud on an em dash: an alarm colour over "no answer yet"
          // is the same lie as a 0 over it, pointing the other way.
          urgent={settingsLoaded}
          // WHY, ON THE TILE THAT STATES THE NUMBER. The SLA settings panel
          // below used to restate both of these counts as pills within 200px
          // of the tiles that already carried them, purely to have somewhere
          // to hang the diagnosis. The number is stated once, here, and the
          // sentence that explains it comes with it.
          foot={
            settingsLoaded && ticketsWithoutSla > 0
              ? t("sla.admin.noSlaFoot")
              : undefined
          }
          zeroLabel={t("sla.admin.noSlaZero")}
        />
        <StatCard
          label={t("sla.admin.staleLabel")}
          value={count(settingsLoaded, staleTicketSla)}
          icon="clock-exclamation"
          to="/service-desk"
          tone="orange"
          foot={
            settingsLoaded && staleTicketSla > 0
              ? t("sla.admin.staleFoot")
              : undefined
          }
          zeroLabel={t("sla.admin.staleZero")}
        />
      </StatsGrid>

      <div className="grid grid-2" style={{ marginBottom: 16 }}>
        <SlaSettingsPanel canAdmin={canAdmin} />
        <PolicyResolverPanel />
      </div>
      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
        <PoliciesPanel
          canAdmin={canAdmin}
          queueName={queueName}
          onCreateCalendar={() => setCalendarCreateNonce((n) => n + 1)}
        />
        <CalendarsPanel canAdmin={canAdmin} openCreateNonce={calendarCreateNonce} />
      </div>
    </div>
  );
}
