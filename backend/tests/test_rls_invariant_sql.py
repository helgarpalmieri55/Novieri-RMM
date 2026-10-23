"""`deploy/postgres/assert-rls.sql` is an assertion, so it is tested like one.

The finding this file closes: CI's per-table RLS check was

    c.relrowsecurity AND NOT c.relforcerowsecurity

which can only ever fire for a table that already has RLS ENABLED. A tenant
table created with RLS entirely OFF short-circuits on the first conjunct and
**passes** -- the precise accident the check exists to catch. The live schema is
clean today (114 tables carry `org_id`, all of them RLS-enabled, forced and
carrying a policy), so nothing was broken; the assertion was.

The tests below are therefore about the ASSERTION, not about the schema:
`test_assertion_passes_on_the_real_schema` pins that the invariant currently
holds, and the failure cases pin that each way of shipping an unprotected
tenant table is caught. RLS enabled AND forced but with no policy is one worth
naming: under FORCE, a policy-less table is not a leak, it is invisible, and the
symptom is empty result sets rather than cross-tenant rows. It still means the
tenant predicate this platform is built on was never written for that table.

BLOCK 3 AND ITS CASES WERE ADDED 2026-08-22 (audit R2), and they close a
different hole from the ones above: a table can be RLS-enabled, forced, and
carry a policy, and still have no tenant predicate anywhere in it.
`platform_error_event` shipped with `USING (true)` and passed every check in
this file, in `test_rls.py`, and in the whole suite, while a route read it with
no tenant GUC and returned other tenants' identifiers
(`docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md` F1). The last
case in this file asserts the one named exemption is still LOAD-BEARING, so an
exemption that stops being needed goes red rather than sitting there.

Driven through `psql -f` rather than through SQLAlchemy on purpose: that is the
exact invocation `.github/workflows/ci.yml` uses, so a file that psql cannot
parse fails here rather than in CI.
"""

import subprocess
from pathlib import Path

import pytest

from tests.conftest import PgCluster

ASSERT_RLS_SQL = (
    Path(__file__).resolve().parent.parent.parent / "deploy" / "postgres" / "assert-rls.sql"
)


def _psql_file(dbname: str) -> subprocess.CompletedProcess[str]:
    """Run the assertion exactly as CI does: `psql -v ON_ERROR_STOP=1 -f -`,
    with the file's text on **stdin**.

    Reading the path directly is what CI does NOT do, and the difference is not
    cosmetic. `sudo -u postgres psql -f <path>` makes the *postgres* user open
    the file, and on a GitHub runner the checkout lives under the runner's home
    where that user has no traversal rights -- so every test here failed with
    `psql: error: ...assert-rls.sql: Permission denied` while passing locally,
    where the repo happens to be world-readable. The workflow step pipes the
    file in (`-f -`) precisely so the server side never touches the filesystem;
    this now matches it, which is what the docstring claimed all along.
    """
    return subprocess.run(
        [
            "sudo", "-u", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1",
            "-d", dbname, "-f", "-",
        ],
        input=ASSERT_RLS_SQL.read_text(),
        capture_output=True,
        text=True,
    )


def _psql(dbname: str, sql: str) -> None:
    subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-X", "-q", "-d", dbname, "-c", sql],
        capture_output=True,
        text=True,
        check=True,
    )


def test_the_assertion_file_exists_where_ci_reads_it() -> None:
    """`ci.yml` pipes this path in from `working-directory: deploy`. A rename
    that misses the workflow turns the security assertion into a step that
    silently asserts nothing, so the path itself is pinned."""
    assert ASSERT_RLS_SQL.is_file()
    ci = (ASSERT_RLS_SQL.parent.parent.parent / ".github" / "workflows" / "ci.yml").read_text()
    assert "postgres/assert-rls.sql" in ci


