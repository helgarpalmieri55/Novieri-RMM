"""agent push-install, cycle B1: two authorization tables and two permission codes

`docs/modules/agent-distribution/spec-push-install.md` §3, §6.3, §8, §14, under
`CLAUDE.md` invariant 2a (approved by the operator 2026-08-26).

**THIS MIGRATION SHIPS A FEATURE THAT REFUSES EVERYTHING AND DISPATCHES
NOTHING.** No job type exists, no agent code exists, and no packet leaves the
building; what lands here is the storage and the permission vocabulary that
cycle B2's dispatch will be refused by. Spec §14 splits the cycles precisely so
the refusals can be found wrong while nothing can reach a machine.

TWO ORG-SCOPED TABLES, both in native `cidr` (spec §3, §6.3). The entire
question these tables answer is containment -- "is 10.0.4.17 inside a prefix
somebody authorized", "is it inside a prefix this credential is bound to" --
and in `text` that is a string comparison that is wrong at every boundary:
`10.200.7.5` starts with `10.20` and is nowhere near `10.20.0.0/16`. This is
IPAM's §0.1 reasoning (migration 0043) and site-server's `segments cidr[]`
(migration 0050), for the same reason. NO EXTENSION IS REQUIRED: `inet`, `cidr`
and the `inet_ops` GiST opclass are all core, so this adds no deploy change.

THREE THINGS HERE ARE NOT ORDINARY DDL:

1. **THE UNIQUE INDEXES ARE PARTIAL**, on `revoked_at IS NULL` and
   `unbound_at IS NULL`. Rows are never hard-deleted -- revoking an
   authorization or unbinding a scope STAMPS -- so a total unique index would
   make a prefix that was ever revoked unauthorizable forever. Partial is also
   why every upsert against them must INFER (`index_elements=` +
   `index_where=`): `ON CONFLICT ON CONSTRAINT` fails at runtime against a
   partial index, which has no `pg_constraint` row to name. Migration 0043 says
   the same thing about `uq_ipam_scope_active` and it is the same trap.

2. **`authorized_by` AND `bound_by` ARE NOT NULL.** There is no
   system-authorized push scope and no system-bound credential. A human holding
   `agent.push.scope.manage` put their name on every administrative login this
   platform will ever cause, and the service refuses `actor_id=None` before the
   database has to.

3. **NEITHER CODE IS SEEDED TO `technician`** (spec §8, D8). `agent.push.run`
   is code execution against machines that are NOT under management, which is a
   strictly larger act than anything a technician does today; `health.action.
   admin` sets the precedent ("configuring what alarms and executing code on
   endpoints are different acts and different blast radii"). Loosening a
   permission later is a decision; tightening one is an incident.

**NEITHER CODE IS A `PLATFORM_OPERATOR_CODE`.** Both act on rows that carry an
`org_id` and are RLS-scoped, so an organisation's own administrator holds them
-- unlike `agent.release.manage`, which writes the one catalogue with no
`org_id` that every tenant's endpoints run from (migration 0070).

**`agent.push.run` IS NOT SUFFICIENT ON ITS OWN**, and that conjunction is
enforced in the service rather than in this seed, because it is a rule about a
CALL and not about a grant: a push additionally requires `credentials.
checkout`, the code that already governs decrypting a stored secret, so push
cannot become a side door around the credential module's own gate (spec §2.5,
§8). `provisioning`'s `wake_on_lan` additionally requiring `site.wake` is the
same pattern in the same place.

THE DOWNGRADE IS WRITTEN TO RUN, and CI runs it: `upgrade head -> downgrade
base -> upgrade head`. It drops the policies, the tables (which drops their
indexes) and both permission rows with their grants.

Revision ID: 0077
Revises: 0076
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0077"
down_revision: str | None = "0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

# Creation order, which is also FK order. The RLS loop and the downgrade both
# walk this list (the latter in reverse).
_TABLES = [
    "push_authorization",
    "push_credential_scope",
]

# The live `permissions` table is (id, code, module, action) -- there is NO
# `resource` and NO `description` column. Descriptions live only in
# `app/core/rbac.py::PERMISSIONS`, matching every other module's migration.
PERMISSIONS = [
    ("agent.push.run", "agent", "push.run"),
    ("agent.push.scope.manage", "agent", "push.scope.manage"),
]

# Spec §8. `technician`, `auditor` and `read-only` receive NEITHER: there is no
# read code for this module in B1, and inventing a third code in the
# implementation would be diverging from an approved spec quietly.
#
# super-admin/org-admin receive both, which is also what keeps
# `test_rbac.py::test_org_admin_has_every_permission_except_the_platform_
# operator_codes` true -- and here that equality is the RIGHT answer rather
# than a test manufacturing a grant, because both codes act on org-scoped,
# RLS-protected rows.
_ALL = [c for c, _, _ in PERMISSIONS]
ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": _ALL,
    "org-admin": _ALL,
}

# Written here exactly as `app/modules/agent_push/models.py` declares them --
# `test_schema_drift.py::test_metadata_indexes_match_live_schema` makes
# Postgres deparse both sides and compares, so a predicate written differently
# in the two places is drift even when it means the same thing.
#
# `op.execute` rather than `op.create_index` so the GiST opclass is explicit
# and the partial predicates read as SQL. `inet_ops` is NOT the default GiST
# opclass for `cidr`, so naming it is required, not decoration.
_INDEXES = [
    "CREATE UNIQUE INDEX uq_push_authorization_active"
    " ON push_authorization (org_id, prefix) WHERE revoked_at IS NULL",
    "CREATE INDEX ix_push_authorization_gist"
    " ON push_authorization USING gist (prefix inet_ops)",
    "CREATE UNIQUE INDEX uq_push_credential_scope_active"
    " ON push_credential_scope (org_id, credential_id, prefix)"
    " WHERE unbound_at IS NULL",
    "CREATE INDEX ix_push_credential_scope_gist"
    " ON push_credential_scope USING gist (prefix inet_ops)",
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
        "push_authorization",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        sa.Column("prefix", postgresql.CIDR(), nullable=False),
        sa.Column("note", sa.String(500), server_default="", nullable=False),
        # NOT NULL: there is no system-authorized push scope. A human with
        # `agent.push.scope.manage` put their name on every administrative
        # login this platform will ever cause.
        sa.Column(
            "authorized_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        # Rows are never hard-deleted: revocation stamps this, so the record of
        # what was authorized when survives as evidence.
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "revoked_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
    )

    op.create_table(
        "push_credential_scope",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        sa.Column(
            "credential_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("credentials.id"),
            nullable=False,
        ),
        sa.Column("prefix", postgresql.CIDR(), nullable=False),
        sa.Column("note", sa.String(500), server_default="", nullable=False),
        sa.Column(
            "bound_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("unbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "unbound_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
    )

    for statement in _INDEXES:
        op.execute(statement)

    # Standard tenant_isolation pattern (0002_rls.py, 0043_ipam.py, 0050). WITH
    # CHECK is stated explicitly. It is NOT filling a gap -- for an ALL-command
    # policy PostgreSQL reuses the USING expression as WITH CHECK whenever WITH
    # CHECK is omitted -- but the explicit clause matters the day a future edit
    # gives this policy a different read predicate than write predicate. A push
    # authorization or a credential scope that leaked across tenants would let
    # one customer's grant authorize an administrative login on another
    # customer's wire, so this is the table where that day must not arrive
    # quietly.
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
    # BY CODE, NOT BY MODULE. `module = 'agent'` already holds
    # `agent.release.read` and `agent.release.manage` from migrations 0060 and
    # 0070; deleting by module here would revoke the ability to install an
    # agent at all on the way down, which `downgrade base -> upgrade head`
    # would then not restore in the right order.
    code_list = ", ".join(f"'{c}'" for c, _, _ in PERMISSIONS)
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code IN ({code_list}))"
    )
    op.execute(f"DELETE FROM permissions WHERE code IN ({code_list})")
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    # Dropping a table drops its indexes; every index in `_INDEXES` belongs to
    # one of these two, so there is nothing to drop separately.
    for table in reversed(_TABLES):
        op.drop_table(table)
