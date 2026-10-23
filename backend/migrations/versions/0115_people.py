"""people: one identity per person, starter and leaver checklists

docs/modules/people/spec.md §1. Five org-scoped tables under forced RLS,
`person_id` on `seat_assignment` and `license_allocation`, three permission
codes, and the backfill in §1.1. The backfill only links on an exact,
case-insensitive email match; anything else is left for review.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0115"
down_revision: str | None = "0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

_TABLES = [
    "person",
    "person_login",
    "device_person",
    "person_checklist",
    "person_checklist_step",
]

PERMISSIONS = [
    ("people.read", "people", "read"),
    ("people.manage", "people", "manage"),
    ("people.offboard", "people", "offboard"),
]
ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": ["people.read", "people.manage", "people.offboard"],
    "org-admin": ["people.read", "people.manage", "people.offboard"],
    "technician": ["people.read", "people.manage"],
    "auditor": ["people.read"],
    "read-only": ["people.read"],
}


def _uuid(name: str, *fk: str, nullable: bool = False, **kw: object) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        *(sa.ForeignKey(f) for f in fk),
        nullable=nullable,
        **kw,  # type: ignore[arg-type]
    )


def _ts(name: str, *, nullable: bool = True, now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if now else None,
    )


def upgrade() -> None:
    op.create_table(
        "person",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid("org_id", "organizations.id"),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        _uuid("user_id", "users.id", nullable=True, unique=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="active"),
        sa.Column("department", sa.String(200), nullable=True),
        sa.Column("job_title", sa.String(200), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("source", sa.String(12), nullable=False, server_default="operator"),
        _uuid("created_by", "users.id", nullable=True),
        _ts("created_at", nullable=False, now=True),
        _ts("updated_at", nullable=False, now=True),
        sa.CheckConstraint(
            "status IN ('starting', 'active', 'leaving', 'left')", name="ck_person_status"
        ),
        sa.CheckConstraint("source IN ('account', 'operator')", name="ck_person_source"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_person_org_lower_email ON person (org_id, lower(email))"
        " WHERE email IS NOT NULL"
    )
    op.create_index("ix_person_org_status", "person", ["org_id", "status"])

    op.create_table(
        "person_login",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid("org_id", "organizations.id"),
        _uuid("person_id", "person.id"),
        sa.Column("login", sa.String(256), nullable=False),
        _uuid("created_by", "users.id", nullable=True),
        _ts("created_at", nullable=False, now=True),
        sa.UniqueConstraint("org_id", "login", name="uq_person_login_org_login"),
    )
    op.create_index("ix_person_login_person", "person_login", ["person_id"])

    op.create_table(
        "device_person",
        _uuid("device_id", "devices.id", primary_key=True),
        _uuid("org_id", "organizations.id"),
        _uuid("person_id", "person.id", nullable=True),
        sa.Column("source", sa.String(12), nullable=False, server_default="inventory"),
        sa.Column("observed_login", sa.String(256), nullable=True),
        _ts("observed_at"),
        _uuid("set_by", "users.id", nullable=True),
        _ts("updated_at", nullable=False, now=True),
        sa.CheckConstraint(
            "source IN ('inventory', 'operator')", name="ck_device_person_source"
        ),
    )
    op.create_index("ix_device_person_org_person", "device_person", ["org_id", "person_id"])

    op.create_table(
        "person_checklist",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid("org_id", "organizations.id"),
        _uuid("person_id", "person.id"),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="open"),
        _uuid("started_by", "users.id"),
        _ts("started_at", nullable=False, now=True),
        _uuid("closed_by", "users.id", nullable=True),
        _ts("closed_at"),
        sa.CheckConstraint("kind IN ('starter', 'leaver')", name="ck_person_checklist_kind"),
        sa.CheckConstraint(
            "status IN ('open', 'completed', 'cancelled')", name="ck_person_checklist_status"
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_person_checklist_open ON person_checklist (person_id)"
        " WHERE status = 'open'"
    )
    op.create_index(
        "ix_person_checklist_org_status", "person_checklist", ["org_id", "status"]
    )

    op.create_table(
        "person_checklist_step",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid("org_id", "organizations.id"),
        _uuid("checklist_id", "person_checklist.id"),
        sa.Column("key", sa.String(40), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("state", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("note", sa.String(500), nullable=False, server_default=""),
        _uuid("updated_by", "users.id", nullable=True),
        _ts("updated_at"),
        sa.UniqueConstraint("checklist_id", "key", name="uq_person_checklist_step_key"),
        sa.CheckConstraint(
            "state IN ('pending', 'done', 'skipped')", name="ck_person_checklist_step_state"
        ),
    )

    op.add_column("seat_assignment", _uuid("person_id", "person.id", nullable=True))
    op.create_index(
        "ix_seat_assignment_org_person", "seat_assignment", ["org_id", "person_id"]
    )
    op.add_column("license_allocation", _uuid("person_id", "person.id", nullable=True))
    op.create_index(
        "ix_license_allocation_org_person", "license_allocation", ["org_id", "person_id"]
    )

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

    _backfill()


def _backfill() -> None:
    # 1. One person per account address; an active account wins over a
    # deleted one, then the oldest. Accounts with no address get their own.
    op.execute(
        """
        INSERT INTO person (id, org_id, display_name, email, user_id, status, source)
        SELECT gen_random_uuid(), u.org_id,
               left(COALESCE(NULLIF(btrim(u.display_name), ''), u.email), 200),
               u.email, u.id,
               CASE WHEN u.status = 'deleted' THEN 'left' ELSE 'active' END,
               'account'
        FROM (
            SELECT DISTINCT ON (org_id, lower(btrim(email)))
                   id, org_id, btrim(email) AS email, display_name, status
            FROM users
            WHERE btrim(email) <> ''
            ORDER BY org_id, lower(btrim(email)), (status = 'active') DESC, created_at, id
        ) u
        """
    )
    op.execute(
        """
        INSERT INTO person (id, org_id, display_name, email, user_id, status, source)
        SELECT gen_random_uuid(), org_id,
               left(COALESCE(NULLIF(btrim(display_name), ''), external_subject), 200),
               NULL, id,
               CASE WHEN status = 'deleted' THEN 'left' ELSE 'active' END,
               'account'
        FROM users
        WHERE btrim(email) = ''
        """
    )
    # 2. Seats whose free-text address matches a person.
    op.execute(
        """
        UPDATE seat_assignment s SET person_id = p.id
        FROM person p
        WHERE p.org_id = s.org_id AND p.email IS NOT NULL
          AND lower(p.email) = lower(btrim(s.assignee_email))
        """
    )
    # 3. Licence allocations to an account: the person linked to it, or the
    # person holding its address (a second account for the same address).
    op.execute(
        """
        UPDATE license_allocation a SET person_id = p.id
        FROM users u
        JOIN person p ON p.org_id = u.org_id
             AND (p.user_id = u.id
                  OR (p.email IS NOT NULL AND lower(p.email) = lower(btrim(u.email))))
        WHERE a.target_type = 'user' AND u.id = a.target_id AND u.org_id = a.org_id
        """
    )
    # 4. Devices whose owner field is a person's address. A person typed it,
    # so the link is an operator one and inventory will not replace it.
    op.execute(
        """
        INSERT INTO device_person (device_id, org_id, person_id, source)
        SELECT d.id, d.org_id, p.id, 'operator'
        FROM devices d
        JOIN person p ON p.org_id = d.org_id AND p.email IS NOT NULL
             AND lower(p.email) = lower(btrim(d.owner))
        """
    )


def downgrade() -> None:
    code_list = ", ".join(f"'{c}'" for c, _, _ in PERMISSIONS)
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code IN ({code_list}))"
    )
    op.execute(f"DELETE FROM permissions WHERE code IN ({code_list})")
    op.drop_index("ix_license_allocation_org_person", table_name="license_allocation")
    op.drop_column("license_allocation", "person_id")
    op.drop_index("ix_seat_assignment_org_person", table_name="seat_assignment")
    op.drop_column("seat_assignment", "person_id")
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    for table in reversed(_TABLES):
        op.drop_table(table)
