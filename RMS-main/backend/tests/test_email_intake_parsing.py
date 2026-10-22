"""MIME parsing, HTML flattening, quote trimming, bounds, loop detection.

Pure -- no database, no fixtures. Every test builds a real RFC822 message
with the stdlib and asserts on the parse RESULT, never on an internal call.
"""

import time
from email.message import EmailMessage

import pytest

from app.modules.email_intake import parsing
from app.modules.email_intake.parsing import (
    MAX_ATTACHMENT_COUNT,
    MAX_MESSAGE_BYTES,
    html_to_text,
    parse_message,
    trim_quoted_history,
)
from app.modules.service_desk import threads

MAILBOX = "helpdesk@acme.test"


def _raw(
    *,
    body: str = "Printer is on fire.",
    subject: str = "Help",
    sender: str = "alice@acme.test",
    to: str = MAILBOX,
    headers: dict[str, str] | None = None,
    message_id: str | None = "<abc123@acme.test>",
) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to
    if message_id is not None:
        message["Message-ID"] = message_id
    for key, value in (headers or {}).items():
        message[key] = value
    message.set_content(body)
    return message.as_bytes()


def _literal(headers: str, body: str = "Printer is on fire.") -> bytes:
    """Raw bytes, byte for byte. Some cases here need a header the stdlib
    builder refuses to emit (a 5 000-character Message-ID, a NUL, an absurd
    Date) or must avoid its header folding entirely."""
    return (headers.rstrip("\r\n") + "\r\n\r\n" + body + "\r\n").encode()


def _html(markup: str) -> bytes:
    message = EmailMessage()
    message["Subject"] = "Help"
    message["From"] = "alice@acme.test"
    message["To"] = MAILBOX
    message["Message-ID"] = "<html1@acme.test>"
    message.set_content(markup, subtype="html")
    return message.as_bytes()


def test_a_plain_message_parses_to_its_text_body() -> None:
    parsed = parse_message(_raw(), mailbox_address=MAILBOX)
    assert parsed.refusal is None
    assert parsed.from_address == "alice@acme.test"
    assert parsed.subject == "Help"
    assert "Printer is on fire." in parsed.body_text
    assert parsed.message_id == "<abc123@acme.test>"


def test_a_message_with_no_message_id_gets_a_stable_synthesised_one() -> None:
    """Spec §2.2: it must still dedupe. Same bytes -> same id, so a
    redelivery of a Message-ID-less message still collides."""
    raw = _raw(message_id=None)
    first = parse_message(raw, mailbox_address=MAILBOX)
    second = parse_message(raw, mailbox_address=MAILBOX)
    assert first.message_id
    assert first.message_id == second.message_id
    other = parse_message(_raw(message_id=None, body="different"), mailbox_address=MAILBOX)
    assert other.message_id != first.message_id


def test_html_only_mail_is_stored_as_text_with_no_markup() -> None:
    """Spec §7 and §11.9. The portal and staff UI would otherwise render
    attacker-supplied markup from an unauthenticated source.

    The `alert(1)` / `color: red` assertions are the point: a tag-only strip
    passes every other assertion here (`<script>alert(1)</script>` becomes
    the visible text `alert(1)` -- no bracket, no "script"), so without them
    the requirement the flattener exists for is untested."""
    parsed = parse_message(
        _html(
            "<html><head><style>p { color: red }</style></head>"
            "<body><p>Printer <b>on fire</b></p>"
            "<script>alert(1)</script></body></html>"
        ),
        mailbox_address=MAILBOX,
    )
    assert parsed.refusal is None
    assert "<" not in parsed.body_text and ">" not in parsed.body_text
    assert "script" not in parsed.body_text.lower()
    # The script and style BODIES, not merely their tags.
    assert "alert(1)" not in parsed.body_text
    assert "color: red" not in parsed.body_text
    assert "Printer" in parsed.body_text and "on fire" in parsed.body_text


