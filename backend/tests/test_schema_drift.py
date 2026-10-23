"""Guards against drift between `app.models` (`Base.metadata`) and the real
database schema produced by `alembic upgrade head`.

Why this exists: this repo has no `create_all` anywhere. Test schema comes
*only* from running the Alembic migrations, and asyncpg binds query
parameters against the live column at the wire protocol level -- it does not
consult the SQLAlchemy model's declared type at all. That means a model
declaring `Numeric(14, 2)` against a migration that actually created
`numeric(12, 2)` passes every single test in the suite, silently, until
production data exceeds the real column. Two such mismatches were caught in
Phase 13 only because a human happened to read the migration beside the
model. This test makes that check automatic and total: it derives its own
list of tables/columns to check by introspecting both `Base.metadata` and
`information_schema`, so it needs no hand-maintained table list and
automatically covers every module registered into `app.models`.

Mirrors the shape of `test_rls.py::test_every_org_scoped_table_has_forced_rls_policy`:
introspect the live (post-migration) database, derive the subject list from
that introspection plus the ORM metadata, accumulate every problem found into
a list, and assert on the whole list at the end -- so one run reports every
offender instead of dying on the first `assert`.

Two tests, the same shape over the same schema:
  - `test_metadata_matches_live_schema` -- tables, columns, nullability and
    the column shapes that silently corrupt data (numeric precision/scale,
    varchar length).
  - `test_metadata_indexes_match_live_schema` -- indexes, including the
    partial-unique ones that several modules rely on to enforce a uniqueness
    rule a constraint cannot express. Read that test's docstring for how the
    `WHERE` predicate is compared without tripping over Postgres's
    normalisation of the expression it stores, and for what that comparison
    does and does not guarantee.
"""

import re

from sqlalchemy import Enum, Numeric, String, Text
from sqlalchemy import text as sqltext
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.schema import CreateIndex
from sqlalchemy.sql.type_api import TypeEngine

from app.models import Base

# Tables that exist in the database but are not, and never will be, modelled
# in `Base.metadata` -- Alembic's own bookkeeping table.
_UNMODELLED_TABLES = {"alembic_version"}

_PG_DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]

# `pg_get_indexdef` schema-qualifies the table: `... ON public.sla_policy ...`
# for the real index, `... ON pg_temp_4.sla_policy ...` for the normalisation
# clone. Everything after the table name is what we actually compare, so the
# qualifier (and only the first one, which is always the table reference) is
# stripped from both sides.
_ON_SCHEMA = re.compile(r"^(CREATE (?:UNIQUE )?INDEX .+? ON )[^ .]+\.")


