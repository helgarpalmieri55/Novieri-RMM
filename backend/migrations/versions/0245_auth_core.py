"""Own authentication, phase 1: the identity core's tables and the two reads
that happen before anyone knows the organization
(`docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §6.1, §6.2;
plan 2026-11-04, Task 1).

THE TABLES. Nine hold one person's credentials and sessions -- password
verifier, TOTP secret, passkeys, recovery codes, remembered devices, sessions,
failure counters, single-use links and WebAuthn challenges -- and two hold an
organization's settings: its sessions (`auth_org_settings`) and the relay its
account mail goes through (`account_mail_settings`, plan Task 3, whose
password stays in the vault behind `credential_id`). Every one carries
`org_id` under the forced `tenant_isolation` policy (the 0210 pattern,
invariant 5).

A PERSON'S ROWS FOLLOW THE PERSON'S `users` ROW (invariant 5a, catalog kind O
with parent `users` through `user_id`). `its_client_from_parent` (0231) copies
`users.client_id` into each row: NULL for a technician -- a global row, read
under any pinned scope and written only under scope all -- and the contact's
own client for a contact (phase 2), so a contact's credentials are inside
their client as §6.1 asks. `its_client_to_children` moves them if that ever
changes. The O policies are 0231's, verbatim. `auth_org_settings` and
`account_mail_settings` are the organization's own configuration and carry no
client (G), like `health_org_settings` and `sla_org_settings`.

NO SECRET IS STORED IN CLEAR. A password is an argon2id hash over an HMAC with
the OpenBao pepper (`hash`, `pepper_version`); a TOTP secret is sealed by the
vault (`secret_sealed`, the ASCII string `app.core.vault.seal_bytes` returns,
bound to the user id); a
session or device cookie and a single-use link are stored as a 32-byte digest
(`token_hash`: SHA-256, or HMAC-SHA256 with the pepper), a recovery code as its
salted argon2id hash (`code_hash`, the encoded form), and CHECKs hold the
lengths. A WebAuthn challenge is not a secret
(the browser signs it in the open) and is stored as issued.

THE TWO FUNCTIONS ARE THE ONLY READ BEFORE THE ORGANIZATION IS KNOWN (§6.2).
Without `app.current_org` RLS shows nothing, and that is correct: a sign-in
names an address, and a request carries a cookie, before anything has said
which organization they belong to. `its_auth_principal(slug, email)` answers
`(user_id, org_id, kind, status)` and nothing else; `its_auth_session(hash)`
answers the session's own columns and its person's status. Both are SECURITY
DEFINER, owned by the migrating role `its_admin` (BYPASSRLS, the 0065/0232
precedent), pinned to `search_path = public, pg_temp`, and executable by
`app_user` alone: EXECUTE is revoked from PUBLIC, so `portal_user` cannot look
anyone up. With the organization they return, the platform opens an ordinary
`org_session` and reads the credentials under RLS.

`its_auth_session` answers a session only when its `kind` is its person's: a
row that disagrees is no session, rather than a contact admitted as staff.

STAFF ADDRESSES BECOME UNIQUE PER ORGANIZATION (plan decision D2): sign-in
looks a technician up by `(organization, lower(email))`, and two technicians
sharing an address would make that lookup answer two people. Contacts are
already held by 0232's `its_contact_email_unique`. The upgrade refuses, by
name, a database where two technicians of one organization share an address,
instead of dying on the index.

`keycloak_event_ingest` stays: MCP still signs in through Keycloak until phase
3, and the table goes in phase 4 with Keycloak (plan decision D4).

The table and column names are written here, not imported: a migration that
imports application code runs against whatever that code has become.

Forward-only. `downgrade()` has never run in production (rollback is
restore-from-backup). CI runs upgrade->downgrade->upgrade.

Revision ID: 0245
Revises: 0244
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0245"
down_revision: str | None = "0244"
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

#: A person's tables, in creation order (`auth_sessions` names a remembered
#: device). Each is O under `users` through `user_id`.
PERSON_TABLES = (
    "auth_passwords",
    "auth_totp",
    "auth_passkeys",
    "auth_recovery_codes",
    "auth_trusted_devices",
    "auth_sessions",
    "auth_failures",
    "auth_action_tokens",
    "auth_webauthn_challenges",
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


def _owner(*, nullable: bool = False, primary_key: bool = False) -> list[sa.Column]:
    """`org_id`, `client_id` (the trigger's) and `user_id`: whose row it is."""
    return [
        _uuid("org_id", sa.ForeignKey("organizations.id")),
        _uuid("client_id", sa.ForeignKey("clients.id"), nullable=True),
        _uuid(
            "user_id",
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=nullable,
            primary_key=primary_key,
        ),
    ]


def _tenant(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )


def _person_scope(table: str) -> None:
    """O under `users` (0231): index, org-consistency key, both triggers, and
    the three restrictive client policies."""
    op.execute(f"CREATE INDEX ix_{table}_client_id ON {table} (client_id)")
    # A foreign key ignores RLS: without this, org A's row could name org B's
    # client under scope all (0231).
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


PRINCIPAL_FUNCTION = """
CREATE FUNCTION its_auth_principal(p_org_slug text, p_email text)
RETURNS TABLE (user_id uuid, org_id uuid, kind text, status text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
#variable_conflict use_column
BEGIN
    -- The person an address names in one organization: at most one staff row
    -- (ux_users_org_lower_email_staff) and at most one contact row (0232),
    -- staff first. Four columns, never the address, a hash or a secret.
    RETURN QUERY
        SELECT u.id, u.org_id, u.kind::text, u.status::text
        FROM organizations o
        JOIN users u ON u.org_id = o.id
        WHERE o.slug = p_org_slug
          AND lower(u.email) = lower(p_email)
        ORDER BY u.kind DESC, u.id;
END $$
"""

SESSION_FUNCTION = """
CREATE FUNCTION its_auth_session(p_token_hash bytea)
RETURNS TABLE (
    session_id uuid, user_id uuid, org_id uuid, kind text, status text,
    last_seen_at timestamptz, expires_at timestamptz, factor_at timestamptz,
    step_up_at timestamptz, ended_at timestamptz
)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
#variable_conflict use_column
BEGIN
    -- The session a cookie's SHA-256 names, and its person's status. A row
    -- whose kind is not its person's is no session (module docstring).
    -- Ended and expired rows are returned as they are: the caller decides.
    RETURN QUERY
        SELECT s.id, s.user_id, s.org_id, s.kind, u.status::text,
               s.last_seen_at, s.expires_at, s.factor_at, s.step_up_at, s.ended_at
        FROM auth_sessions s
        JOIN users u ON u.id = s.user_id AND u.org_id = s.org_id
        WHERE s.token_hash = p_token_hash
          AND u.kind = s.kind;
END $$
"""

#: (signature, body) of the two functions, for the grants and the downgrade.
FUNCTIONS = (
    ("its_auth_principal(text, text)", PRINCIPAL_FUNCTION),
    ("its_auth_session(bytea)", SESSION_FUNCTION),
)

SHARED_STAFF_ADDRESSES = """
DO $$
DECLARE shared integer;
BEGIN
    SELECT count(*) INTO shared FROM (
        SELECT 1 FROM users WHERE kind = 'staff'
        GROUP BY org_id, lower(email) HAVING count(*) > 1
    ) AS d;
    IF shared > 0 THEN
        RAISE EXCEPTION
            '% staff address(es) are shared by two technicians of one organization; '
            'sign-in finds a technician by address, so make each unique and run 0245 again',
            shared;
    END IF;
END $$
"""


def upgrade() -> None:
    op.execute(SHARED_STAFF_ADDRESSES)
    op.execute(
        "CREATE UNIQUE INDEX ux_users_org_lower_email_staff "
        "ON users (org_id, lower(email)) WHERE kind = 'staff'"
    )

    op.create_table(
        "auth_passwords",
        *_owner(primary_key=True),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column("pepper_version", sa.Integer(), nullable=False),
        _when("changed_at", now=True),
        sa.CheckConstraint("pepper_version >= 1", name="ck_auth_passwords_pepper_version"),
    )
    op.create_table(
        "auth_totp",
        *_owner(primary_key=True),
        sa.Column("secret_sealed", sa.LargeBinary(), nullable=False),
        sa.Column("last_step", sa.BigInteger(), nullable=True),
        _when("created_at", now=True),
    )
    op.create_table(
        "auth_passkeys",
        _uuid("id", primary_key=True),
        *_owner(),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "transports",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("backup_eligible", sa.Boolean(), nullable=False),
        sa.Column("backup_state", sa.Boolean(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        _when("created_at", now=True),
        _when("last_used_at", nullable=True),
        sa.UniqueConstraint("credential_id", name="uq_auth_passkeys_credential_id"),
        sa.CheckConstraint("sign_count >= 0", name="ck_auth_passkeys_sign_count"),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 100", name="ck_auth_passkeys_name"
        ),
    )
    op.create_index("ix_auth_passkeys_user_id", "auth_passkeys", ["user_id"])
    op.create_table(
        "auth_recovery_codes",
        _uuid("id", primary_key=True),
        *_owner(),
        sa.Column("code_hash", sa.LargeBinary(), nullable=False),
        _when("used_at", nullable=True),
        sa.CheckConstraint(
            "octet_length(code_hash) BETWEEN 32 AND 256", name="ck_auth_recovery_codes_code_hash"
        ),
    )
    op.create_index("ix_auth_recovery_codes_user_id", "auth_recovery_codes", ["user_id"])
    op.create_table(
        "auth_trusted_devices",
        _uuid("id", primary_key=True),
        *_owner(),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        _when("created_at", now=True),
        _when("expires_at"),
        _when("last_used_at", nullable=True),
        _when("revoked_at", nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_auth_trusted_devices_token_hash"),
        # The key `fk_auth_sessions_device_user` references below.
        sa.UniqueConstraint("id", "user_id", name="uq_auth_trusted_devices_id_user"),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32", name="ck_auth_trusted_devices_token_hash"
        ),
    )
    op.create_index("ix_auth_trusted_devices_user_id", "auth_trusted_devices", ["user_id"])
    op.create_table(
        "auth_sessions",
        _uuid("id", primary_key=True),
        *_owner(),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        _when("created_at", now=True),
        _when("last_seen_at", now=True),
        _when("expires_at"),
        _when("factor_at", nullable=True),
        _when("step_up_at", nullable=True),
        _uuid("trusted_device_id", nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=False, server_default=""),
        sa.Column("ip", sa.Text(), nullable=False, server_default=""),
        _when("ended_at", nullable=True),
        sa.Column("ended_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
        # A session's remembered device is its own person's: a composite key,
        # so no code path can hand one person's device to another's session.
        sa.ForeignKeyConstraint(
            ["trusted_device_id", "user_id"],
            ["auth_trusted_devices.id", "auth_trusted_devices.user_id"],
            name="fk_auth_sessions_device_user",
        ),
        sa.CheckConstraint("kind IN ('staff', 'contact')", name="ck_auth_sessions_kind"),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32", name="ck_auth_sessions_token_hash"
        ),
        sa.CheckConstraint(
            "(ended_at IS NULL) = (ended_reason IS NULL)", name="ck_auth_sessions_ended"
        ),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_table(
        "auth_failures",
        *_owner(primary_key=True),
        sa.Column("factor", sa.Text(), primary_key=True),
        sa.Column("consecutive", sa.Integer(), nullable=False, server_default="0"),
        _when("last_failed_at"),
        _when("disabled_at", nullable=True),
        sa.CheckConstraint(
            "factor IN ('password', 'totp', 'passkey', 'recovery')",
            name="ck_auth_failures_factor",
        ),
        sa.CheckConstraint("consecutive >= 0", name="ck_auth_failures_consecutive"),
    )
    op.create_table(
        "auth_action_tokens",
        _uuid("id", primary_key=True),
        *_owner(),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        _when("created_at", now=True),
        _when("expires_at"),
        _when("used_at", nullable=True),
        _uuid("created_by", sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_auth_action_tokens_token_hash"),
        sa.CheckConstraint(
            "purpose IN ('invite', 'reset', 'recover')", name="ck_auth_action_tokens_purpose"
        ),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32", name="ck_auth_action_tokens_token_hash"
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_auth_action_tokens_expiry"),
    )
    op.create_index("ix_auth_action_tokens_user_id", "auth_action_tokens", ["user_id"])
    op.create_table(
        "auth_webauthn_challenges",
        _uuid("id", primary_key=True),
        *_owner(nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("challenge", sa.LargeBinary(), nullable=False),
        _when("expires_at"),
        _when("used_at", nullable=True),
        sa.CheckConstraint(
            "purpose IN ('register', 'authenticate', 'step_up')",
            name="ck_auth_webauthn_challenges_purpose",
        ),
        # W3C WebAuthn §13.4.3: at least 16 random bytes.
        sa.CheckConstraint(
            "octet_length(challenge) >= 16", name="ck_auth_webauthn_challenges_challenge"
        ),
        # Only signing in may start before the person is known.
        sa.CheckConstraint(
            "purpose = 'authenticate' OR user_id IS NOT NULL",
            name="ck_auth_webauthn_challenges_person",
        ),
    )
    for table in PERSON_TABLES:
        _person_scope(table)

    op.create_table(
        "auth_org_settings",
        _uuid("org_id", sa.ForeignKey("organizations.id"), primary_key=True),
        sa.Column("staff_idle_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("staff_max_hours", sa.Integer(), nullable=False, server_default="12"),
        sa.Column(
            "remember_device_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("remember_device_days", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("remembered_max_hours", sa.Integer(), nullable=False, server_default="168"),
        sa.Column("contact_idle_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("contact_max_days", sa.Integer(), nullable=False, server_default="7"),
        _when("updated_at", now=True),
        _uuid("updated_by", sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.CheckConstraint(
            "staff_idle_minutes BETWEEN 5 AND 480", name="ck_auth_org_settings_staff_idle"
        ),
        sa.CheckConstraint(
            "staff_max_hours BETWEEN 1 AND 24", name="ck_auth_org_settings_staff_max"
        ),
        sa.CheckConstraint(
            "remember_device_days BETWEEN 1 AND 30", name="ck_auth_org_settings_remember_days"
        ),
        sa.CheckConstraint(
            "remembered_max_hours BETWEEN 1 AND 720",
            name="ck_auth_org_settings_remembered_max",
        ),
        sa.CheckConstraint(
            "contact_idle_minutes BETWEEN 5 AND 480", name="ck_auth_org_settings_contact_idle"
        ),
        sa.CheckConstraint(
            "contact_max_days BETWEEN 1 AND 30", name="ck_auth_org_settings_contact_max"
        ),
        sa.CheckConstraint(
            "staff_idle_minutes <= staff_max_hours * 60",
            name="ck_auth_org_settings_staff_idle_within_max",
        ),
    )
    _tenant("auth_org_settings")

    # Settings -> Email (plan Task 3, its decision 1). The relay's password or
    # Microsoft client secret is not a column: it is the vault secret of
    # `credential_id` (invariant 3).
    op.create_table(
        "account_mail_settings",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            primary_key=True,
        ),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("from_address", sa.Text(), nullable=False),
        sa.Column("from_display_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("reply_to", sa.Text(), nullable=False, server_default=""),
        sa.Column("encryption", sa.Text(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False, server_default=""),
        sa.Column("auth_type", sa.Text(), nullable=False, server_default="password"),
        sa.Column("token_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("token_scope", sa.Text(), nullable=False, server_default=""),
        sa.Column("token_client_id", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "credential_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("credentials.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint("length(host) BETWEEN 1 AND 253", name="ck_account_mail_host"),
        sa.CheckConstraint("port BETWEEN 1 AND 65535", name="ck_account_mail_port"),
        sa.CheckConstraint(
            "encryption IN ('none', 'starttls', 'ssl')", name="ck_account_mail_encryption"
        ),
        sa.CheckConstraint("auth_type IN ('password', 'token')", name="ck_account_mail_auth_type"),
    )
    _tenant("account_mail_settings")

    for signature, body in FUNCTIONS:
        op.execute(body)
        # EXECUTE belongs to PUBLIC by default; the portal role and any future
        # one have no business looking a person up.
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO app_user")


def downgrade() -> None:
    for signature, _ in reversed(FUNCTIONS):
        op.execute(f"DROP FUNCTION {signature}")
    op.drop_table("account_mail_settings")
    op.drop_table("auth_org_settings")
    for table in reversed(PERSON_TABLES):
        # The trigger lives on `users`; dropping the child leaves it behind,
        # and the next change of a user's client would name a missing table.
        op.execute(f"DROP TRIGGER IF EXISTS its_client_to_{table} ON users")
        op.drop_table(table)
    op.execute("DROP INDEX ux_users_org_lower_email_staff")
