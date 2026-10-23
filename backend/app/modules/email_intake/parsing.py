"""Pure MIME parsing for email intake (spec §6, §7).

**No database. No dependency on `mailboxes.py` or `models.py`.** This module
turns raw RFC822 bytes into a `ParsedMessage`; nothing here reaches a
session, the vault or a table. That is what makes it usable in a poller
worker that must never let one bad message stop a mailbox.

**`refusal` is a FIELD of the result, never an exception.** Spec §2.2
requires every fetched message to get an `inbound_message` row; an exception
path invites a caller to drop one on the floor. `parse_message` therefore
never raises -- `_parse` below is wrapped in a blanket `try/except Exception`
whose fallback still returns a `ParsedMessage`, with a synthesised
`message_id` so the row it eventually becomes still participates in the
idempotency unique constraint (spec §4).

**Every header helper is TOTAL: it returns a default rather than raising.**
Under `policy.default` even reading a header parses it, so `msg.get("Date")`
on `Date: ... 999999999999 ...` raises `OverflowError` -- which used to
escape and misclassify an auto-reply as `refused_parse`. Nothing between
`message_from_bytes` and the loop-detection call may fail.

**Loop detection runs BEFORE body extraction and short-circuits.** A
malformed auto-reply (garbage MIME structure, undecodable body) must still
be classified `refused_loop`, not `refused_parse` -- §6's failure mode (a
notification loop) is worse than a missing body, and the ordering is what
keeps a broken auto-responder from masquerading as a parse failure.

**Every size bound is checked BEFORE the expensive work it bounds.** The
per-message byte bound is evaluated on `len(raw)` before body extraction, so
attacker HTML never reaches the flattener at full message size, and the
flattener itself is linear in its input (see `html_to_text`). An earlier
quadratic form burned ~57 s of CPU on one 352 KB message.

**A bounds violation (oversized message, too many attachments, an oversized
part) keeps `body_text` and returns `attachments=()`.** Spec §7 is explicit
that the person is not left silent: their words still reach the ticket even
when the message is refused for size. Task 5 relies on this.

**A part that cannot be decoded does not discard the message.** A forwarded
thread (`message/rfc822`) or an unknown charset yields `refused_parse` with
the headers and whatever body was decodable intact -- forwarding a thread to
the helpdesk is one of the most common things a real user does, and the
outer headers are always available.

The per-part attachment size bound is imported from
`app.modules.service_desk.threads.MAX_ATTACHMENT_BYTES` rather than restated
here, so the ticket core's upload cap and this module's inbound cap can never
drift apart.
"""

from __future__ import annotations

import email
import email.errors
import email.policy
import email.utils
import hashlib
import html
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage, Message, MIMEPart

from app.modules.service_desk.threads import MAX_ATTACHMENT_BYTES

# Spec §7: "email bypasses the HTTP body caps entirely" -- these bounds are
# this module's own, enforced on the raw fetched bytes, independent of
# whatever the HTTP layer caps elsewhere.
MAX_MESSAGE_BYTES: int = 25 * 1024 * 1024
MAX_ATTACHMENT_COUNT: int = 20

_SUBJECT_MAX = 512
_MESSAGE_ID_MAX = 998
# `inbound_message.from_address` is `String(320)` (the RFC 5321 maximum) and
# `inbound_message.reason` is `String(200)`. An over-long attacker-supplied
# header must not become an insert error in Task 7.
_FROM_ADDRESS_MAX = 320
_REFUSAL_REASON_MAX = 200
# Belt-and-braces bound on the text handed to the flattener and the quote
# trimmer. The flattener is linear, so this is not what makes it safe; it
# bounds what we are willing to persist, and it guarantees that oversized
# input never reaches the flattener even if the raw-size check is bypassed.
_MAX_BODY_CHARS = 1024 * 1024


@dataclass(frozen=True)
class ParsedAttachment:
    filename: str
    content: bytes


