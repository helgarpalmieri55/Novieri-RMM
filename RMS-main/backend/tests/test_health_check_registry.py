"""The closed check-type registry (docs/modules/endpoint-health-alerting/spec.md
§4.1) against the database constraint that must agree with it.

READ THIS BEFORE ADDING A TEST HERE.

A test that iterates `CHECK_TYPES` and asserts something about `CHECK_TYPES` is
decoration: the fixture and the subject are the same object, so it passes
against any edit that keeps the file self-consistent -- including deleting an
entry. The tests below therefore compare the Python registry against something
that was written independently of it:

  - the `ck_health_check_type` CHECK constraint in migration 0042, read back
    out of `pg_get_constraintdef` -- BOTH directions, so neither a Python entry
    with no SQL member nor a SQL member with no Python entry can survive;
  - the `os_family` CHECK, likewise;
  - the individual per-row facts §4.1 states in prose (`agent.offline` is the
    only server-evaluated type; AV health is Windows-only; SMART is
    unavailable on macOS), each named and asserted literally rather than
    derived from the table.

The shape assertions (`value_kind` in the three legal values, `cadence` in the
two) are the exception and they are deliberately weak: they exist so a new
entry cannot introduce a third `cadence` that `build_policy_params` would then
ship to an agent that has no timer for it.
"""

import re

import pytest
from sqlalchemy import text as sqltext
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.health.registry import (
    CADENCES,
    CHECK_TYPES,
    COMPARATORS,
    OS_FAMILIES,
    VALUE_KINDS,
    agent_check_type_names,
    check_type,
    is_available_on,
)


async def _constraint_def(admin_engine: AsyncEngine, name: str) -> str:
    async with admin_engine.connect() as conn:
        return (
            await conn.execute(
                sqltext(
                    "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
                    "WHERE conname = :n"
                ),
                {"n": name},
            )
        ).scalar_one()


def _quoted_literals(constraint_def: str) -> set[str]:
    """Every single-quoted literal in a deparsed CHECK, minus the type casts
    Postgres adds (`'cpu.busy_pct'::text`)."""
    return set(re.findall(r"'([^']*)'", constraint_def))


# ---------------------------------------------------------------------------
# The Python registry against the SQL enum -- both directions
# ---------------------------------------------------------------------------


async def test_check_type_registry_and_db_constraint_agree(admin_engine: AsyncEngine) -> None:
    """The registry and `ck_health_check_type` are two hand-written lists of
    the same closed set (§0.1: adding a check type is a reviewed code change in
    two repos). Comparing them both ways is the only thing that stops them
    drifting -- a type added to Python but not SQL is a check nobody can store,
    and a type left in SQL after Python drops it is a check the agent will be
    asked to run and cannot."""
    definition = await _constraint_def(admin_engine, "ck_health_check_type")
    assert _quoted_literals(definition) == set(CHECK_TYPES)


async def test_os_family_registry_and_db_constraint_agree(admin_engine: AsyncEngine) -> None:
    definition = await _constraint_def(admin_engine, "ck_health_check_os_family")
    assert _quoted_literals(definition) == set(OS_FAMILIES)


async def test_comparator_registry_and_db_constraint_agree(admin_engine: AsyncEngine) -> None:
    definition = await _constraint_def(admin_engine, "ck_health_check_comparator")
    assert _quoted_literals(definition) == set(COMPARATORS)


# ---------------------------------------------------------------------------
# The individual facts §4.1 states in prose
# ---------------------------------------------------------------------------


def test_the_server_evaluated_checks_are_exactly_these() -> None:
    """§0.6. An agent that is offline cannot report that it is offline, and an
    agent cannot judge its own backups against the server's records
    (spec-server-backup.md §8.1), so these are evaluated by server sweeps -- and
    the distinction is data on the registry row, not a special case buried in a
    sweep."""
    server_side = sorted(n for n, t in CHECK_TYPES.items() if t.evaluated_by == "server")
    assert server_side == [
        "agent.offline",
        "backup.last_run_failed",
        "backup.offsite_lag_h",
        "backup.restore_test_age_d",
        "backup.success_age_h",
        "script.policy_failed",
    ]


def test_agent_offline_is_never_in_the_pushed_set() -> None:
    """`health.policy.apply` never ships it (§0.6). Task 7 builds the push from
    `agent_check_type_names()`, so the exclusion is one list, not one `if` per
    call site."""
    assert "agent.offline" not in agent_check_type_names()
    assert "cpu.busy_pct" in agent_check_type_names()


def test_antivirus_health_is_windows_only() -> None:
    """§4.1: Windows Security Center only. "AV health" on Linux/macOS has no
    vendor-neutral answer, and inventing one produces a check that is
    confidently wrong."""
    assert is_available_on("posture.antivirus_healthy", "windows") is True
    assert is_available_on("posture.antivirus_healthy", "linux") is False
    assert is_available_on("posture.antivirus_healthy", "macos") is False


def test_smart_is_unavailable_on_macos() -> None:
    assert is_available_on("disk.smart_ok", "windows") is True
    assert is_available_on("disk.smart_ok", "linux") is True
    assert is_available_on("disk.smart_ok", "macos") is False


def test_cpu_is_available_everywhere() -> None:
    assert [is_available_on("cpu.busy_pct", os) for os in OS_FAMILIES] == [True, True, True]


# ---------------------------------------------------------------------------
# Shape -- weak on purpose, see the module docstring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CHECK_TYPES))
def test_every_entry_has_a_legal_value_kind_and_cadence(name: str) -> None:
    entry = check_type(name)
    assert entry.value_kind in VALUE_KINDS
    assert entry.cadence in CADENCES
    assert entry.legal_comparators <= set(COMPARATORS)
    assert entry.legal_comparators, f"{name} permits no comparator at all"
    assert entry.os_families <= set(OS_FAMILIES)


@pytest.mark.parametrize("name", sorted(CHECK_TYPES))
def test_an_enum_entry_names_exactly_two_members(name: str) -> None:
    """The `enum` value kind is stored in `threshold_bool` (see
    `registry.render_threshold`), so a third member would have nowhere to go.
    §2.2 fixes the two threshold columns, so this is a property of the schema
    and not a limit of the registry."""
    entry = check_type(name)
    if entry.value_kind == "enum":
        assert len(entry.enum_values) == 2, entry
    else:
        assert entry.enum_values == ()


def test_check_type_rejects_an_unregistered_name() -> None:
    with pytest.raises(KeyError):
        check_type("cpu.temperature")
