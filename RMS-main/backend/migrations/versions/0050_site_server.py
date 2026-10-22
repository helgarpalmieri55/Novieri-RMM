"""site_server: the registry row that makes a promoted Device a site server

`docs/modules/site-server/spec.md` §2.1, §10.1, §10.2, §15.

ONE org-scoped table, and the module's whole schema. A site server is a
`Device` (§0.1) -- it already has a `devices` row, an `agents` row and an
ordinary device-bound certificate -- so nothing here touches `jobs`,
`agents`, `devices`, or any kernel table. That is the load-bearing property
of the design and `tests/test_site_server_kernel_boundary.py` asserts it.

THREE THINGS HERE ARE NOT ORDINARY DDL:

1. `segments` IS `cidr[]`, NOT `text[]`. Containment is the only operation this
   column has -- §6.1's sweeper-preference `<<=`, §5.2's broadcast computation
   -- and in `text` it is a prefix comparison that is wrong at every boundary
   (`10.200.7.0/24` starts with `10.20`). IPAM's §0.1 reasoning; NO EXTENSION IS
   REQUIRED, `cidr` is core.

2. `UNIQUE (device_id)` AND NOT `(org_id, device_id)`. `devices.id` is already
   globally unique, so the composite would be strictly weaker while reading as
   stricter. `agents.device_id` makes the same choice.

3. THE `roles` CHECK ADMITS `cache` AND `pxe`, WHICH v1 DOES NOT HONOUR. That
   vocabulary is the entire forward seam for the deferred cache (§7) and PXE
   (§8) slices: admitting them here is what stops that slice beginning with a
   migration that widens a shipped CHECK on a populated table. The REFUSAL is a
   service rule in `registry.promote`, with §4.4's ungranted invariant amendment
   named in the error message -- one predicate, in one place, to relax on the
   day that amendment is accepted.

NO GiST INDEX ON `segments` (§16): a fleet-sized `&&` scan is a handful of rows.

Revision ID: 0050
Revises: 0049
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

# The live `permissions` table is (id, code, module, action) -- there is NO
# `resource` and NO `description` column. Descriptions live only in
# `app/core/rbac.py::PERMISSIONS`, matching every other module's migration.
PERMISSIONS = [
    ("site.read", "site", "read"),
    ("site.manage", "site", "manage"),
    ("site.wake", "site", "wake"),
]

# Spec §10.2. `site.manage` and `site.wake` are DELIBERATELY SEPARATE, for the
# reason IPAM separates `ipam.sweep.run` from `ipam.scope.manage`: waking a
# machine inside an existing configuration is routine technician work; deciding
# that a host may serve an entire subnet is not. Collapsed into one code,
# anyone who can wake a machine could decide what a site server is.
#
# super-admin/org-admin receive all three so `test_rbac.py::
# test_org_admin_has_all_permissions` continues to hold.
_ALL = [c for c, _, _ in PERMISSIONS]
ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": _ALL,
    "org-admin": _ALL,
    "technician": ["site.read", "site.wake"],
    "auditor": ["site.read"],
    "read-only": ["site.read"],
}


def upgrade() -> None:
    op.create_table(
        "site_server",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("devices.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("segments", postgresql.ARRAY(postgresql.CIDR()), nullable=False),
        sa.Column("roles", postgresql.ARRAY(sa.Text()), nullable=False),
        # NULLABLE AND LOAD-BEARING: IPAM is a pull-forward module and site
        # servers must work in a deployment that never installs it. Grouping
        # convenience, never a dependency.
        sa.Column(
            "ipam_site_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ipam_site.id"),
            nullable=True,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("device_id", name="uq_site_server_device"),
        sa.CheckConstraint(
            "cardinality(segments) BETWEEN 1 AND 32", name="ck_site_server_segment_count"
        ),
        sa.CheckConstraint(
            "roles <@ ARRAY['wake', 'cache', 'pxe']::text[] AND cardinality(roles) >= 1",
            name="ck_site_server_roles",
        ),
    )
    op.execute("CREATE INDEX ix_site_server_org_enabled ON site_server (org_id, enabled)")

    # Standard tenant_isolation pattern (0002_rls.py, 0043_ipam.py). WITH CHECK
    # is stated explicitly. It is NOT filling a gap -- for an ALL-command policy
    # PostgreSQL reuses the USING expression as WITH CHECK whenever WITH CHECK
    # is omitted, so a USING-only policy would reject a cross-org INSERT
    # identically, and DELETING the clause falsifies no test. The mutations that
    # do falsify `tests/test_site_server_migration.py` are `WITH CHECK (true)`
    # and `USING (true)`, and both were run.
    op.execute("ALTER TABLE site_server ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE site_server FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON site_server "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )

    for code, module, action in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', '{module}', '{action}')"
        )
    for role, codes in ROLE_GRANTS.items():
        code_list = ", ".join(f"'{c}'" for c in codes)
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            f"WHERE r.name = '{role}' AND r.org_id IS NULL AND p.code IN ({code_list})"
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE module = 'site')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'site'")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON site_server")
    op.drop_table("site_server")