@dataclass(frozen=True)
class ParsedMessage:
    message_id: str
    from_address: str
    subject: str
    recipients: tuple[str, ...]
    body_text: str
    attachments: tuple[ParsedAttachment, ...]
    received_at: datetime
    refusal: str | None
    # The reason behind `refusal` -- always a str (never None) so a caller
    # can log/store it unconditionally. Empty for an accepted message.
    refusal_reason: str = field(default="")


# ---------------------------------------------------------------------------
# Text hygiene
# ---------------------------------------------------------------------------

# PostgreSQL cannot store NUL in `text`/`varchar` -- psycopg raises
# "A string literal cannot contain NUL (0x00) characters". One attacker NUL
# would otherwise turn into an insert failure in Task 7, i.e. one bad message
# stopping a mailbox (§11.10) routed around this module's no-raise guarantee.
# The other C0 controls are stripped for the same class of reason (they are
# never meaningful in a subject, address or flattened body).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(value: str) -> str:
    """Remove NUL and the other non-printable C0 controls, keeping `\\t`,
    `\\n` and `\\r`."""
    return _CONTROL_CHARS_RE.sub("", value)


# ---------------------------------------------------------------------------
# HTML flattening (spec §7, §11.9)
# ---------------------------------------------------------------------------

# Elements whose CONTENT must go, not just their tags -- a naive tag strip
# leaves the script body behind as visible text.
_RAW_TEXT_ELEMENTS = {"script", "style"}
_CLOSE_TAG_RES = {
    name: re.compile(rf"</\s*{name}", re.IGNORECASE) for name in _RAW_TEXT_ELEMENTS
}
_OPEN_TAG_RE = re.compile(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9]*)")
_INLINE_WS_RE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

# `html.unescape` can turn text back into markup (`&lt;script&gt;`), and a
# doubly-encoded entity needs more than one round. The strip/unescape cycle
# is therefore run to a fixed point, bounded so a pathological input cannot
# spin; whatever the loop leaves behind, the final bracket sweep guarantees
# the declared property.
_MAX_FLATTEN_PASSES = 8


def _skip_tag(source: str, start: int) -> int:
    """Index just past the tag beginning at `start` (a `<`).

    Quote-aware: a `>` inside a quoted attribute value does not end the tag,
    so `<img alt="a > b" onerror=...>` is consumed whole rather than letting
    its tail escape as text. An unterminated tag consumes to end of input.
    Linear -- the scan never revisits a character.
    """
    if source.startswith("<!--", start):
        end = source.find("-->", start + 4)
        return len(source) if end < 0 else end + 3
    index = start + 1
    length = len(source)
    quote = ""
    while index < length:
        char = source[index]
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == ">":
            return index + 1
        index += 1
    return length


def _strip_markup(source: str) -> str:
    """Replace every tag with a space, dropping `<script>`/`<style>` content.

    One left-to-right pass. `str.find` jumps between `<` characters and
    `_skip_tag` only walks inside a tag, so total work is O(len(source)) --
    the property F1 exists to hold. An unclosed `<script>` swallows the rest
    of the input rather than being left as visible text.
    """
    out: list[str] = []
    pos = 0
    length = len(source)
    while pos < length:
        start = source.find("<", pos)
        if start < 0:
            out.append(source[pos:])
            break
        out.append(source[pos:start])
        after_tag = _skip_tag(source, start)
        opening = _OPEN_TAG_RE.match(source, start)
        if opening is not None and not opening.group(1):
            name = opening.group(2).lower()
            if name in _RAW_TEXT_ELEMENTS:
                close = _CLOSE_TAG_RES[name].search(source, after_tag)
                after_tag = len(source) if close is None else _skip_tag(source, close.start())
        out.append(" ")
        pos = after_tag
    return "".join(out)


