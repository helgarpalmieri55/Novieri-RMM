"""`agent_releases` is deliberately GLOBAL, and that has consequences.

Every other tenant-bearing table in this schema carries `org_id` and is covered
by an RLS policy, so a forgotten application-level filter still fails closed at
the database. This table carries neither, on purpose: the artefact bytes are
byte-identical for every tenant (an enrollment token is never inside them --
`agent/packaging/README.md` forbids that), so per-org copies would multiply
storage for no isolation. The isolation that actually matters here is *who may
publish*, and that is an RBAC property, not a row filter.

The consequence is the reason this file exists. **RLS guards nothing on this
table**, so the `require_permission` on each route is the only thing between a
reader and the bytes every endpoint in the fleet will execute. A future reader
who assumes "tenancy is handled at the database layer, as it is everywhere else
here" would be wrong, and would be wrong about the one table where it costs
most. These tests pin the absence as deliberate so that assumption cannot be
made quietly.

See `docs/modules/agent-distribution/spec.md` §4.2.
"""

from sqlalchemy import inspect

from app.models.core import AgentRelease


def test_agent_releases_has_no_org_id() -> None:
    columns = {column.name for column in inspect(AgentRelease).columns}
    assert "org_id" not in columns, (
        "agent_releases is global on purpose (spec §4.2). If it gains org_id, "
        "the explicit RBAC checks on its routes must NOT be relaxed on the "
        "strength of RLS -- re-read this file's docstring before doing that."
    )


def test_the_columns_the_console_and_the_install_command_need() -> None:
    columns = {column.name for column in inspect(AgentRelease).columns}
    missing = {
        "id",
        "version",
        "os",
        "arch",
        "storage_ref",
        "size_bytes",
        "sha256",
        "signed",
        "published_at",
    } - columns
    assert not missing, f"missing columns the console reads: {sorted(missing)}"


def test_one_row_per_version_os_arch() -> None:
    """Two artefacts for the same (version, os, arch) is unresolvable.

    The install command has to name exactly one object, and with two rows there
    is no basis in the data to choose between them -- so the ambiguity would be
    resolved by whichever `ORDER BY` happened to be in the query, silently and
    differently per route.
    """
    table = inspect(AgentRelease).local_table
    unique_column_sets = [
        {column.name for column in constraint.columns}
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    ]
    assert {"version", "os", "arch"} in unique_column_sets, (
        f"expected a uniqueness constraint over (version, os, arch); "
        f"found unique sets {unique_column_sets}"
    )


def test_the_digest_column_is_wide_enough_for_a_sha256_hex_digest() -> None:
    """64 hex characters. A String(32) would truncate silently on some backends.

    Non-obvious enough to pin: the digest is what an operator compares against
    their own `sha256sum` of the file they built, so a truncated value does not
    fail loudly -- it fails as a mismatch the operator cannot explain.
    """
    column = inspect(AgentRelease).columns["sha256"]
    assert column.type.length is not None and column.type.length >= 64
