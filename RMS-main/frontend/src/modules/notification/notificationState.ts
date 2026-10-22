import { translate, type MessageKey } from "../../i18n/core";
import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import type { BadgeVariant } from "../jobs/jobState";
import type { DeliveryStatus } from "./types";

// Delivery status badge coloring, mirroring serviceDeskState.ts's
// ticketStatusVariant precedent: "pending"/"failed" are in-flight or
// retrying (not yet a failure the operator must act on individually),
// "sent" is the successful conclusion, and "dead" is the one outcome that
// reads as a hard failure requiring attention (spec §7: dead letters are
// visible, not a plain terminal state).
const DELIVERY_STATUS_VARIANTS: Record<DeliveryStatus, BadgeVariant> = {
  pending: "muted",
  failed: "warning",
  sent: "success",
  dead: "destructive",
};

export function deliveryStatusVariant(status: DeliveryStatus): BadgeVariant {
  return DELIVERY_STATUS_VARIANTS[status] ?? "muted";
}

export function channelKindLabel(kind: string): string {
  return humaniseEnum(kind === "webhook" ? "webhook" : "email", "ChannelKind");
}

// Auditor A (LOW): "Channel kind is lowercase in the dropdown and capitalised
// in the table ... The delivery-status filter likewise uses raw lowercase
// pending / sent / failed / dead." The same value rendered two ways on one
// screen is the finding; this is the single render site the audit asked for,
// and every panel goes through it. The words are `enums.DeliveryStatus`.
const DELIVERY_STATUSES: ReadonlySet<string> = new Set<DeliveryStatus>(["pending", "sent", "failed", "dead"]);

export function deliveryStatusLabel(status: string): string {
  return DELIVERY_STATUSES.has(status) ? humaniseEnum(status, "DeliveryStatus") : humaniseStatus(status);
}

// Event-type and recipient-kind display names used to live here too, as
// this module's OWN maps -- worded differently from notification/labels.ts's
// (the map docs/design/settings-language.md, "Notifications" specifies, and
// that Settings -> Notifications already rendered through). Two maps for the
// same wire values guaranteed the two screens would read differently, and
// did: `ticket.comment_added` was "Public comment added" here and "Public
// reply added" there. labels.ts (`eventTypeLabel`/`recipientKindLabel`) is
// now the SOLE source -- callers (RulesPanel.tsx) import it directly rather
// than through this module. Do not reintroduce either map here: see
// notification/__tests__/notification.test.tsx's guard against exactly that
// regression ("no longer exports an event-type or recipient-kind label map
// from notificationState.ts").

// --------------------------------------------------------------------------
// Why a delivery did not happen
// --------------------------------------------------------------------------
//
// Auditor A (HIGH), "Delivery failure is reported as a raw enum code -- a
// fourth, distinct error-presentation style": clicking Test rendered
// `Not delivered: scheme_not_allowed`. "The message is a bare snake_case
// backend enum with no explanation of what a scheme is, why it was not
// allowed, or what to do ... The specific message is also unhelpful: the
// correct fix ('URLs must start with https://') is knowable and unstated."
//
// Every string below is one of `delivery.py`'s own reasons -- the
// `_KNOWN_EGRESS_TERMINAL_REASONS` set, `_RETRYABLE_REASONS`, the SMTP
// vocabulary and `channel_disabled` -- transcribed from that module, not
// invented here. Each sentence says WHAT was refused and WHAT WOULD FIX IT,
// because the remedy for every one of them is knowable server-side and was
// simply not being said.
//
// An unknown code falls back to its humanised self rather than to "an error
// occurred": a reason this map has not been taught is still the only handle an
// operator has on the failure, and swallowing it would be a worse defect than
// showing it. It is never printed raw.
const DELIVERY_ERROR_KEYS: Readonly<Record<string, MessageKey>> = {
  scheme_not_allowed: "notification.deliveryError.scheme_not_allowed",
  port_not_allowed: "notification.deliveryError.port_not_allowed",
  blocked_private: "notification.deliveryError.blocked_private",
  blocked_platform_host: "notification.deliveryError.blocked_platform_host",
  dns_failure: "notification.deliveryError.dns_failure",
  redirect_refused: "notification.deliveryError.redirect_refused",
  invalid_url: "notification.deliveryError.invalid_url",
  timeout: "notification.deliveryError.timeout",
  connection_reset: "notification.deliveryError.connection_reset",
  smtp_transient_reject: "notification.deliveryError.smtp_transient_reject",
  smtp_permanent_reject: "notification.deliveryError.smtp_permanent_reject",
  smtp_error: "notification.deliveryError.smtp_error",
  channel_disabled: "notification.deliveryError.channel_disabled",
  sign_in_refused: "notification.deliveryError.sign_in_refused",
  sign_in_unavailable: "notification.deliveryError.sign_in_unavailable",
  sign_in_missing: "notification.deliveryError.sign_in_missing",
};

/**
 * A sentence a person can act on, from one of `delivery.py`'s reason codes.
 *
 * Returns the CLAUSE, not a whole sentence, so the call site supplies the
 * subject it already has ("Not delivered: ...", "Last attempt failed: ...")
 * and the two do not disagree about what was being attempted.
 */
export function deliveryErrorMessage(code: string | null | undefined): string {
  if (!code) return translate("notification.deliveryError.notRecorded");
  return Object.hasOwn(DELIVERY_ERROR_KEYS, code) ? translate(DELIVERY_ERROR_KEYS[code] as MessageKey) : humaniseStatus(code);
}

/**
 * The same translation, for `NotificationDelivery.last_error`.
 *
 * That column holds an EXCERPT rather than a bare code -- `delivery.py`
 * writes `"egress blocked: <reason>"` for every guard refusal, and short
 * constructed strings ("request timed out", "smtp 550 rejected") otherwise.
 * The `egress blocked:` prefix is the one that carries a machine code into a
 * column an operator reads, so it is unwrapped and translated; anything else
 * is already a sentence fragment and passes through untouched rather than
 * being replaced by a guess.
 */
export function deliveryLastErrorMessage(excerpt: string): string {
  const prefix = "egress blocked: ";
  if (excerpt.startsWith(prefix)) {
    const reason = excerpt.slice(prefix.length).trim();
    return Object.hasOwn(DELIVERY_ERROR_KEYS, reason)
      ? translate("notification.deliveryError.blockedBeforeSending", {
          reason: translate(DELIVERY_ERROR_KEYS[reason] as MessageKey),
        })
      : excerpt;
  }
  return excerpt;
}
