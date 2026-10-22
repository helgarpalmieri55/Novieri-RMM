"""The definition registry and the derivation of `requires`
(docs/modules/reporting/spec.md §3, §4.2, §12.1, §12.3).

THE PROPERTY UNDER TEST is that a definition author cannot understate a report's
permission requirement. `requires` is derived from `sources` through
`TABLE_PERMISSIONS` and is not a constructor parameter; a source the map does
not cover fails the import instead of defaulting to anything; a source on the
denylist fails the import whether it was declared honestly or not.

THREE OF THESE TESTS ARE LIVENESS TESTS, and each asserts NON-EMPTINESS BEFORE
asserting content. `assert all(t in Base.metadata.tables for t in _NEVER_A_SOURCE)`
is satisfied by an EMPTY denylist and by an all-typo one alike -- both are
"every entry resolves", vacuously. A denylist whose names have drifted denies
nothing, and a test that cannot tell the difference is decoration.
"""

from types import MappingProxyType
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import Select, select

from app.core.rbac import PERMISSIONS
from app.models import Base, Device
from app.modules.reporting.errors import DenylistedSource, UndeclaredSource
from app.modules.reporting.registry import (
    _NEVER_A_COLUMN,
    _NEVER_A_SOURCE,
    _NEVER_A_TOOL,
    _REGISTRY,
    REGISTRY,
    TABLE_PERMISSIONS,
    ReportDefinition,
    register,
)


class _Params(BaseModel):
    limit: int = 50
    offset: int = 0


class _Row(BaseModel):
    device_id: str


def _stmt(*_args: Any, **_kwargs: Any) -> Select[Any]:
    return select(Device.id)


def _definition(**overrides: Any) -> ReportDefinition:
    """A minimal, otherwise-valid definition, so each test varies exactly the
    one thing it is about."""
    kwargs: dict[str, Any] = {
        "key": "probe",
        "summary": "A probe.",
        "description": "A probe definition that exists only inside this test module.",
        "params": _Params,
        "row_model": _Row,
        "sources": frozenset({"devices"}),
        "build": _stmt,
        "build_totals": _stmt,
        "max_limit": 100,
        "export_columns": ("device_id",),
        "export_free_text": frozenset(),
        "export_row_cap": 10,
    }
    kwargs.update(overrides)
    return ReportDefinition(**kwargs)


# --------------------------------------------------------------------------- #
# TABLE_PERMISSIONS liveness, both directions (§3, §4.2).
# --------------------------------------------------------------------------- #


async def test_the_permission_map_is_not_empty() -> None:
    """Every parametrized case below iterates it. An empty map collects zero
    cases and reports success."""
    assert len(TABLE_PERMISSIONS) >= 10, dict(TABLE_PERMISSIONS)


@pytest.mark.parametrize("table", sorted(TABLE_PERMISSIONS))
async def test_every_mapped_table_is_a_live_table(table: str) -> None:
    """A map entry naming a renamed table maps nothing: the report that reads
    the real table would find no entry, and the one that reads this name cannot
    be built. Either way the conjunction is an assertion about a string."""
    assert table in Base.metadata.tables, sorted(Base.metadata.tables)


@pytest.mark.parametrize("table", sorted(TABLE_PERMISSIONS))
async def test_every_mapped_code_is_a_live_permission(table: str) -> None:
    """A code that is not in `PERMISSIONS` cannot be granted, so a report
    requiring it is unrunnable by everyone including a super-admin -- and
    `has_permission` raises `ValueError` on an unknown code rather than
    returning False, so it is a 500, not a 403."""
    assert TABLE_PERMISSIONS[table] in PERMISSIONS, TABLE_PERMISSIONS[table]


# --------------------------------------------------------------------------- #
# Denylist liveness (§12.3) -- the tripwire that must not rot.
# --------------------------------------------------------------------------- #


async def test_denylisted_sources_are_real_tables() -> None:
    live = {name for name in _NEVER_A_SOURCE if name in Base.metadata.tables}
    # Non-emptiness FIRST: an all-typo denylist and an empty denylist both
    # satisfy a naive `all(...)`, and both deny nothing.
    assert len(live) >= 7, sorted(_NEVER_A_SOURCE)
    assert live == set(_NEVER_A_SOURCE), sorted(set(_NEVER_A_SOURCE) - live)


async def test_denylisted_columns_are_real_columns() -> None:
    live: set[str] = set()
    for entry in _NEVER_A_COLUMN:
        table_name, _, column_name = entry.partition(".")
        table = Base.metadata.tables.get(table_name)
        if table is not None and column_name in table.columns:
            live.add(entry)
    assert len(live) >= 6, sorted(_NEVER_A_COLUMN)
    assert live == set(_NEVER_A_COLUMN), sorted(set(_NEVER_A_COLUMN) - live)


