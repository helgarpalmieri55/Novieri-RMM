"""Licenses/SAM: vendor, product, license, allocation, snapshot, ignore + RLS + RBAC.

docs/modules/licenses/spec.md §2 (data model) and §9 (security invariants).

Revision ID: 0018
Revises: 0017
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = [
    ("licenses.read", "licenses", "read"),
    ("licenses.manage", "licenses", "manage"),
    ("licenses.keys.read", "licenses", "keys.read"),
]
_ALL_LICENSES = ["licenses.read", "licenses.manage", "licenses.keys.read"]
ROLE_GRANTS = {
    "super-admin": _ALL_LICENSES,
    "org-admin": _ALL_LICENSES,
    "technician": ["licenses.read", "licenses.manage"],
    "auditor": ["licenses.read"],
    "read-only": ["licenses.read"],
}


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (org_id = current_setting('app.current_org', true)::uuid) "
        "WITH CHECK (org_id = current_setting('app.current_org', true)::uuid)"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE vendor (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            name varchar(255) NOT NULL,
            website varchar(255),
            contacts jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_vendor_org_name ON vendor (org_id, name)")

    op.execute(
        """
        CREATE TABLE software_product (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            name varchar(255) NOT NULL,
            publisher varchar(255) NOT NULL DEFAULT '',
            match_rules jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_software_product_org_name ON software_product (org_id, name)"
    )

    op.execute(
        """
        CREATE TABLE license (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            product_id uuid NOT NULL REFERENCES software_product(id),
            vendor_id uuid REFERENCES vendor(id),
            type varchar(16) NOT NULL,
            entitlements integer NOT NULL,
            key_sealed text,
            cost numeric(14, 2),
            currency varchar(3),
            po_ref varchar(255),
            purchased_at date,
            maintenance_expires_at date,
            renews_at date,
            agreement_ref varchar(255),
            notes text NOT NULL DEFAULT '',
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_license_org_product ON license (org_id, product_id)")
    op.execute("CREATE INDEX ix_license_org_renews_at ON license (org_id, renews_at)")
    op.execute(
        "CREATE INDEX ix_license_org_maintenance_expires_at ON license"
        " (org_id, maintenance_expires_at)"
    )

    op.execute(
        """
        CREATE TABLE license_allocation (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            license_id uuid NOT NULL REFERENCES license(id),
            target_type varchar(8) NOT NULL,
            target_id uuid NOT NULL,
            allocated_at timestamptz NOT NULL DEFAULT now(),
            released_at timestamptz,
            created_by uuid NOT NULL REFERENCES users(id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_license_allocation_org_license ON license_allocation"
        " (org_id, license_id)"
    )

    op.execute(
        """
        CREATE TABLE license_compliance_snapshot (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            product_id uuid NOT NULL REFERENCES software_product(id),
            entitled integer NOT NULL,
            installed integer NOT NULL,
            delta integer NOT NULL,
            computed_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_license_compliance_snapshot_org_product_computed"
        " ON license_compliance_snapshot (org_id, product_id, computed_at)"
    )

    op.execute(
        """
        CREATE TABLE software_ignore (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            name varchar(300) NOT NULL,
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_software_ignore_org_name UNIQUE (org_id, name)
        )
        """
    )

    for t in (
        "vendor",
        "software_product",
        "license",
        "license_allocation",
        "license_compliance_snapshot",
        "software_ignore",
    ):
        _rls(t)

    for code, module, action in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', '{module}', '{action}')"
        )
    for name, codes in ROLE_GRANTS.items():
        code_list = ", ".join(f"'{c}'" for c in codes)
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            f"WHERE r.name = '{name}' AND r.org_id IS NULL AND p.code IN ({code_list})"
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE module = 'licenses')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'licenses'")
    for t in (
        "license_allocation",
        "license_compliance_snapshot",
        "license",
        "software_product",
        "software_ignore",
        "vendor",
    ):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
