from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

EXPECTED_TABLES = {
    "organizations",
    "users",
    "roles",
    "permissions",
    "role_permissions",
    "user_roles",
    "devices",
    "device_groups",
    "device_tags",
    "agents",
    "policies",
    "credentials",
    "jobs",
    "job_logs",
    "audit_events",
    "enrollment_tokens",
    # inventory module (migration 0005)
    "inventory_snapshots",
    "installed_software",
    "inventory_changes",
    "hardware_components",
    # security-logs module (migration 0006)
    "security_events",
    # documentation module (migration 0007)
    "doc_articles",
    "doc_records",
    "doc_links",
    "doc_versions",
    # credential vault (migration 0008)
    "org_vault_keys",
}


async def test_upgrade_head_creates_core_tables(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        )
        tables = {r.table_name for r in rows}
    missing = EXPECTED_TABLES - tables
    assert not missing, f"missing tables: {missing}"


async def test_job_status_enum_values(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT e.enumlabel FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid "
                "WHERE t.typname = 'job_status' ORDER BY e.enumsortorder"
            )
        )
        values = [r.enumlabel for r in rows]
    assert values == [
        "queued",
        "dispatched",
        "running",
        "succeeded",
        "failed",
        "expired",
        "refused",
        # Migration 0071. APPENDED, never inserted: `enumsortorder` is what
        # `ORDER BY status` and every `<`/`>` on the column mean, so placing a
        # value in the middle would silently re-sort rows written before it
        # existed. `cancelled` is terminal, like the four before it.
        "cancelled",
    ]


async def test_jobs_carries_the_cancellation_columns(admin_engine: AsyncEngine) -> None:
    """Migration 0071's three columns, and their nullability.

    All three are NULLABLE and none has a default: a job nobody asked to
    cancel has no request time, no requester and no outcome, and a zero value
    for any of them would be a claim about an act that never happened.
    """
    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns WHERE table_name = 'jobs' "
                "AND column_name LIKE 'cancel%'"
            )
        )
        columns = {r.column_name: (r.data_type, r.is_nullable) for r in rows}
    assert columns == {
        "cancel_requested_at": ("timestamp with time zone", "YES"),
        "cancel_requested_by": ("uuid", "YES"),
        "cancel_outcome": ("character varying", "YES"),
    }


async def test_jobs_carries_the_retry_lineage_column(admin_engine: AsyncEngine) -> None:
    """Migration 0076's one column, and the self-referencing foreign key.

    NULLABLE WITH NO DEFAULT: a job that is not a retry has no original, and
    any non-null default would be a claim about a lineage that does not exist.

    THE FOREIGN KEY POINTS AT `jobs` ITSELF, which is what makes "retry of"
    checkable rather than a loose uuid. It is deliberately NOT `ON DELETE
    SET NULL` or `CASCADE`: this platform never deletes a job row, and if that
    ever changes a constraint that REFUSES is the right failure -- a retry
    whose original has silently vanished is a lineage that lies.
    """
    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_name = 'jobs' "
                "AND column_name = 'retry_of_job_id'"
            )
        )
        columns = {r.column_name: (r.data_type, r.is_nullable, r.column_default) for r in rows}
        fks = await conn.execute(
            text(
                "SELECT ccu.table_name AS target, rc.delete_rule "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON ccu.constraint_name = tc.constraint_name "
                "JOIN information_schema.referential_constraints rc "
                "  ON rc.constraint_name = tc.constraint_name "
                "WHERE tc.constraint_name = 'jobs_retry_of_job_id_fkey'"
            )
        )
        targets = [(r.target, r.delete_rule) for r in fks]
    assert columns == {"retry_of_job_id": ("uuid", "YES", None)}
    assert targets == [("jobs", "NO ACTION")]


