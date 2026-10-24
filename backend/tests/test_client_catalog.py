"""Spec §5 tests 1, 2 and 11: the client-scope catalog and its guards."""

import ast
import re
from pathlib import Path
from typing import Any

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint, text

from app.core.client_scope import ALL_SCOPE_ALLOWLIST
from app.core.client_scope_catalog import CATALOG, PENDING_PHASE_2, PHASE_1
from app.models.base import Base

BACKEND = Path(__file__).resolve().parent.parent
DEFINITION = "app/core/client_scope.py"


def _app_sources() -> list[tuple[str, ast.Module]]:
    return [
        (str(path.relative_to(BACKEND)), ast.parse(path.read_text()))
        for path in sorted((BACKEND / "app").rglob("*.py"))
    ]


def _is_all_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "all"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ClientScope"
    )


def _calls_to_all(sources: list[tuple[str, ast.Module]]) -> set[str]:
    """`"<path>::<qualified name>"` of every function holding a
    `ClientScope.all()` call: the dotted names of the enclosing classes and
    functions, innermost last, or `"<module>"` at module level. The same key
    `ALL_SCOPE_ALLOWLIST` uses."""
    found: set[str] = set()

    def walk(node: ast.AST, path: str, chain: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = chain
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                inner = (*chain, child.name)
            if _is_all_call(child):
                found.add(f"{path}::{'.'.join(chain) or '<module>'}")
            walk(child, path, inner)

    for path, tree in sources:
        walk(tree, path, ())
    return found


def _constructions(sources: list[tuple[str, ast.Module]]) -> list[str]:
    """Every place outside the definition that builds a scope by hand --
    `ClientScope(...)`, or any call passing `is_all=` (a `dataclasses.replace`
    that widens one) -- instead of through `.all()` / `.only()` / `.narrow()`."""
    found: list[str] = []
    for path, tree in sources:
        if path == DEFINITION:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else None
            )
            if name == "ClientScope" or any(k.arg == "is_all" for k in node.keywords):
                found.append(f"{path}:{node.lineno}")
    return found


def test_client_scope_all_only_in_reviewed_functions() -> None:
    unreviewed = _calls_to_all(_app_sources()) - set(ALL_SCOPE_ALLOWLIST)
    assert not unreviewed, (
        "ClientScope.all() outside the reviewed list (spec §2.3). Pass the "
        f"person's scope, or add the function with a reason: {sorted(unreviewed)}"
    )


def test_the_allowlist_has_not_rotted() -> None:
    stale = set(ALL_SCOPE_ALLOWLIST) - _calls_to_all(_app_sources())
    assert not stale, (
        f"allowlisted functions that no longer call ClientScope.all(): {sorted(stale)}"
    )


def test_the_allowlist_guard_is_not_vacuous() -> None:
    found = _calls_to_all(_app_sources())
    assert "app/api/deps.py::admit" in found
    # Function granularity, not file: the route-reachable function next to
    # the scheduled poll is held to the caller's scope.
    assert "app/modules/email_intake/poller.py::poll_mailbox" in ALL_SCOPE_ALLOWLIST
    assert not any(k.endswith("poller.py::test_mailbox") for k in ALL_SCOPE_ALLOWLIST)


def test_no_route_file_holds_the_all_scope() -> None:
    """Spec §2.3: the HTTP route files (`api.py`, `api/v1/*`) may not use it.
    (Task 9's client creation and archiving will be the named exceptions.)"""
    routes = sorted(
        key
        for key in ALL_SCOPE_ALLOWLIST
        if key.startswith("app/api/v1/") or key.split("::")[0].endswith("/api.py")
    )
    assert routes == []


def test_nobody_builds_a_scope_by_hand() -> None:
    built = _constructions(_app_sources())
    assert not built, (
        "a ClientScope built outside app/core/client_scope.py escapes the "
        f"reviewed list; use .all() (reviewed), .only() or .narrow(): {built}"
    )