def html_to_text(html_content: str) -> str:
    """Flatten HTML to plain text. HTML is never stored or rendered as HTML
    (spec §7) -- the portal and staff UI would otherwise render
    attacker-supplied markup from an unauthenticated source.

    Guarantees, for any input: no `<` and no `>` survive, and no `<script>`
    or `<style>` text survives (closed or not). Runs in time linear in the
    length of the input.
    """
    current = html_content
    for _ in range(_MAX_FLATTEN_PASSES):
        unescaped = html.unescape(_strip_markup(current))
        if unescaped == current:
            break
        current = unescaped
    # Unconditional: whatever the loop left, a bracket never reaches storage.
    if "<" in current or ">" in current:
        current = current.replace("<", " ").replace(">", " ")
    lines = [_INLINE_WS_RE.sub(" ", line).strip() for line in current.splitlines()]
    collapsed = "\n".join(line for line in lines if line)
    return _BLANK_LINES_RE.sub("\n\n", collapsed).strip()


# ---------------------------------------------------------------------------
# Quote trimming (spec §7)
# ---------------------------------------------------------------------------

_QUOTE_SEPARATOR_PATTERNS = (
    re.compile(r"^on .{1,300}\bwrote:\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*original message\s*-{2,}$", re.IGNORECASE),
    re.compile(r"^from:\s*.+$", re.IGNORECASE),
)


def trim_quoted_history(body: str) -> str:
    """Cut at the first quoted-history marker (a leading `>` block or a
    common client separator). Best-effort: if trimming would leave the body
    blank, the untrimmed body is returned instead -- a reply that is
    entirely quote-shaped is still a reply (spec §7)."""
    lines = body.splitlines(keepends=True)
    cut_index: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(">"):
            cut_index = index
            break
        if any(pattern.match(stripped) for pattern in _QUOTE_SEPARATOR_PATTERNS):
            cut_index = index
            break

    if cut_index is None:
        return body

    trimmed = "".join(lines[:cut_index])
    if trimmed.strip():
        return trimmed
    return body


# ---------------------------------------------------------------------------
# Total header access
# ---------------------------------------------------------------------------


def _header_values(msg: EmailMessage, name: str) -> list[str]:
    """Every value of `name`, as plain strings. Total.

    `get_all`, not `get`: consulting only the first `Precedence:` or
    `Return-Path:` lets an attacker evade two §6 refusal conditions by
    prepending a benign header of the same name.

    Under `policy.default` the header value is parsed on access, so this can
    raise (`OverflowError` on an absurd `Date:`); the exception is contained
    here so that nothing between parsing and loop detection can fail.
    """
    try:
        raw_values = msg.get_all(name)
    except Exception:  # noqa: BLE001 -- header access must be total.
        return []
    values: list[str] = []
    for value in raw_values or []:
        try:
            values.append(str(value))
        except Exception:  # noqa: BLE001 -- one bad header, not a bad message.
            continue
    return values


def _first_header(msg: EmailMessage, name: str) -> str | None:
    values = _header_values(msg, name)
    return values[0] if values else None


def _walk(msg: EmailMessage) -> Iterator[EmailMessage]:
    """Every part, defensively -- `walk` itself can trip on a malformed
    structure. Total."""
    try:
        yield from msg.walk()
    except Exception:  # noqa: BLE001 -- a malformed tree is not an exception path.
        return


# ---------------------------------------------------------------------------
# Loop / bounce detection (spec §6)
# ---------------------------------------------------------------------------

_LOOP_PRECEDENCE_VALUES = {"bulk", "list", "junk", "auto_reply"}
# A DSN's machine-readable part. Present in every RFC 3464 bounce, with or
# without the `report-type` parameter on the outer `multipart/report`.
_DSN_PART_TYPES = {"message/delivery-status", "message/feedback-report"}


def _content_type(part: MIMEPart) -> str:
    try:
        return str(part.get_content_type()).lower()
    except Exception:  # noqa: BLE001 -- total.
        return ""