async def test_metadata_matches_live_schema(admin_engine: AsyncEngine) -> None:
    """`Base.metadata` must describe exactly the schema `alembic upgrade head`
    produces: same tables, same columns, agreeing nullability, and -- for the
    column shapes that actually corrupt data if they drift -- agreeing
    numeric precision/scale and varchar length.

    Deliberately NOT compared, because the mapping from a SQLAlchemy type to
    an `information_schema` type is not 1:1 and asserting on it would produce
    noise that has to be suppressed rather than trusted:
      - Exact SQL type name / `data_type` / `udt_name` (e.g. `Enum` compiles to
        a `USER-DEFINED` postgres enum type named after the model's `name=`;
        `UUID` vs `uuid`; `JSONB` vs `jsonb` with an `astext_type`). Getting
        the *shape* of the column right (nullability, precision/scale,
        length) is what prevents silent data loss; the exact type label is
        not. `sqlalchemy.Enum` is a `String` subclass (it carries a
        `.length` derived from its longest member, for backends that
        fall back to varchar) but is deliberately excluded from the
        varchar-length check: as a native Postgres enum it has no
        `character_maximum_length` at all, so comparing it would be a
        guaranteed false positive, not a real check.
      - `server_default` / client-side `default=` values. These legitimately
        differ in spelling between the ORM (`default=dict`, Python-side) and
        the migration (`server_default=...`, DB-side) and are exercised by
        each module's own model/behavior tests, not by a schema-shape check.
      - Enum member sets. `test_migrations.py::test_job_status_enum_values`
        already pins `job_status`'s labels explicitly; duplicating that here
        per-enum would be per-module maintenance, which this test exists to
        avoid.
      - Constraints, foreign keys, defaults, and RLS policies. Covered by
        other tests (`test_rls.py`, per-migration tests) or not
        security/correctness-relevant at the column-shape level this test
        targets. Indexes are compared by
        `test_metadata_indexes_match_live_schema` below.
    """
    async with admin_engine.connect() as conn:
        db_tables = {
            r.table_name
            for r in await conn.execute(
                sqltext(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema='public' AND table_type='BASE TABLE'"
                )
            )
        }
        rows = await conn.execute(
            sqltext(
                "SELECT table_name, column_name, is_nullable, numeric_precision,"
                " numeric_scale, character_maximum_length"
                " FROM information_schema.columns WHERE table_schema='public'"
            )
        )
        db_columns: dict[str, dict[str, object]] = {}
        for r in rows:
            db_columns.setdefault(r.table_name, {})[r.column_name] = {
                "nullable": r.is_nullable == "YES",
                "numeric_precision": r.numeric_precision,
                "numeric_scale": r.numeric_scale,
                "char_max_length": r.character_maximum_length,
            }

    model_tables = set(Base.metadata.tables)

    problems: list[str] = []

    missing_in_db = model_tables - db_tables
    for table in sorted(missing_in_db):
        problems.append(f"{table}: modelled in Base.metadata but has no table in the database")

    extra_in_db = db_tables - model_tables - _UNMODELLED_TABLES
    for table in sorted(extra_in_db):
        problems.append(f"{table}: exists in the database but is not modelled in Base.metadata")

    for table_name in sorted(model_tables & db_tables):
        table = Base.metadata.tables[table_name]
        model_cols = {c.name: c for c in table.columns}
        db_cols = db_columns.get(table_name, {})

        missing_cols = set(model_cols) - set(db_cols)
        for col in sorted(missing_cols):
            problems.append(f"{table_name}.{col}: modelled but no such column in the database")

        extra_cols = set(db_cols) - set(model_cols)
        for col in sorted(extra_cols):
            problems.append(f"{table_name}.{col}: column in the database but not modelled")

        for col_name in sorted(set(model_cols) & set(db_cols)):
            column = model_cols[col_name]
            db_col = db_cols[col_name]
            col_type: TypeEngine = column.type

            model_nullable = bool(column.nullable)
            db_nullable = bool(db_col["nullable"])
            if model_nullable != db_nullable:
                problems.append(
                    f"{table_name}.{col_name}: NOT NULL mismatch"
                    f" (model nullable={model_nullable}, db nullable={db_nullable})"
                )

            if isinstance(col_type, Numeric):
                model_precision = col_type.precision
                model_scale = col_type.scale
                db_precision = db_col["numeric_precision"]
                db_scale = db_col["numeric_scale"]
                if model_precision is not None and model_precision != db_precision:
                    problems.append(
                        f"{table_name}.{col_name}: numeric precision mismatch"
                        f" (model={model_precision}, db={db_precision})"
                    )
                if model_scale is not None and model_scale != db_scale:
                    problems.append(
                        f"{table_name}.{col_name}: numeric scale mismatch"
                        f" (model={model_scale}, db={db_scale})"
                    )

            if (
                isinstance(col_type, String)
                and not isinstance(col_type, Text)
                and not isinstance(col_type, Enum)
            ):
                model_length = col_type.length
                db_length = db_col["char_max_length"]
                if model_length is not None and model_length != db_length:
                    problems.append(
                        f"{table_name}.{col_name}: varchar length mismatch"
                        f" (model={model_length}, db={db_length})"
                    )

    assert not problems, "\n" + "\n".join(problems)


def _strip_schema(indexdef: str) -> str:
    return _ON_SCHEMA.sub(r"\1", indexdef.strip(), count=1)


