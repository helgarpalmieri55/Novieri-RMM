"""CSV export: the writer, reporting's own free-text normalisation, the cap, and
the one audit event (spec §7, §0.7, §0.8).

**THE CAP REFUSES; IT NEVER TRUNCATES.** Every existing exporter in this repo
(`ipam`, `inventory`, `budget`, `backup`, `patch`, `security_logs`) returns the
whole table. Reporting's do not, and the alternative to refusing is worse than
paging: a truncated CSV is indistinguishable from a complete one once it is in a
spreadsheet, so a quietly short file is a WRONG NUMBER, not an inconvenience.
The count runs first, over the whole match, and a match over the cap raises
before a single cell is formatted -- so there is no half-written body to
accidentally return.

**`limit`/`offset` PAGE THE REPORT AND ARE IGNORED HERE, DELIBERATELY.** They
are still accepted (the export shares the report's params model, so a link can
carry the same query string) and still recorded in the audit metadata, but they
do not shrink the file. Two reasons, and the second is the load-bearing one:
a one-row CSV returned for `limit=1` looks exactly as complete as any other
file, and a statement that is already `LIMIT`ed can never match more than its
limit -- so honouring `limit` would make the cap unreachable and §0.8's refusal
dead code.

**TWO-STAGE ESCAPING, AND THE FIRST STAGE IS THIS MODULE'S OWN.**
`app/core/csv_export.py::escape_formula` guards a leading `=`, `+`, `-`, `@` and
NOTHING ELSE -- verified: `_DANGEROUS_PREFIXES = ("=", "+", "-", "@")`. A cell
beginning `\\t=SUM(...)` or `\\r=SUM(...)` is still evaluated by importers that
skip leading whitespace before reading the formula lead-in, and an embedded NUL
truncates the cell for some readers. So reporting normalises its own declared
free-text columns first and THEN calls the shared helper. Extending the shared
helper would be the better fix and is deliberately not done here: six modules
call it, and budget's Odoo export is a pinned wire contract read by an ERP
importer, so changing it is its own reviewed change (§15, spec §0.8).

**PER COLUMN, NEVER BLANKET OVER A ROW.** Only `definition.export_free_text` is
escaped. `csv_export.py`'s docstring spends a paragraph on why: run the escaper
over a column the server itself formatted and a machine reader gets `'-100.00`
for every negative figure -- an import failure at best and a silently wrong
booked number at worst. Reporting has no money columns today and the rule is
still absolute, because the day it has one nobody will re-read this file.
"""

import csv
import io
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.csv_export import escape_formula
from app.modules.reporting.errors import ReportExportTooLarge
from app.modules.reporting.registry import ReportDefinition
from app.modules.reporting.service import run_bounded

# Leading characters a spreadsheet importer skips before it reads the formula
# lead-in, so `\t=SUM(1,1)` is a live formula that `escape_formula` alone lets
# through. Stripped rather than escaped: unlike `-`, none of these is ever
# meaningful data in a hostname, a queue name or a repository name.
_LEADING_LEAD_INS = "\t\r\n"


def normalise_free_text(value: Any) -> Any:
    """Reporting's own pre-stage (§0.8). Non-strings pass through untouched, so
    this is safe to compose with `escape_formula` on any cell."""
    if not isinstance(value, str):
        return value
    return value.replace("\x00", "").lstrip(_LEADING_LEAD_INS)


def _cell(definition: ReportDefinition, column: str, value: Any) -> Any:
    if value is None:
        # Never `"None"`: that is a value a spreadsheet sorts and a machine
        # reader parses.
        return ""
    if column in definition.export_free_text:
        return escape_formula(normalise_free_text(value))
    return value


def render_csv(definition: ReportDefinition, rows: Sequence[BaseModel]) -> str:
    """The header row IS `definition.export_columns`, in order (§7).

    A column cannot appear in a file without appearing in the definition first,
    and `tests/test_reporting_export.py` asserts the emitted header EQUALS the
    declaration rather than being a subset of it -- the subset reading passes
    against an export that emits an extra column, which is the direction that
    leaks.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(definition.export_columns)
    for row in rows:
        # `mode="json"` so a UUID, a datetime and an enum reach the file as the
        # same strings the API serves, rather than as Python `repr`s.
        data = row.model_dump(mode="json")
        writer.writerow(
            [_cell(definition, column, data.get(column)) for column in definition.export_columns]
        )
    return buffer.getvalue()


async def export_report(
    session: AsyncSession,
    definition: ReportDefinition,
    params: BaseModel,
    *,
    org_id: UUID,
    actor_id: UUID | None,
) -> str:
    """Count, refuse or render, and audit -- all inside the caller's
    `org_session` transaction and all under the statement timeout.

    THE AUDIT EVENT IS WRITTEN IN THE SAME TRANSACTION AS THE READ (§7), so a
    file cannot leave the platform without the record of it landing too. It is
    the ONLY thing this module ever writes; running a report writes nothing
    (§0.7).
    """
    rows_stmt = definition.build(org_id=org_id, params=params)

    matched = int(
        (
            await run_bounded(
                session,
                select(func.count()).select_from(rows_stmt.subquery()),
                report_key=definition.key,
            )
        ).scalar_one()
    )
    if matched > definition.export_row_cap:
        # BEFORE a single cell is formatted. There is no partial body to return
        # and nothing to accidentally fall through to.
        raise ReportExportTooLarge(definition.key, definition.export_row_cap, matched)

    result = await run_bounded(session, rows_stmt, report_key=definition.key)
    rows = [definition.row_model.model_validate(dict(mapping)) for mapping in result.mappings()]
    body = render_csv(definition, rows)

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="report.exported",
        target_type="report",
        target_id=definition.key,
        # THE VALIDATED PARAMS, THE COUNT, AND THE FORMAT -- NEVER THE ROWS.
        # `audit_events` is append-only, hash-chained and unprunable by
        # `app_user`, so a row of exported data written here cannot be taken
        # back. The test asserts these three keys as an EQUALITY.
        metadata={
            "params": params.model_dump(mode="json"),
            "row_count": len(rows),
            "format": "csv",
        },
    )
    return body
