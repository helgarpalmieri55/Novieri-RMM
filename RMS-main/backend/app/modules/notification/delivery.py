"""One delivery attempt, and the retry classification around it
(docs/modules/notification/spec.md §5, §7).

Retryable-vs-terminal is not cosmetic, it is security-relevant (invariant on
this module). Retrying an egress rejection five times is five more attempts
to reach a blocked destination -- not five more chances to succeed, since the
allowlist decision does not change between attempts. A 403 will never become
a 200 either. Only conditions that can plausibly change on their own retry
(a transient 5xx, a timeout, a dropped connection, an SMTP 4xx) are retried;
everything else goes straight to `dead`.

THE TWO TRANSPORTS, AND WHY THEY ARE TREATED DIFFERENTLY (§5, §7).

- Webhook delivery goes through `app.core.egress.post_json` -- the ONLY
  outbound HTTP path in this codebase (see that module's docstring for why:
  two rounds of real SSRF bypasses were found and fixed there). This module
  never opens a second HTTP client and never re-implements any part of that
  guard.
- SMTP delivery is DELIBERATELY not egress-guarded -- a recorded decision
  (see `app/core/egress.py`'s own docstring: "Do NOT route SMTP through this
  guard"), not an oversight. An on-prem mail relay on the customer's own LAN
  is the normal deployment for this product, and routing SMTP through a
  deny-private-address guard would make email unusable for exactly the
  customers it targets. `aiosmtplib` is not a dependency of this project;
  stdlib `smtplib` is used via `asyncio.to_thread` so a blocking SMTP
  conversation never stalls the event loop that also serves the agent
  gateway's WebSocket connections.

WHY THE TWO TRANSPORTS' "STATUS CODES" ARE KEPT OUT OF EACH OTHER'S WAY.
`is_retryable`'s numeric branch (`_RETRYABLE_STATUS`) is an HTTP-status
table: 429/500/502/503/504 retry, every other 4xx is terminal. SMTP reply
codes are superficially similar three-digit numbers but mean something
different (RFC 5321: 4yz is transient, 5yz is permanent) -- an SMTP 500
("command not recognised") is a PERMANENT failure, the opposite of what an
HTTP 500 means. If SMTP reply codes were fed through the same numeric
`status_code` branch, a 500 SMTP reply would be misclassified as retryable
by collision with the HTTP table. To keep the two tables from ever mixing,
every SMTP outcome below is classified through `error_reason`
("smtp_permanent_reject" / "smtp_transient_reject" / ...) and NEVER through
`status_code` -- `is_retryable` consults `error_reason` first and only falls
back to the numeric HTTP table when `error_reason` is `None`, which no SMTP
call site here ever passes. `last_status_code` still records the raw SMTP
reply code for diagnosis (spec §2.5 says only "int, NULL", nothing about
which protocol it came from) -- it is just never handed back into
`is_retryable` as the classifying value for an SMTP outcome.

CREDENTIALS (invariant #3, spec §5). Checked out from the vault at point of
use, held only for the duration of one attempt, and never assigned to
anything that outlives the attempt: not `NotificationDelivery.last_error`,
not an audit `metadata` dict, not a log line (this module calls no logger),
not a variable that survives past building the one header/login call that
needs it. Every checkout is followed immediately by an audit record (spec
§5's "every checkout writes an audit event") -- `VaultService.checkout`
itself does not audit (see `app/modules/remote/service.py`'s identical
comment); the caller must, and this is that caller.

- Webhook credential: the vault plaintext IS the bearer token, used verbatim
  as `Authorization: Bearer <token>`. Spec §2.2: "Secret headers (a bearer
  token) go to the vault via credential_id, never into config."
- Email credential: spec §2.2 says `credential_id` holds "the SMTP
  username/password" -- two values behind one vault secret slot. Stored as a
  single JSON-encoded string, `{"username": ..., "password": ...}`; this
  module both defines and consumes that encoding (nothing else in the vault
  API special-cases it -- `VaultService` stores/returns one opaque string per
  credential, as it does for every other credential kind).
- Microsoft 365 email (`config.auth == "microsoft"`): the slot holds
  `{"tenant_id", "client_id", "client_secret"}`. Each send exchanges them for
  an app-only token (`email_intake.tokens.app_access_token`) and signs in to
  the pinned `smtp.office365.com:587` with XOAUTH2. The token is held for one
  SMTP conversation and never stored.

RECIPIENT VS. THE REAL DESTINATION. `delivery.recipient` (spec §2.5) is a
diagnostic value -- an email address, or a webhook HOST -- deliberately never
a full URL with a query string that could carry a token. For a webhook, the
actual destination dialled is `channel.config["url"]` (the channel's own
configured target; §2.3's `channel_default` is the normal shape for a
webhook, one destination for the whole channel), never reconstructed from
`recipient`. For email, `recipient` genuinely IS the address the message is
sent to (the `To:` header) -- there is no separate per-recipient config for
email, so recipient and destination coincide there.

CLAIM LIFECYCLE IS NOT THIS MODULE'S JOB. Spec §7's "claim before send" (a
`SELECT ... FOR UPDATE SKIP LOCKED` claim, and the stuck-claim sweep) belongs
to the worker (Task 10, `app/worker.py`'s `deliver_pending`). This module
performs exactly one attempt against a `NotificationDelivery` the caller has
already claimed and hands back one classified outcome; it does not touch
`claimed_at`.
"""

import asyncio
import json
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any, Literal
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.api.probes import probe_label
from app.core.audit import audit_service
from app.core.egress import EgressBlocked, post_json
from app.core.i18n import N_, tr, tr_marked, use_locale
from app.core.ticket_words import TICKET_STATUS_WORDS
from app.core.vault import VaultService
from app.models import DomainEvent, NotificationChannel, NotificationDelivery, User
from app.modules.email_intake import microsoft
from app.modules.email_intake.identity import reply_to_for_ticket
from app.modules.email_intake.tokens import (
    TokenRevoked,
    TokenTransportError,
    app_access_token,
)
from app.modules.notification.rules import allowlisted_hosts

# Spec §7: five backoff gaps -- 1m, 5m, 25m, 2h, 10h -- between six attempts,
# then dead.
#
# MAX_ATTEMPTS IS DERIVED, NEVER A SEPARATE LITERAL (whole-branch review
# finding I4). It used to be `len(BACKOFF_SECONDS)` (5), which meant five
# attempts had only FOUR gaps between them -- the fifth attempt went straight
# to `dead` without ever consulting, or scheduling off, `BACKOFF_SECONDS[4]`'s
# 10-hour delay, so the real retry window was 2.52h (1m+5m+25m+2h), not the
# 12.52h all five delays sum to. `len(BACKOFF_SECONDS) + 1` makes N delays
# produce N gaps between N+1 attempts -- the number this schedule was
# obviously designed to reach (the long 10h tail exists to ride out a
# destination outage lasting most of a day) -- and, being derived rather than
# a second hand-picked number, this relationship cannot silently drift out of
# sync with `BACKOFF_SECONDS` again the way it did before this fix.
BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 1500, 7200, 36000)
MAX_ATTEMPTS: int = len(BACKOFF_SECONDS) + 1

_EXCERPT_MAX = 500  # matches notification_deliveries.last_error's String(500)
_SMTP_TIMEOUT_S = 30.0

# HTTP status codes that retry (spec §7): a transient 5xx, or 429 (rate
# limited -- the caller should back off and try again, not stop).
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Every `EgressBlocked.reason` `app/core/egress.py::post_json` can currently
# raise -- kept here as an explicit, named list (not because `is_retryable`
# needs it to decide -- see below -- but so a reader, and this module's own
# drift test, can see the full set this module was built against without
# having to re-derive it from egress.py by hand):
#   blocked_private        -- resolves into denied (private/loopback/etc) space
#   blocked_platform_host  -- the platform's own gateway/vault/db, by name
#   dns_failure            -- did not resolve, or resolution errored
#   redirect_refused       -- destination returned a 3xx
#   scheme_not_allowed     -- not http/https, or plain http without an allowlist
#   port_not_allowed       -- host allowlisted, but not for this port (or a
#                              non-default port with no allowlist entry at all)
#   invalid_url            -- e.g. a port outside 0-65535
# Every single one of these is a decision the allowlist/DNS/scheme made about
# the DESTINATION, not a fact about the current network weather -- retrying
# asks the same question and gets the same answer, and for `blocked_private`/
# `blocked_platform_host` specifically, retrying is also five more attempts to
# reach a blocked target. `is_retryable` does not special-case this list by
# name (see its own docstring for why) -- it is retryable-by-exception,
# terminal-by-default, so ANY reason string, including one added to
# `egress.py` after this module was written, is terminal unless explicitly
# added to `_RETRYABLE_REASONS` below.
_KNOWN_EGRESS_TERMINAL_REASONS = frozenset(
    {
        "blocked_private",
        "blocked_platform_host",
        "dns_failure",
        "redirect_refused",
        "scheme_not_allowed",
        "port_not_allowed",
        "invalid_url",
    }
)

# The only `error_reason` strings `is_retryable` treats as retryable. Every
# reason not in this set is terminal -- see `is_retryable`'s docstring.
_RETRYABLE_REASONS = frozenset(
    {"timeout", "connection_reset", "smtp_transient_reject", "sign_in_unavailable"}
)


