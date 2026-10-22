"""CSV export: the header contract, the escaping, the cap, and the audit event
(spec §7, §0.7, §0.8, §12.8, §12.9).

**THE TWO ASSERTIONS THIS FILE EXISTS FOR ARE BOTH NEGATIVE ONES**, and both are
the half that regresses silently:

1. **The cap is observed as a STATUS, never as a row count.** `assert len(rows)
   <= cap` passes against an implementation that truncates, and a truncated CSV
   is indistinguishable from a complete one once it is in a spreadsheet -- a
   wrong-number defect, not a paging inconvenience (§0.8). So every cap
   assertion here reads `response.status_code`, the content type, and the typed
   body; none of them counts rows.
2. **Nothing is audited on a RUN.** §0.7 is a decision that is easy to reverse
   by accident -- a `record()` call added to `execute_report` "for symmetry"
   puts every dashboard refresh on the audit chain's per-org advisory lock, in a
   table that cannot be pruned. The export half of that test would stay green;
   only the run half goes red.

**THE ESCAPING IS SIX CASES, NOT ONE.** `app/core/csv_export.py`'s
`_DANGEROUS_PREFIXES` is `("=", "+", "-", "@")` and nothing else -- re-verified
at the top of `test_the_shared_escaper_still_guards_only_four_prefixes` below.
A single `=` case therefore passes against an implementation that calls the
shared helper and drops reporting's own leading-whitespace normalisation, which
is the exact gap §0.8 exists to close. The tab and CR cases are the ones that go
red for that implementation.

**THE SIGNED-AMOUNT CASE IS THE OTHER DIRECTION**, and it took two attempts to
make it bite. "Escape every column" satisfies all six escaping cases and
corrupts every machine reader, so a server-formatted signed figure must come
back `-5.00` and not `'-5.00`. No v1 report has a column that can go negative,
so this is proved on a probe definition (never registered, like
`tests/test_reporting_sources.py`'s) driven through the same writer the routes
use -- and the probe's column is a `Decimal`, not an `int`, because
`escape_formula` only ever touches a `str`. MEASURED: with an `int` column the
"escape every column" mutation ran GREEN, since `escape_formula(-5)` returns
`-5`. A `Decimal` serialises to the string `"-5.00"` in `mode="json"`, which is
the shape of budget's Odoo `amount` column and the exact case
`app/core/csv_export.py`'s docstring says must never be escaped.
"""

import csv
import io
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.csv_export import _DANGEROUS_PREFIXES, escape_formula
from app.models import AuditEvent, Device

# IMPORTED FOR ITS SIDE EFFECT, AND LOAD-BEARING FOR EVERY PARAMETRIZED TEST
# HERE. `REGISTRY` is populated when the DEFINITIONS package is imported, and
# `@pytest.mark.parametrize(..., sorted(REGISTRY))` is evaluated at COLLECTION
# time -- before any fixture runs, so before `api_env` builds the app that would
# have imported it. Without this line every sweep collects an EMPTY parameter
# set and pytest reports the file as green while asserting nothing.
from app.modules.reporting import definitions as _definitions  # noqa: F401
from app.modules.reporting.export import normalise_free_text, render_csv
from app.modules.reporting.registry import REGISTRY, ReportDefinition
from tests.reporting_fixtures import (
    report_query,
    reporting_user,
    seed_device,
    seed_reportable_org,
)

_FLEET = "fleet-posture"


def export_path(key: str) -> str:
    return f"/api/v1/reports/{key}/export.csv"


async def test_the_registry_is_not_empty() -> None:
    """FIRST, for the reason in the module docstring: every sweep below is
    vacuous over an empty registry, and a vacuous sweep reports success."""
    assert REGISTRY, "the registry is empty; every parametrized test below is vacuous"


# --------------------------------------------------------------------------- #
# §12.8, first clause: the header IS the declaration.
# --------------------------------------------------------------------------- #


def _rows_of(body: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(body)))


