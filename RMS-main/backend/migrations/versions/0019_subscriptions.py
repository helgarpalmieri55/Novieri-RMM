"""Subscriptions: subscription, seat_assignment, subscription_event + RLS + RBAC.

docs/modules/subscriptions/spec.md §2 (data model) and §8 (security invariants).

Revision ID: 0019
Revises: 0018
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = [
    ("subscriptions.read", "subscriptions", "read"),
    ("subscriptions.manage", "subscriptions", "manage"),
]
_ALL_SUBSCRIPTIONS = ["subscriptions.read", "subscriptions.manage"]
ROLE_GRANTS = {
    "super-admin": _ALL_SUBSCRIPTIONS,
    "org-admin": _ALL_SUBSCRIPTIONS,
    "technician": ["subscriptions.read", "subscriptions.manage"],
    "auditor": ["subscriptions.read"],
    "read-only": ["subscriptions.read"],
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
        CREATE TABLE subscription (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            vendor_id uuid REFERENCES vendor(id),
            service_name varchar(255) NOT NULL,
            plan varchar(255) NOT NULL DEFAULT '',
            category varchar(255) NOT NULL DEFAULT '',
            billing_cycle varchar(10) NOT NULL,
            cost_per_cycle numeric(14, 2) NOT NULL,
            currency varchar(3) NOT NULL,
            seats integer NOT NULL DEFAULT 0,
            owner_user_id uuid REFERENCES users(id),
            started_at date,
            renews_at date,
            auto_renew boolean NOT NULL DEFAULT false,
            notice_days integer NOT NULL DEFAULT 0,
            payment_ref varchar(255),
            license_ref uuid,
            status varchar(10) NOT NULL DEFAULT 'active',
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_subscription_org_vendor ON subscription (org_id, vendor_id)"
    )
    op.execute(
        "CREATE INDEX ix_subscription_org_renews_at ON subscription (org_id, renews_at)"
    )
    op.execute(
        "CREATE INDEX ix_subscription_org_owner_user"
        " ON subscription (org_id, owner_user_id)"
    )

    op.execute(
        """
        CREATE TABLE seat_assignment (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            subscription_id uuid NOT NULL REFERENCES subscription(id),
            assignee_name varchar(255) NOT NULL,
            assignee_email varchar(255) NOT NULL,
            assigned_at timestamptz NOT NULL DEFAULT now(),
            released_at timestamptz,
            created_by uuid NOT NULL REFERENCES users(id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_seat_assignment_org_subscription"
        " ON seat_assignment (org_id, subscription_id)"
    )

    op.execute(
        """
        CREATE TABLE subscription_event (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            subscription_id uuid NOT NULL REFERENCES subscription(id),
            type varchar(16) NOT NULL,
            details jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_subscription_event_org_subscription_created"
        " ON subscription_event (org_id, subscription_id, created_at)"
    )

    for t in (
        "subscription",
        "seat_assignment",
        "subscription_event",
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
        "(SELECT id FROM permissions WHERE module = 'subscriptions')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'subscriptions'")
    for t in (
        "subscription_event",
        "seat_assignment",
        "subscription",
    ):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