def is_retryable(status_code: int | None, error_reason: str | None) -> bool:
    """Classify one attempt's outcome as retryable or terminal (spec §7).

    Retryable-by-EXCEPTION, terminal-by-DEFAULT: an `error_reason` is
    retryable only if it is literally one of `_RETRYABLE_REASONS`; any other
    named reason -- every `EgressBlocked` reason `egress.py` raises today,
    every one it might raise tomorrow, and every SMTP terminal reason this
    module names -- is terminal. This is the fail-safe direction for a
    security-relevant classification: a newly added `EgressBlocked` reason
    that nobody taught this function about must default to "stop retrying",
    never to "keep retrying a destination we don't recognise the rejection
    for".

    `status_code` is consulted ONLY when `error_reason` is `None` -- see the
    module docstring's "WHY THE TWO TRANSPORTS' STATUS CODES ARE KEPT OUT OF
    EACH OTHER'S WAY" for why an SMTP reply code must never reach this
    branch: this module never calls `is_retryable` with an SMTP reply code as
    `status_code` and `error_reason=None` together, precisely to avoid an
    SMTP 500 (permanent) colliding with HTTP 500 (transient) in
    `_RETRYABLE_STATUS`.
    """
    if error_reason is not None:
        return error_reason in _RETRYABLE_REASONS
    if status_code is None:
        return False
    return status_code in _RETRYABLE_STATUS


def next_attempt_delay(attempts: int) -> int | None:
    """The delay before the NEXT attempt, given `attempts` already made
    BEFORE the one that just happened -- i.e. `attempts=0` is "this was the
    first attempt", `attempts=1` is "this was the second", and so on.

    Bounded against `len(BACKOFF_SECONDS)`, NOT `MAX_ATTEMPTS` (I4 fix):
    there are only `len(BACKOFF_SECONDS)` real gaps to hand out (indices
    `0..len(BACKOFF_SECONDS) - 1`), one fewer than `MAX_ATTEMPTS` -- the whole
    point of deriving `MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1` is that N
    delays produce N gaps between N+1 attempts. `attempts ==
    len(BACKOFF_SECONDS)` (i.e. every gap already handed out, the delivery is
    on its LAST allowed attempt) correctly returns `None` here -- direct proof
    the function itself is bounded, exercised by this module's own unit
    tests. In practice `_apply_outcome` never calls this with a value that
    reaches that branch: the cap is enforced one step earlier, against the
    delivery's POST-increment attempt count reaching `MAX_ATTEMPTS`, so the
    LAST allowed attempt goes straight to `dead` rather than scheduling a
    (nonexistent) attempt beyond it."""
    if attempts >= len(BACKOFF_SECONDS):
        return None
    return BACKOFF_SECONDS[attempts]


def _sanitize_excerpt(raw: str) -> str:
    """Bounded, control-character-stripped -- the same treatment
    `app/core/egress.py::post_json` gives a webhook response body (spec
    §4.4), applied here to the handful of strings this module itself builds
    for `last_error` (an egress-guard excerpt is already sanitized by
    `post_json` before it ever reaches this module; this exists for the
    SMTP-side messages this module constructs directly)."""
    return "".join(ch for ch in raw if ch.isprintable())[:_EXCERPT_MAX]


@dataclass(frozen=True)
class _AttemptOutcome:
    """The one attempt's result, already reduced to exactly what
    `_apply_outcome` needs -- never a raw exception, a raw response body, or
    a credential."""

    ok: bool
    status_code: int | None
    error_reason: str | None
    error_excerpt: str | None


# Wire values the renderers say inside a sentence. Each table is `N_`-marked and
# translated with `tr_marked` by `_word`: the recipient's language when the table
# knows the value, otherwise the value as it arrived (an unknown status, a check
# type or a drift class stays the identifier the operator can search for).
_TICKET_STATUS_WORDS = TICKET_STATUS_WORDS
_SLA_TARGET_WORDS = {
    "response": N_("response"),
    "resolution": N_("resolution"),
}
_SEVERITY_WORDS = {
    "info": N_("info"),
    "warning": N_("warning"),
    "critical": N_("critical"),
}
# Identifiers an operator searches for (a check type, a comparator, a drift class,
# a confidence, a cleared reason). In English they print as they arrived. In
# Spanish `_coded` puts the console's own word first and keeps the raw identifier
# beside it, so the mail names the thing the way the console does and the code is
# still there to search for. Words mirror frontend/src/i18n/es (enums.HealthCheckType,
# health.comparator, health.cleared, ipam.driftClass, ipam.confidence).
_CHECK_TYPE_WORDS = {
    "cpu_busy_pct": N_("cpu_busy_pct"),
    "memory_used_pct": N_("memory_used_pct"),
    "disk_free_pct": N_("disk_free_pct"),
    "disk_free_bytes": N_("disk_free_bytes"),
    "disk_smart_ok": N_("disk_smart_ok"),
    "process_present": N_("process_present"),
    "service_state": N_("service_state"),
    "posture_disk_encryption": N_("posture_disk_encryption"),
    "posture_firewall_enabled": N_("posture_firewall_enabled"),
    "eventlog_disk_error": N_("eventlog_disk_error"),
    "eventlog_service_crash": N_("eventlog_service_crash"),
    "eventlog_kernel_fault": N_("eventlog_kernel_fault"),
    "script_policy_failed": N_("script_policy_failed"),
    "agent_offline": N_("agent_offline"),
    "backup_success_age_h": N_("backup_success_age_h"),
    "backup_last_run_failed": N_("backup_last_run_failed"),
    "backup_offsite_lag_h": N_("backup_offsite_lag_h"),
    "backup_restore_test_age_d": N_("backup_restore_test_age_d"),
    "posture_antivirus_healthy": N_("posture_antivirus_healthy"),
    "service_running": N_("service_running"),
    "cert_days_left": N_("cert_days_left"),
}
_COMPARATOR_WORDS = {
    "gt": N_("gt"),
    "gte": N_("gte"),
    "lt": N_("lt"),
    "lte": N_("lte"),
    "eq": N_("eq"),
    "neq": N_("neq"),
    "is": N_("is"),
}
_DRIFT_CLASS_WORDS = {
    "documented_absent": N_("documented_absent"),
    "present_undocumented": N_("present_undocumented"),
    "mac_mismatch": N_("mac_mismatch"),
    "claimant_mismatch": N_("claimant_mismatch"),
    "device_moved": N_("device_moved"),
    "conflict": N_("conflict"),
    "dormant": N_("dormant"),
}
_CONFIDENCE_WORDS = {
    "low": N_("low"),
    "medium": N_("medium"),
    "high": N_("high"),
}
_CLEARED_REASON_WORDS = {
    "recovery": N_("recovery"),
    "summary_ok": N_("summary_ok"),
    "unavailable_timeout": N_("unavailable_timeout"),
    "policy_removed": N_("policy_removed"),
    "device_inactive": N_("device_inactive"),
    "manual": N_("manual"),
}
_PROBE_STATUS_WORDS = {
    "unhealthy": N_("unhealthy"),
    "degraded": N_("degraded"),
    "down": N_("down"),
}


def _word(table: dict[str, str], value: object) -> str:
    """`value` said in the current language when `table` knows it, else as it is."""
    text = str(value)
    return tr_marked(table[text]) if text in table else text


def _coded(table: dict[str, str], value: object) -> str:
    """`value` as the console words it with the raw identifier kept beside it
    (`Disco libre (disk_free_pct)`). In English, and for a value the table does not
    know, just the value as it arrived."""
    text = str(value)
    said = _word(table, value)
    return said if said == text else f"{said} ({text})"


def _field(payload: dict[str, Any], key: str, fallback: str) -> Any:
    """`payload[key]`, or `fallback` only when the key is absent (a present
    `None` is printed, exactly as `payload.get(key, fallback)` always did)."""
    return payload[key] if key in payload else fallback


def _ticket_line(payload: dict[str, Any]) -> str:
    """`#<ticket_number> — <subject>` for the human-readable email renderers
    below, falling back gracefully when either key is absent -- a
    `ticket.comment_added`/`ticket.internal_note_added` payload carries
    `ticket_number` but never `subject` (see `service_desk/threads.py`'s
    publisher), and this must still render sensibly rather than KeyError."""
    ticket_number = payload.get("ticket_number")
    subject = payload.get("subject")
    if ticket_number and subject:
        return f"#{ticket_number} — {subject}"
    if ticket_number:
        return f"#{ticket_number}"
    return tr("(ticket)")


def _render_ticket_assigned(event: DomainEvent) -> tuple[str, str]:
    line = _ticket_line(event.payload)
    return (
        tr("[Toeshee] Ticket {line} assigned", line=line),
        tr("Ticket {line} has been assigned to a technician.", line=line) + "\n",
    )


def _render_ticket_status_changed(event: DomainEvent) -> tuple[str, str]:
    payload = event.payload
    line = _ticket_line(payload)
    from_status = _word(_TICKET_STATUS_WORDS, payload.get("from_status", "?"))
    to_status = _word(_TICKET_STATUS_WORDS, payload.get("to_status", "?"))
    return (
        tr(
            "[Toeshee] Ticket {line}: status changed to {to_status}",
            line=line,
            to_status=to_status,
        ),
        tr(
            "Ticket {line} changed status from {from_status} to {to_status}.",
            line=line,
            from_status=from_status,
            to_status=to_status,
        )
        + "\n",
    )


