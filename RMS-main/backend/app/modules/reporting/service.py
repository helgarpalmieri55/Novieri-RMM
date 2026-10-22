"""Bounded execution of a report (spec §6.4, §0.3 cost 1).

BUSINESS LOGIC IS HERE, NOT IN THE ROUTE HANDLER. The generated handler
(`api.py`) resolves the principal, opens `org_session`, calls one function here,
and maps errors -- the convention every module since Phase 11 follows, and one
this module could not break even if it wanted to, because there is no per-report
route function for anyone to put logic in.

**`SET LOCAL statement_timeout` IS THE ANSWER TO §0.3'S ACCEPTED COST.** v1
computes live, on request, with no snapshot layer, so the cost is slow reports on
large orgs. It is mitigated rather than wished away: every statement this module
issues runs under a timeout, and on expiry the caller gets a typed `504` naming
the report -- never a partial page, and never a page silently computed from a
smaller window. No `statement_timeout` was set anywhere in this codebase before
this module; it is one statement inside this module's own session and it forks
nothing.

THREE FACTS ABOUT THAT STATEMENT, MEASURED AGAINST THE DEPLOYED SERVER RATHER
THAN ASSUMED (the implementation plan asserted a different driver and a
different exception, and the code won):

1. **PostgreSQL does not accept a bind parameter in `SET`**, so the value is
   interpolated -- as a literal `'<n>ms'` built from an `int` THIS MODULE
   PRODUCED. It is never a request value; `REPORT_STATEMENT_TIMEOUT_MS` is a
   module constant and the `int()` is belt to the braces.
2. **The driver is `asyncpg`, not `psycopg`.** A cancellation surfaces as
   `sqlalchemy.exc.DBAPIError` (NOT `OperationalError`) whose `.orig` is
   `sqlalchemy.dialects.postgresql.asyncpg.AsyncAdapt_asyncpg_dbapi.Error` -- a
   generic adapter wrapper, not `asyncpg.exceptions.QueryCanceledError`, which
   appears only as `.orig.__cause__` and in the message text. So the narrow
   catch is on the **SQLSTATE**, `57014` (`query_canceled`), which the adapter
   does expose as `.orig.sqlstate`. That is also driver-independent: psycopg's
   `QueryCanceled` carries the same code, so this survives a driver swap that a
   class check would not.
3. **`SET LOCAL` genuinely does not leak.** Measured: `SHOW statement_timeout`
   returns `50ms` inside the transaction that set it and `0` on the next
   checkout of the same pooled connection. `tests/test_reporting_runner.py`
   pins both directions -- the second one falsifies by changing `SET LOCAL` to
   `SET`.

NEVER CATCH BARE `DBAPIError`. A syntax error, a permission error and a
constraint violation all arrive as one, and turning them into a `504
report_timeout` would tell an operator to narrow their window while the real
defect sat in the SQL.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Select, func, select, text
from sqlalchemy.engine import Result
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr_marked
from app.modules.reporting.errors import ReportTimeout
from app.modules.reporting.registry import (
    REGISTRY,
    TABLE_MODULE,
    TABLE_PERMISSIONS,
    ReportDefinition,
    source_recency,
)
from app.modules.reporting.schema import (
    CatalogueEntry,
    ReportCatalogue,
    ReportMeta,
    ReportPage,
    ReportSource,
)

# The ceiling on any report statement. CHOSEN, NOT MEASURED: it is well above
# any query in the seeded test fleet and well below the point at which a browser
# or a reverse proxy gives up on its own, so a report that hits it is reporting
# a real problem rather than a slow afternoon. Recorded in the spec amendment as
# a choice.
REPORT_STATEMENT_TIMEOUT_MS: int = 15_000

# The ceiling on any report's `limit`, and therefore the `le=` on its route's
# `limit` query parameter (§0.10 property 4). Also chosen, not measured.
DEFAULT_MAX_LIMIT: int = 1_000

# PostgreSQL SQLSTATE 57014, `query_canceled`. See the module docstring for why
# this and not an exception class.
_QUERY_CANCELED = "57014"


async def run_bounded(
    session: AsyncSession,
    stmt: Select[Any],
    *,
    timeout_ms: int = REPORT_STATEMENT_TIMEOUT_MS,
    report_key: str = "",
) -> Result[Any]:
    """Execute `stmt` under `SET LOCAL statement_timeout`, mapping a
    cancellation to `ReportTimeout` and letting every other database error
    through untouched."""
    # `SET LOCAL`, inside the caller's transaction, exactly as `org_session`
    # sets `app.current_org`. Re-issuing it per statement is free and keeps this
    # function the single place a report statement can be executed from.
    await session.execute(text(f"SET LOCAL statement_timeout = '{int(timeout_ms)}ms'"))
    try:
        return await session.execute(stmt)
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == _QUERY_CANCELED:
            raise ReportTimeout(report_key) from exc
        raise


def report_sources(definition: ReportDefinition) -> list[ReportSource]:
    """`meta.sources` (§2, §6.3), DERIVED from the definition's declared tables.

    One entry per module, not per table: a report reading `devices` and `agents`
    read one module. Sorted, so the response is stable across runs and a
    contract test can compare it.
    """
    seen: dict[str, ReportSource] = {}
    for table in sorted(definition.sources):
        module = TABLE_MODULE[table]
        if module not in seen:
            seen[module] = ReportSource(
                module=module,
                permission=TABLE_PERMISSIONS[table],
                recency=tr_marked(source_recency(table)),
            )
    return [seen[module] for module in sorted(seen)]


def _said_schema(schema: Any) -> Any:
    """`schema` with every `description` worded in the request's language.

    The parameter descriptions are `N_` marked where the params model declares
    them, so the OpenAPI document (built from the same model) stays English and
    only the catalogue answer is translated. A description that is not marked (a
    model's own docstring) is not in the catalogue and comes back as it was."""
    if isinstance(schema, dict):
        return {
            key: tr_marked(value)
            if key == "description" and isinstance(value, str)
            else _said_schema(value)
            for key, value in schema.items()
        }
    if isinstance(schema, list):
        return [_said_schema(item) for item in schema]
    return schema


def build_catalogue(granted: frozenset[str] | set[str]) -> ReportCatalogue:
    """The whole catalogue, as it looks to a caller holding `granted` (§6.2).

    NO SESSION, NO QUERY, NO ORG. `REGISTRY` is compiled into the image and
    `requires` is derived from it, so this function cannot read org data even by
    accident -- which is why the route needs no source conjunction and why two
    callers holding the same codes in different orgs get byte-identical answers.

    Every definition is listed, entitled or not. Hiding the ones a caller cannot
    run would make an under-permissioned operator's catalogue indistinguishable
    from a smaller deployment's, and the point of the route is to make a 403
    explicable rather than to curate it away.

    `params_schema` IS DERIVED PER ENTRY FROM THE DEFINITION'S OWN PARAMS MODEL
    (§17.11), never written out here: `ReportDefinition.params_json_schema()`
    reads the same `params` attribute `api.py` binds as the route's query model,
    so the published shape and the enforced shape cannot be two different
    documents. It is platform data on the same footing as `summary` and
    `requires` -- compiled into the image, identical for every caller -- and it
    is published for EVERY entry, including the ones this caller may not run,
    for the same §11 reason the rest of the entry is: a report you cannot run is
    a permissions conversation, not a secret, and a params bar that renders
    disabled is a better answer than one the browser had to guess.
    """
    return ReportCatalogue(
        reports=[
            CatalogueEntry(
                key=key,
                summary=tr_marked(REGISTRY[key].summary),
                description=tr_marked(REGISTRY[key].description),
                params_schema=_said_schema(REGISTRY[key].params_json_schema()),
                requires=sorted(REGISTRY[key].requires),
                entitled=REGISTRY[key].requires <= set(granted),
                missing_permissions=sorted(REGISTRY[key].requires - set(granted)),
            )
            for key in sorted(REGISTRY)
        ]
    )


async def execute_report(
    session: AsyncSession,
    definition: ReportDefinition,
    params: BaseModel,
    *,
    org_id: UUID,
    limit: int | None = None,
    offset: int | None = None,
) -> ReportPage[Any]:
    """Count, page, total, and describe -- in that order, all inside the
    caller's `org_session` transaction and all under the statement timeout.

    `limit`/`offset` default to the params model's own, which the route already
    validated against `le=definition.max_limit`. They are accepted explicitly so
    a caller that is not a route (a test, the export writer) does not have to
    mutate a validated params object to page.
    """
    effective_limit = limit if limit is not None else int(getattr(params, "limit", 50))
    effective_offset = offset if offset is not None else int(getattr(params, "offset", 0))

    rows_stmt = definition.build(org_id=org_id, params=params)

    total = (
        await run_bounded(
            session,
            select(func.count()).select_from(rows_stmt.subquery()),
            report_key=definition.key,
        )
    ).scalar_one()

    page = await run_bounded(
        session,
        rows_stmt.limit(effective_limit).offset(effective_offset),
        report_key=definition.key,
    )
    rows = [definition.row_model.model_validate(dict(m)) for m in page.mappings()]

    totals_row = (
        await run_bounded(
            session,
            definition.build_totals(org_id=org_id, params=params),
            report_key=definition.key,
        )
    ).mappings().one()
    totals = {name: int(value) for name, value in totals_row.items()}

    return ReportPage[Any](
        meta=ReportMeta(
            report_key=definition.key,
            generated_at=datetime.now(UTC),
            reproducible=definition.reproducible,
            # A point-in-time report has no window; a windowed one names its
            # params `window_from`/`window_to` so this stays derived rather than
            # per-report code.
            window_from=getattr(params, "window_from", None),
            window_to=getattr(params, "window_to", None),
            total=int(total),
            limit=effective_limit,
            offset=effective_offset,
            sources=report_sources(definition),
        ),
        totals=totals,
        rows=rows,
    )
