"""Own authentication, phase 2: sign in with Microsoft (Entra ID), and the one
read before the organization is known that a sign-in without an address needs
(`docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §4, §6.1,
§6.2; plan 2026-11-06, Task 1).

THE TABLES.

- `auth_external_identities`: a person's link to a Microsoft account, by the
  two claims Microsoft says do not change -- `tid`, the tenant, and `oid`, the
  account in it ("use the immutable claim values tid and oid as a combined
  key") -- and never by address ("Never use it for authorization", the email
  claim). One link per person and provider; one person per (organization,
  provider, tenant, account): the key carries `org_id`, so it does not tell
  one organization that another already holds the account. A person's row:
  O under `users` through `user_id`, 0245's pattern verbatim -- NULL client for
  a technician, the contact's own client for a contact (invariant 5a).
- `auth_entra_settings`: Settings -> Microsoft sign-in, one optional row per
  organization (G). The platform proves itself to Microsoft with a
  certificate it made (operator decision C4, 2026-11-06): the PRIVATE KEY is
  not a column, it is the vault secret of `credential_id` (invariant 3), the
  way Settings -> Email keeps the relay's password; the certificate, which is
  public, is kept in clear with its thumbprint and expiry, to be shown and
  uploaded to Entra. A CHECK stops it being switched on without an
  application, a key and a certificate, and another keeps the certificate's
  three columns together.
  A NEXT CERTIFICATE WAITS BESIDE IT (`next_*`, plan Task 1 decision 5): a
  new key and certificate a person still has to upload to Entra, while
  assertions keep using the current key ("The keyCredentials property is
  multi-valued"). Its four columns go together, and only beside a current
  certificate.
  THE CHECKS MEET `credential_id` / `next_credential_id ... ON DELETE SET
  NULL` ON PURPOSE: deleting either credential while it is in use fails with
  a check violation. No route reaches that (`/credentials` refuses the
  `entra_sign_in` type, plan Task 5); do not drop a CHECK to "repair" it.
- `clients.microsoft_tenants`: the tenants whose accounts a client's contacts
  may sign in with, at most twenty.
- Neither may hold Microsoft's personal-accounts tenant,
  9188040d-6c67-4c5b-b112-36a304b66dad ("For sign-ins to the personal
  Microsoft account tenant ... the value is 9188040d-..."), by a CHECK each.

KNOWN GAP, CARRIED FROM 0245: `auth_external_identities.user_id` references
`users.id` alone, so under scope all a user of another organization would be
accepted. Phase 1's final review left it as a follow-up ("`user_id` key is
not org-checked"); only `entra_flows` writes this table, always with the
session's or the invitation's own person.

`its_auth_org(slug)` IS THE THIRD READ BEFORE THE ORGANIZATION IS KNOWN (§6.2).
A passkey sign-in without an address and a Microsoft sign-in bring no address
for `its_auth_principal`, so this answers the id of the organization a slug
names, and nothing else. SECURITY DEFINER, owned by the migrating role
`its_admin`, `search_path` pinned, EXECUTE for `app_user` alone, as 0245's two.

Names are written here, not imported. Forward-only; `downgrade()` has never
run in production (rollback is restore-from-backup). CI runs
upgrade->downgrade->upgrade.

Revision ID: 0246
Revises: 0245
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0246"
down_revision: str | None = "0245"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

# 0231's client predicates, copied as 0245 copies them: a migration imports nothing.
VISIBLE = "its_client_visible(client_id)"
SCOPE = "current_setting('app.client_scope', true)"
SCOPE_PINNED = (
    f"({SCOPE} = 'all' OR ({SCOPE} = 'list' AND "
    "nullif(current_setting('app.current_clients', true), '') IS NOT NULL))"
)
NULL_PINNED = f"(client_id IS NULL AND {SCOPE_PINNED})"
NULL_ALL = f"(client_id IS NULL AND {SCOPE} = 'all')"

IDENTITIES = "auth_external_identities"
SETTINGS = "auth_entra_settings"
#: Microsoft's personal-accounts tenant: never a client's, never Novieri's.
CONSUMERS = "'9188040d-6c67-4c5b-b112-36a304b66dad'::uuid"
#: The next certificate: its four columns together, and only beside a current one.
NEXT_CHECK = (
    "(next_credential_id IS NULL AND next_certificate_pem IS NULL "
    "AND next_certificate_thumbprint IS NULL AND next_certificate_expires_at IS NULL) "
    "OR (next_credential_id IS NOT NULL AND next_certificate_pem IS NOT NULL "
    "AND next_certificate_thumbprint IS NOT NULL AND next_certificate_expires_at IS NOT NULL "
    "AND certificate_pem IS NOT NULL)"
)

ORG_FUNCTION = """
CREATE FUNCTION its_auth_org(p_org_slug text)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    -- The organization a slug names: its id and nothing else (design §6.2).
    SELECT o.id FROM organizations o WHERE o.slug = p_org_slug