def test_assertion_passes_on_the_real_schema(pg_cluster: PgCluster) -> None:
    """Every table the migrations create is either non-tenant or fully
    RLS-protected. This is the assertion's true-negative half: without it a
    tautologically-failing file would still pass the three cases below."""
    result = _psql_file(pg_cluster.dbname)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("name", "ddl", "expected_state"),
    [
        (
            "rls_off",
            "CREATE TABLE rls_probe (id uuid PRIMARY KEY, org_id uuid NOT NULL)",
            "rls=f forced=f policies=0",
        ),
        (
            "rls_unforced",
            "CREATE TABLE rls_probe (id uuid PRIMARY KEY, org_id uuid NOT NULL);"
            "ALTER TABLE rls_probe ENABLE ROW LEVEL SECURITY;"
            "CREATE POLICY p ON rls_probe USING (true)",
            "rls=t forced=f policies=1",
        ),
        (
            "rls_forced_but_no_policy",
            "CREATE TABLE rls_probe (id uuid PRIMARY KEY, org_id uuid NOT NULL);"
            "ALTER TABLE rls_probe ENABLE ROW LEVEL SECURITY;"
            "ALTER TABLE rls_probe FORCE ROW LEVEL SECURITY",
            "rls=t forced=t policies=0",
        ),
    ],
)
def test_an_unprotected_tenant_table_fails_the_assertion(
    pg_cluster: PgCluster, name: str, ddl: str, expected_state: str
) -> None:
    """Three ways to ship a table with an `org_id` column that RLS does not
    protect; all three must be a non-zero exit, naming the table.

    `rls_off` is the case the previous predicate let through: it is a plain
    CREATE TABLE with an org_id column and nothing else, which is what a
    migration for a new module looks like when its author forgets the RLS
    boilerplate."""
    _psql(pg_cluster.dbname, "DROP TABLE IF EXISTS rls_probe")
    try:
        _psql(pg_cluster.dbname, ddl)
        result = _psql_file(pg_cluster.dbname)
        assert result.returncode != 0, (
            f"{name}: an unprotected tenant table passed the assertion\n{result.stdout}"
        )
        assert "rls_probe" in result.stderr
        assert expected_state in result.stderr
    finally:
        _psql(pg_cluster.dbname, "DROP TABLE IF EXISTS rls_probe")


# --------------------------------------------------------------------------- #
# Block 3: a policy that does not mention `org_id` protects nothing.
# --------------------------------------------------------------------------- #


def test_a_tenant_table_whose_policy_never_mentions_org_id_fails(pg_cluster: PgCluster) -> None:
    """AUDIT R2, AND THE SHAPE IS NOT HYPOTHETICAL -- IT SHIPPED.

    `platform_error_event` carries `org_id` and its only policy is
    `USING (true) WITH CHECK (true)` (migration 0056). It passed blocks 1 and 2
    of this file, it passed `test_rls.py`'s sweep, and it passed 6,900 tests,
    while a route read it with no tenant GUC set and returned every tenant's
    correlation ids to any org administrator
    (`docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md` F1).

    RLS enabled, RLS forced, one policy present, zero tenant scoping. That is
    the state this case constructs, and it must be a non-zero exit."""
    _psql(pg_cluster.dbname, "DROP TABLE IF EXISTS rls_probe")
    try:
        _psql(
            pg_cluster.dbname,
            "CREATE TABLE rls_probe (id uuid PRIMARY KEY, org_id uuid NOT NULL);"
            "ALTER TABLE rls_probe ENABLE ROW LEVEL SECURITY;"
            "ALTER TABLE rls_probe FORCE ROW LEVEL SECURITY;"
            "CREATE POLICY p ON rls_probe USING (true) WITH CHECK (true)",
        )
        result = _psql_file(pg_cluster.dbname)
        assert result.returncode != 0, (
            "a tenant table with USING (true) passed the assertion\n" + result.stdout
        )
        assert "rls_probe" in result.stderr
        assert "never mention org_id" in result.stderr
    finally:
        _psql(pg_cluster.dbname, "DROP TABLE IF EXISTS rls_probe")