def _render_ticket_comment_added(event: DomainEvent) -> tuple[str, str]:
    payload = event.payload
    ticket_number = payload.get("ticket_number", "?")
    comment_body = payload.get("body")
    body = tr("A new comment was added to ticket #{ticket_number}.", ticket_number=ticket_number)
    if comment_body:
        body += f"\n\n{comment_body}\n"
    else:
        body += "\n"
    return (
        tr("[Toeshee] New comment on ticket #{ticket_number}", ticket_number=ticket_number),
        body,
    )


def _render_ticket_internal_note_added(event: DomainEvent) -> tuple[str, str]:
    # Spec §6.4 mechanism 1: this event's payload never carries the note's
    # text (see `service_desk/threads.py`), so this renderer never can and
    # never does render one, regardless of who ends up receiving this email --
    # the requester is dropped from routing for this event type independently
    # (§6.4 mechanism 2), but this renderer does not rely on that at all.
    ticket_number = event.payload.get("ticket_number", "?")
    return (
        tr("[Toeshee] Internal note added to ticket #{ticket_number}", ticket_number=ticket_number),
        tr(
            "An internal note was added to ticket #{ticket_number}. "
            "Sign in to Toeshee to view it.",
            ticket_number=ticket_number,
        )
        + "\n",
    )


def _render_ticket_approval_requested(event: DomainEvent) -> tuple[str, str]:
    line = _ticket_line(event.payload)
    return (
        tr("[Toeshee] Approval requested: ticket {line}", line=line),
        tr("Ticket {line} requires your approval.", line=line) + "\n",
    )


def _render_ticket_approval_decided(event: DomainEvent) -> tuple[str, str]:
    payload = event.payload
    line = _ticket_line(payload)
    decision = payload.get("decision", "decided")
    # "approval approved" is feminine in Spanish and "ticket was approved" is
    # masculine, so each known decision has its own subject and body template.
    if decision == "approved":
        subject = tr("[Toeshee] Ticket {line}: approval approved", line=line)
        body = tr("Ticket {line} was approved.", line=line)
    elif decision == "rejected":
        subject = tr("[Toeshee] Ticket {line}: approval rejected", line=line)
        body = tr("Ticket {line} was rejected.", line=line)
    elif decision == "decided":
        subject = tr("[Toeshee] Ticket {line}: approval decided", line=line)
        body = tr("Ticket {line} was decided.", line=line)
    else:
        subject = tr("[Toeshee] Ticket {line}: approval {decision}", line=line, decision=decision)
        body = tr("Ticket {line} was {decision}.", line=line, decision=decision)
    return (subject, body + "\n")


def _render_ticket_resolved(event: DomainEvent) -> tuple[str, str]:
    line = _ticket_line(event.payload)
    return (
        tr("[Toeshee] Ticket {line} resolved", line=line),
        tr(
            "Your ticket {line} has been resolved. "
            "If you have further questions, sign in to the portal and add a comment to this "
            "ticket.",
            line=line,
        )
        + "\n",
    )


def _render_ticket_sla_breached(event: DomainEvent) -> tuple[str, str]:
    """`ticket.sla_breached` (published by `app.modules.sla`, docs/modules/
    sla/spec.md §6) -- an internal, staff-only signal, never sent to the
    requester: a missed deadline is not something the requester needs told,
    and this event's payload carries no `requester_id` at all for
    `notification.routing.resolve_recipients` to route a `requester`-kind
    rule against in the first place, the same structural guarantee
    `ticket.internal_note_added` relies on (routing.py).

    Deliberately reads only `target`/`due_at` plus the shared `ticket_number`/
    `subject` line -- never `policy_id`/`queue_id`/`priority` (nothing this
    module's copy needs) and never a description or internal-note body
    (invariant #3's "never in logs/responses" sibling: nothing not already
    safe to disclose to staff enters this mail either)."""
    payload = event.payload
    line = _ticket_line(payload)
    target = _word(_SLA_TARGET_WORDS, payload.get("target", "?"))
    due_at = _field(payload, "due_at", tr("an unknown time"))
    return (
        tr("[Toeshee] SLA breached: ticket {line}", line=line),
        tr(
            "The {target} SLA for ticket {line} was due at {due_at} and has been breached.",
            target=target,
            line=line,
            due_at=due_at,
        )
        + "\n"
        + tr("Sign in to Toeshee to review it.")
        + "\n",
    )


# ---------------------------------------------------------------------------
# Endpoint health (docs/modules/endpoint-health-alerting/spec.md §9).
#
# STAFF-FACING, ALL FOUR. Every one of these types is in
# `app.core.events.STAFF_ONLY_EVENT_TYPES` and carries a matching
# `notification_rules` CHECK, so no `requester`-kind rule can select them --
# but, exactly as `_render_ticket_internal_note_added` does, none of these
# renderers relies on that: they render only what is already safe for a
# webhook destination off-platform.
#
# WHAT THESE RENDERERS MUST NEVER READ, even if a future payload carries it
# (spec §9.1): raw event-log message text, script bodies, credentials, full
# summary blobs, or any file path beyond a mount point. `selector` is bounded
# by `health_check`'s own validation (a service name, a mount point) and is the
# only device-side string here.
# ---------------------------------------------------------------------------


def _health_subject_line(payload: dict[str, Any]) -> str:
    """`<check_type> [<selector>] on <hostname>` -- the one identifying phrase
    all four health renderers share. Every part falls back rather than
    KeyErroring: a machine-origin event that lost a key must still deliver a
    readable mail, because the alternative is a raised exception inside the
    delivery worker."""
    check_type = (
        _coded(_CHECK_TYPE_WORDS, payload["check_type"])
        if payload.get("check_type")
        else tr("health check")
    )
    selector = payload.get("selector")
    hostname = payload.get("hostname") or tr("(unknown device)")
    subject = f"{check_type} [{selector}]" if selector else str(check_type)
    return tr("{subject} on {hostname}", subject=subject, hostname=hostname)


def _health_observation(payload: dict[str, Any]) -> str:
    """The observed-vs-threshold pair. Both are strings on the wire (spec §2.4)
    and are rendered as strings here -- rendering them as numbers is the first
    step toward someone aggregating them, which §0.12 exists to prevent."""
    severity = _word(_SEVERITY_WORDS, payload.get("severity", "?"))
    return (
        tr("Observed: {value}", value=payload.get("observed_value", "?"))
        + "\n"
        + tr(
            "Threshold: {comparator} {threshold}",
            comparator=_coded(_COMPARATOR_WORDS, payload.get("comparator", "?")),
            threshold=payload.get("threshold", "?"),
        )
        + "\n"
        + tr("Severity: {severity}", severity=severity)
        + "\n"
    )


def _render_health_alert_raised(event: DomainEvent) -> tuple[str, str]:
    payload = event.payload
    line = _health_subject_line(payload)
    severity = _word(_SEVERITY_WORDS, payload.get("severity", "?"))
    flapping = (" " + tr("This check is currently flapping.")) if payload.get("flapping") else ""
    return (
        tr("[Toeshee] Health alert ({severity}): {line}", severity=severity, line=line),
        tr("A health alert opened for {line}.", line=line)
        + "\n\n"
        + _health_observation(payload)
        + tr("Opened: {opened_at}", opened_at=payload.get("opened_at", "?"))
        + "\n"
        + f"{flapping}\n"
        + tr("Sign in to Toeshee to review it.")
        + "\n",
    )


def _render_health_alert_cleared(event: DomainEvent) -> tuple[str, str]:
    payload = event.payload
    line = _health_subject_line(payload)
    return (
        tr("[Toeshee] Health alert cleared: {line}", line=line),
        tr("The health alert for {line} has cleared.", line=line)
        + "\n\n"
        + _health_observation(payload)
        + tr("Opened: {opened_at}", opened_at=payload.get("opened_at", "?"))
        + "\n"
        + tr(
            "Cleared: {cleared_at} ({reason})",
            cleared_at=payload.get("cleared_at", "?"),
            reason=(
                _coded(_CLEARED_REASON_WORDS, payload["cleared_reason"])
                if "cleared_reason" in payload
                else tr("reason not recorded")
            ),
        )
        + "\n",
    )


def _render_health_alert_escalated(event: DomainEvent) -> tuple[str, str]:
    payload = event.payload
    line = _health_subject_line(payload)
    severity = _word(_SEVERITY_WORDS, payload.get("severity", "?"))
    return (
        tr("[Toeshee] Health alert ESCALATED ({severity}): {line}", severity=severity, line=line),
        tr("The health alert for {line} is still open and has been escalated.", line=line)
        + "\n\n"
        + _health_observation(payload)
        + tr("Opened: {opened_at}", opened_at=payload.get("opened_at", "?"))
        + "\n\n"
        + tr("Nobody has acknowledged it. Sign in to Toeshee to review it.")
        + "\n",
    )


