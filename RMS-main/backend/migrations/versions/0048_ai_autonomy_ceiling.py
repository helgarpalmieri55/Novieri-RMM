"""ai-automation: the autonomy ceiling, and the session widening act mode needs

docs/modules/ai-automation.md §4.1, §5.3, §9.2, §10.1, §10.3, §16.

THREE THINGS HERE ARE NOT ORDINARY DDL.

1. `ai_autonomy_ceiling.level` HAS NO `'autonomous'` VALUE AT ALL (§0.4). The
   ordered vocabulary is `off < suggest < require_approval < autonomous`, and
   the AI personas' half of it simply stops one short. Making that a CHECK
   rather than an application clamp means widening it is a migration a reviewer
   sees -- the same argument `ck_approved_action_autonomous_is_bounded` makes
   for its own gate. The health module keeps its `autonomous`; this layer does
   not get it.

2. `ck_ai_autonomy_ceiling_end_user_is_off` CLOSES A SPEC GAP. §3.1 says the
   `end_user` persona may never be raised above `off` for any risk class, and
   §4.1's table gave no constraint for it. Enforcing that in Python would be
   exactly what §0.4 refuses for `autonomous`, so it is a fourth CHECK.
   Recorded back into spec §4.1.

3. THE TWO `ai_session` CHECKS ARE WIDENED, NOT REPLACED WITH ANYTHING LAXER
   (§5.3, §10.3). `persona` goes from `IN ('admin')` to
   `IN ('admin','technician','end_user')` and `mode` from `IN ('read')` to
   `IN ('read','act')`. A CHECK cannot be widened in place, so each is dropped
   and recreated under the SAME NAME -- the name is what
   `tests/test_ai_admin_migration.py` and the model's `__table_args__` both
   pin. Note what is still refused: `'requester'` is not a persona of this
   layer, and `'read_act'` -- the value `0045`'s own docstring anticipated --
   is not the value that shipped. §0.8 fixes `mode` AT CREATION; there is no
   promotion path, so a two-valued column is the whole vocabulary.

NO `ai_autonomy_ceiling` ROWS ARE SEEDED (§16). A missing row means `off`, so
the layer ships inert, the way `ai_settings.enabled` ships `false`. A migration
that seeded ceilings would be this layer granting itself reach.

NOTE: the live `permissions` table is (id, code, module, action) -- there is NO
`resource` and NO `description` column. Descriptions live only in
`app/core/rbac.py::PERMISSIONS`, matching every other module's migration.

Revision ID: 0048
Revises: 0047
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

PERMISSIONS = [
    ("ai.act.propose", "ai", "act.propose"),
    ("ai.act.confirm", "ai", "act.confirm"),
]

# §16: `ai.act.propose` to the technician tier, `ai.act.confirm` to the admin
# tier, both to super-admin/org-admin so `test_rbac.py::
# test_org_admin_has_all_permissions` continues to hold.
#
# GRANTING `ai.act.propose` TO `technician` CHANGES AN UNRELATED SUITE:
# `tests/test_api_routes.py::test_me_returns_identity_and_permissions` pins the
# technician's exact permission set as a literal, and adding the code to that
# literal is the correct fix, not weakening the assertion. The grant is inert on
# its own -- `ai.use` is still admin-only (0045), so a technician cannot open a
# session at all -- and it grants NO ability to confirm or dispatch, which is
# the whole of §9.2's first row.
ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": ["ai.act.propose", "ai.act.confirm"],
    "org-admin": ["ai.act.propose", "ai.act.confirm"],
    "technician": ["ai.act.propose"],
}


def upgrade() -> None:
    op.create_table(
        "ai_autonomy_ceiling",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("persona", sa.String(16), primary_key=True, nullable=False),
        sa.Column("risk_class", sa.String(8), primary_key=True, nullable=False),
        sa.Column("level", sa.String(20), nullable=False),
        sa.Column(
            "updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "persona IN ('end_user', 'technician', 'admin')",
            name="ck_ai_autonomy_ceiling_persona",
        ),
        sa.CheckConstraint(
            "risk_class IN ('low', 'medium', 'high')",
            name="ck_ai_autonomy_ceiling_risk_class",
        ),
        # Point 1 of the module docstring.
        sa.CheckConstraint(
            "level IN ('off', 'suggest', 'require_approval')",
            name="ck_ai_autonomy_ceiling_level",
        ),
        # Point 2.
        sa.CheckConstraint(
            "NOT (persona = 'end_user' AND level <> 'off')",
            name="ck_ai_autonomy_ceiling_end_user_is_off",
        ),
    )

    # Standard tenant_isolation pattern (0002_rls.py, 0045_ai_admin.py). WITH
    # CHECK is stated explicitly. It is NOT filling a gap -- for an ALL-command
    # policy PostgreSQL reuses the USING expression as WITH CHECK whenever WITH
    # CHECK is omitted -- but the explicit clause matters the day a future edit
    # gives this policy a different read predicate than write predicate.
    op.execute("ALTER TABLE ai_autonomy_ceiling ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ai_autonomy_ceiling FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON ai_autonomy_ceiling "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )

    # Point 3. Dropped and recreated under the same names.
    op.execute("ALTER TABLE ai_session DROP CONSTRAINT ck_ai_session_persona")
    op.execute(
        "ALTER TABLE ai_session ADD CONSTRAINT ck_ai_session_persona "
        "CHECK (persona IN ('admin', 'technician', 'end_user'))"
    )
    op.execute("ALTER TABLE ai_session DROP CONSTRAINT ck_ai_session_mode")
    op.execute(
        "ALTER TABLE ai_session ADD CONSTRAINT ck_ai_session_mode "
        "CHECK (mode IN ('read', 'act'))"
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
    codes = ", ".join(f"'{c}'" for c, _, _ in PERMISSIONS)
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code IN ({codes}))"
    )
    op.execute(f"DELETE FROM permissions WHERE code IN ({codes})")

    # A narrowing that would fail on a stored row is the honest direction here:
    # a session that reached `mode='act'` cannot be represented by the schema
    # this downgrade restores, and silently deleting it would destroy the
    # transcript of a conversation that proposed real actions.
    op.execute("DELETE FROM ai_session WHERE persona <> 'admin' OR mode <> 'read'")
    op.execute("ALTER TABLE ai_session DROP CONSTRAINT ck_ai_session_mode")
    op.execute("ALTER TABLE ai_session ADD CONSTRAINT ck_ai_session_mode CHECK (mode IN ('read'))")
    op.execute("ALTER TABLE ai_session DROP CONSTRAINT ck_ai_session_persona")
    op.execute(
        "ALTER TABLE ai_session ADD CONSTRAINT ck_ai_session_persona CHECK (persona IN ('admin'))"
    )

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON ai_autonomy_ceiling")
    op.drop_table("ai_autonomy_ceiling")
