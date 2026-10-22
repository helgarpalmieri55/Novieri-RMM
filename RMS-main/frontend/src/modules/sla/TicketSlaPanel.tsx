import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { describeSubmitError } from "../../components/form";
import { Badge } from "../../components/ui";
import { Panel } from "../../shell/components";
import { Facts } from "../../shell/viz";
import { ApiError } from "../../lib/api";
import { formatDateTime } from "../../lib/datetime";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";
import { humaniseStatus } from "../../lib/labels";
import { useMePermissions } from "../../lib/mePermissions";
import { useTicketSla, useUserTimezone } from "./api";
import { describeTargetTiming, targetStateLabel, targetStateVariant } from "./slaState";
import type { TargetState, TicketSlaOut } from "./types";

// Spec §9.1's ticket-detail addition: the ticket's response and resolution
// state -- due, met, breached, paused or void -- with the remaining or
// elapsed time a technician prioritises from.
//
// THE SERVER OWNS THE VERDICT. `response_state`/`resolution_state` come from
// `views.target_state`; this panel maps them to a label, a colour and a
// duration and never re-derives one from the timestamps. That is why:
//
//   * a PAUSED clock renders "Paused for 2h — clock stopped" (counting up,
//     never down): the stored due instant is the deadline as it stood when
//     the clock stopped, and `tracking.resume` re-derives it with the pause
//     credited, so any "time remaining" shown now would be wrong the moment
//     the ticket moves;
//   * a VOID promise renders muted as "neither met nor breached", never as a
//     breach, even when `*_breached_at` is set on the row -- the server's
//     precedence already put void on top;
//   * a RUNNING clock past its due instant renders "past due; the server has
//     not recorded a breach yet", not "breached".
//
// The route is gated on `ticket.read` (the same permission the surrounding
// page needs), so there is no separate permission gate here -- and the server
// is the authority regardless of what this renders (invariant #6).
//
// DRAWN AS v3's FACTS STRIP: the two clocks as caption/value cells, each
// value a state pill and the headline beside it, then the attribution facts
// under them.

// The countdown ticks once a minute: `formatDurationMs` has minute
// granularity, so anything faster re-renders without changing a character.
const TICK_MS = 60_000;

/** Who may open Service levels, where a ticket with no SLA is fixed. */
const SLA_READ = "sla.read";

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}

// §0.3's attribution requirement: these three are why a breach happened, and
// they read very differently in a review.
//
// THE ANSWER IS THE HEADLINE, NOT THE ENUM. This fact is keyed "Policy applied
// because" and its value was `policy_source` verbatim -- the word "initial",
// with the sentence that actually answers the question demoted to a grey
// sub-line under it, itself in the wire's lower case. The sentence is the
// value now; the extra consequence, where there is one, is the sub-line.
//
// Keys, not sentences: the words are looked up when the panel renders, so a
// language switch re-words them.
const POLICY_SOURCE_HEADLINES: Record<string, MessageKey> = {
  initial: "sla.ticket.sourceInitial",
  retriage: "sla.ticket.sourceRetriage",
  escalation: "sla.ticket.sourceEscalation",
};

const POLICY_SOURCE_NOTES: Record<string, MessageKey> = {
  retriage: "sla.ticket.noteRetriage",
  escalation: "sla.ticket.noteEscalation",
};

