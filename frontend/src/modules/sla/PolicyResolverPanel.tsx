import { useState } from "react";
import { describeSubmitError } from "../../components/form";
import { Badge, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { Panel } from "../../shell/components";
import { useT } from "../../i18n/context";
import { ApiError } from "../../lib/api";
import { useClientScope } from "../../lib/clientScope";
import { ticketPriorityLabel } from "../service-desk/serviceDeskState";
import { useClientName } from "../settings/api";
import { useSlaCalendars, useSlaPolicies, useSlaTicketQueues } from "./api";
import { formatMinutes, resolutionPreview } from "./slaState";
import { SLA_PRIORITIES } from "./types";
import type { SlaPriority } from "./types";

// WHICH POLICY ACTUALLY APPLIES, AND WHY.
//
// Spec §3's resolution is four levels deep and first-match-wins, with NULL as
// a wildcard in either column. That is exactly the configuration people get
// wrong: a `(any queue, urgent)` policy and a `(Helpdesk, any priority)`
// policy both "look right" for an urgent Helpdesk ticket, and only one of
// them is ever used. A list of policies cannot answer that question; this
// panel does, by walking the same four levels in the same order and showing
// which ones are configured, which one wins, and which are shadowed.
//
// It is a PREVIEW OF A CONFIGURATION, not an authority over any ticket. The
// server resolves a real ticket's policy at creation and on human re-triage
// (`policies.resolve_policy`), and a ticket's actual policy is reported on
// the ticket itself. Nothing here writes, and nothing here changes what a
// ticket got.
export function PolicyResolverPanel() {
  const t = useT();
  const policies = useSlaPolicies();
  const calendars = useSlaCalendars();
  const queues = useSlaTicketQueues();
  const queuesForbidden = queues.error instanceof ApiError && queues.error.status === 403;

  const [queueId, setQueueId] = useState("");
  const [priority, setPriority] = useState<SlaPriority>("normal");

  const forbidden = policies.error instanceof ApiError && policies.error.status === 403;
  const failed = policies.error != null && !forbidden;
  // The client to preview: the one chosen at the top, with phase 2's D2 rule
  // (a client with active policies of its own resolves among those alone).
  // With every client chosen there is no one client, so the panel previews
  // the global policies -- the ones a client without policies of its own
  // resolves among -- and says so (plan Task 11 D11.4).
  const { selected } = useClientScope();
  const clientName = useClientName();
  const preview =
    queueId === "" ? null : resolutionPreview(policies.data ?? [], queueId, priority, selected);

  function calendarName(id: string): string {
    return calendars.data?.find((calendar) => calendar.id === id)?.name ?? id;
  }

  function queueName(id: string): string {
    return queues.data?.find((queue) => queue.id === id)?.name ?? id;
  }

  return (
    <Panel
      title={t("sla.resolver.title")}
      subtitle={t("sla.resolver.subtitle")}
    >
      {policies.isLoading ? (
        <p className="note">{t("sla.common.loading")}</p>
      ) : forbidden ? (
        <p className="note">{t("sla.resolver.forbidden")}</p>
      ) : failed ? (
        <p role="alert" className="notice red">
          {t("sla.resolver.failed", { reason: describeSubmitError(policies.error) })}
        </p>
      ) : (
        <>
          <p className="note" style={{ marginBottom: 8 }}>
            {selected === null
              ? t("sla.resolver.everyClientNote")
              : t("sla.resolver.clientNote", { client: clientName(selected) })}
          </p>
          {/* TWO CONTROLS, ONE EDGE. These sat in a flex row with hardcoded
              widths (256 and 192), so in this panel's column they wrapped
              onto separate lines at two different widths and the panel's only
              two controls shared no edge -- next to a number field filling
              its column in the panel beside it, that reads as an accident.
              `.form-grid` is the kit's own two-column field row: each field
              fills its cell, so they line up at every width the panel takes.
              And a `<select>` wears `.select-field`, not `.input-field`:
              same box, but it is the class the kit's drop-arrow rule is
              written against. */}
          <div className="form-grid" style={{ marginBottom: 12 }}>
            <Field label={t("sla.common.queue")}>
              {queuesForbidden ? (
                <input
                  className={inputClass}
                  placeholder={t("sla.resolver.queueId")}
                  value={queueId}
                  onChange={(e) => setQueueId(e.target.value)}
                />
              ) : (
                <select
                  className="select-field"
                  value={queueId}
                  onChange={(e) => setQueueId(e.target.value)}
                >
                  <option value="">{t("sla.resolver.selectQueue")}</option>
                  {queues.data?.map((queue) => (
                    <option key={queue.id} value={queue.id}>
                      {queue.name}
                    </option>
                  ))}
                </select>
              )}
            </Field>
            <Field label={t("sla.common.priority")}>
              <select
                className="select-field"
                value={priority}
                onChange={(e) => setPriority(e.target.value as SlaPriority)}
              >
                {/* THE LABEL, NOT THE WIRE VALUE. This select listed
                    "low", "normal", "high", "urgent" -- the enum as the API
                    spells it -- two panels above a table that writes
                    "Urgent priority" and "Normal priority" for the same
                    four things. One page, one vocabulary; the value it
                    submits is unchanged. */}
                {SLA_PRIORITIES.map((value) => (
                  <option key={value} value={value}>
                    {ticketPriorityLabel(value)}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          {preview === null && (
            <p className="note">
              {t("sla.resolver.pickQueue")}
            </p>
          )}

          {preview !== null && (
            <>
              {preview.winner === null || preview.winner.policy === null ? (
                <p className="notice gold" style={{ marginBottom: 12 }}>
                  {t("sla.resolver.noMatch", { queue: queueName(queueId), priority: ticketPriorityLabel(priority) })}
                </p>
              ) : (
                <p style={{ marginBottom: 12 }}>
                  {t("sla.resolver.resolved", {
                    level: preview.winner.level,
                    label: preview.winner.label,
                    response: formatMinutes(preview.winner.policy.response_target_minutes),
                    resolution: formatMinutes(preview.winner.policy.resolution_target_minutes),
                    calendar: calendarName(preview.winner.policy.calendar_id),
                  })}
                </p>
              )}

              <Table>
                <thead>
                  <tr>
                    <Th>{t("sla.resolver.colLevel")}</Th>
                    <Th>{t("sla.resolver.colMatches")}</Th>
                    <Th>{t("sla.resolver.colPolicy")}</Th>
                    <Th>{t("sla.resolver.colOutcome")}</Th>
                  </tr>
                </thead>
                <tbody>
                  {preview.levels.map((level) => (
                    <tr key={level.level}>
                      <Td><span className="mono">{String(level.level)}</span></Td>
                      <Td>{level.label}</Td>
                      <Td>
                        {level.policy === null
                          ? "—"
                          : t("sla.resolver.policyCell", {
                              response: formatMinutes(level.policy.response_target_minutes),
                              resolution: formatMinutes(level.policy.resolution_target_minutes),
                              calendar: calendarName(level.policy.calendar_id),
                            })}
                      </Td>
                      <Td>
                        {/* Sentence case, like every other pill in the
                            console. */}
                        {level.wins ? (
                          <Badge variant="success">{t("sla.resolver.applies")}</Badge>
                        ) : level.policy === null ? (
                          <span className="cell-sub">{t("sla.resolver.noActive")}</span>
                        ) : (
                          <Badge variant="muted">{t("sla.resolver.shadowed")}</Badge>
                        )}
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </>
          )}
        </>
      )}
    </Panel>
  );
}