def _render_health_remediation_requires_approval(event: DomainEvent) -> tuple[str, str]:
    """`health.remediation_requires_approval` -- the autonomy dial's middle
    setting (spec §8.3). Names the approved action's KEY and, when the action
    pins a script, that script's NAME and VERSION (spec §2.10: nobody approves
    a body they cannot identify). It never renders the script body: the body is
    arbitrary code, it is `destructive` by default for exactly that reason, and
    a webhook destination is off-platform."""
    payload = event.payload
    line = _health_subject_line(payload)
    action_key = payload.get("remediation_action_key") or tr("(no action key)")
    script_name = payload.get("script_name")
    script_version = payload.get("script_version")
    if script_name:
        pinned = (
            tr(
                "Pinned script: {script_name} (version {script_version})",
                script_name=script_name,
                script_version=script_version,
            )
            + "\n"
        )
    else:
        pinned = ""
    return (
        tr(
            "[Toeshee] Remediation awaiting approval: {action_key} on {hostname}",
            action_key=action_key,
            hostname=payload.get("hostname") or tr("(unknown device)"),
        ),
        tr("A remediation is waiting for approval for {line}.", line=line)
        + "\n\n"
        + tr("Approved action: {action_key}", action_key=action_key)
        + "\n"
        + pinned
        + _health_observation(payload)
        + tr("Opened: {opened_at}", opened_at=payload.get("opened_at", "?"))
        + "\n\n"
        + tr(
            "It will NOT run until someone holding `health.remediate` approves it. "
            "Sign in to Toeshee to approve or refuse it."
        )
        + "\n",
    )


# ---------------------------------------------------------------------------
# IPAM (docs/modules/ipam/spec.md §10).
#
# BOTH ARE MACHINE-ORIGIN AND NEITHER HAS AN ACTOR: drift is found by the daily
# reconcile sweep, and a rejected sweep result is found by the result handler
# with `actor_type='agent'`. Neither renderer reads `event.actor_user_id`.
#
# Neither type is staff-only -- an IPAM event has no ticket and therefore no
# requester to keep it away from -- so in v1 both are routable only through
# `channel_default` rules (§10). That makes the destination plausibly an
# off-platform webhook, which is why these render an address, a prefix and a
# class, and never anything a device said about itself beyond that.
# ---------------------------------------------------------------------------


def _ipam_subnet_phrase(payload: dict[str, Any]) -> str:
    """`10.4.7.0/24 (Manila office)` -- the subnet, named when it has a name.

    Every part falls back rather than KeyErroring: a machine-origin event that
    lost a key must still deliver a readable mail, because the alternative is a
    raised exception inside the delivery worker.
    """
    prefix = payload.get("subnet_prefix") or tr("(no registered subnet)")
    name = payload.get("subnet_name")
    return f"{prefix} ({name})" if name else str(prefix)


def _render_ipam_drift_detected(event: DomainEvent) -> tuple[str, str]:
    """`ipam.drift_detected` -- published only on the SECOND consecutive
    detection (§0.8), so this mail never fires for one sweep that ran while a
    switch was rebooting. The drift CLASS is in the subject line because the
    action differs per class: `documented_absent` is a reclaim candidate,
    `conflict` is two machines answering at one address."""
    payload = event.payload
    drift_class = (
        _coded(_DRIFT_CLASS_WORDS, payload["drift_class"])
        if payload.get("drift_class")
        else tr("drift")
    )
    address = payload.get("address") or tr("(unknown address)")
    severity = _word(_SEVERITY_WORDS, payload.get("severity", "?"))
    device_id = payload.get("device_id")
    claimant = (
        (tr("Documented claimant: device {device_id}", device_id=device_id) + "\n")
        if device_id
        else ""
    )
    return (
        tr(
            "[Toeshee] IPAM drift ({severity}): {drift_class} at {address}",
            severity=severity,
            drift_class=drift_class,
            address=address,
        ),
        tr("The address plan and the fleet disagree about {address}.", address=address)
        + "\n\n"
        + tr("Finding: {drift_class}", drift_class=drift_class)
        + "\n"
        + tr("Severity: {severity}", severity=severity)
        + "\n"
        + tr(
            "Confidence: {confidence}",
            confidence=_coded(_CONFIDENCE_WORDS, payload.get("confidence", "?")),
        )
        + "\n"
        + tr("Subnet: {subnet}", subnet=_ipam_subnet_phrase(payload))
        + "\n"
        + f"{claimant}\n"
        + tr(
            "Seen on two consecutive reconciliation passes. Sign in to Toeshee to "
            "review, document, or acknowledge it."
        )
        + "\n",
    )


def _render_ipam_sweep_rejected(event: DomainEvent) -> tuple[str, str]:
    """`ipam.sweep_rejected` -- A SECURITY SIGNAL, NOT AN OPERATIONS ONE (§10).

    An agent returned a `net.sweep` result that failed the §6.4 check against
    the scope its own job was signed for. The whole result was rejected and
    zero observations were written. The offending sample is bounded to ten
    addresses by the publisher, and this renderer bounds it again rather than
    trusting that: the payload comes from a device that has just been caught
    reporting outside its authorization.
    """
    payload = event.payload
    sample = payload.get("sample") or []
    if isinstance(sample, list):
        shown = ", ".join(str(a) for a in sample[:10])
    else:
        shown = ""
    offending = payload.get("offending_count", "?")
    return (
        tr(
            "[Toeshee] IPAM sweep result REJECTED from device {device}",
            device=payload.get("device_id") or tr("(unknown device)"),
        ),
        tr(
            "A network sweep result was rejected in full and no observations were "
            "recorded from it."
        )
        + "\n\n"
        + tr("Reason: {reason}", reason=_field(payload, "reason", tr("not recorded")))
        + "\n"
        + tr("Device: {device}", device=payload.get("device_id") or tr("(unknown)"))
        + "\n"
        + tr("Subnet: {subnet}", subnet=payload.get("subnet_id") or tr("(unknown)"))
        + "\n"
        + tr("Sweep run: {sweep_run}", sweep_run=payload.get("sweep_run_id") or tr("(unknown)"))
        + "\n"
        + tr("Addresses outside the signed scope: {count}", count=offending)
        + "\n"
        + ((tr("Sample: {sample}", sample=shown) + "\n") if shown else "")
        + "\n"
        + tr(
            "An agent reporting outside the scope its job was signed for is either "
            "faulty or controlled by someone else. Sign in to Toeshee to review it."
        )
        + "\n",
    )


# ---------------------------------------------------------------------------
# Platform self-monitoring (`app/core/health/notify.py`). Probe name, status and
# reason CODE only: the probe vocabulary never carries exception text.
# ---------------------------------------------------------------------------


# Plain words for `ReasonCode`; an unknown code is printed as it is.
_PROBE_REASON_TEXT = {
    "stale": N_("no recent success has been recorded"),
    "failed": N_("the last run reported a failure"),
    "unreachable": N_("it did not answer"),
    "timeout": N_("it answered too slowly"),
    "misconfigured": N_("its settings are wrong or unreadable"),
}


def _probe_name(payload: dict[str, Any]) -> str:
    probe = payload.get("probe")
    return probe_label(str(probe)) if probe else tr("(unknown check)")


def _render_platform_probe_unhealthy(event: DomainEvent) -> tuple[str, str]:
    payload = event.payload
    probe = payload.get("probe") or tr("(unknown probe)")
    name = _probe_name(payload)
    status = _word(_PROBE_STATUS_WORDS, payload.get("status") or "unhealthy")
    reason = payload.get("reason") or tr("not recorded")
    reason_text = _word(_PROBE_REASON_TEXT, reason)
    return (
        tr("[Toeshee] Platform problem: {name} is {status}", name=name, status=status),
        tr(
            "The platform's own check '{name}' has reported {status} for "
            "{samples} consecutive samples.",
            name=name,
            status=status,
            samples=payload.get("consecutive_samples", "?"),
        )
        + "\n\n"
        + tr("Check: {name} ({probe})", name=name, probe=probe)
        + "\n"
        + tr("Status: {status}", status=status)
        + "\n"
        + tr("Reason: {reason}", reason=reason_text)
        + "\n"
        + tr("Since: {since}", since=payload.get("since") or tr("(unknown)"))
        + "\n\n"
        + tr(
            "Sign in to Toeshee and open Platform status for detail. You will be "
            "told when it recovers."
        )
        + "\n",
    )


def _render_platform_probe_recovered(event: DomainEvent) -> tuple[str, str]:
    payload = event.payload
    probe = payload.get("probe") or tr("(unknown probe)")
    name = _probe_name(payload)
    return (
        tr("[Toeshee] Platform recovered: {name} is ok", name=name),
        tr("The platform's own check '{name}' is healthy again.", name=name)
        + "\n\n"
        + tr("Check: {name} ({probe})", name=name, probe=probe)
        + "\n"
        + tr(
            "Unhealthy since: {since}",
            since=payload.get("unhealthy_since") or tr("(unknown)"),
        )
        + "\n"
        + tr("Recovered: {since}", since=payload.get("since") or tr("(unknown)"))
        + "\n",
    )


# ---------------------------------------------------------------------------
# Subscriptions (`app/modules/subscriptions/alerts.py`).
# ---------------------------------------------------------------------------


def _subscription_lines(payload: dict[str, Any]) -> str:
    plan = payload.get("plan")
    return (
        tr("Service: {service}", service=payload.get("service_name") or tr("(unnamed)"))
        + (" (" + str(plan) + ")" if plan else "")
        + "\n"
        + tr(
            "Renews: {renews_at} (in {days} days)",
            renews_at=payload.get("renews_at") or tr("(unknown)"),
            days=payload.get("days_to_renewal", "?"),
        )
        + "\n"
        + tr(
            "Notice deadline: {deadline} (in {days} days)",
            deadline=payload.get("notice_deadline") or tr("(unknown)"),
            days=payload.get("days_to_notice", "?"),
        )
        + "\n"
        + tr(
            "Auto-renews: {answer}",
            answer=tr("yes") if payload.get("auto_renew") else tr("no"),
        )
        + "\n"
    )