def test_the_construction_guard_is_not_vacuous() -> None:
    bad = ast.parse(
        "def f(s):\n"
        "    a = ClientScope(is_all=True, ids=frozenset())\n"
        "    b = replace(s, is_all=True)\n"
    )
    assert _constructions([("app/x.py", bad)]) == ["app/x.py:2", "app/x.py:3"]


# The functions that answer with a person's UN-NARROWED scope: X-Client-Id has
# not been applied to what they return. A door combines them with the
# request's own scope (`deps.admit`, `require_permission`, the portal's
# principal and gate, `read_door_identity`); anything else calling them could
# hand a route a scope wider than the one its request asked for.
UNNARROWED_SCOPE_READERS = frozenset({"person_scope", "clients_holding"})
UNNARROWED_SCOPE_DOORS = frozenset(
    {
        "app/api/deps.py",
        "app/modules/portal/principal.py",
        "app/core/identity/door.py",
        "app/core/rbac.py",
    }
)


def _unnarrowed_scope_uses(sources: list[tuple[str, ast.Module]]) -> list[str]:
    """`path:line:name` for every import of, reference to, or attribute access
    of an un-narrowed scope reader."""
    found: list[str] = []
    for path, tree in sources:
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            found.extend(
                f"{path}:{node.lineno}:{name}"
                for name in names
                if name in UNNARROWED_SCOPE_READERS
            )
    return found


def test_only_the_doors_read_an_unnarrowed_scope() -> None:
    outside = [
        use
        for use in _unnarrowed_scope_uses(_app_sources())
        if use.split(":")[0] not in UNNARROWED_SCOPE_DOORS
    ]
    assert not outside, (
        "person_scope / clients_holding return a scope X-Client-Id has not "
        "narrowed; only the doors may use them (intersect with the request's "
        f"own scope instead): {outside}"
    )


def test_the_unnarrowed_scope_guard_is_not_vacuous() -> None:
    files = {use.split(":")[0] for use in _unnarrowed_scope_uses(_app_sources())}
    assert {
        "app/api/deps.py",
        "app/modules/portal/principal.py",
        "app/core/identity/door.py",
    } <= files
    bad = ast.parse(
        "from app.core.rbac import clients_holding\n"
        "import app.core.identity.door as door\n"
        "x = door.person_scope\n"
    )
    assert _unnarrowed_scope_uses([("app/x.py", bad)]) == [
        "app/x.py:1:clients_holding",
        "app/x.py:3:person_scope",
    ]


def test_every_table_is_classified() -> None:
    live = {t.name for t in Base.metadata.sorted_tables}
    assert live - set(CATALOG) == set(), "unclassified tables (spec §5 test 1)"
    assert set(CATALOG) - live == set(), "catalog names tables that do not exist"


def test_pending_tables_are_in_the_catalog_and_not_global() -> None:
    assert PENDING_PHASE_2 <= set(CATALOG)
    assert all(CATALOG[t].kind in {"C", "H"} for t in PENDING_PHASE_2)


# H children of an O parent. Their client_id is NOT NULL, so a parent row with
# no client (Novieri's own credential) makes the insert fail -- which is the
# point: Annex A, "`credentials`, que tiene que tener cliente". Any other H
# table must hang from a C or H parent; a new exception needs a reason here.
H_OF_AN_OPTIONAL_PARENT = frozenset({"push_credential_scope", "push_credential_realm"})


def test_h_parents_are_scoped_tables() -> None:
    for table, entry in CATALOG.items():
        if entry.kind == "H":
            allowed = {"C", "H", "O"} if table in H_OF_AN_OPTIONAL_PARENT else {"C", "H"}
            assert entry.parent in CATALOG and CATALOG[entry.parent].kind in allowed, table


def test_the_optional_parent_exception_has_not_rotted() -> None:
    for table in H_OF_AN_OPTIONAL_PARENT:
        entry = CATALOG[table]
        assert entry.kind == "H" and entry.parent is not None, table
        assert CATALOG[entry.parent].kind == "O", f"{table}: exempt but its parent is not O"


