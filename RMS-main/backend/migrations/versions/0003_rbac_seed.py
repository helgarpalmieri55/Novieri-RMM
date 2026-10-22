"""Seed baseline permissions and global roles (docs/03-security-model.md).

Runs as the admin role (BYPASSRLS) so global roles (org_id NULL) can be
inserted despite the tenant WITH CHECK policies. Also hardens global
reference tables: the app role cannot rewrite the permission catalog.

Revision ID: 0003
Revises: 0002
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = [
    ("devices.read", "devices", "read"),
    ("devices.manage", "devices", "manage"),
    ("jobs.read", "jobs", "read"),
    ("jobs.dispatch", "jobs", "dispatch"),
    ("enrollment.manage", "enrollment", "manage"),
    ("audit.read", "audit", "read"),
    ("credentials.read", "credentials", "read"),
    ("credentials.manage", "credentials", "manage"),
    ("policies.manage", "policies", "manage"),
    ("orgs.manage", "orgs", "manage"),
]

ALL = [code for code, _, _ in PERMISSIONS]

ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": ALL,
    "org-admin": ALL,
    "technician": ["devices.read", "jobs.read", "jobs.dispatch"],
    "auditor": ["devices.read", "jobs.read", "audit.read", "credentials.read"],
    "read-only": ["devices.read", "jobs.read"],
}

ROLE_DESCRIPTIONS = {
    "super-admin": "Cross-org platform administration (SaaS operator)",
    "org-admin": "Full control within one organization",
    "technician": "Operate endpoints (remote, deploy, patch) within scope",
    "auditor": "Read-only access to inventory, records, and audit log",
    "read-only": "View dashboards and inventory only",
}


def upgrade() -> None:
    for code, module, action in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', '{module}', '{action}')"
        )
    for name, description in ROLE_DESCRIPTIONS.items():
        op.execute(
            "INSERT INTO roles (id, org_id, name, description) "
            f"VALUES (gen_random_uuid(), NULL, '{name}', '{description}')"
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
        "DELETE FROM role_permissions WHERE role_id IN "
        "(SELECT id FROM roles WHERE org_id IS NULL)"
    )
    op.execute("DELETE FROM roles WHERE org_id IS NULL")
    op.execute("DELETE FROM permissions")
