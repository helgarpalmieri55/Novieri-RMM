"""The source-declaration verifier (spec §0.4, §4.2, §12.1, §12.2, §12.3).

THIS FILE IS THE FALSIFICATION, AND IT IS PERMANENT RATHER THAN A ONE-TIME
MANUAL MUTATION. The four probe definitions below are deliberately wrong, live
in this module, and are NEVER registered in `REGISTRY` -- so the property
"`verify_definition` rejects a report whose SQL reads a table it did not
declare" is re-proved on every run, by a definition that is wrong on purpose,
instead of by someone temporarily breaking a real one and remembering to revert.

EACH PROBE ASSERTS ON THE CONTENT OF THE REJECTION, NOT MERELY THAT SOMETHING
RAISED. A `pytest.raises` whose only diagnosis is `DID NOT RAISE` tells you
nothing about WHICH guard fired, and this project has shipped that test more
than once. So: the offending table name must appear in the message, and where
two guards could both fire, the TYPE discriminates which one did.

PROBE 3 IS THE ONE THAT EARNS ITS PLACE MOST. Delete it and a walker that only
inspects `Select.get_final_froms()` -- the obvious implementation -- passes
probes 1, 2 and 4 and ships green, while reading `ticket` through an `EXISTS`
under a `devices.read`-only requirement.
"""

from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel
from sqlalchemy import Select, exists, func, select
from sqlalchemy.sql import visitors

from app.models import Agent, Credential, Device
from app.modules.reporting.errors import DenylistedSource, UndeclaredSource
from app.modules.reporting.registry import TABLE_PERMISSIONS, ReportDefinition
from app.modules.reporting.sources import probe_params, tables_read, verify_definition
from app.modules.service_desk.models import Ticket


class _Params(BaseModel):
    limit: int = 50
    offset: int = 0


class _Row(BaseModel):
    device_id: UUID


def _totals(*, org_id: UUID, params: BaseModel) -> Select[Any]:
    """A totals statement that reads only `devices`, so every probe below
    isolates the thing it is about to its `build`."""
    return select(func.count().label("devices")).select_from(Device).where(
        Device.org_id == org_id
    )


def _probe(key: str, sources: set[str], build: Any) -> ReportDefinition:
    return ReportDefinition(
        key=key,
        summary="A deliberately-wrong probe.",
        description="Never registered. Exists so the verifier is falsified on every run.",
        params=_Params,
        row_model=_Row,
        sources=frozenset(sources),
        build=build,
        build_totals=_totals,
        max_limit=100,
        export_columns=("device_id",),
        export_free_text=frozenset(),
        export_row_cap=10,
    )


# --------------------------------------------------------------------------- #
# Probe 1: the undeclared join.
# --------------------------------------------------------------------------- #


def _build_undeclared_join(*, org_id: UUID, params: BaseModel) -> Select[Any]:
    return (
        select(Device.id, Ticket.subject)
        .join(Ticket, Ticket.org_id == Device.org_id)
        .where(Device.org_id == org_id)
    )


async def test_an_undeclared_join_is_rejected_and_the_table_is_named() -> None:
    """A report whose SQL joins a table it did not declare must fail the build.

    Without this, `sources={"devices"}` derives `requires={"devices.read"}` and
    the report serves ticket subjects to a caller `GET /api/v1/tickets` refuses
    -- with every permission test in this module still green, because they all
    read `requires`, and `requires` would be honestly derived from a dishonest
    declaration.
    """
    probe = _probe("probe-undeclared-join", {"devices"}, _build_undeclared_join)
    assert probe.requires == {"devices.read"}, "the probe's premise: it under-requires"

    with pytest.raises(UndeclaredSource) as exc:
        verify_definition(probe)
    # THE TABLE NAME IS IN THE MESSAGE. Asserting only that something raised
    # would pass against a verifier that rejected this definition for an
    # unrelated reason, and would leave the reader with `DID NOT RAISE` as their
    # entire diagnosis on the day it regresses.
    assert "ticket" in str(exc.value), str(exc.value)
    assert exc.value.tables == {"ticket"}, exc.value.tables
    assert exc.value.report_key == "probe-undeclared-join"


# --------------------------------------------------------------------------- #
# Probe 2: the denylisted source, refused BEFORE the undeclared check.
# --------------------------------------------------------------------------- #


def _build_denylisted(*, org_id: UUID, params: BaseModel) -> Select[Any]:
    return (
        select(Device.id)
        .join(Credential, Credential.org_id == Device.org_id)
        .where(Device.org_id == org_id)
    )


