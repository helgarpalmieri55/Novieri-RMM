// Display-name maps for wire values this module renders on screen.
//
// docs/design/settings-language.md, "Notifications": the wire values
// (`event_type`, `recipient_kind`) are a contract with the server
// (`app/core/events.py`'s registry; `RecipientKind` mirrors the schema) and
// do not change. Only how they READ changes -- so every wire value gets
// exactly one display string, defined once, here.
//
// EXHAUSTIVENESS IS A COMPILE ERROR, NOT A RUNTIME CHECK. `EVENT_TYPE_LABELS`
// is typed `Record<EventType, string>`, and `EventType` is derived from
// `EVENT_TYPES` (`(typeof EVENT_TYPES)[number]`) rather than hand-declared --
// so a new entry added to `EVENT_TYPES` without a matching entry here makes
// this file fail to compile (TS2741, "missing the following properties").
// `RecipientKind` is already a hand-written literal union in `types.ts`, so
// `Record<RecipientKind, string>` gets the same guarantee for free. Neither
// map can be loosened to `Record<string, string>` without losing this.
//
// The lookup functions below still take a plain `string` (matching the wire
// types actually returned by the server -- `RuleOut.event_type: string`) and
// fall back to the humanised value for one a client does not yet recognise (an
// older client against a newer server's registry). That fallback is a
// runtime concern for data this client did not itself enumerate; it does not
// weaken the exhaustiveness guarantee above, which is about `EVENT_TYPES`
// (this client's own registry mirror) never outrunning its own label map.

import { lazyLabels } from "../../i18n/lazy";
import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import { EVENT_TYPES } from "./types";
import type { EventType, RecipientKind } from "./types";

// THE WORDS ARE THE CATALOGUE'S, READ WHEN A LABEL IS READ. These two records
// keep the shape the rest of the console (and its tests) index, but each label
// is an accessor over `enums.EventType` / `enums.RecipientKind`, so a language
// switch shows on the next render. The wire event type has `.` in it and `.`
// separates catalogue paths, so its key writes `_` instead.
export const EVENT_TYPE_LABELS: Record<EventType, string> = lazyLabels<EventType>({
  "ticket.assigned": "enums.EventType.ticket_assigned",
  "ticket.status_changed": "enums.EventType.ticket_status_changed",
  "ticket.comment_added": "enums.EventType.ticket_comment_added",
  "ticket.internal_note_added": "enums.EventType.ticket_internal_note_added",
  "ticket.approval_requested": "enums.EventType.ticket_approval_requested",
  "ticket.approval_decided": "enums.EventType.ticket_approval_decided",
  "ticket.resolved": "enums.EventType.ticket_resolved",
  "platform.probe_unhealthy": "enums.EventType.platform_probe_unhealthy",
  "platform.probe_recovered": "enums.EventType.platform_probe_recovered",
  "subscription.notice_deadline_approaching": "enums.EventType.subscription_notice_deadline_approaching",
  "subscription.renewal_approaching": "enums.EventType.subscription_renewal_approaching",
  "device.joined_via_link": "enums.EventType.device_joined_via_link",
  "device.removal_incomplete": "enums.EventType.device_removal_incomplete",
  "push.run.completed": "enums.EventType.push_run_completed",
  "push.run.stopped": "enums.EventType.push_run_stopped",
});

/**
 * `rule.event_type` (or an event-type select's own value) -> what the operator reads.
 * An event this client has not met is humanised, never printed raw.
 */
export function eventTypeLabel(eventType: string): string {
  return Object.hasOwn(EVENT_TYPE_LABELS, eventType)
    ? EVENT_TYPE_LABELS[eventType as EventType]
    : humaniseStatus(eventType);
}

export const RECIPIENT_KIND_LABELS: Record<RecipientKind, string> = lazyLabels<RecipientKind>({
  queue_members: "enums.RecipientKind.queue_members",
  assignee: "enums.RecipientKind.assignee",
  requester: "enums.RecipientKind.requester",
  channel_default: "enums.RecipientKind.channel_default",
});

/** `rule.recipient_kind` (or a recipients select's own value) -> what the operator reads. */
export function recipientKindLabel(kind: RecipientKind): string {
  return humaniseEnum(kind, "RecipientKind");
}

// Re-exported so call sites that only need "every legal event type, in
// order, with its label" (the event-type `<select>`) don't also need to
// import `EVENT_TYPES` from `./types` directly.
export { EVENT_TYPES };