$$
"""
ORG_SIGNATURE = "its_auth_org(text)"


def _uuid(name: str, *args: object, nullable: bool = False, **kw: object) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, nullable=nullable, **kw)


def _when(name: str, *, nullable: bool = False, now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if now else None,
    )


def _tenant(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )


def _person_scope(table: str) -> None:
    """0245's O-under-`users` treatment, verbatim: index, org-consistency key,
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
        IDENTITIES,
        _uuid("id", primary_key=True),
        _uuid("org_id", sa.ForeignKey("organizations.id")),
        _uuid("client_id", sa.ForeignKey("clients.id"), nullable=True),
        _uuid("user_id", sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("provider", sa.Text(), nullable=False),
        _uuid("tenant_id"),
        _uuid("subject"),
        _when("created_at", now=True),
        _when("last_used_at", nullable=True),
        sa.UniqueConstraint(
            "org_id", "provider", "tenant_id", "subject", name="uq_auth_external_identities_subject"
        ),
        sa.UniqueConstraint("user_id", "provider", name="uq_auth_external_identities_user"),
        sa.CheckConstraint("provider IN ('entra')", name="ck_auth_external_identities_provider"),
    )
    _person_scope(IDENTITIES)

    op.create_table(
        SETTINGS,
        _uuid("org_id", sa.ForeignKey("organizations.id"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        _uuid("app_client_id", nullable=True),
        _uuid("credential_id", sa.ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True),
        sa.Column("certificate_pem", sa.Text(), nullable=True),
        sa.Column("certificate_thumbprint", sa.Text(), nullable=True),
        _when("certificate_expires_at", nullable=True),
        _uuid(
            "next_credential_id",
            sa.ForeignKey("credentials.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("next_certificate_pem", sa.Text(), nullable=True),
        sa.Column("next_certificate_thumbprint", sa.Text(), nullable=True),
        _when("next_certificate_expires_at", nullable=True),
        _uuid("staff_tenant_id", nullable=True),
        _when("updated_at", now=True),
        _uuid("updated_by", sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.CheckConstraint(
            "NOT enabled OR (app_client_id IS NOT NULL AND credential_id IS NOT NULL "
            "AND certificate_pem IS NOT NULL)",
            name="ck_auth_entra_settings_ready",
        ),
        sa.CheckConstraint(
            "(certificate_pem IS NULL) = (certificate_thumbprint IS NULL) "
            "AND (certificate_pem IS NULL) = (certificate_expires_at IS NULL)",
            name="ck_auth_entra_settings_certificate",
        ),
        sa.CheckConstraint(NEXT_CHECK, name="ck_auth_entra_settings_next_certificate"),
        sa.CheckConstraint(
            f"staff_tenant_id IS DISTINCT FROM {CONSUMERS}",
            name="ck_auth_entra_settings_no_consumers",
        ),
    )
    _tenant(SETTINGS)

    op.add_column(
        "clients",
        sa.Column(
            "microsoft_tenants",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.create_check_constraint(
        "ck_clients_microsoft_tenants", "clients", "cardinality(microsoft_tenants) <= 20"
    )
    op.create_check_constraint(
        "ck_clients_microsoft_tenants_no_consumers",
        "clients",
        f"NOT ({CONSUMERS} = ANY (microsoft_tenants))",
    )

    op.execute(ORG_FUNCTION)
    op.execute(f"REVOKE ALL ON FUNCTION {ORG_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {ORG_SIGNATURE} TO app_user")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {ORG_SIGNATURE}")
    op.drop_constraint("ck_clients_microsoft_tenants_no_consumers", "clients", type_="check")
    op.drop_constraint("ck_clients_microsoft_tenants", "clients", type_="check")
    op.drop_column("clients", "microsoft_tenants")
    op.drop_table(SETTINGS)
    # The trigger lives on `users` (0245's downgrade says why it is dropped by hand).
    op.execute(f"DROP TRIGGER IF EXISTS its_client_to_{IDENTITIES} ON users")
    op.drop_table(IDENTITIES)