# --------------------------------------------------------------------------- #
# Spec §5 test 2: the catalog agrees with the database (migration 0231).
# --------------------------------------------------------------------------- #

# THE EXPECTED POLICIES, WRITTEN ONCE. Spec §2.2 in SQL. Postgres deparses
# both these and the live policies (`deparse_policies`), so the comparison is
# exact and canonical: casts, parentheses and spelling come from the same
# deparser on both sides. Like `test_schema_drift.py`'s index comparison, a predicate
# that says the same thing in a different SHAPE fails -- a false failure,
# never a false pass.
_SCOPE = "current_setting('app.client_scope', true)"
_VISIBLE = "its_client_visible(client_id)"
# A NULL client is "every client": readable under a scope that names at
# least one client (spec §2.2: no scope, or an empty one, sees zero rows) ...
_NULL_PINNED = (
    f"(client_id IS NULL AND ({_SCOPE} = 'all' OR ({_SCOPE} = 'list' AND "
    "nullif(current_setting('app.current_clients', true), '') IS NOT NULL)))"
)
# ... and written, changed or deleted only under scope all.
_NULL_ALL = f"(client_id IS NULL AND {_SCOPE} = 'all')"

#: THE EXCEPTION, NAMED: the one O table whose `client_isolation` WITH CHECK
#: admits a NULL row under any pinned scope -- phase 1's client-less domain
#: events. `users` left it in Task 8: no narrowed path writes a staff row
#: (`PATCH /me` and the login bookkeeping run under scope all since Task 6b),
#: so a staff row is created, changed and deleted only under scope all, like
#: any other global row. Anything else here is a finding.
NULL_WRITABLE_FROM_ANY_SCOPE = frozenset({"domain_events"})

Shape = tuple[str, str | None, str | None]  # (cmd, USING, WITH CHECK) as written


def expected_client_policies(table: str) -> dict[str, Shape]:
    """Spec §2.2's client policies for `table`, by name, as written. Shared
    with `tests/test_client_isolation.py`'s per-table sweep."""
    if table == "clients":
        return {
            "client_isolation": (
                "ALL",
                f"its_client_visible(id) AND (kind <> 'unassigned' OR {_SCOPE} = 'all')",
                f"{_SCOPE} = 'all' OR its_client_visible(id)",
            )
        }
    entry = PHASE_1[table]
    if entry.kind in {"C", "H"}:
        return {"client_isolation": ("ALL", _VISIBLE, _VISIBLE)}
    read = f"{_NULL_PINNED} OR {_VISIBLE}"
    modify = f"{_NULL_ALL} OR {_VISIBLE}"
    insert = read if table in NULL_WRITABLE_FROM_ANY_SCOPE else modify
    policies: dict[str, Shape] = {
        "client_isolation": ("ALL", read, insert),
        # `users` included since Task 8: a narrowed scope changes no column
        # of a staff row (`its_user_identity_guard` then holds a visible
        # contact's kind, external_subject and status).
        "client_isolation_update": ("UPDATE", modify, None),
        "client_isolation_delete": ("DELETE", modify, None),
    }
    # CREATING a staff row is scope all (Task 6b, re-review M2): first-login
    # provisioning and `POST /users`, both every-client. Named on `users`
    # although `client_isolation`'s WITH CHECK now says the same. An INSERT
    # policy has no USING.
    if table == "users":
        policies["client_isolation_insert"] = ("INSERT", None, modify)
    return policies


# Policies other modules put on these tables. The full set per table is
# asserted, so a new one has to be added here on purpose.
OTHER_POLICIES: dict[str, set[str]] = {
    "users": {"portal_isolation"},
    "doc_articles": {"portal_isolation"},
}