async def test_a_table_cannot_be_both_readable_and_forbidden() -> None:
    """If a name were on both, which one won would depend on the order the
    checks happen to run in -- and `__post_init__`'s ordering test below would
    be asserting nothing."""
    assert not (_NEVER_A_SOURCE & set(TABLE_PERMISSIONS))


async def test_the_never_a_tool_list_is_a_set_of_method_path_pairs() -> None:
    """Empty until the export routes exist (§10.3). Its liveness test lives in
    `test_reporting_properties.py`, which has a built app to resolve against;
    this only pins the SHAPE, so an entry written as a bare string is caught
    where it is written rather than where it is read."""
    assert isinstance(_NEVER_A_TOOL, frozenset)
    for entry in _NEVER_A_TOOL:
        assert isinstance(entry, tuple) and len(entry) == 2, entry
        method, path = entry
        assert method.isupper() and path.startswith("/"), entry


# --------------------------------------------------------------------------- #
# The derivation (§3, §12.1).
# --------------------------------------------------------------------------- #


async def test_requires_is_derived_from_sources() -> None:
    definition = _definition(sources=frozenset({"devices", "agents", "health_alert"}))
    assert definition.requires == {"devices.read", "health.read"}


async def test_requires_cannot_be_passed_to_the_constructor() -> None:
    """§3: "a definition that could hand-write it could understate it". The
    enforcement is `field(init=False)`, so this is a `TypeError` from
    `__init__`'s signature -- not a validation branch someone can relax."""
    with pytest.raises(TypeError) as exc:
        _definition(requires=frozenset())
    assert "requires" in str(exc.value), str(exc.value)


async def test_an_unmapped_source_fails_at_construction_and_names_the_table() -> None:
    """§3: a table absent from the map raises rather than defaulting to
    anything. The table name must be IN THE MESSAGE: a bare "it raised" tells
    whoever hits it nothing about which entry to add, and would pass equally
    against a definition that raised for an unrelated reason."""
    with pytest.raises(UndeclaredSource) as exc:
        _definition(sources=frozenset({"devices", "ticket_comment"}))
    assert "ticket_comment" in str(exc.value), str(exc.value)
    assert exc.value.tables == {"ticket_comment"}


async def test_a_denylisted_source_is_refused_before_the_unmapped_check() -> None:
    """§3, §8: a forbidden table declared honestly is still refused.

    THE ORDERING IS THE OBSERVABLE HERE, not merely the rejection. Every
    `_NEVER_A_SOURCE` entry is absent from `TABLE_PERMISSIONS` (asserted above),
    so if `__post_init__` ran the mapping check first this would raise
    `UndeclaredSource` -- naming the same table, with a message that says the
    map is incomplete rather than that the table is forbidden. Swap the two
    blocks in `__post_init__` and this test goes red on the exception TYPE, with
    a different message; that contrast is the point.
    """
    with pytest.raises(DenylistedSource) as exc:
        _definition(sources=frozenset({"devices", "credentials"}))
    assert "credentials" in str(exc.value), str(exc.value)
    assert exc.value.name == "credentials"
    # And it is not the weaker error: `UndeclaredSource` is not in this type's
    # ancestry, so `pytest.raises(DenylistedSource)` above genuinely
    # discriminates.
    assert not issubclass(DenylistedSource, UndeclaredSource)


# --------------------------------------------------------------------------- #
# The registry mapping itself (§3).
# --------------------------------------------------------------------------- #


async def test_the_registry_is_a_mapping_proxy_and_rejects_writes() -> None:
    """§3: "a mapping any code in the process can widen at runtime is not
    compiled into the image"."""
    assert isinstance(REGISTRY, MappingProxyType)
    with pytest.raises(TypeError):
        REGISTRY["injected"] = _definition()  # type: ignore[index]


async def test_register_refuses_a_key_that_is_not_url_safe() -> None:
    """§6.1 generates a LITERAL path per definition. A key containing a slash
    would mint a path segment nobody declared; an empty key would collide with
    the catalogue route at `/api/v1/reports` itself."""
    for bad in ("", "a/b"):
        with pytest.raises(ValueError) as exc:
            register(_definition(key=bad))
        assert "URL-safe" in str(exc.value), str(exc.value)
        assert bad not in REGISTRY


async def test_register_refuses_a_duplicate_key() -> None:
    """Two definitions under one key means one of them is unreachable and
    nobody can tell which from the catalogue."""
    key = "probe-duplicate-key"
    register(_definition(key=key))
    try:
        with pytest.raises(ValueError) as exc:
            register(_definition(key=key))
        assert "duplicate" in str(exc.value), str(exc.value)
    finally:
        # `REGISTRY` is process-global and the real definitions register at
        # import; leaving a probe behind would leak into every other test
        # file's sweep over `REGISTRY` (and into the catalogue's
        # `len(REGISTRY) == 4`).
        _REGISTRY.pop(key, None)
    assert key not in REGISTRY
