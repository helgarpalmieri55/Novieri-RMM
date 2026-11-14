"""The OAuth server's tables (migration 0247; autenticación propia design §5,
§6.1).

`OAuthClient` is an organization's AI app (G); `OAuthRefreshFamily`,
`OAuthRefreshToken` and `OAuthCode` are a person's (O under `users` through
`user_id`): `client_id` on those three is the MSP client the trigger copies
from `users`, never sent; the AI app is `oauth_client_id`. Only
`OAuthClient.client_id` is an OAuth client id.

NOTHING HERE HOLDS A SECRET IN CLEAR: `code_hash` and `token_hash` are the
SHA-256 of a random 256-bit value (`app.core.auth.oauth.grants.hash_secret`);
the access token is never stored.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, client_org_fk, uuid7_pk

#: `oauth_clients.kind`: registered (RFC 7591) or a client metadata document.
CLIENT_KINDS = ("registered", "metadata_document")
#: `oauth_refresh_families.revoked_reason` (migration 0247's CHECK).
REVOKE_REASONS = (
    "refresh_reused",
    "code_reused",
    "revoked_by_app",
    "revoked_in_settings",
    "signed_out_everywhere",
    "password_changed",
    "factors_reset",
    "disabled",
    "deleted",
    "person_inactive",
)


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _client_id() -> Mapped[UUID | None]:
    """The trigger's: copied from `users.client_id`, never sent
    (`app.models.auth._client_id`, the same column)."""
    return mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        server_onupdate=FetchedValue(),
    )


def _app_of_the_org(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["oauth_client_id", "org_id"],
        ["oauth_clients.id", "oauth_clients.org_id"],
        name=f"fk_{table}_app_org",
        ondelete="CASCADE",
    )


class OAuthClient(Base):
    """An AI app this organization knows (design §5.4, §5.5)."""

    __tablename__ = "oauth_clients"
    __table_args__ = (
        UniqueConstraint("org_id", "client_id", name="uq_oauth_clients_org_client"),
        UniqueConstraint("id", "org_id", name="uq_oauth_clients_id_org"),
        CheckConstraint(_one_of("kind", CLIENT_KINDS), name="ck_oauth_clients_kind"),
        CheckConstraint(
            "char_length(client_id) BETWEEN 1 AND 255", name="ck_oauth_clients_client_id"
        ),
        CheckConstraint(
            "client_name IS NULL OR char_length(client_name) BETWEEN 1 AND 200",
            name="ck_oauth_clients_client_name",
        ),
        CheckConstraint(
            "cardinality(redirect_uris) BETWEEN 1 AND 10", name="ck_oauth_clients_redirect_uris"
        ),
        CheckConstraint(
            "(kind = 'metadata_document') = (metadata_expires_at IS NOT NULL)",
            name="ck_oauth_clients_metadata",
        ),
        Index("ix_oauth_clients_org_last_used", "org_id", "last_used_at"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    client_name: Mapped[str | None] = mapped_column(Text, default=None)
    redirect_uris: Mapped[list[str]] = mapped_column(ARRAY(Text))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(server_default=func.now())
    metadata_expires_at: Mapped[datetime | None] = mapped_column(default=None)


class OAuthRefreshFamily(Base):
    """One person's grant to one AI app: its refresh tokens rotate inside it,
    and reusing one ends it (design §5.3)."""

    __tablename__ = "oauth_refresh_families"
    __table_args__ = (
        client_org_fk("oauth_refresh_families"),
        _app_of_the_org("oauth_refresh_families"),
        UniqueConstraint("id", "user_id", name="uq_oauth_refresh_families_id_user"),
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_reason IS NULL)",
            name="ck_oauth_refresh_families_revoked",
        ),
        CheckConstraint(
            "revoked_reason IS NULL OR " + _one_of("revoked_reason", REVOKE_REASONS),
            name="ck_oauth_refresh_families_reason",
        ),
        CheckConstraint("expires_at > created_at", name="ck_oauth_refresh_families_expiry"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID | None] = _client_id()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    oauth_client_id: Mapped[UUID] = mapped_column(index=True)
    resource: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_reason: Mapped[str | None] = mapped_column(Text, default=None)


class OAuthRefreshToken(Base):
    """A refresh token a family issued, as its hash. `used_at` set: rotated,
    and presenting it again ends the family."""

    __tablename__ = "oauth_refresh_tokens"
    __table_args__ = (
        client_org_fk("oauth_refresh_tokens"),
        UniqueConstraint("token_hash", name="uq_oauth_refresh_tokens_token_hash"),
        ForeignKeyConstraint(
            ["family_id", "user_id"],
            ["oauth_refresh_families.id", "oauth_refresh_families.user_id"],
            name="fk_oauth_refresh_tokens_family_user",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "octet_length(token_hash) = 32", name="ck_oauth_refresh_tokens_token_hash"
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID | None] = _client_id()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    family_id: Mapped[UUID] = mapped_column(index=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    used_at: Mapped[datetime | None] = mapped_column(default=None)


class OAuthCode(Base):
    """A one-time authorization code (design §5.2: 60 s), as its hash, bound to
    the app, the exact redirect it was asked for and the PKCE challenge."""

    __tablename__ = "oauth_codes"
    __table_args__ = (
        client_org_fk("oauth_codes"),
        _app_of_the_org("oauth_codes"),
        # A code's family is its own person's. The family's end empties
        # `family_id` alone (PostgreSQL 15+'s column list): a bare SET NULL on
        # this composite key would empty `user_id` too. Written exactly as the
        # database has it, which autogenerate reflects and compares equal.
        ForeignKeyConstraint(
            ["family_id", "user_id"],
            ["oauth_refresh_families.id", "oauth_refresh_families.user_id"],
            name="fk_oauth_codes_family_user",
            ondelete="SET NULL (family_id)",
        ),
        UniqueConstraint("code_hash", name="uq_oauth_codes_code_hash"),
        CheckConstraint("octet_length(code_hash) = 32", name="ck_oauth_codes_code_hash"),
        CheckConstraint(
            "char_length(code_challenge) = 43", name="ck_oauth_codes_code_challenge"
        ),
        CheckConstraint("expires_at > created_at", name="ck_oauth_codes_expiry"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID | None] = _client_id()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    oauth_client_id: Mapped[UUID] = mapped_column(index=True)
    code_hash: Mapped[bytes] = mapped_column(LargeBinary)
    redirect_uri: Mapped[str] = mapped_column(Text)
    code_challenge: Mapped[str] = mapped_column(Text)
    resource: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column()
    used_at: Mapped[datetime | None] = mapped_column(default=None)
    family_id: Mapped[UUID | None] = mapped_column(index=True, default=None)
