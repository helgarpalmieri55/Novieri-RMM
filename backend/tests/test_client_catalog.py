"""Spec §5 tests 1, 2 and 11: the client-scope catalog and its guards."""

import ast
from pathlib import Path

from sqlalchemy import text

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


async def test_phase_1_tables_carry_the_restrictive_policy(admin_engine) -> None:
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT tablename, permissive, roles, cmd, qual, with_check FROM pg_policies "
                    "WHERE policyname = 'client_isolation'"
                )
            )
        ).all()
    by_table = {r.tablename: r for r in rows}
    for table, entry in PHASE_1.items():
        row = by_table.get(table)
        assert row is not None, f"{table}: no client_isolation policy (spec §5 test 2)"
        assert row.permissive == "RESTRICTIVE", table
        # A restrictive policy bound to one role leaves every other role
        # (portal_user, a future one) unfiltered; one bound to SELECT leaves
        # writes unfiltered.
        assert list(row.roles) == ["public"], f"{table}: policy bound to {row.roles}"
        assert row.cmd == "ALL", f"{table}: policy covers {row.cmd} only"
        assert "its_client_visible" in row.qual, table
        # USING scopes reads; WITH CHECK is what stops a write landing in a
        # client outside the scope.
        assert row.with_check is not None, f"{table}: no WITH CHECK"
        assert "its_client_visible" in row.with_check, f"{table}: WITH CHECK ignores the scope"
        if entry.kind == "O":
            assert "IS NULL" in row.qual, f"{table}: O tables must read NULL rows"


# The second parent of the two O tables that have two (migration 0231): a
# doc_versions row hangs from an article OR a record, a
# health_profile_assignment row from a device OR a group. One trigger per
# parent, or the rows of the second parent read as "every client".
EXTRA_PARENT_TRIGGERS: dict[tuple[str, str], tuple[str, str]] = {
    ("doc_versions", "its_client_from_record"): ("doc_records", "record_id"),
    ("health_profile_assignment", "its_client_from_group"): ("device_groups", "device_group_id"),
}

# pg_trigger.tgtype bits: ROW (1), BEFORE (2), INSERT (4), UPDATE (16).
BEFORE_INSERT_OR_UPDATE_ROW = 1 | 2 | 4 | 16


async def test_h_tables_carry_the_inheritance_trigger(admin_engine) -> None:
    """Existence is not enough: a literal copy of the catalog with the wrong
    parent or fk would still create a trigger of the right name. So each
    trigger's ARGUMENTS are held to the catalog's `(parent, fk)`, and its
    column list must hold the fk -- a trigger that does not fire when the fk
    changes lets a row keep the client of the parent it no longer has."""
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname AS tbl, t.tgname AS name, t.tgargs AS args, "
                    "t.tgtype AS type, t.tgfoid::regproc::text AS fn, "
                    "ARRAY(SELECT a.attname::text FROM pg_attribute a "
                    "      WHERE a.attrelid = t.tgrelid "
                    "        AND a.attnum = ANY (t.tgattr::int2[])) AS columns "
                    "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE NOT t.tgisinternal AND t.tgname IN "
                    "('its_client_from_parent', 'its_client_from_record', "
                    "'its_client_from_group', 'its_job_client')"
                )
            )
        ).all()
    found = {(r.tbl, r.name): r for r in rows}
    expected: dict[tuple[str, str], tuple[str, str]] = {
        (t, "its_client_from_parent"): (e.parent, e.fk)
        for t, e in PHASE_1.items()
        if e.parent is not None and e.fk is not None and t != "jobs"
    }
    expected.update(EXTRA_PARENT_TRIGGERS)
    missing = sorted(set(expected) - set(found))
    assert not missing, f"missing triggers: {missing}"

    for key, (parent, fk) in sorted(expected.items()):
        row = found[key]
        # tgargs is the arguments' bytes, each one NUL-terminated.
        args = tuple(a.decode() for a in bytes(row.args).split(b"\x00")[:-1])
        assert args == (parent, fk), f"{key}: arguments {args}, catalog says {(parent, fk)}"
        assert row.fn == "its_client_from_parent", f"{key}: executes {row.fn}"
        assert row.type & BEFORE_INSERT_OR_UPDATE_ROW == BEFORE_INSERT_OR_UPDATE_ROW, key
        # The fk, and client_id itself: an UPDATE that rewrites client_id
        # alone must meet the trigger too (spec §2.4: a client_id that differs
        # from the parent's raises).
        assert {fk, "client_id"} <= set(row.columns), f"{key}: fires on {row.columns}"

    # jobs: its own function, keyed on what the target is.
    job = found.get(("jobs", "its_job_client"))
    assert job is not None, "jobs: no its_job_client trigger"
    assert job.fn == "its_job_client"
    assert job.type & BEFORE_INSERT_OR_UPDATE_ROW == BEFORE_INSERT_OR_UPDATE_ROW
    assert {"target_id", "target_type", "client_id"} <= set(job.columns), job.columns