async def deparse_policies(conn, shapes: set[Shape]) -> dict[Shape, tuple[str, str | None]]:
    """Each shape's (qual, with_check) as `pg_policies` prints it, from a
    policy built on a temporary table with the columns the predicates name.
    The caller rolls the transaction back."""
    await conn.execute(
        text("CREATE TEMP TABLE policy_probe (id uuid, client_id uuid, kind varchar(20))")
    )
    names: dict[str, Shape] = {}
    for n, (cmd, using, check) in enumerate(sorted(shapes, key=repr)):
        names[f"p{n}"] = (cmd, using, check)
        using_ = f" USING ({using})" if using is not None else ""
        with_check = f" WITH CHECK ({check})" if check is not None else ""
        await conn.execute(
            text(
                f"CREATE POLICY p{n} ON policy_probe AS RESTRICTIVE FOR {cmd}"
                f"{using_}{with_check}"
            )
        )
    rows = (
        await conn.execute(
            text(
                "SELECT policyname, qual, with_check FROM pg_policies "
                "WHERE tablename = 'policy_probe' AND schemaname LIKE 'pg_temp%'"
            )
        )
    ).all()
    return {names[r.policyname]: (r.qual, r.with_check) for r in rows}


async def test_phase_1_tables_carry_the_restrictive_policy(admin_engine) -> None:
    tables = [*PHASE_1, "clients"]
    expected = {t: expected_client_policies(t) for t in tables}
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT tablename, policyname, permissive, roles, cmd, qual, with_check "
                    "FROM pg_policies WHERE schemaname = 'public' AND tablename = ANY(:t)"
                ),
                {"t": tables},
            )
        ).all()
        shapes = {s for p in expected.values() for s in p.values()}
        deparsed = await deparse_policies(conn, shapes)
        await conn.rollback()

    live: dict[str, dict[str, Any]] = {}
    for r in rows:
        live.setdefault(r.tablename, {})[r.policyname] = r
    for table in tables:
        want = expected[table]
        have = live.get(table, {})
        # The FULL set: a missing policy and an extra one both fail.
        assert set(have) == set(want) | {"tenant_isolation"} | OTHER_POLICIES.get(table, set()), (
            f"{table}: policies {sorted(have)}"
        )
        for name, shape in want.items():
            row = have[name]
            assert row.permissive == "RESTRICTIVE", (table, name)
            # Bound to one role, a restrictive policy leaves every other role
            # (portal_user, a future one) unfiltered.
            assert list(row.roles) == ["public"], (table, name, row.roles)
            assert row.cmd == shape[0], (table, name, row.cmd)
            assert (row.qual, row.with_check) == deparsed[shape], (
                f"{table}.{name}: live\n  USING {row.qual}\n  CHECK {row.with_check}\n"
                f"expected\n  USING {deparsed[shape][0]}\n  CHECK {deparsed[shape][1]}"
            )
        if table in PHASE_1 and PHASE_1[table].kind == "O":
            assert "IS NULL" in have["client_isolation"].qual, f"{table}: O reads NULL rows"


# The second parent of the two O tables that have two (migration 0231): a
# doc_versions row hangs from an article OR a record, a
# health_profile_assignment row from a device OR a group. One trigger per
# parent, or the rows of the second parent read as "every client".
EXTRA_PARENT_TRIGGERS: dict[tuple[str, str], tuple[str, str]] = {
    ("doc_versions", "its_client_from_record"): ("doc_records", "record_id"),
    ("health_profile_assignment", "its_client_from_group"): ("device_groups", "device_group_id"),
}

# pg_trigger.tgtype bits: ROW (1), BEFORE (2), INSERT (4), DELETE (8),
# UPDATE (16), TRUNCATE (32), INSTEAD (64).
BEFORE_INSERT_OR_UPDATE_ROW = 1 | 2 | 4 | 16
AFTER_UPDATE_ROW = 1 | 16


def _edges() -> dict[tuple[str, str], tuple[str, str]]:
    """(child, trigger) -> (parent, fk) for every inheritance edge: the
    catalog's parent of each H and O table but `jobs` (whose target is a
    device OR a group, its own trigger), plus the second parents."""
    edges = {
        (t, "its_client_from_parent"): (e.parent, e.fk)
        for t, e in PHASE_1.items()
        if e.parent is not None and e.fk is not None and t != "jobs"
    }
    edges.update(EXTRA_PARENT_TRIGGERS)
    return edges