def _render_subscription_notice_deadline_approaching(event: DomainEvent) -> tuple[str, str]:
    payload = event.payload
    name = payload.get("service_name") or tr("a subscription")
    return (
        tr(
            "[Toeshee] Cancellation notice deadline for {name}: {deadline}",
            name=name,
            deadline=payload.get("notice_deadline") or tr("(unknown)"),
        ),
        tr("The last day to give notice before this subscription renews is coming up.")
        + "\n\n"
        + _subscription_lines(payload)
        + "\n"
        + tr(
            "After the notice deadline the renewal can no longer be cancelled or "
            "renegotiated. Sign in to Toeshee to review it."
        )
        + "\n",
    )


def _render_subscription_renewal_approaching(event: DomainEvent) -> tuple[str, str]:
    payload = event.payload
    name = payload.get("service_name") or tr("a subscription")
    return (
        tr(
            "[Toeshee] Subscription renewal: {name} on {renews_at}",
            name=name,
            renews_at=payload.get("renews_at") or tr("(unknown)"),
        ),
        tr("This subscription renews soon.")
        + "\n\n"
        + _subscription_lines(payload)
        + "\n"
        + tr("Sign in to Toeshee to review it or record the renewal.")
        + "\n",
    )


def _strip_controls(raw: str) -> str:
    """`raw` without its C0 (incl. CR, LF, TAB, NUL), DEL and C1 characters."""
    return "".join(ch for ch in raw if not (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F))


def _render_device_joined_via_link(event: DomainEvent) -> tuple[str, str]:
    payload = event.payload
    # The hostname is whatever the enrolling agent reported -- through a
    # leaked link code, anyone's. A CR/LF in it made the subject an invalid
    # header and the "waiting for review" email was never sent (final review
    # M3); the rest of C0/C1 would reach a mail client's terminal or pane.
    name = _strip_controls(str(payload.get("hostname") or "")) or tr("(unnamed computer)")
    return (
        tr("[Toeshee] New computer waiting for review: {name}", name=name),
        tr("A computer joined through the install link and is waiting for review.")
        + "\n\n"
        + tr("Computer: {name}", name=name)
        + "\n"
        + tr(
            "Operating system: {os_family}",
            os_family=payload.get("os_family") or tr("(unknown)"),
        )
        + "\n"
        + tr("Joined from: {source}", source=payload.get("source_address") or tr("(unknown)"))
        + "\n"
        + tr("Joined: {joined_at}", joined_at=payload.get("joined_at") or tr("(unknown)"))
        + "\n\n"
        + tr(
            "Until someone approves it, it receives only inventory, health monitoring and "
            "certificate renewal. Approve or reject it on the Devices page."
        )
        + "\n",
    )


def _render_device_removal_incomplete(event: DomainEvent) -> tuple[str, str]:
    """The rejection's outcome is a wire value; the mail says it in words.

    The hostname is agent-reported, so it gets `_render_device_joined_via_link`'s
    treatment: a CR/LF would make the subject an invalid header and this email
    -- the only one saying an agent is still installed -- would never be sent.
    """
    payload = event.payload
    name = _strip_controls(str(payload.get("hostname") or "")) or tr("(unnamed computer)")
    why = (
        tr("Its agent is too old to remove itself.")
        if payload.get("rejection_outcome") == "agent_cannot_uninstall"
        else tr("Its agent did not confirm it removed itself.")
    )
    return (
        tr("[Toeshee] Rejected computer still has the agent: {name}", name=name),
        tr("A computer that joined through the install link was rejected. {why}", why=why)
        + "\n\n"
        + tr("Computer: {name}", name=name)
        + "\n"
        + tr(
            "Joined from: {source}",
            source=payload.get("link_joined_from") or tr("(unknown)"),
        )
        + "\n"
        + tr("Rejected: {rejected_at}", rejected_at=payload.get("rejected_at") or tr("(unknown)"))
        + "\n\n"
        + tr(
            "Its certificate has been revoked, so it can no longer connect, but the agent "
            "may still be installed. Uninstall it on the computer by hand, then press "
            '"I removed it" beside it under Waiting for review on the Devices page.'
        )
        + "\n",
    )


def _render_push_run_completed(event: DomainEvent) -> tuple[str, str]:
    payload = event.payload
    prefix = _strip_controls(str(payload.get("prefix") or "")) or tr("(unknown subnet)")
    return (
        tr("[Toeshee] Push install finished: {prefix}", prefix=prefix),
        tr("A push install run someone confirmed has finished.")
        + "\n\n"
        + tr("Subnet: {subnet}", subnet=prefix)
        + "\n"
        + tr(
            "Machines in the run: {count}",
            count=_field(payload, "target_count", tr("(unknown)")),
        )
        + "\n"
        + tr("Installed: {count}", count=payload.get("installed", 0))
        + "\n"
        + tr("Failed: {count}", count=payload.get("failed", 0))
        + "\n"
        + tr("Refused before anything was sent: {count}", count=payload.get("refused", 0))
        + "\n"
        + tr("Finished: {finished_at}", finished_at=payload.get("finished_at") or tr("(unknown)"))
        + "\n\n"
        + tr("Open Devices, then Push runs, to see what happened on each machine.")
        + "\n",
    )


def _render_push_run_stopped(event: DomainEvent) -> tuple[str, str]:
    payload = event.payload
    prefix = _strip_controls(str(payload.get("prefix") or "")) or tr("(unknown subnet)")
    name = _strip_controls(str(payload.get("credential_name") or "")) or tr(
        "(unnamed credential)"
    )
    return (
        tr("[Toeshee] Push install stopped: {prefix}", prefix=prefix),
        tr("A push install run was stopped to protect its credential from lockout.")
        + "\n\n"
        + tr(
            "The credential {name} was refused by {count} machines, so nothing more was "
            "presented with it.",
            name=name,
            count=_field(payload, "auth_failures", tr("(unknown)")),
        )
        + "\n\n"
        + tr("Subnet: {subnet}", subnet=prefix)
        + "\n"
        + tr(
            "Machines in the run: {count}",
            count=_field(payload, "target_count", tr("(unknown)")),
        )
        + "\n"
        + tr("Installed before the stop: {count}", count=payload.get("installed", 0))
        + "\n"
        + tr("Machines still waiting: {count}", count=payload.get("waiting", 0))
        + "\n"
        + tr("Stopped: {stopped_at}", stopped_at=payload.get("stopped_at") or tr("(unknown)"))
        + "\n\n"
        + tr(
            "Check the account's password and whether it is locked, then open Devices, "
            "Push runs, and Resume the run. It starts again with one machine."
        )
        + "\n",
    )


def _render_generic(event: DomainEvent) -> tuple[str, str]:
    """Fail-safe fallback for an event type this module has not been
    explicitly taught a rendering for (a machine-origin event type from a
    future module, or `send_test`'s own synthetic `notification.test_send`
    event, which is never a member of `EVENT_TYPES` at all). Never raises,
    and still surfaces the ticket-shaped fields when the payload happens to
    carry them -- this is the old (pre-M6-fix) machine-notice format, kept
    only as the safety net beneath `_EMAIL_RENDERERS`, never the normal path
    for a registered ticket event type (see `test_every_registered_event_
    type_has_an_email_renderer`, which pins that `_EMAIL_RENDERERS` covers
    every member of `EVENT_TYPES` today)."""
    payload = event.payload
    lines = [tr("Event: {event_type}", event_type=event.event_type)]
    if "ticket_number" in payload:
        lines.append(tr("Ticket: #{ticket_number}", ticket_number=payload["ticket_number"]))
    if "subject" in payload:
        lines.append(tr("Subject: {subject}", subject=payload["subject"]))
    lines.append(tr("Occurred: {occurred_at}", occurred_at=event.occurred_at.isoformat()))
    return (f"[Toeshee] {event.event_type}", "\n".join(lines) + "\n")