def _detect_loop(msg: EmailMessage, *, from_address: str, mailbox_address: str) -> str | None:
    """Returns a reason string if the message must be refused as a loop
    risk, else None. Every check here reads headers only -- it must run
    before body extraction can fail on a malformed part (spec §7's ordering
    requirement: a malformed auto-reply is still `refused_loop`).

    §6's other arm of the last condition -- "the org's own notification
    sender" -- is NOT evaluated here. A pure parser cannot look up the org's
    notification channel config, and handing it a database session to do so
    would be the wrong seam; that check belongs to Task 5 (`intake.py`),
    which has one.
    """
    for auto_submitted in _header_values(msg, "Auto-Submitted"):
        if auto_submitted.strip().lower() != "no":
            return f"Auto-Submitted: {auto_submitted}"

    for precedence in _header_values(msg, "Precedence"):
        if precedence.strip().lower() in _LOOP_PRECEDENCE_VALUES:
            return f"Precedence: {precedence}"

    if _header_values(msg, "X-Auto-Response-Suppress"):
        return "X-Auto-Response-Suppress header present"

    for return_path in _header_values(msg, "Return-Path"):
        if return_path.strip() == "<>":
            return "Return-Path: <> (null return path -- a bounce envelope)"

    if _content_type(msg) == "multipart/report":
        try:
            report_type = str(msg.get_param("report-type", "") or "").strip().lower()
        except Exception:  # noqa: BLE001 -- total.
            report_type = ""
        if report_type == "delivery-status":
            return "multipart/report; report-type=delivery-status (a DSN)"

    # "...or the message is otherwise a DSN" (§6). Two signals the
    # `report-type` parameter misses entirely: a bounce that carries the
    # delivery-status part without declaring the parameter, and the
    # Gmail-style bounce identified only by `X-Failed-Recipients`.
    for part in _walk(msg):
        if _content_type(part) in _DSN_PART_TYPES:
            return f"{_content_type(part)} part present (a DSN)"

    if _header_values(msg, "X-Failed-Recipients"):
        return "X-Failed-Recipients header present (a DSN)"

    if from_address and mailbox_address and from_address == mailbox_address:
        return "From address matches the mailbox's own address"

    return None


# ---------------------------------------------------------------------------
# Malformed-structure detection (spec §7)
# ---------------------------------------------------------------------------

# The stdlib records these rather than raising, and they are exactly §7's
# "malformed MIME structure": a boundary that never appears, a multipart that
# never closes, a multipart with no parts, headers that never end. Without
# consulting them a malformed message that happens to carry a `From:` is
# accepted and becomes a ticket with an empty description.
_STRUCTURAL_DEFECTS: tuple[type[email.errors.MessageDefect], ...] = (
    email.errors.StartBoundaryNotFoundDefect,
    email.errors.CloseBoundaryNotFoundDefect,
    email.errors.MultipartInvariantViolationDefect,
    email.errors.MissingHeaderBodySeparatorDefect,
)


def _detect_structural_defect(msg: EmailMessage) -> str | None:
    """A reason string if the MIME structure is broken, else None. Walks
    parts, not just the root -- the defect is usually recorded on the
    container, but a truncated inner part records it on itself."""
    for part in _walk(msg):
        try:
            defects = list(part.defects)
        except Exception:  # noqa: BLE001 -- total.
            continue
        for defect in defects:
            if isinstance(defect, _STRUCTURAL_DEFECTS):
                return f"malformed MIME structure: {type(defect).__name__}"
    return None


# ---------------------------------------------------------------------------
# Header extraction helpers (all total)
# ---------------------------------------------------------------------------

# Deliberately loose -- this is not address validation, it is "is this
# address-shaped at all". `From: Alice Smith` parses to `alice smith`, which
# is not an address and must not reach Task 7's sender resolution.
_ADDRESS_RE = re.compile(r"^[^\s@<>,;:\\\"]+@[^\s@<>,;:\\\"]+$")


def _is_address_shaped(address: str) -> bool:
    return bool(_ADDRESS_RE.match(address))