@pytest.mark.parametrize(
    ("markup", "forbidden"),
    [
        ("&lt;script&gt;alert('xss')&lt;/script&gt;", "alert"),
        ("&#60;script&#62;alert(1)&#60;/script&#62;", "alert"),
        ('<img alt="a > b" src=x onerror=alert(1)>tail', "onerror"),
        ("<p>x</p><script>alert('pwn')", "alert"),
        # A bracket that only appears once unescaping has run, outside any
        # tag: nothing downstream of the strip would remove it.
        ("<p>3 &gt; 2</p><script>alert(1)</script>", "alert"),
    ],
)
def test_no_markup_survives_html_flattening(markup: str, forbidden: str) -> None:
    """The declared property, stated plainly: no `<`, no `>`, and no script
    or style text reaches `body_text` -- whatever the encoding, whatever the
    quoting, closed tag or not. Each input here defeated the previous
    flattener (entity-encoded markup was re-materialised by an unescape that
    ran AFTER the strip; a `>` inside a quoted attribute ended the "tag"
    early; an unclosed `<script>` was never stripped at all)."""
    parsed = parse_message(_html(markup), mailbox_address=MAILBOX)
    assert "<" not in parsed.body_text, parsed.body_text
    assert ">" not in parsed.body_text, parsed.body_text
    assert "script" not in parsed.body_text.lower(), parsed.body_text
    assert forbidden not in parsed.body_text.lower(), parsed.body_text


