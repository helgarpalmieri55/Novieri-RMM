"""Own authentication, phase 3: the OAuth server's tables
(`docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §5, §6.1;
plan 2026-11-06, Task 5).

FOUR TABLES. `oauth_clients` holds the AI apps this organization knows: each
one that registered itself (RFC 7591, `kind = 'registered'`, a client id the
platform made) and each client metadata document read (`kind =
'metadata_document'`, whose URL is the client id, cached until
`metadata_expires_at`). It is the organization's (G): no person, no client.
`oauth_refresh_families` holds one grant of one person to one app (design
§5.3: reusing a refresh token "revoca toda la familia"); `oauth_refresh_tokens`
every refresh token a family issued, so a used one is recognised when it comes
back; `oauth_codes` the one-time authorization codes. Those three are a
person's (O under `users` through `user_id`), with 0245's policies and
triggers verbatim, so a technician's rows are global rows.

`client_id` IN THE THREE O TABLES IS THE MSP CLIENT the trigger copies; the AI
app is `oauth_client_id`. Only `oauth_clients.client_id` is an OAuth client id,
as in `mcp_revocation` (both G).

A GRANT NAMES AN APP OF ITS OWN ORGANIZATION: `(oauth_client_id, org_id) ->
oauth_clients (id, org_id)`, because a foreign key ignores RLS (the 0231
reason for `fk_*_client_org`). An app's grants and codes go with it (ON
DELETE CASCADE): an expired registration takes its grants along (plan D10).

NO SECRET IN CLEAR. A code and a refresh token are stored as the SHA-256 of a
random 256-bit value (`code_hash`, `token_hash`, CHECKs of 32 bytes); the
access token is never stored.

The table and column names are written here, not imported: a migration that
imports application code runs against whatever that code has become.

Forward-only. `downgrade()` has never run in production (rollback is
restore-from-backup). CI runs upgrade->downgrade->upgrade.

Revision ID: 0247
Revises: 0246
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0247"
down_revision: str | None = "0246"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

# 0231's client predicates, copied: a migration imports nothing.
VISIBLE = "its_client_visible(client_id)"
SCOPE = "current_setting('app.client_scope', true)"
SCOPE_PINNED = (
    f"({SCOPE} = 'all' OR ({SCOPE} = 'list' AND "
    "nullif(current_setting('app.current_clients', true), '') IS NOT NULL))"
)
NULL_PINNED = f"(client_id IS NULL AND {SCOPE_PINNED})"
NULL_ALL = f"(client_id IS NULL AND {SCOPE} = 'all')"

#: A person's tables, in creation order. Each is O under `users` through `user_id`.
PERSON_TABLES = ("oauth_refresh_families", "oauth_refresh_tokens", "oauth_codes")

#: Why a family stopped (plan Task 5, decision 3).
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


def _uuid(name: str, *args: object, nullable: bool = False, **kw: object) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, nullable=nullable, **kw)


def _when(name: str, *, nullable: bool = False, now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if now else None,
    )


def _owner() -> list[sa.Column]:
    """`org_id`, `client_id` (the trigger's) and `user_id`: whose row it is."""
    return [
        _uuid("org_id", sa.ForeignKey("organizations.id")),
        _uuid("client_id", sa.ForeignKey("clients.id"), nullable=True),
        _uuid("user_id", sa.ForeignKey("users.id", ondelete="CASCADE")),
    ]


def _app_of_the_org(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["oauth_client_id", "org_id"],
        ["oauth_clients.id", "oauth_clients.org_id"],
        name=f"fk_{table}_app_org",
        ondelete="CASCADE",
    )


def _tenant(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )


def _person_scope(table: str) -> None:
    """O under `users` (0231, as 0245 applies it): index, org-consistency key,
    both triggers, and the three restrictive client policies."""
    op.execute(f"CREATE INDEX ix_{table}_client_id ON {table} (client_id)")
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_client_org "
        "FOREIGN KEY (client_id, org_id) REFERENCES clients (id, org_id)"
    )
    op.execute(
        f"CREATE TRIGGER its_client_from_parent BEFORE INSERT OR UPDATE OF user_id, client_id "
        f"ON {table} FOR EACH ROW EXECUTE FUNCTION its_client_from_parent('users', 'user_id')"
    )
    op.execute(
        f"CREATE TRIGGER its_client_to_{table} AFTER UPDATE ON users FOR EACH ROW "
        "WHEN (OLD.client_id IS DISTINCT FROM NEW.client_id) "
        f"EXECUTE FUNCTION its_client_to_children('{table}', 'user_id')"
    )
    _tenant(table)
    read = f"{NULL_PINNED} OR {VISIBLE}"
    modify = f"{NULL_ALL} OR {VISIBLE}"
    op.execute(
        f"CREATE POLICY client_isolation ON {table} AS RESTRICTIVE FOR ALL "
        f"USING ({read}) WITH CHECK ({modify})"
    )
    op.execute(
        f"CREATE POLICY client_isolation_update ON {table} AS RESTRICTIVE FOR UPDATE "
        f"USING ({modify})"
    )
    op.execute(
        f"CREATE POLICY client_isolation_delete ON {table} AS RESTRICTIVE FOR DELETE "
        f"USING ({modify})"
    )


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        _uuid("id", primary_key=True),
        _uuid("org_id", sa.ForeignKey("organizations.id")),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("client_name", sa.Text(), nullable=True),
        sa.Column("redirect_uris", postgresql.ARRAY(sa.Text()), nullable=False),
        _when("created_at", now=True),
        _when("last_used_at", now=True),
        _when("metadata_expires_at", nullable=True),
        sa.UniqueConstraint("org_id", "client_id", name="uq_oauth_clients_org_client"),
        # The key the grants' `_app_org` foreign keys reference.
        sa.UniqueConstraint("id", "org_id", name="uq_oauth_clients_id_org"),
        sa.CheckConstraint(
            "kind IN ('registered', 'metadata_document')", name="ck_oauth_clients_kind"
        ),
        sa.CheckConstraint(
            "char_length(client_id) BETWEEN 1 AND 255", name="ck_oauth_clients_client_id"
        ),
        sa.CheckConstraint(
            "client_name IS NULL OR char_length(client_name) BETWEEN 1 AND 200",
            name="ck_oauth_clients_client_name",
        ),
        sa.CheckConstraint(
            "cardinality(redirect_uris) BETWEEN 1 AND 10", name="ck_oauth_clients_redirect_uris"
        ),
        sa.CheckConstraint(
            "(kind = 'metadata_document') = (metadata_expires_at IS NOT NULL)",
            name="ck_oauth_clients_metadata",
        ),
    )
    op.create_index(
        "ix_oauth_clients_org_last_used", "oauth_clients", ["org_id", "last_used_at"]
    )
    _tenant("oauth_clients")

    reasons = ", ".join(f"'{r}'" for r in REVOKE_REASONS)
    op.create_table(
        "oauth_refresh_families",
        _uuid("id", primary_key=True),
        *_owner(),
        _uuid("oauth_client_id"),
        sa.Column("resource", sa.Text(), nullable=False),
        _when("created_at", now=True),
        _when("last_used_at", now=True),
        _when("expires_at"),
        _when("revoked_at", nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        _app_of_the_org("oauth_refresh_families"),
        # The key `fk_oauth_refresh_tokens_family_user` references: a token is
        # its own family's person's.
        sa.UniqueConstraint("id", "user_id", name="uq_oauth_refresh_families_id_user"),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_reason IS NULL)",
            name="ck_oauth_refresh_families_revoked",
        ),
        sa.CheckConstraint(
            f"revoked_reason IS NULL OR revoked_reason IN ({reasons})",
            name="ck_oauth_refresh_families_reason",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_oauth_refresh_families_expiry"),
    )
    op.create_index(
        "ix_oauth_refresh_families_user_id", "oauth_refresh_families", ["user_id"]
    )
    op.create_index(
        "ix_oauth_refresh_families_oauth_client_id", "oauth_refresh_families", ["oauth_client_id"]
    )
    op.create_table(
        "oauth_refresh_tokens",
        _uuid("id", primary_key=True),
        *_owner(),
        _uuid("family_id"),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        _when("created_at", now=True),
        _when("used_at", nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_oauth_refresh_tokens_token_hash"),
        sa.ForeignKeyConstraint(
            ["family_id", "user_id"],
            ["oauth_refresh_families.id", "oauth_refresh_families.user_id"],
            name="fk_oauth_refresh_tokens_family_user",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32", name="ck_oauth_refresh_tokens_token_hash"
        ),
    )
    op.create_index("ix_oauth_refresh_tokens_user_id", "oauth_refresh_tokens", ["user_id"])
    op.create_index("ix_oauth_refresh_tokens_family_id", "oauth_refresh_tokens", ["family_id"])
    op.create_table(
        "oauth_codes",
        _uuid("id", primary_key=True),
        *_owner(),
        _uuid("oauth_client_id"),
        sa.Column("code_hash", sa.LargeBinary(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        _when("created_at", now=True),
        _when("expires_at"),
        _when("used_at", nullable=True),
        _uuid(
            "family_id",
            sa.ForeignKey("oauth_refresh_families.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _app_of_the_org("oauth_codes"),
        sa.UniqueConstraint("code_hash", name="uq_oauth_codes_code_hash"),
        sa.CheckConstraint("octet_length(code_hash) = 32", name="ck_oauth_codes_code_hash"),
        sa.CheckConstraint(
            "char_length(code_challenge) = 43", name="ck_oauth_codes_code_challenge"
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_oauth_codes_expiry"),
    )
    op.create_index("ix_oauth_codes_user_id", "oauth_codes", ["user_id"])
    for table in PERSON_TABLES:
        _person_scope(table)


def downgrade() -> None:
    for table in reversed(PERSON_TABLES):
        # The trigger lives on `users`; dropping the child leaves it behind
        # (0245's reason).
        op.execute(f"DROP TRIGGER IF EXISTS its_client_to_{table} ON users")
    op.drop_table("oauth_codes")
    op.drop_table("oauth_refresh_tokens")
    op.drop_table("oauth_refresh_families")
    op.drop_table("oauth_clients")