def _synthesised_message_id(raw: bytes) -> str:
    # Deterministic over the raw bytes -- a redelivery of the same
    # Message-ID-less message synthesises the same id and collides on the
    # `(org_id, message_id)` unique constraint (spec §2.2, §4).
    digest = hashlib.sha256(raw).hexdigest()
    return f"<synthesised-{digest}@email-intake.invalid>"[:_MESSAGE_ID_MAX]


def _extract_message_id(msg: EmailMessage, raw: bytes) -> str:
    header = _first_header(msg, "Message-ID")
    if header is not None:
        candidate = _clean(header).strip()
        if candidate:
            return candidate[:_MESSAGE_ID_MAX]
    return _synthesised_message_id(raw)


def _extract_from_address(msg: EmailMessage) -> str:
    header = _first_header(msg, "From")
    if header is None:
        return ""
    try:
        _, address = email.utils.parseaddr(header)
    except Exception:  # noqa: BLE001 -- total.
        return ""
    # Lower-cased per §5.1 -- this is what makes §6's own-mailbox loop check
    # case-insensitive, so `From: HelpDesk@ACME.test` cannot evade it.
    # Returned UNtruncated so the caller can tell "too long" from "malformed";
    # the caller bounds it to `_FROM_ADDRESS_MAX` before it reaches the result.
    return _clean(address).strip().lower()


def _extract_subject(msg: EmailMessage) -> str:
    header = _first_header(msg, "Subject")
    return _clean(header or "")[:_SUBJECT_MAX]


def _extract_recipients(msg: EmailMessage) -> tuple[str, ...]:
    """`To`, `Cc` and `Delivered-To` -- the reply token may arrive on any of
    the three (spec §5.2). Case is preserved; token matching is expected to
    compare case-insensitively."""
    seen: list[str] = []
    for header_name in ("To", "Cc", "Delivered-To"):
        values = _header_values(msg, header_name)
        try:
            pairs = email.utils.getaddresses(values)
        except Exception:  # noqa: BLE001 -- total.
            continue
        for _display_name, address in pairs:
            cleaned = _clean(address)[:_FROM_ADDRESS_MAX]
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
    return tuple(seen)


def _extract_received_at(msg: EmailMessage) -> datetime:
    """Total. `Date: Mon, 1 Jan 999999999999 10:00:00 +0000` raises
    `OverflowError` on header *access* under `policy.default`; a bad clock on
    an auto-responder must not be reclassified as a parse failure."""
    date_header = _first_header(msg, "Date")
    if date_header is not None:
        try:
            parsed_date = email.utils.parsedate_to_datetime(date_header)
        except Exception:  # noqa: BLE001 -- total; fall back to "now".
            parsed_date = None
        if parsed_date is not None:
            if parsed_date.tzinfo is None:
                parsed_date = parsed_date.replace(tzinfo=UTC)
            return parsed_date
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Body + attachment extraction (spec §7)
# ---------------------------------------------------------------------------


def _decode_part_text(part: MIMEPart) -> str | None:
    """A part's text, falling back to a lossy decode. None if even the raw
    payload is unavailable.

    An unknown charset (`charset=nope-99`) raises `LookupError` from
    `get_content()`. Refusing the whole message for that discards headers
    that parsed perfectly well, so we decode the bytes lossily instead --
    the requester's words still reach the ticket."""
    try:
        content = part.get_content()
    except Exception:  # noqa: BLE001 -- fall through to the lossy path.
        content = None
    if isinstance(content, str):
        return content
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001 -- total.
        payload = None
    if isinstance(payload, bytes | bytearray):
        return bytes(payload).decode("utf-8", errors="replace")
    if isinstance(content, Message):
        # A forwarded thread reached as the body part.
        try:
            return content.as_string()
        except Exception:  # noqa: BLE001 -- total.
            return None
    return None