async def test_security_events_append_only(admin_engine: AsyncEngine, app_engine) -> None:
    """The app role can never rewrite security evidence (spec §3): UPDATE and
    DELETE are revoked; only the worker's admin-engine retention path deletes."""
    import pytest
    from sqlalchemy.exc import DBAPIError, ProgrammingError

    with pytest.raises((DBAPIError, ProgrammingError)):
        async with app_engine.connect() as conn:
            await conn.execute(text("UPDATE security_events SET username = 'x'"))
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with app_engine.connect() as conn:
            await conn.execute(text("DELETE FROM security_events"))


async def test_article_full_text_search_has_its_index(admin_engine: AsyncEngine) -> None:
    """`ix_doc_articles_fts` exists, and is the GIN expression index it claims.

    IT DID NOT EXIST FOR A MONTH. 0007 created it in raw SQL and never declared
    it on `DocArticle`. 0008 was autogenerated one migration later, saw an index
    its metadata did not recognise, and emitted a bare
    `op.drop_index('ix_doc_articles_fts')` into its `upgrade()` -- so every
    database upgraded past 0008 lost it. Article search has been running
    `to_tsvector(...) @@ ...` on a sequential scan ever since and no test went
    red, because no test asked whether the index was there.

    Asserting the ACCESS METHOD and the expression, not just the name: a btree
    of the same name satisfies `indexname` and answers no `@@` query at all.
    """
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT am.amname AS access_method, "
                    "pg_get_indexdef(i.indexrelid) AS definition "
                    "FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid "
                    "JOIN pg_am am ON am.oid = c.relam "
                    "WHERE c.relname = 'ix_doc_articles_fts'"
                )
            )
        ).one_or_none()
    assert row is not None, "ix_doc_articles_fts is missing: article FTS is a sequential scan"
    assert row.access_method == "gin"
    assert "to_tsvector" in row.definition
    assert "title" in row.definition and "body" in row.definition


async def test_no_index_in_the_database_is_unknown_to_the_models(
    admin_engine: AsyncEngine,
) -> None:
    """Every standalone index at head is declared on a model, and vice versa.

    THIS IS THE GUARD FOR THE CLASS, not for the one instance above. An index
    the model metadata does not know about is not merely undocumented -- the
    next `alembic revision --autogenerate` emits a bare `op.drop_index` for it,
    which a reviewer reads as one line of noise in a hundred-line diff. That is
    exactly how `ix_doc_articles_fts` died, and running autogenerate against
    head today showed `ix_devices_org_provenance` (migration 0078) loaded the
    same way, four days old and one autogenerate from gone.

    A DECLARED expression index does still appear in autogenerate, because
    SQLAlchemy renders the expression differently from the way Postgres reads it
    back -- but as a drop/add PAIR, which is survivable: applying it recreates
    the index. A bare drop is not, and that is the whole difference this test
    is defending.

    THE REVERSE DIRECTION IS THE HALF THAT CATCHES THE ORIGINAL: an index the
    models declare and the database does not have is a migration that was never
    written, or one that was silently undone.
    """
    import app.models  # noqa: F401  (importing registers every table on the metadata)
    from app.models.base import Base

    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            text(
                # Constraint-backed indexes (PRIMARY KEY, UNIQUE) belong to the
                # constraint, not to an `Index()`, and autogenerate compares them
                # as constraints. Only standalone indexes are in scope here.
                "SELECT c.relname AS name, t.relname AS table_name "
                "FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "JOIN pg_class t ON t.oid = i.indrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = 'public' "
                "AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid)"
            )
        )
        # Only tables the models describe are the models' to own. `alembic_version`
        # is alembic's, and anything a future extension creates is its own.
        tables = set(Base.metadata.tables)
        in_database = {r.name for r in rows if r.table_name in tables}

    declared = {
        index.name
        for table in Base.metadata.tables.values()
        for index in table.indexes
        if index.name
    }

    undeclared = sorted(in_database - declared)
    missing = sorted(declared - in_database)

    assert not undeclared, (
        "these indexes exist in the database and no model declares them, so the next "
        f"autogenerate will emit a bare drop_index for each: {undeclared}"
    )
    assert not missing, (
        "these indexes are declared on a model and are not in the database -- a migration "
        f"was never written, or one silently removed them: {missing}"
    )
