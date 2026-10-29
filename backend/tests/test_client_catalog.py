"""Spec §5 tests 1, 2 and 11: the client-scope catalog and its guards."""

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Column, ForeignKeyConstraint, Index, UniqueConstraint, text

from app.core.client_scope import ALL_SCOPE_ALLOWLIST
from app.core.client_scope_catalog import CATALOG, PENDING_PHASE_2, PHASE_1
from app.core.global_rows import SECOND_PARENTS
from app.models.base import Base
from tests.client_scope_support import deparse_policies, expected_client_policies

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
    Task 9's client creation and archiving, which spec §2.3 names as the
    exceptions, turned out to need none: `require_permission_on_every_client`
    admits only a request whose own scope is every client, and the route
    passes that scope on."""
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

# The expected policies are written once, in `tests/client_scope_support.py`
# (`expected_client_policies`, `deparse_policies`), and shared with
# `tests/test_client_isolation.py`'s per-table sweep.

# Policies other modules put on these tables. The full set per table is
# asserted, so a new one has to be added here on purpose.
OTHER_POLICIES: dict[str, set[str]] = {
    "users": {"portal_isolation"},
    "doc_articles": {"portal_isolation"},
    # The portal's own policies (0028, 0038): a requester reads their own
    # tickets. ANDed with the client policy, never replaced by it.
    "ticket": {"portal_isolation"},
    "ticket_comment": {"portal_isolation"},
    "ticket_attachment": {"portal_isolation"},
    "ticket_approval": {"portal_isolation"},
    "ticket_status_history": {"portal_isolation"},
    "ticket_sla": {"portal_isolation"},
}


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

CLIENT_KEYS = {
    "uq_clients_id_org",
    "uq_sites_id_client",
    "uq_person_id_client",
    # A budget line's and an expense's category and cost center are of the
    # row's client (plan 2026-10-26 Task 8): the keys those rows reference.
    "uq_expense_category_id_client",
    "uq_cost_center_id_client",
    # A subnet's VLAN is of the subnet's client (plan 2026-10-26 Task 10
    # review, M3): the key `fk_ipam_subnet_vlan_client` references.
    "uq_ipam_vlan_id_client",
}
CLIENT_FK = re.compile(
    r"fk_\w+_(client_org|site_client|person_client|category_client|cost_center_client"
    r"|vlan_client)"
)


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
                    "                       OR c.conname LIKE 'fk\\_%\\_site\\_client' "
                    "                       OR c.conname LIKE 'fk\\_%\\_person\\_client' "
                    "                       OR c.conname LIKE 'fk\\_%\\_category\\_client' "
                    "                       OR c.conname LIKE 'fk\\_%\\_cost\\_center\\_client' "
                    "                       OR c.conname LIKE 'fk\\_%\\_vlan\\_client')) "
                    "   OR (c.contype = 'u' AND c.conname = ANY (:keys))"
                ),
                {"keys": sorted(CLIENT_KEYS)},
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
    # the grants -- plus the six tables that name a site.
    expected = (
        {
            f"fk_{t}_client_org"
            for t, e in PHASE_1.items()
            if e.kind == "O" or (e.kind == "C" and t != "sites")
        }
        | {"fk_user_roles_client_org"}
        # Informative client of an audit event (plan 2026-10-26 Task 1, C6):
        # G table, no client policy, but its client is its own org's.
        | {"fk_audit_events_client_org"}
        # A VLAN's, a subnet's and a site server's site is of the row's
        # client too (plan 2026-10-26 Task 10, migration 0240).
        | {
            f"fk_{t}_site_client"
            for t in (
                "devices", "enrollment_tokens", "install_links",
                "ipam_vlan", "ipam_subnet", "site_server",
            )
        }
        # A row naming a person is of that person's client (plan 2026-10-26
        # Task 5): who uses a device is one of the device's client's people;
        # who holds a license or a seat, of the license's or subscription's
        # (Task 6).
        | {
            "fk_device_person_person_client",
            "fk_license_allocation_person_client",
            "fk_seat_assignment_person_client",
        }
        # A budget line's and an expense's category and cost center are of
        # the row's client (plan 2026-10-26 Task 8).
        | {
            f"fk_{t}_{parent}_client"
            for t in ("budget_line", "expense")
            for parent in ("category", "cost_center")
        }
        # A subnet's VLAN is of the subnet's client (Task 10 review, M3).
        | {"fk_ipam_subnet_vlan_client"}
        | CLIENT_KEYS
    )
    assert set(live) == expected, f"live vs catalog: {sorted(set(live) ^ expected)}"
    assert sorted(set(live) - set(declared)) == [], "in the database, not on a model"
    assert sorted(set(declared) - set(live)) == [], "on a model, not in the database"
    differ = {n: (live[n], declared[n]) for n in live if live[n] != declared[n]}
    assert not differ, f"declared differently from the database: {differ}"


# --------------------------------------------------------------------------- #
# Task 8b: a natural key on a per-client table is unique PER CLIENT.
# --------------------------------------------------------------------------- #

# A key unique per ORGANISATION over a table whose rows belong to clients is a
# rule the application cannot see from a narrowed scope: a technician limited
# to client X runs the duplicate check under X's RLS, misses Y's row, and the
# INSERT dies on the index -- a 500 where an every-client admin gets a 409,
# which tells X's technician Y's names one guess at a time (Task 7's
# `device_groups`, Task 8's review). And two clients cannot both have
# "Servers". So every unique key on a phase-1 table must be one of:
#
#   SAFE   it holds `client_id`; or a column that PINS the row's client -- the
#          catalog's inheritance fk, a second parent's fk (`global_rows.
#          SECOND_PARENTS`), or a column X under a foreign key `(X, client_id)
#          REFERENCES p (id, client_id)` on a table whose `client_id` is NOT
#          NULL or a key that is MATCH FULL (else a row without a client skips
#          it) -- that cannot be NULL in a colliding row (NOT NULL, or the
#          index treats NULLs as distinct); or a surrogate column below
#          (random, so equal only for the same thing);
#   LEAKY  anything else: a natural key scoped by `org_id` or by nothing.
#
# A LEAKY key fails the guard unless `ORG_WIDE_KEYS` names it with a reason.

#: Columns holding a RANDOM value, so two rows share one only by being the
#: same thing: a collision on one tells nobody anything about another client.
#: `id` is every table's (uuid7) primary key.
SURROGATE_COLUMNS: dict[tuple[str, str], str] = {
    ("*", "id"): "every table's uuid7 primary key",
    ("agents", "cert_fingerprint"): (
        "the SHA-256 of a certificate over a keypair the device generated; the gateway "
        "finds the agent by it, so it must be unique across the platform, not per client"
    ),
    ("enrollment_tokens", "token_hash"): "the hash of a random single-use token",
    ("install_links", "code_hash"): "the hash of a random install code",
    ("ticket", "email_token"): (
        "a random 256-bit reply token (`email_intake/identity.py::mint_email_token`)"
    ),
}

#: Unique keys on phase-1 tables that are deliberately organisation-wide.
#: Each is LEAKY by the rules above, and kept on purpose; the reason says why
#: no client namespace applies. A key that stops being LEAKY, or stops
#: existing, fails `test_the_org_wide_keys_have_not_rotted`.
ORG_WIDE_KEYS: dict[str, str] = {
    "users_org_id_external_subject_key": (
        "an identity, not a name: one subject of the identity provider is one person in "
        "the organisation, and `deps.admit` finds the user by it. Per client, one login "
        "could hold a row in each client. Written only under scope all (first-login "
        "provisioning, `POST /users`), and the value is an opaque id the IdP issues"
    ),
    "uq_inbound_message_org_message_id": (
        "the poller's idempotency key: an RFC 5322 Message-ID names one email, fetched "
        "from the organisation's own mailboxes (`email_mailbox` is G). Written only by the "
        "poller under scope all; per client, one email could open a ticket in each client "
        "the intake ever decided it belonged to"
    ),
    "uq_agent_update_rings_ordinal": (
        "a ring's position in ONE rollout, and the rollout (`agent_update_rollouts`, G) "
        "spans clients: ring 1 a pilot, ring 2 one client, ring 3 another. Per client, two "
        "rings could share a position and the order would be undefined. The rings are "
        "written with the rollout, by `enumerate`, under `require_permission_on_every_client`"
    ),
    "ticket_org_id_number_key": (
        "the ticket number comes from the organisation's one counter "
        "(`ticket_number_counter`, G): nobody types it, every client's tickets share the "
        "sequence, and a moved ticket keeps its number. Per client, INC-12 could name two "
        "tickets"
    ),
    "person_user_id_key": (
        "an account is one person: one user, one person row, in the client the account "
        "belongs to (`its_person_account_guard`). It is an identity, not a name -- a "
        "`users.id` nobody types -- so a collision tells nobody another client's names"
    ),
}


@dataclass(frozen=True)
class UniqueKey:
    table: str
    name: str
    columns: tuple[str | None, ...]  # None: an expression column
    not_null: frozenset[str]
    nulls_not_distinct: bool


def _surrogate(table: str, column: str | None) -> bool:
    return (table, column) in SURROGATE_COLUMNS or ("*", column) in SURROGATE_COLUMNS


def leaky_because(key: UniqueKey, pinning: frozenset[str]) -> str | None:
    """Why `key` is LEAKY by the rules above, or None when it is SAFE."""
    columns = set(key.columns)
    if "client_id" in columns:
        return None
    if any(_surrogate(key.table, c) for c in columns):
        return None
    for column in columns & pinning:
        if column in key.not_null or not key.nulls_not_distinct:
            return None
    if columns & pinning:
        return (
            f"its client column {sorted(columns & pinning)} is nullable under NULLS NOT "
            "DISTINCT: rows without a parent collide across clients"
        )
    return f"{key.columns} names no client"


async def _unique_keys(admin_engine) -> tuple[list[UniqueKey], dict[str, frozenset[str]]]:
    async with admin_engine.connect() as conn:
        return await _unique_keys_on(conn)


async def _unique_keys_on(conn) -> tuple[list[UniqueKey], dict[str, frozenset[str]]]:
    """Every unique index (constraints' included) on a phase-1 table, and per
    table the columns that pin a row's client."""
    tables = list(PHASE_1)
    rows = (
        await conn.execute(
            text(
                "SELECT c.relname AS tbl, i.relname AS name, "
                "x.indnullsnotdistinct AS nnd, "
                "ARRAY(SELECT coalesce(a.attname::text, '') "
                "      FROM unnest(x.indkey::int2[]) WITH ORDINALITY AS k(n, o) "
                "      LEFT JOIN pg_attribute a "
                "        ON a.attrelid = x.indrelid AND a.attnum = k.n "
                "      WHERE k.o <= x.indnkeyatts ORDER BY k.o) AS cols, "
                "ARRAY(SELECT a.attname::text FROM pg_attribute a "
                "      WHERE a.attrelid = x.indrelid AND a.attnotnull "
                "        AND a.attnum = ANY (x.indkey::int2[])) AS not_null "
                "FROM pg_index x JOIN pg_class c ON c.oid = x.indrelid "
                "JOIN pg_class i ON i.oid = x.indexrelid "
                "WHERE x.indisunique AND c.relnamespace = 'public'::regnamespace "
                "  AND c.relname = ANY (:t)"
            ),
            {"t": tables},
        )
    ).all()
    fks = (
        await conn.execute(
            text(
                "SELECT t.relname AS tbl, "
                "ARRAY(SELECT a.attname::text FROM unnest(f.conkey) WITH ORDINALITY "
                "      AS k(n, o) JOIN pg_attribute a "
                "      ON a.attrelid = f.conrelid AND a.attnum = k.n ORDER BY k.o) AS cols, "
                "ARRAY(SELECT a.attname::text FROM unnest(f.confkey) WITH ORDINALITY "
                "      AS k(n, o) JOIN pg_attribute a "
                "      ON a.attrelid = f.confrelid AND a.attnum = k.n ORDER BY k.o) "
                "      AS ref_cols, "
                "f.confmatchtype = 'f' AS match_full, "
                "coalesce((SELECT a.attnotnull FROM pg_attribute a "
                "          WHERE a.attrelid = f.conrelid AND a.attname = 'client_id'), false) "
                "      AS client_not_null "
                "FROM pg_constraint f JOIN pg_class t ON t.oid = f.conrelid "
                "WHERE f.contype = 'f' AND t.relnamespace = 'public'::regnamespace "
                "  AND t.relname = ANY (:t)"
            ),
            {"t": tables},
        )
    ).all()
    keys = [
        UniqueKey(
            table=r.tbl,
            name=r.name,
            columns=tuple(c or None for c in r.cols),
            not_null=frozenset(r.not_null),
            nulls_not_distinct=r.nnd,
        )
        for r in rows
    ]
    pinning: dict[str, set[str]] = {}
    for table, entry in PHASE_1.items():
        pins = pinning.setdefault(table, set())
        if entry.fk is not None:
            pins.add(entry.fk)
        if table in SECOND_PARENTS:
            pins.add(SECOND_PARENTS[table][1])
    for fk in fks:
        pairs = dict(zip(fk.ref_cols, fk.cols, strict=True))
        # Checked only where the row HAS a client: under MATCH SIMPLE a NULL
        # client_id skips the key, and the row may hold any X (review M1).
        checked = fk.client_not_null or fk.match_full
        if set(pairs) == {"id", "client_id"} and pairs["client_id"] == "client_id" and checked:
            pinning[fk.tbl].add(pairs["id"])
    return keys, {t: frozenset(p) for t, p in pinning.items()}


async def test_natural_keys_on_per_client_tables_are_per_client(admin_engine) -> None:
    """THE GUARD (Task 8b). Introspects the live database, so a migration that
    adds `UNIQUE (org_id, name)` to a phase-1 table goes red here, not in a
    narrowed technician's 500."""
    keys, pinning = await _unique_keys(admin_engine)
    leaky = {
        f"{k.table}.{k.name}": why
        for k in keys
        if (why := leaky_because(k, pinning[k.table])) is not None
        and k.name not in ORG_WIDE_KEYS
    }
    assert not leaky, (
        "unique keys on per-client tables that are unique per ORGANISATION (Task 8b): "
        "add client_id to each (C/H: `(org_id, client_id, ...)`; O: `UNIQUE NULLS NOT "
        "DISTINCT (org_id, client_id, ...)`), or name it in ORG_WIDE_KEYS with the reason "
        f"it is deliberately org-wide: {leaky}"
    )


async def test_the_org_wide_keys_have_not_rotted(admin_engine) -> None:
    keys, pinning = await _unique_keys(admin_engine)
    by_name = {k.name: k for k in keys}
    assert sorted(set(ORG_WIDE_KEYS) - set(by_name)) == [], "allowlisted keys that do not exist"
    still_leaky = {
        name for name in ORG_WIDE_KEYS if leaky_because(by_name[name], pinning[by_name[name].table])
    }
    assert still_leaky == set(ORG_WIDE_KEYS), (
        f"allowlisted keys that are per client now: {sorted(set(ORG_WIDE_KEYS) - still_leaky)}"
    )


async def test_the_unique_key_guard_is_not_vacuous(admin_engine) -> None:
    """It reads the keys it judges: every SAFE leg and the pinning it derives
    are seen in the live schema, and the old per-organisation shapes -- the
    keys 0231 replaced -- are judged LEAKY."""
    keys, pinning = await _unique_keys(admin_engine)
    by_name = {k.name: k for k in keys}
    safe = {
        "device_tags_device_id_key_key",  # the inheritance fk
        "uq_health_assignment_group",  # a second parent's fk
        "uq_install_links_site",  # (site_id, client_id) -> sites (id, client_id)
        "enrollment_tokens_token_hash_key",  # a surrogate
        "uq_device_groups_client_live_name",  # client_id
        "device_person_pkey",  # a primary key over the fk
    }
    assert safe <= set(by_name), sorted(safe - set(by_name))
    for name in safe:
        assert leaky_because(by_name[name], pinning[by_name[name].table]) is None, name
    assert "site_id" in pinning["install_links"] and "record_id" in pinning["doc_versions"]
    assert len(keys) > 100, len(keys)  # every phase-1 primary key, at least

    def old(table: str, *columns: str, nnd: bool = False) -> str | None:
        key = UniqueKey(table, "old", columns, frozenset({"org_id"}), nnd)
        return leaky_because(key, pinning[table])

    assert old("script_policies", "org_id", "name") is not None
    assert old("push_host_key", "org_id", "address") is not None
    assert old("software_ignore", "org_id", "name") is not None
    # Holds the fk, but a group-less gate is no client's: NULLS NOT DISTINCT
    # made every client's "every device" gate one row.
    assert old("change_gates", "org_id", "device_group_id", nnd=True) is not None
    # The same column with NULLs distinct pins the client wherever it is set.
    assert old("patch_policies", "org_id", "device_group_id") is None
    # IPAM (plan 2026-10-26 Task 10): 0043's per-organisation VRF name and
    # site-less VLAN number are LEAKY; a VLAN's site is pinned to its client
    # by `fk_ipam_vlan_site_client`, so the per-site key needs no client; a
    # subnet's VRF and an address's are their inheritance foreign key.
    assert old("ipam_vrf", "org_id", "name") is not None
    assert old("ipam_vlan", "org_id", "vid") is not None
    assert "site_id" in pinning["ipam_vlan"] and "site_id" in pinning["ipam_subnet"]
    for name in ("uq_ipam_vlan_site_vid", "uq_ipam_subnet_org_vrf_prefix",
                 "uq_ipam_address_org_vrf_addr"):
        assert leaky_because(by_name[name], pinning[by_name[name].table]) is None, name
    # Push install (plan 2026-10-26 Task 12): 0077's per-organisation active
    # authorization and 0200's one-active-run-per-organisation are LEAKY; a
    # credential scope's key holds its credential, the inheritance foreign key.
    assert old("push_authorization", "org_id", "prefix") is not None
    assert old("push_run", "org_id") is not None
    assert "credential_id" in pinning["push_credential_scope"]
    assert leaky_because(
        by_name["uq_push_credential_scope_active"], pinning["push_credential_scope"]
    ) is None


#: Migration 0231's per-client natural keys (Task 8b), with their columns and
#: whether NULLs are not distinct. Each must be declared on its model too:
#: `alembic revision --autogenerate` proposes a bare DROP for any key the
#: metadata does not know, and `test_schema_drift.py` compares no constraint.
PER_CLIENT_KEYS: dict[str, tuple[str, tuple[str, ...], bool]] = {
    "uq_device_groups_client_live_name": ("device_groups", ("org_id", "client_id", "name"), False),
    "uq_script_policies_client_name": ("script_policies", ("org_id", "client_id", "name"), False),
    "uq_push_host_key_client_address": (
        "push_host_key", ("org_id", "client_id", "address"), True,
    ),
    "uq_backup_offsite_targets_client_name": (
        "backup_offsite_targets", ("org_id", "client_id", "name"), True,
    ),
    "uq_software_ignore_client_name": ("software_ignore", ("org_id", "client_id", "name"), True),
    "uq_backup_recovery_keys_client_active": (
        "backup_recovery_keys", ("org_id", "client_id", "fingerprint"), True,
    ),
    "uq_patch_approvals_client_patch_ring": (
        "patch_approvals", ("org_id", "client_id", "patch_id", "ring_name"), True,
    ),
    "uq_sla_policy_client_queue_and_priority": (
        "sla_policy", ("org_id", "client_id", "queue_id", "priority"), True,
    ),
    "uq_sla_policy_client_queue_wildcard_priority": (
        "sla_policy", ("org_id", "client_id", "queue_id"), True,
    ),
    "uq_sla_policy_client_wildcard_queue_priority": (
        "sla_policy", ("org_id", "client_id", "priority"), True,
    ),
    "uq_sla_policy_client_wildcard_both": ("sla_policy", ("org_id", "client_id"), True),
    "uq_health_assignment_client_org": (
        "health_profile_assignment", ("org_id", "client_id"), True,
    ),
    "uq_change_gates_client_group": (
        "change_gates", ("org_id", "client_id", "device_group_id"), True,
    ),
    # Plan 2026-10-26 Task 5 (migration 0235). An expression column reads as "".
    "uq_person_login_client_login": ("person_login", ("org_id", "client_id", "login"), False),
    "uq_person_client_lower_email": ("person", ("org_id", "client_id", ""), False),
    # Plan 2026-10-26 Task 8 (migration 0238): each client its own settings
    # row, category keys and seeded defaults, cost center codes, budget names
    # and materialized slots.
    "budget_settings_pkey": ("budget_settings", ("client_id",), False),
    "uq_expense_category_client_key": (
        "expense_category", ("org_id", "client_id", "key"), False,
    ),
    "uq_expense_category_client_system_key": (
        "expense_category", ("org_id", "client_id", "system_key"), False,
    ),
    "uq_cost_center_client_code": ("cost_center", ("org_id", "client_id", "code"), False),
    "uq_budget_client_year_name": (
        "budget", ("org_id", "client_id", "fiscal_year", "name"), False,
    ),
    "uq_expense_client_autofeed": (
        "expense", ("org_id", "client_id", "source", "link_id", "period_key"), False,
    ),
    # Plan 2026-10-26 Task 10 (migration 0240): a VRF's name and a site-less
    # VLAN's number are per client; a VLAN with a site is unique per site,
    # which `fk_ipam_vlan_site_client` pins to the client.
    "uq_ipam_vrf_client_name": ("ipam_vrf", ("org_id", "client_id", "name"), False),
    "uq_ipam_vlan_client_vid": ("ipam_vlan", ("org_id", "client_id", "vid"), False),
    "uq_ipam_vlan_site_vid": ("ipam_vlan", ("org_id", "site_id", "vid"), False),
    # Plan 2026-10-26 Task 12 (migration 0241): a prefix is authorized per
    # client, and one run is active per client (operator decision D3).
    "uq_push_authorization_client_active": (
        "push_authorization", ("org_id", "client_id", "prefix"), False,
    ),
    "uq_push_run_active": ("push_run", ("org_id", "client_id"), False),
}


async def test_the_per_client_keys_are_in_the_database_and_on_the_models(admin_engine) -> None:
    keys, _ = await _unique_keys(admin_engine)
    live = {
        k.name: (k.table, tuple(c or "" for c in k.columns), k.nulls_not_distinct)
        for k in keys
        if k.name in PER_CLIENT_KEYS
    }
    assert live == PER_CLIENT_KEYS, f"live: {live}"

    declared: dict[str, tuple[str, tuple[str, ...], bool]] = {}
    for table in Base.metadata.tables.values():
        for con in [*table.constraints, *table.indexes]:
            name = con.name if isinstance(con.name, str) else ""
            if name in PER_CLIENT_KEYS:
                # An index's expression (`lower(email)`) is not one of its
                # `columns`; `expressions` holds them all, in order, and the
                # live side reads an expression as "" (`_unique_keys_on`).
                columns = (
                    tuple(e.name if isinstance(e, Column) else "" for e in con.expressions)
                    if isinstance(con, Index)
                    else tuple(c.name for c in con.columns)  # type: ignore[union-attr]
                )
                nnd = bool(con.dialect_options["postgresql"].get("nulls_not_distinct"))
                declared[name] = (table.name, columns, nnd)
    assert declared == PER_CLIENT_KEYS, f"declared: {declared}"


async def test_a_composite_key_pins_only_a_client_it_cannot_skip(admin_engine) -> None:
    """Task 8b review M1. `(X, client_id) REFERENCES p (id, client_id)` checks
    NOTHING for a row whose `client_id` is NULL under the default MATCH
    SIMPLE: such a row may hold any X, so X pins its client only where
    `client_id` is NOT NULL or the key is MATCH FULL. Shown on `install_links`
    (`uq_install_links_site` is SAFE through `fk_install_links_site_client`),
    each leg inside one transaction that is rolled back."""
    async with admin_engine.connect() as conn:
        keys, pinning = await _unique_keys_on(conn)
        assert "site_id" in pinning["install_links"]

        await conn.execute(text("ALTER TABLE install_links ALTER COLUMN client_id DROP NOT NULL"))
        keys, pinning = await _unique_keys_on(conn)
        assert "site_id" not in pinning["install_links"]
        site = next(k for k in keys if k.name == "uq_install_links_site")
        assert leaky_because(site, pinning["install_links"]) == "('site_id',) names no client"

        await conn.execute(
            text("ALTER TABLE install_links DROP CONSTRAINT fk_install_links_site_client")
        )
        await conn.execute(
            text(
                "ALTER TABLE install_links ADD CONSTRAINT fk_install_links_site_client "
                "FOREIGN KEY (site_id, client_id) REFERENCES sites (id, client_id) MATCH FULL"
            )
        )
        keys, pinning = await _unique_keys_on(conn)
        assert "site_id" in pinning["install_links"]
        await conn.rollback()