async def _entitled(api_env: Any, org_id: UUID, key: str) -> dict[str, str]:
    _, sub = await reporting_user(
        api_env, org_id, "reporting.read", *sorted(REGISTRY[key].requires)
    )
    return await api_env.headers(org_id, sub)


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_the_header_equals_the_declared_export_columns(api_env: Any, key: str) -> None:
    """§7, §0.8's enforceable clause: *a column cannot appear in a file without
    appearing in the definition first.*

    Asserted as EQUALITY AND IN ORDER, not as a subset -- a subset assertion
    passes against an export that emits an extra column, which is the direction
    that leaks.
    """
    definition = REGISTRY[key]
    org_id = await seed_reportable_org(api_env)
    response = await api_env.client.get(
        f"{export_path(key)}{report_query(key)}",
        headers=await _entitled(api_env, org_id, key),
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")

    rows = _rows_of(response.text)
    assert rows, "no CSV at all; the header assertion below would be vacuous"
    assert tuple(rows[0]) == definition.export_columns
    # And the file is not header-only: `seed_reportable_org` guarantees every
    # report has at least one row, so a body of zero rows means the export ran a
    # different query from the report.
    assert len(rows) > 1, response.text


# --------------------------------------------------------------------------- #
# §12.8, second clause: the escaping. SIX CASES.
# --------------------------------------------------------------------------- #


async def test_the_shared_escaper_still_guards_only_four_prefixes() -> None:
    """The PREMISE of the two-stage design, pinned (Global Constraint 8).

    Reporting normalises leading `\\t`/`\\r`/`\\n` and strips NUL itself
    *because* the shared helper does not. If somebody extends
    `app/core/csv_export.py` -- six modules call it, one of them a pinned ERP
    wire contract -- this goes red and the normalisation stage can be
    reconsidered, rather than silently becoming redundant.
    """
    assert _DANGEROUS_PREFIXES == ("=", "+", "-", "@")
    assert escape_formula("\tsum") == "\tsum", "the shared helper does not guard a tab"
    assert escape_formula("\rsum") == "\rsum", "the shared helper does not guard a CR"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("=SUM(1,1)", "'=SUM(1,1)"),
        ("+SUM(1,1)", "'+SUM(1,1)"),
        ("-SUM(1,1)", "'-SUM(1,1)"),
        ("@SUM(1,1)", "'@SUM(1,1)"),
        ("\t=SUM(1,1)", "'=SUM(1,1)"),
        ("\r=SUM(1,1)", "'=SUM(1,1)"),
    ],
)
async def test_a_formula_lead_in_in_free_text_round_trips_neutralised(
    api_env: Any, payload: str, expected: str
) -> None:
    """§12.8. `hostname` is agent-supplied free text and is on
    `fleet-posture`'s `export_free_text`, so a hostname is the real path a
    payload takes into a spreadsheet.

    THE TAB AND CR CASES ARE THE FALSIFIERS. Delete reporting's normalisation
    and the first four still pass (the shared helper handles them) while these
    two come back as `\\t=SUM(1,1)` -- a cell Excel still evaluates, because a
    leading whitespace character is skipped before the `=` is read.
    """
    org_id = await seed_reportable_org(api_env)
    device = await seed_device(api_env.admin_engine, org_id, hostname=payload)

    response = await api_env.client.get(
        export_path(_FLEET), headers=await _entitled(api_env, org_id, _FLEET)
    )
    assert response.status_code == 200, response.text

    rows = _rows_of(response.text)
    header = rows[0]
    hostname_at = header.index("hostname")
    id_at = header.index("device_id")
    matched = [row for row in rows[1:] if row[id_at] == str(device.device_id)]
    assert len(matched) == 1, f"{len(matched)} rows for the seeded device"
    cell = matched[0][hostname_at]

    assert cell == expected, repr(cell)
    # Stated separately from the equality above, because it is the property --
    # the equality is one instance of it.
    assert not cell.startswith(("\t", "\r", "\n")), repr(cell)
    assert not cell.startswith(_DANGEROUS_PREFIXES), repr(cell)


async def test_a_null_column_renders_empty_never_the_string_none(api_env: Any) -> None:
    """§7. A device with no agent has a null `last_seen_at`; `"None"` in that
    cell is a value a spreadsheet sorts and a machine reader parses."""
    org_id = await api_env.create_org("rep-null")
    device = await seed_device(api_env.admin_engine, org_id, hostname="no-agent")

    response = await api_env.client.get(
        export_path(_FLEET), headers=await _entitled(api_env, org_id, _FLEET)
    )
    assert response.status_code == 200, response.text
    rows = _rows_of(response.text)
    header = rows[0]
    (row,) = [r for r in rows[1:] if r[header.index("device_id")] == str(device.device_id)]

    assert row[header.index("last_seen_at")] == ""
    assert row[header.index("device_group_id")] == ""
    assert row[header.index("newest_alert_opened_at")] == ""
    assert "None" not in response.text


# --------------------------------------------------------------------------- #
# The OTHER direction: a column the server formatted is never escaped.
# --------------------------------------------------------------------------- #