def _extract_body_text(msg: EmailMessage) -> str:
    """Prefer the first `text/plain` part; flatten HTML if the message is
    HTML-only. `get_body` already implements the plain-over-html
    preference. Total -- an undecodable body is "" here, and the caller
    decides what that means.

    The content is capped at `_MAX_BODY_CHARS` BEFORE flattening: the
    flattener is linear, but oversized attacker HTML has no business
    reaching it at all."""
    try:
        body_part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:  # noqa: BLE001 -- total.
        body_part = None
    if body_part is None:
        return ""
    content = _decode_part_text(body_part)
    if content is None:
        return ""
    content = content[:_MAX_BODY_CHARS]
    # Flattening runs ONLY for `text/html`. A plain-text body containing
    # `if a < b and b > c` must survive intact (F15).
    if _content_type(body_part) == "text/html":
        content = html_to_text(content)
    return _clean(content)


def _extract_attachments(
    msg: EmailMessage,
) -> tuple[tuple[ParsedAttachment, ...], str | None, str | None]:
    """Returns (attachments, oversized_reason, undecodable_reason).

    The per-message byte bound is checked by the caller, before body
    extraction. What is left here is the per-message attachment count and
    the per-part bound (`MAX_ATTACHMENT_BYTES`, imported from the ticket
    core so the two can never drift). Attachment content is only decoded
    once we know the count is within bounds.
    """
    try:
        parts = list(msg.iter_attachments())
    except Exception:  # noqa: BLE001 -- total.
        return (), None, "attachment structure could not be walked"
    if len(parts) > MAX_ATTACHMENT_COUNT:
        return (), f"message exceeds the {MAX_ATTACHMENT_COUNT}-attachment count bound", None

    attachments: list[ParsedAttachment] = []
    for part in parts:
        try:
            content = part.get_content()
        except Exception:  # noqa: BLE001 -- handled below via the raw payload.
            content = None
        if isinstance(content, str):
            content_bytes = content.encode("utf-8", errors="replace")
        elif isinstance(content, bytes | bytearray):
            content_bytes = bytes(content)
        elif isinstance(content, Message):
            # A forwarded message (`message/rfc822`) -- an everyday Outlook
            # or Gmail forward. Serialise it back to bytes and treat it as
            # the attachment it is, rather than discarding the whole mail.
            try:
                content_bytes = content.as_bytes()
            except Exception:  # noqa: BLE001
                return (), None, "a forwarded message part could not be re-serialised"
        else:
            try:
                payload = part.get_payload(decode=True)
            except Exception:  # noqa: BLE001 -- total.
                payload = None
            if not isinstance(payload, bytes | bytearray):
                return (), None, "a part had no decodable content"
            content_bytes = bytes(payload)
        if len(content_bytes) > MAX_ATTACHMENT_BYTES:
            return (), "an attachment exceeds the per-part size bound", None
        filename = _clean(part.get_filename() or "") or "attachment"
        attachments.append(ParsedAttachment(filename=filename, content=content_bytes))
    return tuple(attachments), None, None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _fallback_result(raw: bytes, *, reason: str) -> ParsedMessage:
    """The message could not be interpreted at all. Still returns a row-able
    result with a synthesised, deterministic `message_id` -- spec §2.2's
    'every fetched message gets a row' applies even to garbage bytes."""
    return ParsedMessage(
        message_id=_synthesised_message_id(raw),
        from_address="",
        subject="",
        recipients=(),
        body_text="",
        attachments=(),
        received_at=datetime.now(UTC),
        refusal="refused_parse",
        refusal_reason=_clean(reason)[:_REFUSAL_REASON_MAX],
    )


def parse_message(raw: bytes, *, mailbox_address: str) -> ParsedMessage:
    """Parse raw RFC822 bytes into a `ParsedMessage`. NEVER raises -- a
    malformed message must not stop a mailbox (spec §7, §11.10)."""
    try:
        return _parse_message(raw, mailbox_address=mailbox_address)
    except Exception as exc:  # noqa: BLE001 -- spec §7: no exception may escape.
        return _fallback_result(raw, reason=f"unparseable message: {exc}")


