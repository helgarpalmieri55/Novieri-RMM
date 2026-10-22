"""THE SOURCE-DECLARATION VERIFIER -- the load-bearing module of this package
(spec §0.4, §4.2, §12.2).

**WHAT WOULD BE FALSE WITHOUT THIS FILE.** §0.4's guarantee is that *an operator
never sees through a report what the module APIs would refuse them directly*.
The mechanism is: a definition declares the tables it reads, `TABLE_PERMISSIONS`
names the read code governing each, `requires` is the union, and the caller must
hold all of them. Every step of that is sound EXCEPT the first, which is a
DECLARATION -- and a declaration nobody checks is a comment. A definition could
declare `sources={"devices"}`, join `ticket` in its `build()`, derive
`requires={"devices.read"}`, and serve ticket subjects to a caller the ticket
API refuses, with every other test in this module green. Nothing else in the
design catches that. This file is what makes `sources` a verified fact.

That is why the verifier ships BEFORE the first report and why every report task
is blocked on it: sequencing it afterwards would mean shipping at least one
report on an unverified declaration, which is the whole hole.

**HOW THE WALK WORKS, AND WHY NOT `get_final_froms()`.**
`sqlalchemy.sql.visitors.iterate` descends the entire clause tree, so a table
reachable only inside a correlated subquery, an `EXISTS`, a CTE or a scalar
sub-select is still found. `Select.get_final_froms()` returns only the top-level
FROM list and would report `{"devices"}` for

    select(Device.id).where(exists().where(Ticket.org_id == Device.org_id))

-- a report that reads `ticket` while declaring only `devices`, which is exactly
the leak above wearing a subquery. `tests/test_reporting_sources.py` keeps a
permanent probe for that shape; deleting the probe is what would let a
`get_final_froms()` walker ship green.

**THE REGEX IS THE ALARM, NOT THE GUARANTEE.** `iterate` is a traversal, and a
traversal can have a gap on a construct nobody here has used yet. So the
statement is additionally compiled under the PostgreSQL dialect and every
identifier following `FROM`/`JOIN` that resolves to a REAL table in
`Base.metadata` is asserted to be a subset of what `iterate` found. If the text
names a table the walker did not, `tables_read` has a hole and the build fails
loudly rather than under-reporting silently. It is deliberately NOT the primary
mechanism: a regex over SQL text cannot see through a subquery alias and would
be the wrong thing to trust.
"""

import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError
from sqlalchemy import Select, Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import visitors

from app.models import Base
from app.modules.reporting.errors import DenylistedSource, UndeclaredSource
from app.modules.reporting.registry import _NEVER_A_SOURCE, REGISTRY, ReportDefinition

# Identifiers following FROM/JOIN in compiled SQL. A sub-select renders as
# `FROM (SELECT ...)`, which this deliberately does not match -- the alarm only
# ever fires on a bare table name, and every name it yields is filtered through
# `Base.metadata.tables` before it is compared, so a CTE name or an alias can
# never make it a false positive.
_FROM_OR_JOIN = re.compile(r'\b(?:FROM|JOIN)\s+"?([A-Za-z_][A-Za-z0-9_$]*)"?')


def tables_read(stmt: Select[Any]) -> frozenset[str]:
    """Every table name `stmt` reads -- through joins, subqueries, CTEs,
    `EXISTS` and scalar sub-selects alike."""
    # The `isinstance(element, Table)` filter is what makes this comprehension's
    # type sound: `iterate` yields every `ClauseElement` in the tree.
    elements: Iterator[Any] = visitors.iterate(stmt)
    walked = {element.name for element in elements if isinstance(element, Table)}
    # `Select.compile` is untyped in SQLAlchemy 2.0.51's stubs, hence the narrow
    # ignore. Compiling is side-effect-free and never touches a connection.
    compiled = str(stmt.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]
    textual = {
        name for name in _FROM_OR_JOIN.findall(compiled) if name in Base.metadata.tables
    }
    if not textual <= walked:
        # The alarm on the guarantee (module docstring). Not a `ReportingError`:
        # this is not a bad definition, it is a bug in this file, and it must
        # not be catchable by anything that handles a bad definition.
        raise AssertionError(  # i18n-exempt: build-time, aborts the import and is never shown
            "tables_read has a traversal gap: the compiled SQL names "
            f"{sorted(textual - walked)}, which the clause walk did not find. "
            "Every authorization claim in this module rests on that walk."
        )
    return frozenset(walked)