class _ProbeParams(BaseModel):
    limit: int = 50
    offset: int = 0


class _ProbeRow(BaseModel):
    device_id: UUID = Field(description="The subject.")
    hostname: str = Field(description="Free text.")
    # A `Decimal`, NOT an `int`, AND THE DIFFERENCE IS THE WHOLE TEST.
    # `escape_formula` only ever touches a `str`, so an `int` of `-5` survives a
    # blanket "escape every column" implementation untouched and this test would
    # pass against the bug -- MEASURED: the first version of this probe used an
    # `int` and the mutation that escapes every column ran GREEN. Pydantic
    # renders a `Decimal` as a STRING in `mode="json"` (`"-5.00"`), which is
    # exactly the shape of budget's Odoo `amount` column -- the one
    # `csv_export.py`'s docstring says must never be escaped, because an ERP
    # importer handed `'-5.00` books a silently wrong figure.
    balance: Decimal = Field(description="A signed amount the server formatted.")


def _probe_build(*, org_id: UUID, params: BaseModel) -> Select[Any]:
    return select(Device.id.label("device_id")).where(Device.org_id == org_id)


def _probe_totals(*, org_id: UUID, params: BaseModel) -> Select[Any]:
    return select(func.count().label("devices")).select_from(Device).where(
        Device.org_id == org_id
    )


_PROBE = ReportDefinition(
    key="probe-export",
    summary="Never registered; exists so the writer is falsified on every run.",
    description="Never registered.",
    params=_ProbeParams,
    row_model=_ProbeRow,
    sources=frozenset({"devices"}),
    build=_probe_build,
    build_totals=_probe_totals,
    max_limit=100,
    export_columns=("device_id", "hostname", "balance"),
    export_free_text=frozenset({"hostname"}),
    export_row_cap=10,
)


async def test_a_signed_number_is_not_escaped() -> None:
    """The failure mode "escape the whole row" satisfies all six cases above and
    corrupts every machine reader: `csv_export.py`'s own docstring spends a
    paragraph on budget's Odoo export, where `'-100.00` is a booked figure that
    is silently wrong.

    Driven through the SAME writer the routes use, on a probe definition,
    because no v1 report has a column that can go negative -- so without the
    probe this property is untestable and would be untested.
    """
    row = _ProbeRow(device_id=uuid4(), hostname="-CorpName", balance=Decimal("-5.00"))
    rows = _rows_of(render_csv(_PROBE, [row]))

    assert rows[0] == ["device_id", "hostname", "balance"]
    # The declared free-text column IS escaped ...
    assert rows[1][1] == "'-CorpName"
    # ... and the signed amount beside it is NOT.
    assert rows[1][2] == "-5.00", rows[1]


async def test_normalisation_strips_nul_which_the_database_cannot_store() -> None:
    """§0.8 names NUL alongside the leading whitespace. PostgreSQL rejects a NUL
    inside a `text` value outright, so this stage can never be exercised through
    a seeded row -- it is proved here or nowhere."""
    assert normalise_free_text("a\x00b") == "ab"
    assert normalise_free_text("\x00=SUM(1,1)") == "=SUM(1,1)"
    assert normalise_free_text(None) is None
    assert normalise_free_text(7) == 7
    # Escaping happens AFTER normalisation, so a NUL cannot hide a lead-in.
    rows = _rows_of(
        render_csv(_PROBE, [_ProbeRow(device_id=uuid4(), hostname="\x00=X", balance=Decimal(1))])
    )
    assert rows[1][1] == "'=X", rows[1]


# --------------------------------------------------------------------------- #
# §12.8, fourth clause / Global Constraint 7: the cap REFUSES, never truncates.
# --------------------------------------------------------------------------- #


@contextmanager
def capped_at(definition: ReportDefinition, cap: int) -> Iterator[None]:
    """Lower a definition's export cap for one test.

    `ReportDefinition` is a frozen dataclass on purpose, and the shipped cap is
    50_000 -- seeding 50_001 devices to observe a 413 would be a test nobody
    runs. `object.__setattr__` with a `finally` restore is the narrowest way to
    make the boundary observable; it does NOT touch the code under test.
    """
    original = definition.export_row_cap
    object.__setattr__(definition, "export_row_cap", cap)
    try:
        yield
    finally:
        object.__setattr__(definition, "export_row_cap", original)