def test_html_flattening_is_linear_in_the_size_of_its_input() -> None:
    """Unauthenticated CPU exhaustion. The regex this replaced re-scanned to
    end-of-input for every unmatched `<script` opener: 15.3 s measured on
    180 KiB, ~57 s on one 352 KB message, and `MAX_MESSAGE_BYTES` (25 MiB)
    was checked only AFTER body extraction, so the flattener saw the lot.
    Anyone who can mail the published helpdesk address could stall the
    mailbox (§11.10). Quadratic behaviour on this input would take ~30
    minutes; the bound below is loose enough not to be flaky."""
    source = "<script>x" * (2 * 1024 * 1024 // 9)
    started = time.perf_counter()
    flattened = html_to_text(source)
    elapsed = time.perf_counter() - started
    assert "script" not in flattened.lower()
    assert elapsed < 5.0, f"html_to_text took {elapsed:.1f}s on {len(source)} characters"


def test_a_large_hostile_html_message_parses_promptly_end_to_end() -> None:
    """The same property through `parse_message`, which is where the size
    check has to precede body extraction for it to hold."""
    started = time.perf_counter()
    parsed = parse_message(_html("<script>x" * (1024 * 1024 // 9)), mailbox_address=MAILBOX)
    elapsed = time.perf_counter() - started
    assert "script" not in parsed.body_text.lower()
    assert elapsed < 5.0, f"parse_message took {elapsed:.1f}s on a 1 MiB HTML part"


def test_plain_text_bodies_are_not_run_through_the_html_flattener() -> None:
    """Flattening is for `text/html` parts only. `if a < b and b > c` is not
    markup, and a flattener applied to it silently deletes half the line."""
    parsed = parse_message(
        _raw(body="if a < b and b > c then\nkeep <this>"), mailbox_address=MAILBOX
    )
    assert parsed.refusal is None
    assert "if a < b and b > c then" in parsed.body_text
    assert "keep <this>" in parsed.body_text


def test_the_plain_part_is_preferred_over_the_html_part() -> None:
    message = EmailMessage()
    message["Subject"] = "Help"
    message["From"] = "alice@acme.test"
    message["To"] = MAILBOX
    message["Message-ID"] = "<multi1@acme.test>"
    message.set_content("plain version")
    message.add_alternative("<p>html version</p>", subtype="html")
    parsed = parse_message(message.as_bytes(), mailbox_address=MAILBOX)
    assert "plain version" in parsed.body_text
    assert "html version" not in parsed.body_text


def test_quoted_history_is_trimmed() -> None:
    body = (
        "Yes, still broken.\n"
        "\n"
        "On Mon, 1 Jan 2026 at 10:00, Helpdesk <helpdesk@acme.test> wrote:\n"
        "> Have you tried turning it off and on again?\n"
        "> -- Helpdesk\n"
    )
    trimmed = trim_quoted_history(body)
    assert "still broken" in trimmed
    assert "turning it off" not in trimmed


def test_trimming_never_returns_an_empty_body() -> None:
    """Spec §7: the heuristic is best-effort and must NEVER drop the whole
    body. A reply that is entirely quote-shaped is still a reply."""
    body = "> everything I say looks like a quote\n> second line\n"
    assert trim_quoted_history(body).strip() != ""


def test_attachments_are_extracted_with_their_filenames() -> None:
    message = EmailMessage()
    message["Subject"] = "Screenshot"
    message["From"] = "alice@acme.test"
    message["To"] = MAILBOX
    message["Message-ID"] = "<att1@acme.test>"
    message.set_content("see attached")
    message.add_attachment(b"\x89PNG\r\n", maintype="image", subtype="png", filename="shot.png")
    parsed = parse_message(message.as_bytes(), mailbox_address=MAILBOX)
    assert parsed.refusal is None
    assert [a.filename for a in parsed.attachments] == ["shot.png"]
    assert parsed.attachments[0].content.startswith(b"\x89PNG")


def test_an_oversized_message_is_refused_parse_but_keeps_its_text() -> None:
    """Spec §7: size bounds are enforced HERE, because email bypasses the
    HTTP body caps entirely. The person is not left silent -- the text body
    still reaches the ticket."""
    message = EmailMessage()
    message["Subject"] = "Big"
    message["From"] = "alice@acme.test"
    message["To"] = MAILBOX
    message["Message-ID"] = "<big1@acme.test>"
    message.set_content("the actual words")
    message.add_attachment(
        b"x" * (MAX_MESSAGE_BYTES + 1), maintype="application", subtype="octet-stream",
        filename="huge.bin",
    )
    parsed = parse_message(message.as_bytes(), mailbox_address=MAILBOX)
    assert parsed.refusal == "refused_parse"
    assert "the actual words" in parsed.body_text
    assert parsed.attachments == ()


def test_too_many_attachments_is_refused_parse() -> None:
    message = EmailMessage()
    message["Subject"] = "Many"
    message["From"] = "alice@acme.test"
    message["To"] = MAILBOX
    message["Message-ID"] = "<many1@acme.test>"
    message.set_content("body")
    for i in range(MAX_ATTACHMENT_COUNT + 1):
        message.add_attachment(
            b"x", maintype="application", subtype="octet-stream", filename=f"f{i}.bin"
        )
    parsed = parse_message(message.as_bytes(), mailbox_address=MAILBOX)
    assert parsed.refusal == "refused_parse"


def test_garbage_bytes_are_refused_parse_without_raising() -> None:
    """Spec §7 and §11.10: one bad message must not stop a mailbox."""
    parsed = parse_message(b"\x00\xff not a message at all", mailbox_address=MAILBOX)
    assert parsed.refusal == "refused_parse"


@pytest.mark.parametrize(
    "headers",
    [
        {"Auto-Submitted": "auto-replied"},
        {"Auto-Submitted": "auto-generated"},
        {"Precedence": "bulk"},
        {"Precedence": "list"},
        {"Precedence": "junk"},
        {"Precedence": "auto_reply"},
        {"X-Auto-Response-Suppress": "All"},
        {"Return-Path": "<>"},
    ],
)
def test_auto_reply_and_bounce_headers_are_refused_loop(headers: dict[str, str]) -> None:
    """Spec §6, §11.5. One case per refusal header. The failure this
    prevents: we notify, their out-of-office replies, the reply becomes a
    comment, the comment notifies them again."""
    parsed = parse_message(_raw(headers=headers), mailbox_address=MAILBOX)
    assert parsed.refusal == "refused_loop", headers


def test_auto_submitted_no_is_not_a_loop() -> None:
    """RFC 3834: `Auto-Submitted: no` is the explicit 'this is a human'
    value. Refusing it would silently drop legitimate mail from clients that
    set it."""
    parsed = parse_message(
        _raw(headers={"Auto-Submitted": "no"}), mailbox_address=MAILBOX
    )
    assert parsed.refusal is None


def test_a_delivery_status_report_is_refused_loop() -> None:
    """Spec §6, §11.6: a bounce must not become a comment."""
    message = EmailMessage()
    message["Subject"] = "Undelivered Mail Returned to Sender"
    message["From"] = "MAILER-DAEMON@acme.test"
    message["To"] = MAILBOX
    message["Message-ID"] = "<dsn1@acme.test>"
    message.make_mixed()
    message.set_type("multipart/report")
    message.set_param("report-type", "delivery-status")
    parsed = parse_message(message.as_bytes(), mailbox_address=MAILBOX)
    assert parsed.refusal == "refused_loop"


def test_mail_from_the_mailbox_itself_is_refused_loop() -> None:
    """Spec §6: the tightest loop of all."""
    parsed = parse_message(_raw(sender=MAILBOX), mailbox_address=MAILBOX)
    assert parsed.refusal == "refused_loop"


def test_recipients_include_to_cc_and_delivered_to() -> None:
    """Spec §5.2: the reply token may arrive on any of the three."""
    parsed = parse_message(
        _raw(
            to="helpdesk+t-TOKEN@acme.test",
            headers={"Cc": "someone@acme.test", "Delivered-To": "other@acme.test"},
        ),
        mailbox_address=MAILBOX,
    )
    assert "helpdesk+t-token@acme.test" in [r.lower() for r in parsed.recipients]
    assert "someone@acme.test" in parsed.recipients
    assert "other@acme.test" in parsed.recipients


# ---------------------------------------------------------------------------
# Quote trimming -- one test per rule, so neither is silently deletable
# ---------------------------------------------------------------------------


def test_a_leading_quote_block_alone_is_enough_to_trim() -> None:
    body = "Yes, still broken.\n> Have you tried turning it off and on again?\n"
    trimmed = trim_quoted_history(body)
    assert "still broken" in trimmed
    assert "turning it off" not in trimmed


@pytest.mark.parametrize(
    "separator",
    [
        "On Mon, 1 Jan 2026 at 10:00, Helpdesk <helpdesk@acme.test> wrote:",
        "-----Original Message-----",
        "From: Helpdesk <helpdesk@acme.test>",
    ],
)
def test_each_client_separator_alone_is_enough_to_trim(separator: str) -> None:
    """One case per separator pattern. With a single fixture carrying both a
    separator and `>` lines, either rule could be deleted unnoticed."""
    trimmed = trim_quoted_history(f"Yes, still broken.\n{separator}\nquoted history here\n")
    assert "still broken" in trimmed
    assert "quoted history here" not in trimmed


# ---------------------------------------------------------------------------
# Size bounds -- each tripped INDEPENDENTLY of the others
# ---------------------------------------------------------------------------


def test_the_per_message_byte_bound_is_tripped_by_a_body_with_no_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolates `MAX_MESSAGE_BYTES`: no attachment exists, so no other bound
    can account for the refusal. Spec §7: the text still reaches the ticket."""
    monkeypatch.setattr(parsing, "MAX_MESSAGE_BYTES", 2048)
    parsed = parse_message(
        _raw(body="the actual words\n" + "y" * 8192), mailbox_address=MAILBOX
    )
    assert parsed.refusal == "refused_parse"
    assert "the actual words" in parsed.body_text
    assert parsed.attachments == ()


def test_the_per_part_bound_is_tripped_by_one_attachment_under_the_message_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolates the imported `MAX_ATTACHMENT_BYTES`: the whole message is
    far under `MAX_MESSAGE_BYTES` and there is a single attachment, so only
    the per-part bound can refuse it."""
    monkeypatch.setattr(parsing, "MAX_ATTACHMENT_BYTES", 64)
    message = EmailMessage()
    message["Subject"] = "Big part"
    message["From"] = "alice@acme.test"
    message["To"] = MAILBOX
    message["Message-ID"] = "<part1@acme.test>"
    message.set_content("the actual words")
    message.add_attachment(
        b"x" * 256, maintype="application", subtype="octet-stream", filename="big.bin"
    )
    parsed = parse_message(message.as_bytes(), mailbox_address=MAILBOX)
    assert parsed.refusal == "refused_parse"
    assert "the actual words" in parsed.body_text
    assert parsed.attachments == ()


def test_the_per_part_bound_is_the_ticket_cores_constant_not_a_restatement() -> None:
    """Spec §7 names `MAX_ATTACHMENT_BYTES` as the per-part reference. If it
    were restated here, the inbound cap and the upload cap could drift."""
    assert parsing.MAX_ATTACHMENT_BYTES is threads.MAX_ATTACHMENT_BYTES


# ---------------------------------------------------------------------------
# Header truncation and normalisation (columns in `inbound_message`)
# ---------------------------------------------------------------------------


def test_an_over_long_subject_is_truncated_to_the_column_width() -> None:
    parsed = parse_message(
        _literal("From: alice@acme.test\r\nSubject: " + "s" * 4000), mailbox_address=MAILBOX
    )
    assert len(parsed.subject) == 512


def test_an_over_long_message_id_is_truncated_to_the_column_width() -> None:
    parsed = parse_message(
        _literal("From: alice@acme.test\r\nMessage-ID: <" + "z" * 5000 + ">"),
        mailbox_address=MAILBOX,
    )
    assert len(parsed.message_id) == 998


def test_an_over_long_from_address_is_bounded() -> None:
    """`inbound_message.from_address` is `String(320)`; 410 characters used
    to pass straight through toward it."""
    parsed = parse_message(
        _literal("From: " + "a" * 400 + "@acme.test"), mailbox_address=MAILBOX
    )
    assert len(parsed.from_address) <= 320
    assert parsed.refusal == "refused_parse"


def test_an_over_long_refusal_reason_is_bounded() -> None:
    """`inbound_message.reason` is `String(200)` and the reason echoes
    attacker-supplied header text."""
    parsed = parse_message(
        _literal("From: alice@acme.test\r\nAuto-Submitted: " + "q" * 900),
        mailbox_address=MAILBOX,
    )
    assert parsed.refusal == "refused_loop"
    assert len(parsed.refusal_reason) <= 200


def test_the_from_address_is_lower_cased() -> None:
    """Spec §5.1. Load-bearing: it is what makes §6's own-mailbox loop check
    case-insensitive, so `From: HelpDesk@ACME.test` cannot evade it."""
    parsed = parse_message(_raw(sender="Alice <ALICE@ACME.test>"), mailbox_address=MAILBOX)
    assert parsed.from_address == "alice@acme.test"


def test_mail_from_the_mailbox_in_a_different_case_is_still_refused_loop() -> None:
    parsed = parse_message(_raw(sender="HelpDesk@ACME.test"), mailbox_address=MAILBOX)
    assert parsed.refusal == "refused_loop"


def test_nul_bytes_do_not_survive_into_any_text_field() -> None:
    """PostgreSQL cannot store NUL in `text`/`varchar`. One attacker NUL
    would otherwise become an insert error in Task 7 -- one bad message
    stopping a mailbox (§11.10), routed around the no-raise guarantee."""
    parsed = parse_message(
        _literal("From: a\x00lice@acme.test\r\nSubject: he\x00llo", body="bo\x00dy"),
        mailbox_address=MAILBOX,
    )
    assert "\x00" not in parsed.subject
    assert "\x00" not in parsed.body_text
    assert "\x00" not in parsed.from_address
    assert "\x00" not in parsed.refusal_reason
    assert "hello" == parsed.subject


# ---------------------------------------------------------------------------
# Malformed structure (spec §7) and the ordering guarantee (spec §6)
# ---------------------------------------------------------------------------


def test_a_multipart_whose_boundary_never_appears_is_refused_parse() -> None:
    """Spec §7: a malformed MIME structure yields `refused_parse`. The stdlib
    records `StartBoundaryNotFoundDefect` on exactly this input; having a
    `From:` is not evidence that the message is well-formed."""
    parsed = parse_message(
        _literal(
            "From: alice@acme.test\r\nContent-Type: multipart/mixed; boundary=nope",
            body="no boundary anywhere",
        ),
        mailbox_address=MAILBOX,
    )
    assert parsed.refusal == "refused_parse"
    assert parsed.from_address == "alice@acme.test"


def test_a_from_header_that_is_not_an_address_is_refused_parse() -> None:
    """`From: Alice Smith` used to yield `from_address='alice smith'`, which
    was then handed to Task 7's sender resolution as if it were an address."""
    parsed = parse_message(_literal("From: Alice Smith"), mailbox_address=MAILBOX)
    assert parsed.refusal == "refused_parse"


def test_an_auto_reply_with_an_absurd_date_is_still_refused_loop() -> None:
    """The ordering guarantee, broken by a header rather than a body: under
    `policy.default` merely READING `Date: ... 999999999999 ...` raises
    `OverflowError`, which used to escape and reclassify an auto-responder
    as a parse failure -- the exact misclassification the ordering exists to
    prevent (§6, §7)."""
    parsed = parse_message(
        _literal(
            "From: ooo@acme.test\r\nAuto-Submitted: auto-replied\r\n"
            "Date: Mon, 1 Jan 999999999999 10:00:00 +0000"
        ),
        mailbox_address=MAILBOX,
    )
    assert parsed.refusal == "refused_loop"
    assert parsed.from_address == "ooo@acme.test"


def test_an_unparseable_date_falls_back_instead_of_refusing_the_message() -> None:
    """Date extraction is total: a nonsense `Date:` costs the message its
    timestamp, not its ticket."""
    parsed = parse_message(
        _literal("From: alice@acme.test\r\nDate: Mon, 32 Feb 2026 10:00:00 +0000"),
        mailbox_address=MAILBOX,
    )
    assert parsed.refusal is None
    assert parsed.received_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Evasion and DSN breadth (spec §6, §11.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        "Precedence: normal\r\nPrecedence: bulk",
        "Return-Path: <alice@acme.test>\r\nReturn-Path: <>",
        "Auto-Submitted: no\r\nAuto-Submitted: auto-replied",
    ],
)
def test_a_benign_first_header_does_not_hide_a_later_loop_header(headers: str) -> None:
    """Only the FIRST header of each name used to be consulted, so prepending
    a benign one evaded two §6 conditions outright."""
    parsed = parse_message(
        _literal("From: alice@acme.test\r\n" + headers), mailbox_address=MAILBOX
    )
    assert parsed.refusal == "refused_loop"


def test_a_bounce_with_a_delivery_status_part_but_no_report_type_is_refused_loop() -> None:
    """Spec §6 "or the message is otherwise a DSN", §11.6. Plenty of real
    bounces carry the machine-readable part without declaring `report-type`
    on the container."""
    raw = (
        b"From: MAILER-DAEMON@acme.test\r\nTo: helpdesk@acme.test\r\n"
        b"Subject: Undelivered Mail\r\nMessage-ID: <dsn2@acme.test>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
        b"--B\r\nContent-Type: message/delivery-status\r\n\r\n"
        b"Reporting-MTA: dns; acme.test\r\n\r\n"
        b"Final-Recipient: rfc822; bob@x.test\r\nAction: failed\r\n"
        b"--B--\r\n"
    )
    parsed = parse_message(raw, mailbox_address=MAILBOX)
    assert parsed.refusal == "refused_loop"


def test_a_bounce_identified_by_x_failed_recipients_is_refused_loop() -> None:
    """The Gmail-style bounce: no `multipart/report`, no delivery-status
    part, just the header."""
    parsed = parse_message(
        _literal(
            "From: mailer-daemon@googlemail.com\r\nTo: helpdesk@acme.test\r\n"
            "X-Failed-Recipients: bob@x.test",
            body="Delivery to the following recipient failed permanently.",
        ),
        mailbox_address=MAILBOX,
    )
    assert parsed.refusal == "refused_loop"


# ---------------------------------------------------------------------------
# Per-part failures do not discard the message (spec §7)
# ---------------------------------------------------------------------------


def test_a_forwarded_message_keeps_its_headers_and_body() -> None:
    """Forwarding a thread to the helpdesk is one of the most common things
    a real user does. The `message/rfc822` part used to raise inside
    attachment extraction and unwind to a total-loss result: empty
    `from_address`, empty `subject`, empty `body_text`."""
    inner = EmailMessage()
    inner["Subject"] = "Original subject"
    inner["From"] = "bob@vendor.test"
    inner["To"] = "alice@acme.test"
    inner.set_content("the original thread")

    message = EmailMessage()
    message["Subject"] = "Fwd: Original subject"
    message["From"] = "alice@acme.test"
    message["To"] = MAILBOX
    message["Message-ID"] = "<fwd1@acme.test>"
    message.set_content("Can you look at this please")
    message.add_attachment(inner, filename="forwarded.eml")

    parsed = parse_message(message.as_bytes(), mailbox_address=MAILBOX)
    assert parsed.from_address == "alice@acme.test"
    assert parsed.subject == "Fwd: Original subject"
    assert "Can you look at this please" in parsed.body_text


def test_an_unknown_charset_falls_back_to_a_lossy_decode() -> None:
    """`charset=nope-99` raises `LookupError` from `get_content()`. Refusing
    the whole message for that discarded headers that had parsed perfectly
    well and left the requester silent."""
    parsed = parse_message(
        _literal(
            "From: alice@acme.test\r\nTo: helpdesk@acme.test\r\n"
            "Subject: Printer\r\nMessage-ID: <cs1@acme.test>\r\n"
            "Content-Type: text/plain; charset=nope-99",
            body="Printer is on fire.",
        ),
        mailbox_address=MAILBOX,
    )
    assert parsed.from_address == "alice@acme.test"
    assert parsed.subject == "Printer"
    assert "Printer is on fire." in parsed.body_text