async def _client_triggers(admin_engine) -> list[Any]:
    async with admin_engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    text(
                        "SELECT c.relname AS tbl, t.tgname AS name, t.tgargs AS args, "
                        "t.tgtype AS type, t.tgfoid::regproc::text AS fn, "
                        "pg_get_triggerdef(t.oid) AS triggerdef, "
                        "ARRAY(SELECT a.attname::text FROM pg_attribute a "
                        "      WHERE a.attrelid = t.tgrelid "
                        "        AND a.attnum = ANY (t.tgattr::int2[])) AS columns "
                        "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                        "WHERE NOT t.tgisinternal AND (t.tgname LIKE 'its\\_client\\_%' "
                        "OR t.tgname = 'its_job_client')"
                    )
                )
            ).all()
        )


def _args(row: Any) -> tuple[str, ...]:
    # tgargs is the arguments' bytes, each one NUL-terminated.
    return tuple(a.decode() for a in bytes(row.args).split(b"\x00")[:-1])


async def test_h_tables_carry_the_inheritance_trigger(admin_engine) -> None:
    """Existence is not enough: a literal copy of the catalog with the wrong
    parent or fk would still create a trigger of the right name. So each
    trigger's ARGUMENTS are held to the catalog's `(parent, fk)`, its column
    list must hold the fk, and the SET is exact -- a trigger the catalog does
    not name (on a G table, say) fails too."""
    rows = await _client_triggers(admin_engine)
    found = {
        (r.tbl, r.name): r for r in rows if r.name.startswith("its_client_from_")
    }
    expected = _edges()
    assert set(found) == set(expected), (
        f"missing {sorted(set(expected) - set(found))}, extra {sorted(set(found) - set(expected))}"
    )
    for key, (parent, fk) in sorted(expected.items()):
        row = found[key]
        assert _args(row) == (parent, fk), f"{key}: arguments {_args(row)}, catalog {(parent, fk)}"
        assert row.fn == "its_client_from_parent", f"{key}: executes {row.fn}"
        assert row.type == BEFORE_INSERT_OR_UPDATE_ROW, f"{key}: tgtype {row.type}"
        # The fk, and client_id itself: an UPDATE that rewrites client_id
        # alone must meet the trigger too (spec §2.4).
        assert set(row.columns) == {fk, "client_id"}, f"{key}: fires on {row.columns}"

    # jobs: its own function, keyed on what the target is.
    jobs = [r for r in rows if r.name == "its_job_client"]
    assert [r.tbl for r in jobs] == ["jobs"], "its_job_client belongs on jobs alone"
    assert jobs[0].fn == "its_job_client"
    assert jobs[0].type == BEFORE_INSERT_OR_UPDATE_ROW
    assert set(jobs[0].columns) == {"target_id", "target_type", "client_id"}, jobs[0].columns


async def test_a_parents_client_change_reaches_every_child(admin_engine) -> None:
    """The other direction of every edge (spec §2.4): when a parent's client
    changes -- an article moved, a job retargeted, an alert's device
    corrected -- its children follow in the same statement, or they keep the
    old client and stay readable where the parent no longer is. One
    `its_client_to_<child>` trigger per edge, and no other."""
    rows = await _client_triggers(admin_engine)
    found = {(r.tbl, r.name): r for r in rows if r.name.startswith("its_client_to_")}
    expected = {
        (parent, f"its_client_to_{child}"): (child, fk)
        for (child, _), (parent, fk) in _edges().items()
    }
    assert len(expected) == len(_edges()), "two edges share a trigger name"
    assert set(found) == set(expected), (
        f"missing {sorted(set(expected) - set(found))}, extra {sorted(set(found) - set(expected))}"
    )
    for key, (child, fk) in sorted(expected.items()):
        row = found[key]
        assert _args(row) == (child, fk), f"{key}: arguments {_args(row)}"
        assert row.fn == "its_client_to_children", f"{key}: executes {row.fn}"
        # AFTER UPDATE, with NO column list: a parent's client changes inside
        # its own BEFORE trigger (a job retargeted, an alert's device moved),
        # and `UPDATE OF client_id` fires only when the statement names it.
        assert row.type == AFTER_UPDATE_ROW, f"{key}: tgtype {row.type}"
        assert list(row.columns) == [], f"{key}: limited to {row.columns}"
        when = "WHEN ((old.client_id IS DISTINCT FROM new.client_id))"
        assert when in row.triggerdef, row.triggerdef