async def _org_with_devices(api_env: Any, count: int) -> UUID:
    org_id = await api_env.create_org("rep-cap")
    for index in range(count):
        await seed_device(api_env.admin_engine, org_id, hostname=f"host-{index:03d}")
    return org_id


async def test_over_the_cap_is_a_413_and_there_is_no_csv_body(api_env: Any) -> None:
    """GLOBAL CONSTRAINT 7. THE OBSERVATION IS THE STATUS AND THE BODY, NEVER A
    ROW COUNT: `assert len(rows) <= cap` is satisfied by the truncating
    implementation this refuses to be.

    FALSIFY by making the export `.limit(cap)` instead of raising: this returns
    200 with a `text/csv` body of exactly `cap` rows and both remaining
    assertions go red naming the content type.
    """
    definition = REGISTRY[_FLEET]
    org_id = await _org_with_devices(api_env, 3)
    headers = await _entitled(api_env, org_id, _FLEET)

    with capped_at(definition, 2):
        response = await api_env.client.get(export_path(_FLEET), headers=headers)

    assert response.status_code == 413, response.text
    assert not response.headers["content-type"].startswith("text/csv")
    detail = response.json()["detail"]
    assert detail["error"] == "report_export_too_large"
    assert detail["cap"] == 2
    assert detail["matched"] == 3
    assert detail["report_key"] == _FLEET
    # No header row, no data row, nothing that could be opened as a file.
    assert "device_id" not in response.text


async def test_exactly_at_the_cap_succeeds(api_env: Any) -> None:
    """The boundary, so the comparison is `>` and not `>=` by accident. Without
    this a cap of N that refused at N rows would pass every other test here."""
    definition = REGISTRY[_FLEET]
    org_id = await _org_with_devices(api_env, 3)
    headers = await _entitled(api_env, org_id, _FLEET)

    with capped_at(definition, 3):
        response = await api_env.client.get(export_path(_FLEET), headers=headers)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert len(_rows_of(response.text)) == 4  # header + 3


async def test_the_export_ignores_limit_and_exports_the_whole_match(api_env: Any) -> None:
    """`limit` pages the REPORT; it does not shrink the FILE.

    An export that honoured `limit=1` would hand back a one-row CSV that looks
    complete -- §0.8's defect wearing a query parameter -- and would also make
    the cap unreachable, since a limited statement can never match more than the
    limit. The cap is measured over the WHOLE match; so is the file.
    """
    org_id = await _org_with_devices(api_env, 3)
    headers = await _entitled(api_env, org_id, _FLEET)
    response = await api_env.client.get(
        f"{export_path(_FLEET)}?limit=1", headers=headers
    )
    assert response.status_code == 200, response.text
    assert len(_rows_of(response.text)) == 4, response.text


# --------------------------------------------------------------------------- #
# §12.9: audited on export, and NOTHING on a run. Both directions.
# --------------------------------------------------------------------------- #


async def _audit_count(api_env: Any, org_id: UUID) -> int:
    async with AsyncSession(api_env.admin_engine) as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(AuditEvent).where(
                        AuditEvent.org_id == org_id
                    )
                )
            ).scalar_one()
        )


async def test_a_run_writes_nothing_and_an_export_writes_exactly_one(
    api_env: Any,
) -> None:
    """§0.7, BOTH HALVES IN ONE TEST so they cannot drift apart.

    The run half is the one that silently regresses: adding a `record()` call to
    `execute_report` leaves the export half green and puts every dashboard
    refresh on `pg_advisory_xact_lock(hashtext('audit:' || org))`, in a table
    `app_user` may not prune (`docs/07-backup-restore.md` §4).

    FALSIFY the export half by deleting the `record()` call: the second delta is
    0 instead of 1.
    """
    org_id = await seed_reportable_org(api_env)
    headers = await _entitled(api_env, org_id, _FLEET)

    before = await _audit_count(api_env, org_id)
    run = await api_env.client.get(f"/api/v1/reports/{_FLEET}", headers=headers)
    assert run.status_code == 200, run.text
    after_run = await _audit_count(api_env, org_id)
    assert after_run == before, "running a report wrote an audit event (§0.7)"

    export = await api_env.client.get(export_path(_FLEET), headers=headers)
    assert export.status_code == 200, export.text
    after_export = await _audit_count(api_env, org_id)
    assert after_export == after_run + 1, "the export wrote no audit event"