# THE SINGLE COPY of "which event type renders which subject/body" (spec
# §6.4/§0.4, whole-branch review finding M6). Spec §14 defers a
# TEMPLATE-EDITING UI, not rendering itself ("payload rendering is code in
# v1") -- this dict, plus the small render functions above, is exactly that
# code: no template engine, no string-substitution DSL, just one function per
# event type reading the same payload keys `_event_payload`/`routing.py`
# already document as this module's fixed contract. `test_every_registered_
# event_type_has_an_email_renderer` (test_notification_delivery.py) asserts
# this dict's keys equal `app.core.events.EVENT_TYPES` exactly, so a future
# event type added to the registry without a renderer here fails the suite
# instead of silently falling back to `_render_generic`'s machine notice --
# the exact failure mode this fix closes for today's seven ticket event
# types (six actor-originated ones plus `ticket.sla_breached`, published by
# the SLA sweep with no human actor at all -- see the next paragraph).
#
# MACHINE-ORIGIN EVENTS WITH NO ACTOR. None of these renderers reads
# `event.actor_user_id` at all -- every one of them renders purely from
# `event.payload` (plus `event.event_type`/`event.occurred_at` in the
# fallback) -- so a future Phase-16 alert event with `actor_user_id=None`
# renders exactly as sensibly as a human-actioned one; there is no branch
# here that assumes an actor exists.
_EMAIL_RENDERERS: dict[str, Callable[[DomainEvent], tuple[str, str]]] = {
    "ticket.assigned": _render_ticket_assigned,
    "ticket.status_changed": _render_ticket_status_changed,
    "ticket.comment_added": _render_ticket_comment_added,
    "ticket.internal_note_added": _render_ticket_internal_note_added,
    "ticket.approval_requested": _render_ticket_approval_requested,
    "ticket.approval_decided": _render_ticket_approval_decided,
    "ticket.resolved": _render_ticket_resolved,
    "ticket.sla_breached": _render_ticket_sla_breached,
    "health.alert_raised": _render_health_alert_raised,
    "health.alert_cleared": _render_health_alert_cleared,
    "health.alert_escalated": _render_health_alert_escalated,
    "health.remediation_requires_approval": _render_health_remediation_requires_approval,
    "ipam.drift_detected": _render_ipam_drift_detected,
    "ipam.sweep_rejected": _render_ipam_sweep_rejected,
    "platform.probe_unhealthy": _render_platform_probe_unhealthy,
    "platform.probe_recovered": _render_platform_probe_recovered,
    "subscription.notice_deadline_approaching": (
        _render_subscription_notice_deadline_approaching
    ),
    "subscription.renewal_approaching": _render_subscription_renewal_approaching,
    "device.joined_via_link": _render_device_joined_via_link,
    "device.removal_incomplete": _render_device_removal_incomplete,
    "push.run.completed": _render_push_run_completed,
    "push.run.stopped": _render_push_run_stopped,
}


def _render_email(event: DomainEvent) -> tuple[str, str]:
    """The email `(subject, body)` for `event` -- see `_EMAIL_RENDERERS`'s
    docstring. Falls back to `_render_generic` for any event type not (yet)
    in that dict, which today only happens for `send_test`'s own synthetic
    `notification.test_send` event -- every real, registered event type is
    covered and pinned by `test_every_registered_event_type_has_an_email_
    renderer`."""
    renderer = _EMAIL_RENDERERS.get(event.event_type)
    if renderer is not None:
        return renderer(event)
    return _render_generic(event)


def _event_payload(event: DomainEvent) -> dict[str, Any]:
    """The webhook body: the outbox event's own publishable fields (spec
    §2.1/§6.4 -- `event.payload` never carries non-publishable content, e.g.
    an internal note's body, because the ticket core never put it there).
    This module does not additionally filter `event.payload`; it forwards
    exactly what routing already trusted enough to resolve recipients from."""
    return {
        "event_id": str(event.id),
        "event_type": event.event_type,
        "subject_type": event.subject_type,
        "subject_id": str(event.subject_id),
        "occurred_at": event.occurred_at.isoformat(),
        "payload": event.payload,
    }


async def _send_webhook(
    session: AsyncSession,
    *,
    delivery: NotificationDelivery,
    channel: NotificationChannel,
    event: DomainEvent,
    vault: VaultService,
    actor_type: Literal["user", "system"] = "system",
    actor_id: UUID | None = None,
) -> _AttemptOutcome:
    """`actor_type`/`actor_id` attribute the credential-checkout audit record
    below (whole-branch review finding M7). Default to `("system", None)` --
    correct for the worker's real delivery path (`attempt_delivery`), where no
    human triggered this send. `send_test` passes the real caller's identity
    instead, because a `POST /channels/{id}/test` checkout was caused by a
    named user pressing the test button, not by the delivery sweep -- the
    hash-chained audit log (invariant #4) must attribute a privileged action
    to who actually did it."""
    url = str(channel.config.get("url", ""))
    # Spec §7: stable across every attempt of this delivery, so an at-least-
    # once consumer can dedupe -- `delivery.event_id` never changes between
    # retries of the same row.
    headers: dict[str, str] = {"X-Toeshee-Event-Id": str(delivery.event_id)}

    if channel.credential_id is not None:
        token = await vault.checkout(
            session, org_id=delivery.org_id, credential_id=channel.credential_id
        )
        await audit_service.record(
            session,
            org_id=delivery.org_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action="notification.credential_used",
            target_type="credential",
            target_id=str(channel.credential_id),
            metadata={"channel_id": str(channel.id), "delivery_id": str(delivery.id)},
        )
        headers["Authorization"] = f"Bearer {token}"
        del token  # never retained past the header it was built for (invariant #3)

    hosts = await allowlisted_hosts(session, org_id=delivery.org_id)
    try:
        response = await post_json(
            url, _event_payload(event), headers=headers, allowlisted_hosts=hosts
        )
    except EgressBlocked as exc:
        # `exc.reason` is already a CATEGORY, never a per-IP/per-host detail
        # (egress.py's own invariant) -- safe to place directly in
        # `last_error`.
        return _AttemptOutcome(
            ok=False,
            status_code=None,
            error_reason=exc.reason,
            error_excerpt=_sanitize_excerpt(f"egress blocked: {exc.reason}"),
        )
    except TimeoutError:
        return _AttemptOutcome(
            ok=False, status_code=None, error_reason="timeout", error_excerpt="request timed out"
        )
    except httpx.TransportError:
        # Connection-level failures `post_json` does not itself categorize
        # (refused, reset, aborted mid-stream) -- distinct from the DNS/
        # allowlist/redirect categories above, which are `EgressBlocked`.
        return _AttemptOutcome(
            ok=False,
            status_code=None,
            error_reason="connection_reset",
            error_excerpt="connection error",
        )

    if 200 <= response.status_code < 300:
        return _AttemptOutcome(
            ok=True, status_code=response.status_code, error_reason=None, error_excerpt=None
        )
    # `response.excerpt` is already bounded (64 KiB read cap -> 500 chars) and
    # control-character-stripped by `post_json` itself (spec §4.4) -- never a
    # raw response body.
    return _AttemptOutcome(
        ok=False,
        status_code=response.status_code,
        error_reason=None,
        error_excerpt=response.excerpt,
    )


def _send_smtp_sync(
    *,
    host: str,
    port: int,
    use_tls: bool,
    username: str | None,
    password: str | None,
    from_address: str,
    to_address: str,
    subject: str,
    body: str,
    reply_to: str | None = None,
    access_token: str | None = None,
) -> None:
    """Blocking SMTP conversation -- always called through `asyncio.to_thread`
    (see the module docstring: this process also serves the agent gateway's
    WebSocket connections on the same event loop, so a blocking `smtplib`
    call inline would stall every other connection for as long as the SMTP
    round-trip takes).

    `use_tls` means STARTTLS on the given port (the common on-prem relay
    shape, e.g. port 587), not implicit TLS/SMTPS -- `config`'s shape (spec
    §2.2: `{host, port, from_address, use_tls}`) has no separate scheme field
    to select SMTPS, and STARTTLS is what a customer's internal relay
    predominantly speaks.

    `reply_to` is OPTIONAL and defaults to None -- absent, the message this
    builds is byte-for-byte what it was before email intake existed. When
    present it is the email-intake module's per-ticket reply address (see
    `email_intake.identity.reply_to_for_ticket`), and the header is added
    only then: an empty or always-present `Reply-To` would invite replies to
    an address nothing polls.
    """
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_address
    message["To"] = to_address
    if reply_to is not None:
        message["Reply-To"] = reply_to
    message.set_content(body)

    with smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT_S) as client:
        if use_tls:
            client.starttls(context=ssl.create_default_context())
        if access_token is not None:
            _authenticate_xoauth2(client, username or from_address, access_token)
        elif username:
            client.login(username, password or "")
        client.send_message(message)


def _authenticate_xoauth2(client: smtplib.SMTP, user: str, access_token: str) -> None:
    """`AUTH XOAUTH2` with the SASL string as the initial response. On failure
    Microsoft sends a 334 JSON challenge and waits for an empty line; answering
    it lets the server finish with 535 instead of holding the connection."""
    client.ehlo_or_helo_if_needed()

    def respond(challenge: bytes | None = None) -> str:
        if challenge is not None:
            return ""
        return f"user={user}\x01auth=Bearer {access_token}\x01\x01"

    client.auth("XOAUTH2", respond, initial_response_ok=True)


async def _recipient_language(session: AsyncSession, org_id: UUID, address: str) -> str:
    """The recipient's own language (spec §3.4); English for an address that is no user's.

    Looked up in `org_id` only (invariant 5): the `org_id` predicate is explicit
    here, and the callers hand in the delivery's own org-scoped session, so row-level
    security says the same thing a second time. An address that is a user of another
    organization is, for this one, no user's, and gets English.

    The address is compared case-insensitively (`ix_users_org_lower_email` serves it).
    That index is not unique, so two users of one organization can in principle share
    an address; the earliest-created one (uuid7 ids sort by creation time) decides,
    so the answer is the same on every run instead of whatever row the planner
    returns first.

    `es`, `es-MX` and `ES` are Spanish; everything else, `None` included, is English
    (the console writes only `en` and `es`; an older stored `en-GB` stays English)."""
    locale = await session.scalar(
        select(User.locale)
        .where(User.org_id == org_id, func.lower(User.email) == address.lower())
        .order_by(User.id)
        .limit(1)
    )
    return "es" if (locale or "").lower().startswith("es") else "en"


