import { Link } from "react-router-dom";
import { useT } from "../../i18n/context";
import type { PickerOption } from "../../components/picker";
import { Badge, Table, Td, Th } from "../../components/ui";
import { Icon } from "../../shell/icons";
import { Person, type Kind } from "../../shell/viz";
import { describeAge } from "../jobs/jobState";
import { formatDateTime } from "../../lib/datetime";
import { nameForOption } from "./pickerOptions";
import { initialsOf, personIndex } from "../../lib/people";
import {
  describeDue,
  ticketPriorityLabel,
  ticketPriorityVariant,
  ticketStatusLabel,
  ticketStatusVariant,
  ticketTypeLabel,
} from "./serviceDeskState";
import type { TicketOut } from "./types";

/**
 * One ticket row, shared by `/service-desk` and `/service-desk/my-work`: the
 * number and subject (the number stays its own link, since that is what gets
 * pasted into a channel), when the next SLA deadline falls, then state,
 * owner and age.
 */
export function TicketTable({
  tickets,
  queueName,
  userOptions,
  now,
}: {
  tickets: readonly TicketOut[];
  queueName: (id: string) => string;
  /** The directory, so the requester and assignee are names rather than ids. */
  userOptions: readonly PickerOption[];
  now: number;
}) {
  const t = useT();
  return (
    <Table>
      <thead>
        <tr>
          <Th>{t("serviceDesk.table.ticket")}</Th>
          <Th>{t("serviceDesk.table.due")}</Th>
          <Th>{t("serviceDesk.table.type")}</Th>
          <Th>{t("serviceDesk.table.status")}</Th>
          <Th>{t("serviceDesk.table.priority")}</Th>
          <Th>{t("serviceDesk.table.assignee")}</Th>
          <Th>{t("serviceDesk.table.age")}</Th>
        </tr>
      </thead>
      <tbody>
        {tickets.map((ticket) => {
          const href = `/service-desk/tickets/${ticket.id}`;
          const requester = nameForOption(userOptions, ticket.requester_id);
          const assignee = nameForOption(userOptions, ticket.assigned_to);
          return (
            <tr key={ticket.id}>
              {/* THE ONE COLUMN THAT MUST STAY READABLE, so it is the one
                  that states a measure. At 390 this table scrolls, and an
                  auto-layout column with nothing to hold it open was starved
                  to ~95px: `INC-4124 Docking station not detecting second
                  screen` came out as seven stacked one-word lines and the row
                  stood 200px tall. `clip` floors AND caps it at 280px and
                  ellipsises the overflow, as v3 does; the subject is also the
                  link to the ticket, and `title` carries it in full. */}
              <Td lead={<Icon name="ticket" size={18} />} gutter={rowKind(ticket)} clip={280}>
                <div className="cell-title">
                  <Link className="mono" to={href}>
                    {ticket.number}
                  </Link>{" "}
                  <Link to={href}>{ticket.subject}</Link>
                </div>
                <div className="cell-sub">
                  {requester ? `${requester} · ` : ""}
                  {queueName(ticket.queue_id)}
                </div>
              </Td>
              <Td>
                <DueCell ticket={ticket} now={now} />
              </Td>
              <Td>{ticketTypeLabel(ticket.type)}</Td>
              <Td>
                <Badge variant={ticketStatusVariant(ticket.status)}>
                  {ticketStatusLabel(ticket.status)}
                </Badge>
              </Td>
              <Td>
                <Badge variant={ticketPriorityVariant(ticket.priority)}>
                  {ticketPriorityLabel(ticket.priority)}
                </Badge>
              </Td>
              <Td>
                {ticket.assigned_to && assignee ? (
                  <Person
                    initials={initialsOf(assignee)}
                    index={personIndex(ticket.assigned_to)}
                    name={assignee}
                  />
                ) : (
                  <span className="cell-sub">{t("serviceDesk.table.unassigned")}</span>
                )}
              </Td>
              <Td>
                <span className="mono">{describeAge(ticket.created_at, now)}</span>
              </Td>
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}

/**
 * The row's severity edge. Only the two priorities a technician picks work
 * from earn one -- the design draws the edge on the rows that need a person,
 * and an edge on every row is no edge at all.
 */
export function rowKind(ticket: Pick<TicketOut, "priority">): Kind | undefined {
  if (ticket.priority === "urgent") return "crit";
  if (ticket.priority === "high") return "warn";
  return undefined;
}

/** The next SLA deadline, relative to now, and which clock it belongs to. */
function DueCell({ ticket, now }: { ticket: TicketOut; now: number }) {
  useT(); // re-render on a language switch: the label is worded by describeDue()
  const due = describeDue(ticket.sla, now);
  const exact = ticket.sla?.next_due_at ? formatDateTime(ticket.sla.next_due_at) : undefined;
  if (!ticket.sla) return <span className="cell-sub">{due.label}</span>;
  return (
    <span title={exact}>
      <Badge variant={due.variant}>{due.label}</Badge>
      {due.detail && <div className="cell-sub">{due.detail}</div>}
    </span>
  );
}
