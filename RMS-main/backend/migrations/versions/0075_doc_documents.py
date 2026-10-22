"""documentation: stored documents (files)

`docs/modules/documentation/spec-attachments.md` §2.4, cycle A of §16.

Three tables, a forced `tenant_isolation` RLS policy on each, one FTS index,
and one new permission code with its role grants.

WHY RLS ON ALL THREE AND WHY `FORCE`. `docs/04-data-model.md` Conventions: a
tenant-scoped table without an RLS policy is a security bug, and FORCE is what
makes the policy apply to the table's OWNER too -- the admin role that runs
these migrations. `doc_document_versions` is the row that holds the object key,
so it is the row that stands between one tenant's token and another tenant's
bytes: the object store has no row-level security of its own, and §3.3's whole
argument rests on "the only way to learn a key is through an RLS-protected
row".

THE FTS INDEX INDEXES THREE NOT-NULL COLUMNS ON PURPOSE. `title || ' ' ||
description || ' ' || category` is NULL if any operand is NULL, and a NULL
`to_tsvector` argument indexes nothing while raising no error -- a search box
that silently matches nothing. All three columns are NOT NULL with defaults, so
the expression cannot go NULL. `filename` is deliberately NOT here: it lives on
`doc_document_versions`, a different row, and is matched with `ilike` against
the CURRENT version only (§8).

Revision ID: 0075
Revises: 0074
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("doc_documents", "doc_document_versions", "doc_document_links")

_PURGE = "docs.document.purge"


def upgrade() -> None:
    op.create_table(
        "doc_documents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "category", sa.String(length=100), nullable=False, server_default="General"
        ),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # 0, not 1: a document exists before its first file does (§4.1).
        sa.Column(
            "current_version", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_doc_documents_org_category", "doc_documents", ["org_id", "category"]
    )

    op.create_table(
        "doc_document_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        # The `s3://bucket/key` ref, or '' once purged. Never in a response.
        sa.Column("object_key", sa.Text(), nullable=False, server_default=""),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("viewer", sa.String(length=16), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("uploaded_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purged_by", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["doc_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["purged_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "version", name="uq_doc_document_versions_doc_version"
        ),
    )
    op.create_index(
        "ix_doc_document_versions_org_doc",
        "doc_document_versions",
        ["org_id", "document_id", "version"],
    )

    op.create_table(
        "doc_document_links",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("target_type", sa.String(length=20), nullable=False),
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["doc_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "target_type", "target_id", name="uq_doc_document_links_target"
        ),
    )
    op.create_index(
        "ix_doc_document_links_org_target",
        "doc_document_links",
        ["org_id", "target_type", "target_id"],
    )

    # RLS -- the standard `tenant_isolation` pattern 0002/0007 establish, on
    # every one of the three. See the module docstring for why the versions
    # table in particular cannot be the one that is forgotten.
    ctx = "current_setting('app.current_org', true)::uuid"
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (org_id = {ctx}) WITH CHECK (org_id = {ctx})"
        )

    # Metadata FTS (§8). Same shape as `ix_doc_articles_fts` so the console's
    # existing search groups one more `kind` and learns no new query.
    op.execute(
        "CREATE INDEX ix_doc_documents_fts ON doc_documents "
        "USING gin(to_tsvector('english', title || ' ' || description || ' ' || category))"
    )

    # ONE new code (§9). Seeded as the `(code, module, action)` tuple 0010 and
    # 0018 use for their own three-segment codes.
    op.execute(
        "INSERT INTO permissions (id, code, module, action) "
        f"VALUES (gen_random_uuid(), '{_PURGE}', 'docs', 'document.purge')"
    )
    # SUPER-ADMIN AND ORG-ADMIN ONLY -- technician is deliberately absent, and
    # that is the entire point of the code existing separately from
    # `docs.manage` (which technician holds). A technician can archive a
    # document; only an admin can destroy its bytes.
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r, permissions p "
        "WHERE r.name IN ('super-admin', 'org-admin') AND r.org_id IS NULL "
        f"AND p.code = '{_PURGE}'"
    )


def downgrade() -> None:
    # CI runs `upgrade head -> downgrade base -> upgrade head`, so this path is
    # exercised rather than assumed -- the class of defect 0001's `downgrade()`
    # shipped with (an enum left behind, so the next `upgrade head` died) is
    # caught there. Order matters twice below.
    #
    # 1. The permission rows go BEFORE the tables, and `role_permissions`
    #    before `permissions` -- the grant rows reference the code row.
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code = '{_PURGE}')"
    )
    op.execute(f"DELETE FROM permissions WHERE code = '{_PURGE}'")
    # 2. Policies and indexes go with their tables: `DROP TABLE` takes both,
    #    so there is nothing left behind for the next `upgrade head` to
    #    collide with. `doc_document_versions` and `doc_document_links` are
    #    dropped before `doc_documents` because both reference it.
    op.drop_index("ix_doc_document_links_org_target", table_name="doc_document_links")
    op.drop_table("doc_document_links")
    op.drop_index("ix_doc_document_versions_org_doc", table_name="doc_document_versions")
    op.drop_table("doc_document_versions")
    op.execute("DROP INDEX IF EXISTS ix_doc_documents_fts")
    op.drop_index("ix_doc_documents_org_category", table_name="doc_documents")
    op.drop_table("doc_documents")