async def _send_email(
    session: AsyncSession,
    *,
    delivery: NotificationDelivery,
    channel: NotificationChannel,
    event: DomainEvent,
    vault: VaultService,
    actor_type: Literal["user", "system"] = "system",
    actor_id: UUID | None = None,
) -> _AttemptOutcome:
    """`actor_type`/`actor_id` -- see `_send_webhook`'s docstring; identical
    reasoning, mirrored here for the email transport (whole-branch review
    finding M7)."""
    config = channel.config
    host = str(config["host"])
    port = int(config["port"])
    use_tls = bool(config.get("use_tls", True))
    from_address = str(config["from_address"])

    username: str | None = None
    password: str | None = None
    access_token: str | None = None
    signs_in_with_microsoft = config.get("auth") == "microsoft"
    if signs_in_with_microsoft:
        # Pinned, whatever `config` says (see `microsoft.SMTP_HOST`).
        host, port, use_tls = microsoft.SMTP_HOST, microsoft.SMTP_PORT, True
        if channel.credential_id is None:
            return _sign_in_failure("sign_in_missing", "Microsoft 365 sign-in is not set")
    if channel.credential_id is not None:
        secret = await vault.checkout(
            session, org_id=delivery.org_id, credential_id=channel.credential_id
        )
        metadata = {"channel_id": str(channel.id), "delivery_id": str(delivery.id)}
        if signs_in_with_microsoft:
            metadata["sign_in"] = "microsoft"
        await audit_service.record(
            session,
            org_id=delivery.org_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action="notification.credential_used",
            target_type="credential",
            target_id=str(channel.credential_id),
            metadata=metadata,
        )
        # Spec §2.2: `credential_id` holds "the SMTP username/password" -- one
        # vault secret slot for two values, so this module encodes/decodes a
        # small JSON object rather than inventing a second credential column.
        try:
            creds = json.loads(secret)
        except ValueError:
            creds = None
        del secret
        if not isinstance(creds, dict):
            return _sign_in_failure(
                "sign_in_missing", "the stored sign-in is unreadable; enter it again"
            )
        if signs_in_with_microsoft:
            outcome = await _microsoft_token(creds)
            del creds
            if isinstance(outcome, _AttemptOutcome):
                return outcome
            access_token = outcome
            username = from_address
        else:
            username = creds.get("username")
            password = creds.get("password")
            del creds  # never retained past this point (invariant #3)

    # Whole-branch review finding M6: this used to be an unreadable machine
    # notice (`Event: ticket.status_changed / Subject: ticket <uuid> /
    # Occurred: ...`) that never looked at `event.payload` at all, despite
    # every publisher putting `ticket_number`/`subject` there for exactly this
    # purpose (spec §0.4 -- telling a requester their ticket was resolved is
    # the module's headline value). `_render_email` renders a real subject
    # line and body per event type, data-driven, from that payload -- see its
    # docstring and `_EMAIL_RENDERERS`.
    #
    # Spec §3.4: rendered in the RECIPIENT's language, always inside an explicit
    # `use_locale`. Never the ambient `locale_var`: the worker has no request, and
    # `send_test` runs inside the requester's (whose `Accept-Language` says nothing
    # about whoever the mail is addressed to). The rendered text goes to the SMTP
    # call below and nowhere else -- no delivery column holds it, and `last_error`
    # is built from the English literals in this function's `except` arms.
    with use_locale(await _recipient_language(session, delivery.org_id, delivery.recipient)):
        subject, body = _render_email(event)

    # Spec correction C3, email-intake §5.2. A ticket notification is the
    # ONLY outbound mail this platform sends that a human can meaningfully
    # reply to, so it is the only one that carries a `Reply-To` -- and
    # resolving it here is also the only thing in production that ever mints
    # a ticket's `email_token`. `subject_type` is the condition, not
    # `event_type`: a future ticket event type added to the registry is
    # repliable for free, and a non-ticket event (a Phase 16 alert, or
    # `send_test`'s own synthetic `notification_channel`-subject event)
    # never is, because a reply to it would resolve to no ticket.
    #
    # `delivery.recipient` is passed because only the ticket's REQUESTER is
    # offered a reply address (Task 8 review F2): the same ticket event also
    # goes to the assignee and to queue members (`routing.py`), and
    # email-intake §13 defers staff replying by email -- `intake.py`
    # quarantines a reply whose sender is not the requester, silently. A
    # technician handed a repliable address gets no comment, no bounce, and
    # no notice that their reply went nowhere. `reply_to_for_ticket` decides
    # that, not this module.
    #
    # Returns None -- and writes nothing -- whenever the org has no active
    # inbound mailbox, which is every org that has not configured email
    # intake; see `reply_to_for_ticket`'s docstring for the full list of
    # refusals. This module deliberately does not second-guess that answer.
    reply_to: str | None = None
    if event.subject_type == "ticket":
        reply_to = await reply_to_for_ticket(
            session, delivery.org_id, event.subject_id, delivery.recipient
        )

    try:
        await asyncio.to_thread(
            _send_smtp_sync,
            host=host,
            port=port,
            use_tls=use_tls,
            username=username,
            password=password,
            from_address=from_address,
            to_address=delivery.recipient,
            subject=subject,
            body=body,
            reply_to=reply_to,
            access_token=access_token,
        )
    except smtplib.SMTPResponseException as exc:
        # Covers SMTPConnectError/SMTPHeloError/SMTPDataError/
        # SMTPAuthenticationError/SMTPSenderRefused too -- all carry a real
        # SMTP reply code. RFC 5321: 5yz is a PERMANENT failure, 4yz is
        # transient -- the opposite of what these numbers mean over HTTP (see
        # the module docstring). Never fed through `is_retryable`'s
        # `status_code` branch -- classified here via `error_reason` instead.
        code = exc.smtp_code
        terminal = code >= 500
        return _AttemptOutcome(
            ok=False,
            status_code=code,
            error_reason="smtp_permanent_reject" if terminal else "smtp_transient_reject",
            error_excerpt=_sanitize_excerpt(f"smtp {code} rejected"),
        )
    except smtplib.SMTPRecipientsRefused:
        # No single reply code (a per-recipient refusal dict) -- the address
        # itself is bad, which a retry will not fix.
        return _AttemptOutcome(
            ok=False,
            status_code=None,
            error_reason="smtp_permanent_reject",
            error_excerpt="smtp recipient refused",
        )
    except smtplib.SMTPServerDisconnected:
        return _AttemptOutcome(
            ok=False,
            status_code=None,
            error_reason="connection_reset",
            error_excerpt="smtp server disconnected",
        )
    except smtplib.SMTPException:
        # Any other smtplib failure not classified above (e.g.
        # SMTPNotSupportedError) -- treated as terminal rather than retried
        # blindly against an unrecognised protocol failure.
        return _AttemptOutcome(
            ok=False, status_code=None, error_reason="smtp_error", error_excerpt="smtp send failed"
        )
    except TimeoutError:
        return _AttemptOutcome(
            ok=False,
            status_code=None,
            error_reason="timeout",
            error_excerpt="smtp connection timed out",
        )
    except OSError:
        # Connection-level failures below smtplib's own exception hierarchy
        # (refused, reset, DNS failure for the relay host) -- an on-prem
        # relay blip, not a permanent rejection.
        return _AttemptOutcome(
            ok=False,
            status_code=None,
            error_reason="connection_reset",
            error_excerpt="smtp connection error",
        )

    return _AttemptOutcome(ok=True, status_code=None, error_reason=None, error_excerpt=None)


def _sign_in_failure(reason: str, excerpt: str) -> _AttemptOutcome:
    return _AttemptOutcome(ok=False, status_code=None, error_reason=reason, error_excerpt=excerpt)


async def _microsoft_token(creds: dict[str, Any]) -> "str | _AttemptOutcome":
    """An app-only token for SMTP.SendAsApp, or the classified failure. Never
    carries provider text: `TokenRevoked`/`TokenTransportError` do not."""
    tenant_id, client_id, client_secret = (
        creds.get("tenant_id"),
        creds.get("client_id"),
        creds.get("client_secret"),
    )
    if not (
        isinstance(tenant_id, str) and isinstance(client_id, str) and isinstance(client_secret, str)
    ):
        return _sign_in_failure("sign_in_missing", "Microsoft 365 sign-in is not set")
    try:
        return await app_access_token(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            scope=microsoft.SMTP_APP_SCOPE,
        )
    except (TokenRevoked, ValueError):
        return _sign_in_failure("sign_in_refused", "Microsoft 365 refused the sign-in")
    except TokenTransportError:
        return _sign_in_failure("sign_in_unavailable", "Microsoft 365 sign-in could not be reached")
    finally:
        client_secret = None


