"""ai-admin: conversations, transcripts, injection flags, org settings

docs/modules/ai-admin/spec.md §3.1-§3.4, §0.10, §0.11.

FOUR org-scoped tables, all `ENABLE + FORCE ROW LEVEL SECURITY` with a
`tenant_isolation` policy carrying BOTH `USING` and `WITH CHECK`.

FOUR THINGS HERE ARE NOT ORDINARY DDL:

1. `ai_session.persona` AND `ai_session.mode` ARE CHECK-CONSTRAINED TO ONE
   VALUE EACH (§3.1, §12.2). The columns exist from day one because the AI
   Automation layer's other personas share this table rather than forking it,
   and act mode widens `mode` to `'read_act'`. Constraining them now means that
   widening is a REVIEWED MIGRATION rather than a value someone writes.

2. `ai_message.role` IS CHECK-CONSTRAINED TO ('user','assistant') (§3.2). Tool
   results are NOT stored: they would make this table a second, unaudited,
   un-RLS-reasoned shadow copy of every record the agent ever read. The CHECK is
   what makes that a schema property rather than a convention.

3. `ai_settings.enabled` DEFAULTS FALSE (§0.10). The surface ships off. No
   provider and no key ship with it, and an upgrade must not turn an
   egress-capable feature on for every org.

4. `ai_settings`' FOUR TUNABLES CARRY CHECK CEILINGS THAT §3.4 DID NOT SPECIFY.
   Every write to this table is `ai.admin`, so without them an org-admin can set
   `max_turns_per_org_per_hour = 10_000_000` and
   `transcript_retention_days = 36500`, which turns §10.3's rate limit and
   §0.7's retention trade into decoration. Bounds match how `sla_org_settings`
   and `health_org_settings` bound theirs. Recorded back into spec §3.4.

`ai_injection_flag.excerpt` is `String(400)`, NOT `text`: it is the one place
retrieved -- attacker-controlled, unauthenticated-ingress -- text is
deliberately stored (§3.3), and it must be bounded by the column rather than by
whoever writes the caller.

Revision ID: 0045
Revises: 0044
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

# Creation order, which is also FK order. The RLS loop and the downgrade both
# walk this list (the latter in reverse).
_TABLES = ["ai_session", "ai_message", "ai_injection_flag", "ai_settings"]

# NOTE: the live `permissions` table is (id, code, module, action) -- there is
# NO `resource` and NO `description` column. Descriptions live only in
# `app/core/rbac.py::PERMISSIONS`, matching every other module's migration.
PERMISSIONS = [
    ("ai.use", "ai", "use"),
    ("ai.admin", "ai", "admin"),
]

# §0.11 says `ai.admin` is "admin tier, never technician" and does NOT say which
# roles get `ai.use`. RESOLUTION, recorded in the spec: `ai.use` is seeded to the
# two admin roles ONLY in v1. The surface ships off anyway (`enabled=false`) and
# the permission grants nothing on its own -- every tool call is still gated on
# the caller's own module permissions -- so seeding it broadly would mean every
# existing technician silently gains an AI surface on upgrade without anyone
# deciding to. An org that wants technicians to use it grants it deliberately.
#
# super-admin/org-admin receive both so `test_rbac.py::
# test_org_admin_has_all_permissions` continues to hold.
_ALL = [c for c, _, _ in PERMISSIONS]
ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": _ALL,
    "org-admin": _ALL,
}

# Every index that is not backed by a constraint, written here exactly as
# `app/modules/ai_admin/models.py` declares it -- `test_schema_drift.py::
# test_metadata_indexes_match_live_schema` makes Postgres deparse both sides and
# compares, so a predicate or sort order written differently in the two places is
# drift even when it means the same thing.
#
# `ai_message (session_id, seq)` is DELIBERATELY ABSENT: `uq_ai_message_session_seq`
# already builds that index, and a second copy would be an unmodelled duplicate.
_INDEXES = [
    "CREATE INDEX ix_ai_session_org_user_recent"
    " ON ai_session (org_id, user_id, last_turn_at DESC)",
]


def _org_fk() -> sa.Column:
    return sa.Column(
        "org_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("organizations.id"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "ai_session",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("persona", sa.String(16), server_default="admin", nullable=False),
        sa.Column("mode", sa.String(8), server_default="read", nullable=False),
        sa.Column("title", sa.String(200), server_default="", nullable=False),
        sa.Column("model_id", sa.String(100), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_turn_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_output_tokens", sa.Integer(), server_default="0", nullable=False),
        # §3.1, §12.2: one legal value each in v1.
        sa.CheckConstraint("persona IN ('admin')", name="ck_ai_session_persona"),
        sa.CheckConstraint("mode IN ('read')", name="ck_ai_session_mode"),
    )

    op.create_table(
        "ai_message",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(12), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("tool_calls", postgresql.JSONB(), nullable=True),
        sa.Column("flags", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "seq", name="uq_ai_message_session_seq"),
        # §3.2: tool results are not stored.
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_ai_message_role"),
    )

    op.create_table(
        "ai_injection_flag",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("message_seq", sa.Integer(), nullable=False),
        sa.Column("source_tool", sa.String(100), nullable=False),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_id", sa.String(100), server_default="", nullable=False),
        sa.Column("matched_rule", sa.String(40), nullable=False),
        # Bounded by the column, not by the caller (§3.3).
        sa.Column("excerpt", sa.String(400), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "ai_settings",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            primary_key=True,
        ),
        # §0.10: the surface ships off.
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("provider_key", sa.String(40), nullable=True),
        sa.Column("transcript_retention_days", sa.Integer(), server_default="30", nullable=False),
        sa.Column(
            "max_turns_per_user_per_minute", sa.Integer(), server_default="6", nullable=False
        ),
        sa.Column(
            "max_turns_per_org_per_hour", sa.Integer(), server_default="200", nullable=False
        ),
        sa.Column("daily_token_budget", sa.Integer(), nullable=True),
        sa.Column(
            "updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # Ceilings this build added; see the module docstring, point 4.
        sa.CheckConstraint(
            "transcript_retention_days BETWEEN 1 AND 365",
            name="ck_ai_settings_retention_days",
        ),
        sa.CheckConstraint(
            "max_turns_per_user_per_minute BETWEEN 1 AND 60",
            name="ck_ai_settings_turns_per_user",
        ),
        sa.CheckConstraint(
            "max_turns_per_org_per_hour BETWEEN 1 AND 5000",
            name="ck_ai_settings_turns_per_org",
        ),
        sa.CheckConstraint(
            "daily_token_budget IS NULL OR daily_token_budget > 0",
            name="ck_ai_settings_token_budget",
        ),
    )

    for statement in _INDEXES:
        op.execute(statement)

    # Standard tenant_isolation pattern (0002_rls.py, 0033_sla.py, 0042, 0043).
    # WITH CHECK is stated explicitly. It is NOT filling a gap -- for an
    # ALL-command policy PostgreSQL reuses the USING expression as WITH CHECK
    # whenever WITH CHECK is omitted, so a USING-only policy here would reject a
    # cross-org INSERT identically. The explicit clause matters the day a future
    # edit gives this policy a different read predicate than write predicate.
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
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
        "(SELECT id FROM permissions WHERE module = 'ai')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'ai'")
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    # Dropping a table drops its indexes; every index in `_INDEXES` belongs to
    # one of these four, so there is nothing to drop separately.
    for table in reversed(_TABLES):
        op.drop_table(table)