def _sentinel(annotation: Any) -> Any:
    """A value that satisfies `annotation` well enough to BUILD a statement.

    Verification compiles a definition's SQL; it never executes it, so these
    values are never compared against a row and never leave this process. They
    exist because a definition whose params are REQUIRED (`sla-attainment`'s
    `from`/`to`, §5.4) cannot be instantiated with `Params()`, and the
    alternative -- `model_construct`, which skips validation -- would leave
    required attributes unset and blow up inside `build()` with an
    `AttributeError` that says nothing about what went wrong.
    """
    origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)[0]
    if origin in (Union, UnionType):
        for arg in get_args(annotation):
            if arg is not type(None):
                return _sentinel(arg)
        return None
    if origin in (list, set, frozenset, tuple):
        return origin()
    if annotation is datetime:
        return datetime.now(UTC) - timedelta(days=1)
    if annotation is date:
        return (datetime.now(UTC) - timedelta(days=1)).date()
    if annotation is UUID:
        return uuid4()
    if annotation is bool:
        return False
    if annotation is int:
        return 1
    if annotation in (float, Decimal):
        return annotation(1)
    if annotation is str:
        return "x"
    return None


def probe_params(model: type[BaseModel]) -> BaseModel:
    """The definition's own default params where it has them, and a typed
    sentinel where a field is required."""
    try:
        return model()
    except ValidationError:
        pass
    supplied: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        if info.is_required():
            supplied[info.alias or name] = _sentinel(info.annotation)
    return model(**supplied)


def verify_definition(definition: ReportDefinition) -> None:
    """Raise unless every table `definition`'s statements read is declared in
    its `sources` and none of them is denylisted.

    THE CHECK IS `read ⊆ declared`, NOT EQUALITY, and that is deliberate.
    Over-declaring is conservative: it grows `requires`, so it can only make a
    report HARDER to run, never easier. A rule punishing a definition for
    declaring a table it stopped reading would push authors toward the narrowest
    declaration that passes today, which is the opposite of the incentive this
    verifier exists to create. `tests/test_reporting_sources.py` states this
    explicitly so nobody "fixes" it into equality.

    THE DENYLIST IS CHECKED FIRST, over what the SQL actually READS -- not only
    over what was declared, which `ReportDefinition.__post_init__` already
    refused. A statement reaching `credentials` through a join it never declared
    is refused as FORBIDDEN, not as merely undeclared: the remedy for the former
    is to delete the join, and for the latter to widen `sources`, and reporting
    the wrong one invites exactly the wrong fix.
    """
    params = probe_params(definition.params)
    org_id = uuid4()
    read: set[str] = set()
    for build in (definition.build, definition.build_totals):
        read |= tables_read(build(org_id=org_id, params=params))

    forbidden = sorted(read & _NEVER_A_SOURCE)
    if forbidden:
        raise DenylistedSource(definition.key, forbidden[0])

    undeclared = read - definition.sources
    if undeclared:
        raise UndeclaredSource(definition.key, undeclared)


def verify_registry() -> None:
    """Every definition in `REGISTRY`.

    Called by `definitions/__init__.py` AT IMPORT, so a bad definition fails the
    application's import rather than only the test suite. A verifier that ran
    only under pytest would let a bad definition reach a running server whenever
    the suite was skipped, and the failure it prevents is a data disclosure --
    the wrong side of that trade.
    """
    for definition in REGISTRY.values():
        verify_definition(definition)
