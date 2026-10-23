"""Email intake: per-org inbound mailbox config and the inbound-message
disposition record.

docs/modules/email-intake/spec.md §2. Every column here must match
`backend/migrations/versions/0032_email_intake.py` exactly in type, length
and nullability -- `test_schema_drift.py` compares `Base.metadata` to the
live schema. The drift test does NOT compare defaults, so any Python-side
default below is load-bearing on its own.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid7_pk


def _utcnow() -> datetime:
    return datetime.now(UTC)


# Spec §2.2. `created` etc. are the seven dispositions; every fetched message
# gets exactly one row with one of these -- nothing is ever silently dropped,
# because a helpdesk mailbox that quietly eats mail is indistinguishable from
# one nobody is emailing.
DISPOSITIONS: frozenset[str] = frozenset(
    {
        "created",
        "commented",
        "reopened",
        "quarantined",
        "refused_loop",
        "refused_rate",
        "refused_parse",
    }
)


class EmailMailbox(Base):
    """Per-org inbound IMAP configuration (spec §2.1)."""

    __tablename__ = "email_mailbox"
    __table_args__ = (UniqueConstraint("org_id", "address", name="uq_email_mailbox_org_address"),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    address: Mapped[str] = mapped_column(String(320))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    use_ssl: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    folder: Mapped[str] = mapped_column(String(100), server_default="INBOX", default="INBOX")
    username: Mapped[str] = mapped_column(String(320))
    # Nullable since migration 0110 (spec §2.1 amendment A5). The check
    # constraint `ck_email_mailbox_credential` means a mailbox with no
    # credential can never be active, so there is still no unauthenticated IMAP.
    credential_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("credentials.id"), default=None
    )
    #: `password` or `microsoft_oauth`; the check constraint is the authority.
    auth_kind: Mapped[str] = mapped_column(
        String(20), server_default="password", default="password"
    )
    #: `ok` or `needs_reconsent`. `poller._claim` only claims `ok`, so a revoked
    #: Microsoft sign-in stops being polled until a person reconnects it.
    credential_state: Mapped[str] = mapped_column(String(20), server_default="ok", default="ok")
    # NOT NULL and load-bearing: `ticket.category_id` is NOT NULL and an email
    # carries no category, so without a configured default no inbound message
    # could ever become a ticket (spec §2.1).
    default_category_id: Mapped[UUID] = mapped_column(ForeignKey("ticket_category.id"))
    reopen_window_days: Mapped[int] = mapped_column(Integer, server_default="14", default=14)
    active: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(default=None)
    # A bounded, SANITIZED excerpt -- never a raw server response, never the
    # password (spec §2.1, invariant #3). `_sanitize_excerpt` in poller.py is
    # the only thing that writes it.
    last_error: Mapped[str | None] = mapped_column(String(500), default=None)
    # Not in spec §2.1's column list: required by §4's per-mailbox
    # `SELECT ... FOR UPDATE SKIP LOCKED` claim, which cannot be implemented
    # without it (Task 10 amends the spec).
    claimed_at: Mapped[datetime | None] = mapped_column(default=None)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class InboundMessage(Base):
    """One row per FETCHED message -- the idempotency key and the disposition
    record (spec §2.2, §4)."""

    __tablename__ = "inbound_message"
    __table_args__ = (
        # THIS CONSTRAINT IS THE IDEMPOTENCY MECHANISM (spec §4 rule 1).
        # IMAP has no transactions: the fetch, the ticket write and the \Seen
        # flag are three operations across two systems. A redelivered message
        # collides here instead of creating a second ticket.
        UniqueConstraint("org_id", "message_id", name="uq_inbound_message_org_message_id"),
        CheckConstraint(
            "disposition IN ('created', 'commented', 'reopened', 'quarantined', "
            "'refused_loop', 'refused_rate', 'refused_parse')",
            name="ck_inbound_message_disposition_valid",
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (O): NULL is every client. Set by the application.
    client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clients.id"), default=None, index=True
    )
    mailbox_id: Mapped[UUID] = mapped_column(ForeignKey("email_mailbox.id"))
    # RFC 5322's line-length limit. A message with no Message-ID gets a
    # synthesised one (parsing.py) so it still dedupes.
    message_id: Mapped[str] = mapped_column(String(998))
    from_address: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(512), server_default="", default="")
    received_at: Mapped[datetime] = mapped_column()
    disposition: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(200), server_default="", default="")
    ticket_id: Mapped[UUID | None] = mapped_column(ForeignKey("ticket.id"), default=None)
    sender_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    # The PARSED, plain-text body -- never the raw RFC822 message. Retaining
    # full raw messages indefinitely is a privacy liability (complete headers,
    # routing, every recipient) for forensics this product has not asked for
    # (spec §2.2).
    body_text: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class EmailOAuthApp(Base):
    """The Azure application an org's mailboxes consent against. One per org.

    The client secret is not here: it is a vault credential of type
    `email_oauth_client`, reached through `credential_id` (invariant #3).
    """

    __tablename__ = "email_oauth_app"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200))
    client_id: Mapped[str] = mapped_column(String(200))
    credential_id: Mapped[UUID] = mapped_column(ForeignKey("credentials.id"))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class EmailOAuthAuthorization(Base):
    """One in-flight consent: single use, ten minutes, bound to the person who
    started it. The tenant binding is RLS on the completing call, not `state`
    (design §4.3)."""

    __tablename__ = "email_oauth_authorization"
    __table_args__ = (
        UniqueConstraint("org_id", "state", name="uq_email_oauth_auth_org_state"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    mailbox_id: Mapped[UUID] = mapped_column(ForeignKey("email_mailbox.id", ondelete="CASCADE"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    state: Mapped[str] = mapped_column(String(64))
    sealed_verifier: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column()
    consumed_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