def _result(
    *,
    message_id: str,
    from_address: str,
    subject: str,
    recipients: tuple[str, ...],
    body_text: str,
    attachments: tuple[ParsedAttachment, ...],
    received_at: datetime,
    refusal: str | None,
    refusal_reason: str,
) -> ParsedMessage:
    return ParsedMessage(
        message_id=message_id,
        from_address=from_address,
        subject=subject,
        recipients=recipients,
        body_text=body_text,
        attachments=attachments,
        received_at=received_at,
        refusal=refusal,
        refusal_reason=_clean(refusal_reason)[:_REFUSAL_REASON_MAX],
    )


def _parse_message(raw: bytes, *, mailbox_address: str) -> ParsedMessage:
    mailbox_lower = mailbox_address.strip().lower()
    msg: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)

    message_id = _extract_message_id(msg, raw)
    full_from_address = _extract_from_address(msg)
    # Bounded against `inbound_message.from_address`'s `String(320)`: an
    # over-long header must not become an insert error in Task 7.
    from_address = full_from_address[:_FROM_ADDRESS_MAX]

    # Loop detection runs BEFORE anything that can fail and short-circuits:
    # a malformed auto-reply is still classified `refused_loop`, not
    # `refused_parse` (spec §6, §7's ordering requirement). Everything above
    # is total by construction.
    loop_reason = _detect_loop(
        msg, from_address=full_from_address, mailbox_address=mailbox_lower
    )

    subject = _extract_subject(msg)
    recipients = _extract_recipients(msg)
    received_at = _extract_received_at(msg)

    if loop_reason is not None:
        return _result(
            message_id=message_id,
            from_address=from_address,
            subject=subject,
            recipients=recipients,
            body_text="",
            attachments=(),
            received_at=received_at,
            refusal="refused_loop",
            refusal_reason=loop_reason,
        )

    # §7: a malformed MIME structure yields `refused_parse`. "No `From:`" only
    # ever caught header-LESS input; a `From:` that is not address-shaped, or
    # a boundary that never appears, is just as malformed.
    malformed_reason: str | None = None
    if not full_from_address:
        malformed_reason = "message has no From address"
    elif len(full_from_address) > _FROM_ADDRESS_MAX:
        malformed_reason = f"From address exceeds {_FROM_ADDRESS_MAX} characters"
    elif not _is_address_shaped(full_from_address):
        malformed_reason = "From header is not an email address"
    else:
        malformed_reason = _detect_structural_defect(msg)

    # The per-message byte bound is checked HERE, before body extraction, so
    # attacker HTML never reaches the flattener at full message size (F1).
    oversized_reason: str | None = None
    if len(raw) > MAX_MESSAGE_BYTES:
        oversized_reason = "message exceeds the maximum message size"

    body_text = _extract_body_text(msg)
    if body_text:
        body_text = trim_quoted_history(body_text)

    attachments: tuple[ParsedAttachment, ...] = ()
    undecodable_reason: str | None = None
    if oversized_reason is None:
        attachments, oversized_reason, undecodable_reason = _extract_attachments(msg)

    # Spec §7: a bounds violation, a malformed structure or an undecodable
    # part keeps `body_text` and drops the attachments -- the person is not
    # left silent, and the outer headers we already have are never discarded.
    refusal_reason = malformed_reason or oversized_reason or undecodable_reason
    if refusal_reason is not None:
        return _result(
            message_id=message_id,
            from_address=from_address,
            subject=subject,
            recipients=recipients,
            body_text=body_text,
            attachments=(),
            received_at=received_at,
            refusal="refused_parse",
            refusal_reason=refusal_reason,
        )

    return _result(
        message_id=message_id,
        from_address=from_address,
        subject=subject,
        recipients=recipients,
        body_text=body_text,
        attachments=attachments,
        received_at=received_at,
        refusal=None,
        refusal_reason="",
    )