def _apply_outcome(
    delivery: NotificationDelivery, outcome: _AttemptOutcome, *, now: datetime
) -> None:
    """Spec §7's backoff schedule, applied to a real delivery row.

    `delivery.attempts` counts COMPLETED attempts (1 after this one, if this
    is the first). The five spec values (1m, 5m, 25m, 2h, 10h) are the delay
    scheduled after failures #1-#5 respectively -- `next_attempt_delay` is
    called with the PRE-increment count (`delivery.attempts - 1`) so the
    first failure gets `BACKOFF_SECONDS[0]` (1m), the second gets
    `BACKOFF_SECONDS[1]` (5m), and so on through the fifth failure getting
    `BACKOFF_SECONDS[4]` (10h). The cap is enforced HERE, against the
    POST-increment count reaching `MAX_ATTEMPTS` (`len(BACKOFF_SECONDS) + 1`
    == 6, I4 fix -- derived, not a separate literal, so it cannot silently
    drift out of sync with `BACKOFF_SECONDS` again) -- not by relying on
    `next_attempt_delay`'s own internal bound -- so the 6th (and final
    allowed) attempt goes straight to `dead` without scheduling a 7th that
    must never happen. "Capped at N attempts, then dead" (spec §7) means
    exactly N attempts total (N-1 retries after the first), not N retries."""
    delivery.attempts += 1
    delivery.last_status_code = outcome.status_code

    if outcome.ok:
        delivery.status = "sent"
        delivery.sent_at = now
        delivery.last_error = None
        delivery.next_attempt_at = None
        return

    delivery.last_error = outcome.error_excerpt
    delay = None
    if is_retryable(outcome.status_code, outcome.error_reason) and delivery.attempts < MAX_ATTEMPTS:
        delay = next_attempt_delay(delivery.attempts - 1)
    if delay is None:
        # Terminal (not retryable), OR retryable but the cap is reached --
        # spec §7, "go straight to dead without burning every attempt" for
        # the terminal case, and "capped at MAX_ATTEMPTS, then dead" for the
        # exhaustion case.
        delivery.status = "dead"
        delivery.next_attempt_at = None
    else:
        delivery.status = "failed"
        delivery.next_attempt_at = now + timedelta(seconds=delay)


async def attempt_delivery(
    session: AsyncSession, *, delivery: NotificationDelivery, vault: VaultService
) -> None:
    """Perform exactly one delivery attempt for `delivery` and record the
    classified outcome on it (spec §7). The caller is responsible for having
    already claimed `delivery` (see the module docstring's "CLAIM LIFECYCLE"
    note) and for committing afterward.

    `channel`/`event` are re-loaded org-scoped (invariant #5, defense in
    depth) even though `delivery` already carries `channel_id`/`event_id` --
    the same discipline `channels.py`/`rules.py` apply to every other
    cross-referenced id in this module."""
    channel = (
        await session.execute(
            select(NotificationChannel).where(
                NotificationChannel.org_id == delivery.org_id,
                NotificationChannel.id == delivery.channel_id,
            )
        )
    ).scalar_one_or_none()
    if channel is None:
        # A channel referenced by a delivery row should be unreachable in
        # practice (channels.py never hard-deletes out from under a
        # referencing FK) -- surfacing this loudly, rather than silently
        # marking the delivery dead, keeps a real data-integrity bug from
        # masquerading as an ordinary terminal delivery failure.
        raise ValueError(  # i18n-exempt: integrity guard in the delivery worker, logged
            f"delivery {delivery.id} references missing channel {delivery.channel_id}"
        )

    event = (
        await session.execute(
            select(DomainEvent).where(
                DomainEvent.org_id == delivery.org_id, DomainEvent.id == delivery.event_id
            )
        )
    ).scalar_one_or_none()
    if event is None:
        raise ValueError(  # i18n-exempt: integrity guard in the delivery worker, logged
            f"delivery {delivery.id} references missing event {delivery.event_id}"
        )

    if not channel.active:
        # `NotificationChannel.active` (whole-branch review finding I2) --
        # the only per-channel kill switch this product exposes (an admin's
        # obvious containment action on discovering a channel pointed
        # somewhere it should not be), and, before this fix, consulted by no
        # decision anywhere: `resolve_recipients`'s channel query and this
        # function's own channel load both omitted it entirely, so `PATCH
        # {"active": false}` flipped only the UI badge while deliveries kept
        # sending. Gated HERE, in the delivery path, rather than in
        # `routing.resolve_recipients` -- routing.py is out of scope for this
        # fix -- which is sufficient to make disabling a channel actually
        # stop outbound sends: a queued row against a now-disabled channel is
        # a TERMINAL, non-retryable outcome (`is_retryable` has no
        # "channel_disabled" entry in `_RETRYABLE_REASONS`, so `_apply_
        # outcome` always sends it straight to `dead`), never sent, so
        # disabling is immediate rather than eventually-consistent with
        # whatever the retry schedule happens to do next. `send_test`
        # deliberately does NOT consult this: an admin testing a channel's
        # configuration before re-enabling it is a legitimate use of a
        # currently-disabled channel, not a delivery.
        _apply_outcome(
            delivery,
            _AttemptOutcome(
                ok=False,
                status_code=None,
                error_reason="channel_disabled",
                error_excerpt="channel is disabled",
            ),
            now=datetime.now(UTC),
        )
        session.add(delivery)
        await session.flush()
        return

    if channel.kind == "webhook":
        outcome = await _send_webhook(
            session, delivery=delivery, channel=channel, event=event, vault=vault
        )
    else:
        outcome = await _send_email(
            session, delivery=delivery, channel=channel, event=event, vault=vault
        )

    _apply_outcome(delivery, outcome, now=datetime.now(UTC))
    session.add(delivery)
    await session.flush()


@dataclass(frozen=True)
class TestSendOutcome:
    """A test send's result, reduced to exactly the credential-free,
    response-body-free shape the API is allowed to return (spec §9's
    `TestSendResult`: `delivered`, a raw status code -- HTTP, or an SMTP
    reply code for a permanent rejection -- and the CATEGORY `error_reason`,
    never `error_excerpt`/a raw response body)."""

    delivered: bool
    status_code: int | None
    error: str | None


async def send_test(
    session: AsyncSession,
    *,
    org_id: UUID,
    channel: NotificationChannel,
    vault: VaultService,
    actor_id: UUID,
) -> TestSendOutcome:
    """Test-send `channel` against its REAL configured destination (spec §5,
    §9's `POST /channels/{id}/test`) -- through the IDENTICAL path a real
    delivery attempt for that channel kind uses, never a second, unguarded
    code path. "It's only a test" is exactly how an SSRF bypass ships (spec
    §12.6), so this function does not open a request or a socket of its own:
    a webhook test is dispatched through `_send_webhook`, which itself goes
    through `app.core.egress.post_json` -- the only outbound HTTP path in
    this codebase -- so a channel pointing at a private address is refused
    here exactly as it would be for a real delivery. An email test is
    dispatched through `_send_email`, which is deliberately NOT
    egress-guarded, for the identical, recorded reason `_send_email` itself
    is not (module docstring, "Do NOT route SMTP through this guard") -- an
    on-prem SMTP relay on the customer's own LAN is the normal deployment
    this product targets, and a test send must behave exactly like the real
    delivery it is testing, not a stricter or looser variant of it.

    `actor_id` (whole-branch review finding M7) is the NAMED USER who pressed
    the test-send button -- the API route's `ctx.user_id`, never `None` here.
    Passed straight through to `_send_webhook`/`_send_email` as `actor_type=
    "user", actor_id=actor_id`, so the credential-checkout audit record this
    test send triggers (if the channel holds one) attributes correctly to a
    real person, not to `("system", None)` -- correct for the worker's
    delivery path, wrong here: a hash-chained audit record that always says
    "system" for a checkout a human visibly caused is not a truthful record
    of a privileged action (invariant #4).

    Builds a throwaway, UNPERSISTED `DomainEvent`/`NotificationDelivery`
    pair -- neither is ever `session.add`-ed -- purely to hand
    `_send_webhook`/`_send_email` the shape they already expect. A test send
    writes no outbox event and no delivery row of its own: delivery
    ATTEMPTS are not audited (spec §8), and a test send is not a delivery
    attempt in the first place -- the caller (the API route) is responsible
    for auditing the TEST SEND ITSELF, once, per spec §5/§8.

    Any credential the channel holds is checked out (and, itself, audited --
    `_send_webhook`/`_send_email` already do this) for the duration of this
    one call and never returned here; only `delivered`/`status_code`/`error`
    -- never `error_excerpt`, which can carry a bounded but real response
    excerpt -- are handed back."""
    test_event = DomainEvent(
        id=uuid7(),
        org_id=org_id,
        event_type="notification.test_send",
        subject_type="notification_channel",
        subject_id=channel.id,
        payload={},
        actor_user_id=None,
        occurred_at=datetime.now(UTC),
    )
    test_recipient = (
        str(channel.config.get("url", ""))
        if channel.kind == "webhook"
        else str(channel.config.get("from_address", ""))
    )
    test_delivery = NotificationDelivery(
        id=uuid7(),
        org_id=org_id,
        event_id=test_event.id,
        channel_id=channel.id,
        recipient=test_recipient,
        status="pending",
        attempts=0,
    )

    if channel.kind == "webhook":
        outcome = await _send_webhook(
            session,
            delivery=test_delivery,
            channel=channel,
            event=test_event,
            vault=vault,
            actor_type="user",
            actor_id=actor_id,
        )
    else:
        outcome = await _send_email(
            session,
            delivery=test_delivery,
            channel=channel,
            event=test_event,
            vault=vault,
            actor_type="user",
            actor_id=actor_id,
        )

    return TestSendOutcome(
        delivered=outcome.ok, status_code=outcome.status_code, error=outcome.error_reason
    )
