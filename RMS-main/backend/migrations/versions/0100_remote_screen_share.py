"""remote_screen_share: columns and permissions for screen-share phase 0

docs/modules/remote-control/spec-screen-share.md §5.1.

``remote_sessions`` gains ``input_enabled`` (the gateway turns it off when the
endpoint user presses Stop control) and ``recording_truncated`` (the
recording hit the policy cap and the session went on unrecorded).
``remote_policies`` gains the consent timeout, whether input is allowed at all,
privacy blanking (phase 2, stored now so the policy is complete) and the
per-session recording cap.

``ticket_id`` / ``alert_id`` from spec §5.1 belong to the phase 2 entry points
and arrive with them.

Two permission codes: ``remote.unattended`` (unattended screen sessions, in
addition to policy) and ``remote.privacy_blank`` (phase 2). Both go to
super-admin and org-admin only.

Both tables already FORCE RLS (migration 0010); new columns inherit that.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0100"
down_revision: str | None = "0095"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TWO_GIB = 2 * 1024 * 1024 * 1024

PERMISSIONS = [
    ("remote.unattended", "remote", "unattended"),
    ("remote.privacy_blank", "remote", "privacy_blank"),
]
ROLES = ("super-admin", "org-admin")


def upgrade() -> None:
    op.add_column(
        "remote_sessions",
        sa.Column("input_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "remote_sessions",
        sa.Column(
            "recording_truncated", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "remote_policies",
        sa.Column("consent_timeout_s", sa.Integer(), nullable=False, server_default="30"),
    )
    op.add_column(
        "remote_policies",
        sa.Column("allow_input", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "remote_policies",
        sa.Column(
            "allow_privacy_blank", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "remote_policies",
        sa.Column(
            "recording_max_bytes",
            sa.BigInteger(),
            nullable=False,
            server_default=str(_TWO_GIB),
        ),
    )
    op.create_check_constraint(
        "ck_remote_policies_consent_timeout",
        "remote_policies",
        "consent_timeout_s BETWEEN 15 AND 120",
    )

    for code, module, action in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', '{module}', '{action}')"
        )
    code_list = ", ".join(f"'{c}'" for c, _, _ in PERMISSIONS)
    for role in ROLES:
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            f"WHERE r.name = '{role}' AND r.org_id IS NULL AND p.code IN ({code_list})"
        )


def downgrade() -> None:
    code_list = ", ".join(f"'{c}'" for c, _, _ in PERMISSIONS)
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code IN ({code_list}))"
    )
    op.execute(f"DELETE FROM permissions WHERE code IN ({code_list})")
    op.drop_constraint("ck_remote_policies_consent_timeout", "remote_policies")
    for column in ("recording_max_bytes", "allow_privacy_blank", "allow_input", "consent_timeout_s"):
        op.drop_column("remote_policies", column)
    for column in ("recording_truncated", "input_enabled"):
        op.drop_column("remote_sessions", column)