async def test_a_denylisted_source_is_refused_before_the_undeclared_check() -> None:
    """`credentials` is neither declared NOR mapped, so BOTH guards could fire.

    The denylist must win, and the exception TYPE is what makes that observable:
    reverse the two blocks in `verify_definition` and this raises
    `UndeclaredSource` naming the same table, with a message that says "add it
    to `sources`" -- advice which, followed, would put the vault surface in a
    report. The remedy for a denylisted read is to delete the join; the remedy
    for an undeclared one is to widen `sources`; reporting the wrong one invites
    exactly the wrong fix.
    """
    probe = _probe("probe-denylisted", {"devices"}, _build_denylisted)

    with pytest.raises(DenylistedSource) as exc:
        verify_definition(probe)
    assert "credentials" in str(exc.value), str(exc.value)
    assert exc.value.name == "credentials"
    # Discriminating: `DenylistedSource` is not a subclass of
    # `UndeclaredSource`, so the `raises` above genuinely tells the two apart.
    assert not issubclass(DenylistedSource, UndeclaredSource)


async def test_a_denylisted_source_declared_honestly_is_still_refused() -> None:
    """The construction-time half of the same rule (§3): declaring it is not a
    mitigation. `ReportDefinition.__post_init__` refuses before a statement
    exists to verify."""
    with pytest.raises(DenylistedSource) as exc:
        _probe("probe-denylisted-declared", {"devices", "credentials"}, _build_denylisted)
    assert "credentials" in str(exc.value), str(exc.value)


# --------------------------------------------------------------------------- #
# Probe 3: the subquery hider. THE PROBE THAT PROVES THE TRAVERSAL IS DEEP.
# --------------------------------------------------------------------------- #


def _build_subquery_hider(*, org_id: UUID, params: BaseModel) -> Select[Any]:
    """`ticket` appears NOWHERE in the top-level FROM list -- it is reachable
    only through the correlated `EXISTS`."""
    return (
        select(Device.id)
        .where(Device.org_id == org_id)
        .where(exists().where(Ticket.org_id == Device.org_id))
    )


async def test_a_table_reachable_only_through_exists_is_still_found() -> None:
    """DELETE THIS TEST AND A WALKER THAT ONLY INSPECTS `get_final_froms()`
    SHIPS GREEN.

    That walker is the obvious implementation, it passes probes 1, 2 and 4, and
    it reports `{"devices"}` for this statement -- so the report would be
    authorized on `devices.read` alone while reading `ticket`. The two
    assertions below are separated on purpose: the first pins the walk itself
    (so a regression is diagnosed at `tables_read`, not at the verifier), the
    second pins that the verifier acts on it.
    """
    stmt = _build_subquery_hider(org_id=UUID(int=0), params=_Params())

    # The premise, made explicit: the shallow answer really is different.
    shallow = {f.name for f in stmt.get_final_froms() if hasattr(f, "name")}
    assert shallow == {"devices"}, shallow
    assert tables_read(stmt) == {"devices", "ticket"}, tables_read(stmt)

    probe = _probe("probe-subquery-hider", {"devices"}, _build_subquery_hider)
    with pytest.raises(UndeclaredSource) as exc:
        verify_definition(probe)
    assert "ticket" in str(exc.value), str(exc.value)


async def test_a_table_reachable_only_through_a_scalar_subquery_is_still_found() -> None:
    """The same shape one level further in: a correlated scalar sub-select in
    the SELECT list rather than in the WHERE clause. `iterate` is claimed to
    descend the whole clause tree; this is the second construct that claim is
    asserted on rather than assumed."""

    def build(*, org_id: UUID, params: BaseModel) -> Select[Any]:
        newest = (
            select(func.max(Ticket.created_at))
            .where(Ticket.org_id == Device.org_id)
            .correlate(Device)
            .scalar_subquery()
        )
        return select(Device.id, newest.label("newest")).where(Device.org_id == org_id)

    probe = _probe("probe-scalar-subquery", {"devices"}, build)
    with pytest.raises(UndeclaredSource) as exc:
        verify_definition(probe)
    assert "ticket" in str(exc.value), str(exc.value)


# --------------------------------------------------------------------------- #
# Probe 4: over-declaration PASSES. Stated explicitly so nobody "fixes" it.
# --------------------------------------------------------------------------- #


def _build_devices_only(*, org_id: UUID, params: BaseModel) -> Select[Any]:
    return select(Device.id).where(Device.org_id == org_id)


