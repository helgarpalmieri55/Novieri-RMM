"""install link: one switchable enrollment link per org, and devices waiting for review

docs/superpowers/specs/2026-09-28-install-link-and-windows-push-design.md §1.1,
§1.5 and §3.

`install_links` -- ONE ROW PER ORG (a unique `org_id`), because the operator
asked for one link for all devices. The code is stored ONLY as its SHA-256
(`code_hash`, unique across orgs, because the public endpoints resolve the org
FROM the hash exactly as `/enroll/v1` resolves it from a token id). `networks`
is native `cidr[]` for the reason migrations 0043/0050/0077 give: the only
question asked of it is containment.

THE CHECK CONSTRAINT RESTATES THE API'S RULE: a link that is on has a code and
at least one network (spec §1.1: "an empty list is not 'anywhere'"). The API
refuses first with a sentence; this is the floor under a future caller that
forgets.

`devices.review_state` -- `approved` for every existing row and every token
enrollment; `pending` for a link enrollment; `rejected` once a technician
rejects it (plan D1: a tombstone, because no device-delete path exists and
`security_events` is append-only). `link_joined_from`/`link_joined_at` say how,
from where and when a machine joined, and the rate limit counts
`link_joined_at` inside the last hour. `rejection_outcome` is how a
rejection ENDED -- NULL while the agent still has time to confirm its uninstall,
then `agent_confirmed`, `timeout`, `agent_did_not_confirm` (its uninstall job
failed, was refused or expired) or `agent_cannot_uninstall` (too old to be
told) -- so the row itself says whether someone has to remove the agent by
hand.

`removal_acknowledged_at` is when a technician said they did (`POST
/devices/{id}/review/acknowledge-removal`). Until then a rejected row whose
outcome is `timeout`, `agent_did_not_confirm` or `agent_cannot_uninstall` is
listed in *Waiting for review* -- the only place a rejected row is ever shown
-- because otherwise nobody is told the agent may still be on the computer.
The CHECK says it can only be set on such a row: anywhere else it would record
a manual removal that nobody had to do.

BOTH `rejection_outcome` AND `removal_acknowledged_at` WERE ADDED TO THIS
MIGRATION IN PLACE, NOT IN A NEW ONE. That is the forward-only rule's reason
applied, not waived: the rule protects databases that have already run an
`upgrade()`, and 0170 has never been merged to `main` or deployed anywhere, so
no database holds a version of it without these columns. Once it is merged,
any further change is a new migration.

Two permission codes: `enrollment.link.manage` (admin tier) and
`devices.review` (technician tier). Neither is a platform-operator code: both
act on org-scoped, RLS-protected rows.

Revision ID: 0170
Revises: 0160
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0170"
down_revision: str | None = "0160"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

PERMISSIONS = [
    ("enrollment.link.manage", "enrollment", "link.manage"),
    ("devices.review", "devices", "review"),
]
ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": ["enrollment.link.manage", "devices.review"],
    "org-admin": ["enrollment.link.manage", "devices.review"],
    "technician": ["devices.review"],
}


def upgrade() -> None:
    op.create_table(
        "install_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=True),
        sa.Column(
            "networks",
            postgresql.ARRAY(postgresql.CIDR()),
            server_default=sa.text("'{}'::cidr[]"),
            nullable=False,
        ),
        sa.Column("disable_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hourly_limit", sa.Integer(), server_default=sa.text("50"), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rate_limited_audit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("org_id", name="uq_install_links_org"),
        sa.UniqueConstraint("code_hash", name="uq_install_links_code_hash"),
        sa.CheckConstraint("hourly_limit BETWEEN 1 AND 500", name="ck_install_links_hourly_limit"),
        sa.CheckConstraint("cardinality(networks) <= 64", name="ck_install_links_network_count"),
        sa.CheckConstraint(
            "NOT enabled OR (code_hash IS NOT NULL AND cardinality(networks) >= 1)",
            name="ck_install_links_on_needs_code_and_network",
        ),
    )
    op.execute("ALTER TABLE install_links ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE install_links FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON install_links "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )

    op.add_column(
        "devices",
        sa.Column("review_state", sa.String(10), server_default="approved", nullable=False),
    )
    op.create_check_constraint(
        "ck_devices_review_state",
        "devices",
        "review_state IN ('approved', 'pending', 'rejected')",
    )
    op.add_column("devices", sa.Column("link_joined_from", postgresql.INET(), nullable=True))
    op.add_column(
        "devices", sa.Column("link_joined_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("devices", sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("devices", sa.Column("rejection_outcome", sa.String(24), nullable=True))
    op.create_check_constraint(
        "ck_devices_rejection_outcome",
        "devices",
        "rejection_outcome IS NULL OR rejection_outcome IN ('agent_confirmed', 'timeout', "
        "'agent_did_not_confirm', 'agent_cannot_uninstall')",
    )
    op.add_column(
        "devices",
        sa.Column("removal_acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_devices_removal_acknowledged",
        "devices",
        "removal_acknowledged_at IS NULL OR (review_state = 'rejected' AND rejection_outcome "
        "IN ('timeout', 'agent_did_not_confirm', 'agent_cannot_uninstall'))",
    )
    op.create_index("ix_devices_org_review_state", "devices", ["org_id", "review_state"])

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
    # BY CODE, never by module: `module = 'devices'` and `module = 'enrollment'`
    # hold codes from migration 0003 that must survive the way down.
    code_list = ", ".join(f"'{c}'" for c, _, _ in PERMISSIONS)
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code IN ({code_list}))"
    )
    op.execute(f"DELETE FROM permissions WHERE code IN ({code_list})")
    op.drop_index("ix_devices_org_review_state", table_name="devices")
    op.drop_constraint("ck_devices_removal_acknowledged", "devices", type_="check")
    op.drop_column("devices", "removal_acknowledged_at")
    op.drop_constraint("ck_devices_rejection_outcome", "devices", type_="check")
    op.drop_column("devices", "rejection_outcome")
    op.drop_column("devices", "rejected_at")
    op.drop_column("devices", "link_joined_at")
    op.drop_column("devices", "link_joined_from")
    op.drop_constraint("ck_devices_review_state", "devices", type_="check")
    op.drop_column("devices", "review_state")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON install_links")
    op.drop_table("install_links")