export function TicketSlaPanel({ ticketId }: { ticketId: string }) {
  const t = useT();
  const sla = useTicketSla(ticketId);
  const me = useMePermissions();
  // Every instant below is rendered in the READER's zone, not the browser's
  // and not the calendar's (cycle 5). A deadline is one moment; two people
  // reading this ticket from two machines must not see two different times
  // for it, which is what `new Date(x).toLocaleString()` produced.
  const zone = useUserTimezone();
  const data = sla.data;

  const anyRunning =
    data != null && (data.response_state === "running" || data.resolution_state === "running");
  const anyPaused =
    data != null && (data.response_state === "paused" || data.resolution_state === "paused");
  // A paused clock's "paused for" reading counts up in real time, so it ticks
  // too -- what it must never do is count DOWN.
  const now = useNow(anyRunning || anyPaused);

  const forbidden = sla.error instanceof ApiError && sla.error.status === 403;

  return (
    <Panel title={t("sla.ticket.title")}>
      {forbidden && (
        <p className="note">{t("sla.ticket.forbidden")}</p>
      )}
      {/* `role="alert"`, because a technician who cannot see this panel's state
          is prioritising blind and nothing else on the page says so. The
          sentence comes from the shared `describeSubmitError`: the old
          `describeError(sla.error)` was the error's `.message`, so a bodyless
          503 rendered an EMPTY paragraph and the failure was invisible. */}
      {sla.isError && !forbidden && (
        <p role="alert" className="notice red">
          {describeSubmitError(sla.error)}
        </p>
      )}
      {data && !data.has_sla && (
        <div>
          <Badge variant={targetStateVariant("none")}>{targetStateLabel("none")}</Badge>
          <p className="note" style={{ marginTop: 8 }}>
            {t("sla.ticket.noSlaNote")}
          </p>
          {/* WHERE IT IS FIXED (cross-module-guidance §2 Tickets #1). Offered
              as a link only to an account that can open Service levels;
              anyone else is told who can. The server gates the page either
              way -- this decides only what is worth rendering. */}
          {me.has(SLA_READ) ? (
            <p className="note">
              <Link className="btn small" to="/sla">
                {t("sla.ticket.setTargets")}
              </Link>
            </p>
          ) : (
            <p className="note">{t("sla.ticket.askAdmin")}</p>
          )}
        </div>
      )}
      {data && data.has_sla && (
        <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr)", gap: 12 }}>
          <Facts
            items={[
              { k: t("sla.common.response"), v: <TargetValue target="response" sla={data} now={now} zone={zone} /> },
              { k: t("sla.common.resolution"), v: <TargetValue target="resolution" sla={data} now={now} zone={zone} /> },
            ]}
          />
          <SlaFootnotes sla={data} zone={zone} />
        </div>
      )}
    </Panel>
  );
}

function TargetValue({
  target,
  sla,
  now,
  zone,
}: {
  target: "response" | "resolution";
  sla: TicketSlaOut;
  now: number;
  zone: string | null;
}) {
  useT();
  const state: TargetState =
    target === "response" ? sla.response_state : sla.resolution_state;
  const timing = describeTargetTiming(
    {
      state,
      dueAt: target === "response" ? sla.response_due_at : sla.resolution_due_at,
      metAt: target === "response" ? sla.response_met_at : sla.resolution_met_at,
      breachedAt:
        target === "response" ? sla.response_breached_at : sla.resolution_breached_at,
      pausedAt: sla.paused_at,
      pausedMinutesTotal: sla.paused_minutes_total,
      voidedAt: sla.voided_at,
    },
    now,
    zone,
  );

  return (
    <span className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr)", gap: 4 }}>
      <span className="flex flex-wrap items-center gap-2">
        <Badge variant={targetStateVariant(state)}>{targetStateLabel(state)}</Badge>
        <b>{timing.headline}</b>
      </span>
      {timing.detail && <span className="note">{timing.detail}</span>}
    </span>
  );
}

// The attribution half of §0.3: which policy applies and WHY, plus the
// once-only escalation stamp. Both are facts recorded on the row, not
// verdicts.
function SlaFootnotes({ sla, zone }: { sla: TicketSlaOut; zone: string | null }) {
  const t = useT();
  const source = sla.policy_source;
  // An unrecognised source still says something rather than nothing: the raw
  // value, humanised, is better than "—" when the server grows a fourth one.
  const headlineKey = source ? POLICY_SOURCE_HEADLINES[source] : undefined;
  const headline = source ? (headlineKey ? translate(headlineKey) : humaniseStatus(source)) : "—";
  const noteKey = source ? POLICY_SOURCE_NOTES[source] : undefined;
  const sourceNote = noteKey ? translate(noteKey) : undefined;
  return (
    // Four cells fill the two-across strip.
    <Facts
      items={[
        { k: t("sla.ticket.factSource"), v: (
          <>
            {headline}
            {sourceNote && <span className="note" style={{ display: "block" }}>{sourceNote}</span>}
          </>
        ) },
        // "No" is a real answer (this ticket has not escalated) and is
        // deliberately NOT `NO_DATE`: an em dash here would read as "we do
        // not know", which is a different and worse claim.
        { k: t("sla.ticket.factEscalated"), v: sla.escalated_at ? formatDateTime(sla.escalated_at, zone) : t("sla.ticket.no") },
        { k: t("sla.ticket.factPolicy"), v: sla.policy_name ?? "—" },
        { k: t("sla.ticket.factHours"), v: sla.calendar_name ?? "—" },
      ]}
    />
  );
}