async def test_over_declaring_a_source_passes() -> None:
    """THE CHECK IS `read ⊆ declared`, NOT EQUALITY, AND THAT IS DELIBERATE.

    A definition declaring `ticket` and not reading it derives
    `requires={"devices.read", "ticket.read"}` -- a STRICTLY HARDER report to
    run. Over-declaration can only ever refuse a caller the narrower declaration
    would have admitted; it can never admit one. A rule that punished it would
    push authors toward the narrowest declaration that passes today, which is
    the opposite of the incentive this verifier exists to create.

    This test exists because a reader will otherwise assume equality and "fix"
    the subset check.
    """
    probe = _probe("probe-over-declared", {"devices", "ticket"}, _build_devices_only)
    assert probe.requires == {"devices.read", "ticket.read"}
    verify_definition(probe)  # no raise


async def test_the_verifier_also_walks_the_totals_statement() -> None:
    """`build_totals` is a second statement over the same declaration, so it is
    a second door into the same room. Verifying only `build` would let a totals
    query join anything it liked -- and totals are what a dashboard renders
    largest."""

    def totals_with_a_hidden_join(*, org_id: UUID, params: BaseModel) -> Select[Any]:
        return (
            select(func.count().label("n"))
            .select_from(Device)
            .join(Ticket, Ticket.org_id == Device.org_id)
            .where(Device.org_id == org_id)
        )

    probe = ReportDefinition(
        key="probe-totals-join",
        summary="A probe whose ROW query is honest and whose TOTALS query is not.",
        description="Never registered.",
        params=_Params,
        row_model=_Row,
        sources=frozenset({"devices"}),
        build=_build_devices_only,
        build_totals=totals_with_a_hidden_join,
        max_limit=100,
        export_columns=("device_id",),
        export_free_text=frozenset(),
        export_row_cap=10,
    )
    with pytest.raises(UndeclaredSource) as exc:
        verify_definition(probe)
    assert "ticket" in str(exc.value), str(exc.value)


# --------------------------------------------------------------------------- #
# `tables_read`'s own alarm.
# --------------------------------------------------------------------------- #


async def test_the_compiled_sql_tripwire_agrees_with_the_walk() -> None:
    """The regex is the ALARM on the guarantee, not the guarantee. It fires when
    the compiled text names a real table the clause walk missed -- an
    `AssertionError`, deliberately NOT a `ReportingError`, because that is a bug
    in `sources.py` and must not be catchable by anything handling a bad
    definition.

    Falsified by monkeypatching the walk to under-report: with `iterate`
    returning nothing, the text still says `FROM devices`, and the alarm fires.
    """
    stmt = _build_devices_only(org_id=UUID(int=0), params=_Params())
    assert tables_read(stmt) == {"devices"}

    original = visitors.iterate
    try:
        visitors.iterate = lambda *a, **k: iter(())  # type: ignore[assignment]
        with pytest.raises(AssertionError) as exc:
            tables_read(stmt)
        assert "traversal gap" in str(exc.value), str(exc.value)
        assert "devices" in str(exc.value), str(exc.value)
    finally:
        visitors.iterate = original  # type: ignore[assignment]


async def test_probe_params_supplies_required_fields() -> None:
    """A definition with REQUIRED params (`sla-attainment`'s `from`/`to`, §5.4)
    still has to be verifiable. `model()` raises for it; `model_construct()`
    would leave the attributes unset and fail inside `build()` with an
    `AttributeError` that says nothing."""

    class Required(BaseModel):
        window_from: Any
        count: int
        note: str = "default"

    built = probe_params(Required)
    assert built.note == "default"
    assert built.count == 1


# --------------------------------------------------------------------------- #
# The real registry.
#
# §12.1/§12.2/§12.3's sweep over `REGISTRY` lives in
# `tests/test_reporting_runner.py`, WITH THE FIRST DEFINITION, and not here.
#
# The reason is constraint 3's empty-answer trap taken seriously rather than
# ceremonially. This module ships BEFORE any report exists -- that ordering is a
# security property, not a convenience (§0.4: sequencing the verifier after the
# first report means shipping one report on an unverified declaration). So at
# this commit `REGISTRY` is empty by construction, and
# `assert REGISTRY, "the registry is empty"` would be a RED test, while
# `@pytest.mark.parametrize("key", sorted(REGISTRY))` would collect zero cases
# and report success. Writing the sweep here would therefore mean either a
# knowingly-failing commit or exactly the vacuous green the constraint exists to
# forbid. It goes where its first non-vacuous case does.
# --------------------------------------------------------------------------- #


async def test_the_agent_certificate_columns_are_reachable_but_forbidden() -> None:
    """The premise `_NEVER_A_COLUMN` rests on, asserted rather than assumed:
    `agents` IS a mapped, readable source, so its certificate columns are
    reachable through a legitimate `devices.read` report. That is precisely why
    the prohibition has to be column-level rather than table-level."""
    assert TABLE_PERMISSIONS["agents"] == "devices.read"
    assert hasattr(Agent, "cert_fingerprint")
