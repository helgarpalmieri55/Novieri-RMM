"""Pre-Phase-3 security hardening (audit-driven).

- New credentials.checkout permission so decrypting a stored secret is a
  distinct privilege from reading credential metadata. Granted to operators
  (super-admin, org-admin) — NOT auditor/read-only, who keep metadata-only
  access. Fixes the finding that a read-only auditor could exfiltrate every
  plaintext secret via the checkout endpoint.
- Enforce the audit_events append-only REVOKE in the migration itself (it was
  only in the test harness before), matching the security_events pattern.

Revision ID: 0009
Revises: 0008
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECKOUT_GRANTS = ["super-admin", "org-admin"]


def upgrade() -> None:
    op.execute(
        "INSERT INTO permissions (id, code, module, action) VALUES "
        "(gen_random_uuid(), 'credentials.checkout', 'credentials', 'checkout')"
    )
    for role in CHECKOUT_GRANTS:
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            f"WHERE r.name = '{role}' AND r.org_id IS NULL "
            "AND p.code = 'credentials.checkout'"
        )
    # Append-only enforcement for the audit log at the grant layer, not only via
    # the immutability trigger (defense in depth; prod runs migrations, not the
    # test harness). Guarded so it is a no-op if the app role isn't present yet.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_user') THEN "
        "REVOKE UPDATE, DELETE ON audit_events FROM app_user; END IF; END $$;"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE code = 'credentials.checkout')"
    )
    op.execute("DELETE FROM permissions WHERE code = 'credentials.checkout'")