def test_a_restrictive_org_id_policy_does_not_satisfy_block_three(pg_cluster: PgCluster) -> None:
    """`polpermissive` is load-bearing, so it is asserted rather than assumed.

    Postgres ORs permissive policies and ANDs restrictive ones on top. A
    restrictive policy can only narrow what some permissive policy already
    allows -- so a table whose ONLY org-scoped predicate is restrictive, beside
    a permissive `USING (true)`, is still wide open to any session that does
    not match the restrictive policy's role. Counting "a policy mentioning
    org_id" without reading `polpermissive` would score that table as
    protected, and this codebase has four real restrictive policies
    (`portal_isolation` on `audit_events`, `ticket`, `users`) for such a check
    to trip over."""
    _psql(pg_cluster.dbname, "DROP TABLE IF EXISTS rls_probe")
    try:
        _psql(
            pg_cluster.dbname,
            "CREATE TABLE rls_probe (id uuid PRIMARY KEY, org_id uuid NOT NULL);"
            "ALTER TABLE rls_probe ENABLE ROW LEVEL SECURITY;"
            "ALTER TABLE rls_probe FORCE ROW LEVEL SECURITY;"
            "CREATE POLICY p ON rls_probe USING (true) WITH CHECK (true);"
            "CREATE POLICY r ON rls_probe AS RESTRICTIVE TO portal_user "
            "USING (org_id = current_setting('app.current_org', true)::uuid)",
        )
        result = _psql_file(pg_cluster.dbname)
        assert result.returncode != 0, (
            "a restrictive org_id policy was accepted as tenant scoping\n" + result.stdout
        )
        assert "rls_probe" in result.stderr
    finally:
        _psql(pg_cluster.dbname, "DROP TABLE IF EXISTS rls_probe")


def test_a_properly_scoped_tenant_table_passes(pg_cluster: PgCluster) -> None:
    """The true-negative half of block 3. Without it, a predicate that failed
    every table on earth would satisfy the two cases above and nobody would
    know until CI went red on the real schema -- which
    `test_assertion_passes_on_the_real_schema` does check, but only because a
    table shaped exactly like this one is what every migration writes."""
    _psql(pg_cluster.dbname, "DROP TABLE IF EXISTS rls_probe")
    try:
        _psql(
            pg_cluster.dbname,
            "CREATE TABLE rls_probe (id uuid PRIMARY KEY, org_id uuid NOT NULL);"
            "ALTER TABLE rls_probe ENABLE ROW LEVEL SECURITY;"
            "ALTER TABLE rls_probe FORCE ROW LEVEL SECURITY;"
            "CREATE POLICY tenant_isolation ON rls_probe FOR ALL "
            "USING (org_id = current_setting('app.current_org', true)::uuid) "
            "WITH CHECK (org_id = current_setting('app.current_org', true)::uuid)",
        )
        result = _psql_file(pg_cluster.dbname)
        assert result.returncode == 0, result.stderr
    finally:
        _psql(pg_cluster.dbname, "DROP TABLE IF EXISTS rls_probe")


def test_the_named_exemption_is_still_needed(pg_cluster: PgCluster) -> None:
    """AN EXEMPTION NOBODY RE-EXAMINES IS WHERE THE NEXT FINDING HIDES.

    Block 3 exempts `platform_error_event` by name. This asserts the exemption
    is load-bearing -- that the table genuinely has no permissive policy
    mentioning `org_id` today. If somebody later gives it a real tenant policy,
    this goes red and the exemption should be deleted rather than left standing
    over a table that no longer needs it."""
    result = subprocess.run(
        [
            "sudo", "-u", "postgres", "psql", "-X", "-A", "-t",
            "-d", pg_cluster.dbname, "-c",
            "SELECT count(*) FROM pg_policy p "
            "JOIN pg_class c ON c.oid = p.polrelid "
            "WHERE c.relname = 'platform_error_event' AND p.polpermissive "
            "AND pg_get_expr(p.polqual, p.polrelid) LIKE '%org_id%'",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "0", (
        "platform_error_event now has a tenant-scoped policy -- remove it from "
        "the exemption list in deploy/postgres/assert-rls.sql and from "
        "POLICY_WITHOUT_ORG_ID_BY_DESIGN in tests/test_rls.py"
    )


def test_a_table_without_org_id_is_out_of_scope(pg_cluster: PgCluster) -> None:
    """`alembic_version`, `permissions` and `role_permissions` legitimately have
    no RLS: they hold no tenant data. The assertion keys on the presence of an
    `org_id` column rather than on a maintained allowlist, so it must not fail
    on a non-tenant table -- an assertion that cries wolf gets an exemption
    added, and the exemption is where the next unprotected table hides."""
    _psql(pg_cluster.dbname, "DROP TABLE IF EXISTS rls_probe_global")
    try:
        _psql(
            pg_cluster.dbname,
            "CREATE TABLE rls_probe_global (id uuid PRIMARY KEY, label text NOT NULL)",
        )
        result = _psql_file(pg_cluster.dbname)
        assert result.returncode == 0, result.stderr
    finally:
        _psql(pg_cluster.dbname, "DROP TABLE IF EXISTS rls_probe_global")