# --------------------------------------------------------------------------- #
# Migration 0231's constraints are declared on the models too.
# --------------------------------------------------------------------------- #

CLIENT_KEYS = {"uq_clients_id_org", "uq_sites_id_client"}
CLIENT_FK = re.compile(r"fk_\w+_(client_org|site_client)")


async def test_the_client_constraints_are_declared_on_the_models(admin_engine) -> None:
    """`alembic revision --autogenerate` proposes a bare DROP for any
    constraint the metadata does not know -- the accident that deleted
    `ix_doc_articles_fts` for a month (`test_migrations.py`). The org- and
    site-consistency keys are the client boundary where a foreign key is the
    only guard (RLS does not apply to FK checks), so each one in the database
    must be declared on its model with the same columns and target, and vice
    versa."""
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.conname AS name, t.relname AS tbl, r.relname AS ref, "
                    "ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY "
                    "      AS k(n, i) JOIN pg_attribute a "
                    "      ON a.attrelid = c.conrelid AND a.attnum = k.n ORDER BY k.i) AS cols, "
                    "ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY "
                    "      AS k(n, i) JOIN pg_attribute a "
                    "      ON a.attrelid = c.confrelid AND a.attnum = k.n ORDER BY k.i) "
                    "      AS ref_cols "
                    "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                    "LEFT JOIN pg_class r ON r.oid = c.confrelid "
                    "WHERE (c.contype = 'f' AND (c.conname LIKE 'fk\\_%\\_client\\_org' "
                    "                       OR c.conname LIKE 'fk\\_%\\_site\\_client')) "
                    "   OR (c.contype = 'u' AND c.conname IN ('uq_clients_id_org', "
                    "                                         'uq_sites_id_client'))"
                )
            )
        ).all()
    live = {
        r.name: (r.tbl, tuple(r.cols), r.ref, tuple(r.ref_cols)) for r in rows
    }

    declared: dict[str, tuple[str, tuple[str, ...], str | None, tuple[str, ...]]] = {}
    for table in Base.metadata.tables.values():
        for con in table.constraints:
            name = str(con.name) if isinstance(con.name, str) else ""
            if isinstance(con, ForeignKeyConstraint) and CLIENT_FK.fullmatch(name):
                declared[name] = (
                    table.name,
                    tuple(con.column_keys),
                    con.referred_table.name,
                    tuple(e.column.name for e in con.elements),
                )
            elif isinstance(con, UniqueConstraint) and name in CLIENT_KEYS:
                declared[name] = (table.name, tuple(c.name for c in con.columns), None, ())

    # Not vacuous: the set is what the catalog says it must be. Every table
    # whose client_id the application writes -- C but `sites`, every O, and
    # the grants -- plus the three tables that name a site.
    expected = (
        {
            f"fk_{t}_client_org"
            for t, e in PHASE_1.items()
            if e.kind == "O" or (e.kind == "C" and t != "sites")
        }
        | {"fk_user_roles_client_org"}
        | {f"fk_{t}_site_client" for t in ("devices", "enrollment_tokens", "install_links")}
        | CLIENT_KEYS
    )
    assert set(live) == expected, f"live vs catalog: {sorted(set(live) ^ expected)}"
    assert sorted(set(live) - set(declared)) == [], "in the database, not on a model"
    assert sorted(set(declared) - set(live)) == [], "on a model, not in the database"
    differ = {n: (live[n], declared[n]) for n in live if live[n] != declared[n]}
    assert not differ, f"declared differently from the database: {differ}"