async def test_the_recorded_event_names_the_report_and_carries_no_rows(
    api_env: Any,
) -> None:
    """§7: `target_type="report"`, `target_id=key`, metadata exactly
    `{params, row_count, format}`.

    THE KEY SET IS ASSERTED AS EQUALITY. A subset assertion passes against
    metadata that also carries `rows` -- which is the one thing §7 says it must
    never contain, and the audit table is append-only and unprunable, so a row
    of exported data written there cannot be taken back.
    """
    org_id = await seed_reportable_org(api_env)
    headers = await _entitled(api_env, org_id, _FLEET)

    response = await api_env.client.get(
        f"{export_path(_FLEET)}?os_family=windows", headers=headers
    )
    assert response.status_code == 200, response.text
    exported_rows = len(_rows_of(response.text)) - 1
    assert exported_rows > 0

    async with AsyncSession(api_env.admin_engine) as session:
        events = list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "report.exported",
                    )
                )
            ).scalars()
        )
    assert len(events) == 1, [e.action for e in events]
    event = events[0]
    assert event.target_type == "report"
    assert event.target_id == _FLEET
    assert set(event.meta) == {"params", "row_count", "format"}, event.meta
    assert event.meta["format"] == "csv"
    assert event.meta["row_count"] == exported_rows
    # The VALIDATED params, so the record says what was asked for.
    assert event.meta["params"]["os_family"] == "windows"


# --------------------------------------------------------------------------- #
# The export is never a weaker gate than the report it exports (§7).
# --------------------------------------------------------------------------- #


_CONJUNCTION_CASES = sorted(
    (key, code) for key in REGISTRY for code in sorted(REGISTRY[key].requires)
)


@pytest.mark.parametrize(("key", "withheld"), _CONJUNCTION_CASES)
async def test_the_export_route_denies_a_missing_source_code(
    api_env: Any, key: str, withheld: str
) -> None:
    """An export route carries the SAME two dependencies as its report route.

    THE ENTITLED CALLER MUST GET A NON-EMPTY FILE first: a 403 and a 200 with a
    header-only CSV are equally "not a leak", and only one of them is the
    behaviour under test.
    """
    definition = REGISTRY[key]
    org_id = await seed_reportable_org(api_env)
    path = f"{export_path(key)}{report_query(key)}"

    all_codes = ["reporting.read", *sorted(definition.requires)]
    _, entitled = await reporting_user(api_env, org_id, *all_codes)
    ok = await api_env.client.get(path, headers=await api_env.headers(org_id, entitled))
    assert ok.status_code == 200, ok.text
    assert len(_rows_of(ok.text)) > 1, "entitled caller got an empty file; the 403 proves nothing"

    partial = [code for code in all_codes if code != withheld]
    _, denied = await reporting_user(api_env, org_id, *partial)
    refused = await api_env.client.get(path, headers=await api_env.headers(org_id, denied))
    assert refused.status_code == 403, refused.text
    detail = refused.json()["detail"]
    assert detail["error"] == "report_permission_denied"
    assert detail["missing"] == [withheld], detail


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_an_export_denied_by_permission_writes_no_audit_event(
    api_env: Any, key: str
) -> None:
    """The gate runs BEFORE the handler body (§4.1), so a refused export leaves
    no trace of the parameters it was asked for -- and, more to the point, never
    reaches the read at all."""
    org_id = await seed_reportable_org(api_env)
    _, sub = await reporting_user(api_env, org_id, "reporting.read")
    before = await _audit_count(api_env, org_id)
    refused = await api_env.client.get(
        f"{export_path(key)}{report_query(key)}",
        headers=await api_env.headers(org_id, sub),
    )
    assert refused.status_code == 403, refused.text
    assert await _audit_count(api_env, org_id) == before


# --------------------------------------------------------------------------- #
# The generated export routes themselves (§6.1, §10.3).
# --------------------------------------------------------------------------- #


def _flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    for route in getattr(router, "routes", []):
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            found.extend(_flatten(original, prefix + (getattr(context, "prefix", "") or "")))
        else:
            found.append((prefix + getattr(route, "path", ""), route))
    return found


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_every_definition_has_exactly_one_export_route(api_env: Any, key: str) -> None:
    """One per definition, generated by the same loop -- so an export cannot be
    forgotten for one report or hand-written differently for another."""
    matched = [
        route
        for path, route in _flatten(api_env.app.router)
        if path == export_path(key)
        and isinstance(route, APIRoute)
        and "GET" in (route.methods or set())
    ]
    assert len(matched) == 1, f"{len(matched)} routes claim {export_path(key)}"
    assert matched[0].endpoint.__module__.startswith("app.modules.reporting")