async def test_metadata_indexes_match_live_schema(admin_engine: AsyncEngine) -> None:
    """Every index in `Base.metadata` must exist in the migration-built schema
    with the same columns in the same order, the same uniqueness, and -- for
    partial indexes -- the same `WHERE` predicate; and the schema must hold no
    unmodelled index.

    Why an index is worth this: several modules enforce correctness with
    PARTIAL UNIQUE indexes, because a plain `UNIQUE` constraint cannot express
    them -- NULLs do not collide in Postgres, so "at most one active SLA policy
    per resolution level" (`sla_policy`, four indexes, each keyed on a
    different NULL/NOT NULL combination of `queue_id`/`priority`), "one budget
    line per category with, and one without, a cost centre" (`budget_line`),
    and "one autofeed expense per source link" (`expense`) are all indexes and
    nothing else. If the predicate in `models.py` and the predicate in the
    migration disagree, the uniqueness rule the module believes it has is not
    the rule the database enforces, and every other test still passes --
    `sla/policies.py::_POLICY_INDEX_BY_LEVEL` even repeats those predicates a
    third time as its `ON CONFLICT` arbiters. Indexes also drift
    deliberately (migration 0039 repointed two of 0033's partial indexes when
    the sweep query widened), which is exactly when a silent divergence between
    the redefinition and the model is easiest to introduce.

    HOW THE PREDICATE IS COMPARED, since this is the part that is easy to get
    wrong: Postgres does not store predicate text, it stores a parsed node
    tree, and `pg_get_indexdef` deparses it -- adding casts (`source <>
    'manual'` comes back as `(source)::text <> 'manual'::text`), full
    parenthesisation, and canonical spelling. Comparing that against the
    model's raw `postgresql_where=text(...)` string would fail on every partial
    index in the repo. So this test does not compare strings written by two
    different authors: it makes Postgres deparse BOTH sides. For each table it
    creates a temporary clone (`CREATE TEMP TABLE x (LIKE public.x)`, same
    name, same column types, in `pg_temp` which precedes `public` on the
    search path), issues the ORM index's own compiled `CREATE INDEX` DDL
    against that clone, and reads back `pg_get_indexdef` for it. Both sides are
    then the output of the same deparser over columns of the same types, so a
    string comparison of the two is meaningful: casts, parenthesisation and
    spelling are canonical on both sides. The clone is temporary and the whole
    thing runs in a transaction that is rolled back, so no lock is taken on a
    real table and nothing is left behind. Because the comparison is over the
    full `pg_get_indexdef`, it covers not just the predicate but the access
    method, column order, expression columns (`lower(email)` in the model
    normalises to the stored `lower((email)::text)` and matches), operator
    classes, `INCLUDE`/`NULLS NOT DISTINCT`, sort order and storage parameters.

    WHAT THAT DOES AND DOES NOT GUARANTEE -- stated exactly, because a drift
    test is trusted and must not look stronger than it is. If two definitions
    compare equal they ARE the same index: same deparser, same column types,
    identical output means identical DDL. The converse does not hold.
    Postgres canonicalises the FORM of a predicate, not its LOGIC: it does not
    reorder the arguments of `AND`/`OR`, and it does not rewrite an operator
    into an equivalent one. So a model predicate that says the same thing in a
    different shape than the migration --
    `priority IS NULL AND queue_id IS NOT NULL AND active` against the
    migration's `queue_id IS NOT NULL AND priority IS NULL AND active`, or
    `active = true` against `active` -- is reported as a mismatch even though
    the database enforces the identical rule. Both were tried; both fail. That
    is a false FAILURE, never a false pass, and the fix is to write the two the
    same way, which is what `sla/models.py` already demands of its third copy
    of these predicates in `policies.py::_POLICY_INDEX_BY_LEVEL`. Every index
    in the repo agrees today with no suppression, so this costs nothing now.

    Deliberately NOT compared:
      - Indexes that back a constraint -- PRIMARY KEY, UNIQUE, EXCLUDE. The ORM
        expresses those as `PrimaryKeyConstraint`/`UniqueConstraint`/
        `Column(unique=True)`, which are `table.constraints`, not
        `table.indexes`, so they are not comparable index-to-index and are
        excluded from BOTH sides (399 of the 481 indexes in the current schema
        are constraint-backed). Constraint drift is a real, separate gap that
        this test does not close. It is however not silent in the one direction
        that matters here: if a model declares an `Index` where the migration
        made a constraint (or vice versa) the name shows up as
        missing/unmodelled below, and the failure message says which.
      - Index tablespace and comments (`pg_get_indexdef` omits both) and index
        validity (`indisvalid`), which cannot drift from a model.
      - Migration history. This compares `models.py` against the schema at
        HEAD, so an index created and later dropped by a subsequent migration
        is correctly invisible here.
    """
    live_defs: dict[tuple[str, str], str] = {}
    constraint_backed: dict[tuple[str, str], str] = {}

    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            sqltext(
                "SELECT t.relname AS table_name, i.relname AS index_name,"
                " ix.indisprimary AS is_primary, c.contype AS contype,"
                " pg_get_indexdef(ix.indexrelid) AS indexdef"
                " FROM pg_index ix"
                " JOIN pg_class i ON i.oid = ix.indexrelid"
                " JOIN pg_class t ON t.oid = ix.indrelid"
                " JOIN pg_namespace n ON n.oid = t.relnamespace"
                " LEFT JOIN pg_constraint c ON c.conindid = ix.indexrelid"
                " WHERE n.nspname = 'public' AND t.relkind IN ('r', 'p')"
            )
        )
        for r in rows:
            key = (r.table_name, r.index_name)
            contype = r.contype.decode() if isinstance(r.contype, bytes) else r.contype
            if r.is_primary or contype is not None:
                constraint_backed[key] = "primary key" if r.is_primary else f"contype={contype}"
            else:
                live_defs[key] = _strip_schema(r.indexdef)

        db_tables = {
            r.table_name
            for r in await conn.execute(
                sqltext(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema='public' AND table_type='BASE TABLE'"
                )
            )
        }

        problems: list[str] = []
        model_defs: dict[tuple[str, str], str] = {}
        unnormalisable: set[tuple[str, str]] = set()

        # Normalise the ORM's indexes through Postgres itself, one table at a
        # time, on a throwaway clone of that table.
        # The SELECTs above have already autobegun a transaction on this
        # connection; everything below happens inside it and is rolled back.
        try:
            await conn.execute(sqltext("SET LOCAL search_path TO pg_temp, public"))
            for table_name in sorted(set(Base.metadata.tables) & db_tables):
                table = Base.metadata.tables[table_name]
                if not table.indexes:
                    continue
                sp = await conn.begin_nested()
                try:
                    await conn.execute(
                        sqltext(f'CREATE TEMP TABLE "{table_name}" (LIKE public."{table_name}")')
                    )
                    for index in sorted(table.indexes, key=lambda i: i.name or ""):
                        key = (table_name, str(index.name))
                        ddl = str(CreateIndex(index).compile(dialect=_PG_DIALECT)).strip()
                        inner = await conn.begin_nested()
                        try:
                            await conn.execute(sqltext(ddl))
                            await inner.commit()
                        except Exception as exc:  # noqa: BLE001 -- reported, not raised
                            await inner.rollback()
                            unnormalisable.add(key)
                            problems.append(
                                f"{table_name}.{index.name}: the index as modelled cannot be"
                                f" created against the live table shape ({exc.__class__.__name__}:"
                                f" {str(exc).splitlines()[0]}); modelled DDL was: {ddl}"
                            )
                            continue
                        defn = (
                            await conn.execute(
                                sqltext(
                                    "SELECT pg_get_indexdef(i.oid) AS d FROM pg_class i"
                                    " WHERE i.relnamespace = pg_my_temp_schema()"
                                    " AND i.relname = :name"
                                ),
                                {"name": index.name},
                            )
                        ).scalar_one()
                        model_defs[key] = _strip_schema(defn)
                finally:
                    await sp.rollback()
        finally:
            await conn.rollback()

    for key in sorted(set(model_defs) - set(live_defs)):
        table_name, index_name = key
        hint = ""
        if key in constraint_backed:
            hint = (
                f" -- the database has an index of that name but it backs a constraint"
                f" ({constraint_backed[key]}); the migration made a constraint where the"
                f" model declares an Index"
            )
        problems.append(
            f"{table_name}.{index_name}: modelled in Base.metadata but no such index in the"
            f" database{hint}\n    modelled: {model_defs[key]}"
        )

    modelled_constraint_names = {
        (t.name, c.name)
        for t in Base.metadata.tables.values()
        for c in t.constraints
        if c.name is not None
    }
    for key in sorted(set(live_defs) - set(model_defs)):
        table_name, index_name = key
        if table_name not in set(Base.metadata.tables):
            continue  # unmodelled table; reported by the column-shape test
        if key in unnormalisable:
            continue  # already reported above, as the index that would not build
        hint = ""
        if key in modelled_constraint_names:
            hint = (
                " -- Base.metadata declares a CONSTRAINT of that name; the migration created a"
                " bare index instead"
            )
        problems.append(
            f"{table_name}.{index_name}: index exists in the database but is not modelled in"
            f" Base.metadata{hint}\n    in database: {live_defs[key]}"
        )

    for key in sorted(set(model_defs) & set(live_defs)):
        table_name, index_name = key
        if model_defs[key] != live_defs[key]:
            problems.append(
                f"{table_name}.{index_name}: index definition mismatch"
                f"\n    modelled:    {model_defs[key]}"
                f"\n    in database: {live_defs[key]}"
            )

    assert not problems, "\n" + "\n".join(problems)
