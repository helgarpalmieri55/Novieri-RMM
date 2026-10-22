"""Service Desk: ticket core -- queues, categories, tickets, comments,
attachments, approvals, status history.

docs/modules/service-desk/spec.md §2 (data model) and §8.2 (RBAC).

Revision ID: 0023
Revises: 0022
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = [
    ("ticket.read", "ticket", "read"),
    ("ticket.write", "ticket", "write"),
    ("ticket.assign", "ticket", "assign"),
    ("ticket.approve", "ticket", "approve"),
    ("ticket.admin", "ticket", "admin"),
]
_ALL = [c for c, _, _ in PERMISSIONS]
# Spec §0.6: the INVERSE of Budget. This is a technician's primary tool, so
# technician holds read/write/assign. `approve` is withheld deliberately -- §4
# forbids a requester approving their own request, and that separation is
# meaningless if every technician holds both halves.
ROLE_GRANTS = {
    "super-admin": _ALL,
    "org-admin": _ALL,
    "technician": ["ticket.read", "ticket.write", "ticket.assign"],
    "auditor": ["ticket.read"],
    "read-only": ["ticket.read"],
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
        CREATE TABLE ticket_queue (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            key varchar(64) NOT NULL,
            name varchar(255) NOT NULL,
            description text NOT NULL DEFAULT '',
            active boolean NOT NULL DEFAULT true,
            sort_order integer NOT NULL DEFAULT 0,
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, key)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE ticket_category (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            key varchar(64) NOT NULL,
            name varchar(255) NOT NULL,
            type varchar(16) NOT NULL,
            requires_approval boolean NOT NULL DEFAULT false,
            approver_role_id uuid REFERENCES roles(id),
            default_queue_id uuid REFERENCES ticket_queue(id),
            active boolean NOT NULL DEFAULT true,
            sort_order integer NOT NULL DEFAULT 0,
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, key)
        )
        """
    )
    # Spec §2: requires_approval is meaningful only for requests.
    op.execute(
        "ALTER TABLE ticket_category ADD CONSTRAINT ck_category_approval_is_request_only"
        " CHECK (type = 'request' OR requires_approval = false)"
    )

    # No surrogate id: org_id is the primary key (spec §2). Still org-scoped,
    # so test_rls.py demands RLS on it exactly like every other table here.
    op.execute(
        """
        CREATE TABLE ticket_number_counter (
            org_id uuid PRIMARY KEY REFERENCES organizations(id),
            next_incident integer NOT NULL DEFAULT 1,
            next_request integer NOT NULL DEFAULT 1
        )
        """
    )

    op.execute(
        """
        CREATE TABLE ticket (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            number varchar(24) NOT NULL,
            type varchar(16) NOT NULL,
            category_id uuid NOT NULL REFERENCES ticket_category(id),
            queue_id uuid NOT NULL REFERENCES ticket_queue(id),
            status varchar(32) NOT NULL,
            priority varchar(16) NOT NULL DEFAULT 'normal',
            subject varchar(255) NOT NULL,
            description text NOT NULL DEFAULT '',
            requester_id uuid REFERENCES users(id),
            assigned_to uuid REFERENCES users(id),
            device_id uuid REFERENCES devices(id),
            origin varchar(16) NOT NULL,
            source_alert_id uuid,
            resolved_at timestamptz,
            closed_at timestamptz,
            created_by uuid REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, number)
        )
        """
    )
    op.execute(
        "ALTER TABLE ticket ADD CONSTRAINT ck_ticket_status_matches_type CHECK ("
        " (type = 'incident' AND status IN"
        "   ('new','open','pending','resolved','closed','cancelled'))"
        " OR (type = 'request' AND status IN"
        "   ('new','pending_approval','in_progress','fulfilled','closed',"
        "    'rejected','cancelled')))"
    )
    # Spec §2: a machine-origin ticket has no human requester or author, and
    # inventing a synthetic user to satisfy NOT NULL would put a fake person in
    # the audit trail. Every other origin must keep its human attribution.
    op.execute(
        "ALTER TABLE ticket ADD CONSTRAINT ck_ticket_human_attribution CHECK ("
        " origin = 'alert' OR (requester_id IS NOT NULL AND created_by IS NOT NULL))"
    )
    op.execute("CREATE INDEX ix_ticket_org_status ON ticket (org_id, status)")
    op.execute(
        "CREATE INDEX ix_ticket_org_queue_status ON ticket (org_id, queue_id, status)"
    )
    op.execute(
        "CREATE INDEX ix_ticket_org_assigned_status ON ticket (org_id, assigned_to, status)"
    )
    op.execute("CREATE INDEX ix_ticket_org_requester ON ticket (org_id, requester_id)")
    op.execute("CREATE INDEX ix_ticket_org_device ON ticket (org_id, device_id)")

    op.execute(
        """
        CREATE TABLE ticket_comment (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            ticket_id uuid NOT NULL REFERENCES ticket(id),
            author_id uuid REFERENCES users(id),
            author_type varchar(16) NOT NULL,
            body text NOT NULL,
            visibility varchar(16) NOT NULL DEFAULT 'public',
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # Spec §2: a machine comment is never attributed to a person.
    op.execute(
        "ALTER TABLE ticket_comment ADD CONSTRAINT ck_comment_author CHECK ("
        " (author_type = 'user' AND author_id IS NOT NULL)"
        " OR (author_type = 'system' AND author_id IS NULL))"
    )
    op.execute("CREATE INDEX ix_ticket_comment_org_ticket ON ticket_comment (org_id, ticket_id)")

    op.execute(
        """
        CREATE TABLE ticket_attachment (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            ticket_id uuid NOT NULL REFERENCES ticket(id),
            object_key text NOT NULL,
            filename varchar(255) NOT NULL,
            byte_size integer NOT NULL,
            uploaded_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_ticket_attachment_org_ticket ON ticket_attachment (org_id, ticket_id)"
    )

    op.execute(
        """
        CREATE TABLE ticket_approval (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            ticket_id uuid NOT NULL REFERENCES ticket(id),
            state varchar(16) NOT NULL,
            decided_by uuid REFERENCES users(id),
            decided_at timestamptz,
            reason text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, ticket_id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE ticket_status_history (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            ticket_id uuid NOT NULL REFERENCES ticket(id),
            from_status varchar(32),
            to_status varchar(32) NOT NULL,
            actor_type varchar(16) NOT NULL,
            actor_id uuid REFERENCES users(id),
            reason text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_ticket_status_history_org_ticket "
        "ON ticket_status_history (org_id, ticket_id)"
    )

    for t in (
        "ticket_queue",
        "ticket_category",
        "ticket_number_counter",
        "ticket",
        "ticket_comment",
        "ticket_attachment",
        "ticket_approval",
        "ticket_status_history",
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
        "(SELECT id FROM permissions WHERE module = 'ticket')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'ticket'")
    for t in (
        "ticket_status_history",
        "ticket_approval",
        "ticket_attachment",
        "ticket_comment",
        "ticket",
        "ticket_number_counter",
        "ticket_category",
        "ticket_queue",
    ):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
