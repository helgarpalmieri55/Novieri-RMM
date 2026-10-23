"""Inventory API routes (spec §8). All gated by devices.read."""

import csv
import io
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, Response
from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_permission
from app.core.audit import audit_service
from app.core.csv_export import escape_formula
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.models import (
    Agent,
    Device,
    HardwareComponent,
    InstalledSoftware,
    InventoryChange,
    InventorySnapshot,
)

router = APIRouter(tags=["inventory"])


class ComponentOut(BaseModel):
    type: str
    identifier: str
    attributes: dict[str, Any]


class SoftwareOut(BaseModel):
    name: str
    version: str
    publisher: str
    install_date: str | None


class DeviceInventoryOut(BaseModel):
    summary: dict[str, Any]
    hardware: list[ComponentOut]
    software: list[SoftwareOut]
    facts: dict[str, Any]
    snapshot_taken_at: datetime | None


class InventoryChangeOut(BaseModel):
    id: UUID
    ts: datetime
    change_type: str
    subject: str
    from_value: str | None
    to_value: str | None


class ChangesPage(BaseModel):
    items: list[InventoryChangeOut]
    total: int


class SoftwareAggregateOut(BaseModel):
    name: str
    version: str
    publisher: str
    device_count: int


class SoftwarePage(BaseModel):
    items: list[SoftwareAggregateOut]
    total: int


class SoftwareDeviceOut(BaseModel):
    device_id: UUID
    hostname: str
    version: str


@router.get("/devices/{device_id}/inventory", response_model=DeviceInventoryOut)
async def device_inventory(
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.read")),
) -> DeviceInventoryOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one_or_none()
        if device is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such device"))
        components = (
            (
                await session.execute(
                    select(HardwareComponent)
                    .where(HardwareComponent.device_id == device_id)
                    .order_by(HardwareComponent.type, HardwareComponent.identifier)
                )
            )
            .scalars()
            .all()
        )
        software = (
            (
                await session.execute(
                    select(InstalledSoftware)
                    .where(InstalledSoftware.device_id == device_id)
                    .order_by(InstalledSoftware.name)
                )
            )
            .scalars()
            .all()
        )
        snapshot = (
            await session.execute(
                select(InventorySnapshot)
                .where(InventorySnapshot.device_id == device_id)
                .order_by(InventorySnapshot.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        return DeviceInventoryOut(
            summary={
                "hostname": device.hostname,
                "fqdn": device.fqdn,
                "os_family": device.os_family,
                "os_version": device.os_version,
                "architecture": device.architecture,
                "serial": device.serial,
                "cpu": device.cpu,
                "ram_bytes": device.ram_bytes,
                "status": device.status,
            },
            hardware=[
                ComponentOut(type=c.type, identifier=c.identifier, attributes=c.attributes)
                for c in components
            ],
            software=[
                SoftwareOut(
                    name=s.name,
                    version=s.version,
                    publisher=s.publisher,
                    install_date=s.install_date.isoformat() if s.install_date else None,
                )
                for s in software
            ],
            facts=(snapshot.raw.get("facts", {}) if snapshot else {}),
            snapshot_taken_at=snapshot.taken_at if snapshot else None,
        )


@router.get("/devices/{device_id}/inventory/changes", response_model=ChangesPage)
async def device_changes(
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.read")),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ChangesPage:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        base = select(InventoryChange).where(InventoryChange.device_id == device_id)
        total = (
            await session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    base.order_by(InventoryChange.id.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return ChangesPage(
            items=[
                InventoryChangeOut(
                    id=c.id,
                    ts=c.ts,
                    change_type=c.change_type,
                    subject=c.subject,
                    from_value=c.from_value,
                    to_value=c.to_value,
                )
                for c in rows
            ],
            total=total,
        )


@router.get("/software", response_model=SoftwarePage)
async def software_search(
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.read")),
    name: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> SoftwarePage:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        base = select(
            InstalledSoftware.name,
            InstalledSoftware.version,
            InstalledSoftware.publisher,
            func.count(InstalledSoftware.device_id).label("device_count"),
        ).group_by(
            InstalledSoftware.name, InstalledSoftware.version, InstalledSoftware.publisher
        )
        if name:
            base = base.where(InstalledSoftware.name.ilike(f"%{name}%"))
        total = (
            await session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        rows = (
            await session.execute(
                base.order_by(InstalledSoftware.name).limit(limit).offset(offset)
            )
        ).all()
        return SoftwarePage(
            items=[
                SoftwareAggregateOut(
                    name=r.name, version=r.version, publisher=r.publisher,
                    device_count=r.device_count,
                )
                for r in rows
            ],
            total=total,
        )


@router.get("/software/{name}/devices", response_model=list[SoftwareDeviceOut])
async def software_devices(
    name: str,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.read")),
) -> list[SoftwareDeviceOut]:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        rows = (
            await session.execute(
                select(InstalledSoftware, Device.hostname)
                .join(Device, Device.id == InstalledSoftware.device_id)
                .where(InstalledSoftware.name == name)
                .order_by(Device.hostname)
            )
        ).all()
        return [
            SoftwareDeviceOut(device_id=s.device_id, hostname=hostname, version=s.version)
            for s, hostname in rows
        ]


# ---------------------------------------------------------------- exports
#
# TWO FORMATS OVER ONE ROW BUILDER, AND THE ROW BUILDER IS THE POINT. The
# operator asked for a real workbook rather than a CSV ("export inventory
# exports a .json instead of an .xlsx" -- the `.json` half was the console
# saving a 401 body, fixed in `frontend/src/lib/download.ts`). Adding a
# second renderer over a second query is how two exports of the same estate
# come to disagree the first time a column is added to one of them, so the
# query and the escaping decisions live here once and each renderer only
# decides how the cells are written down.
#
# THE CSV ROUTES STAY. They are shipped GET routes and this repository
# cannot enumerate what outside the console reads them -- a scheduled fetch,
# a CMDB import, a spreadsheet's own web query. Deleting a route to change a
# file format is a breaking change made on behalf of consumers nobody
# counted. Which format the console's button asks for is a console-side
# choice, and that is the half that moved.

# APPEND ONLY, AND THE ORDER OF THE FIRST SEVEN IS PART OF THE CONTRACT.
# `docs/modules/inventory/spec.md` §8 describes these as "Power-BI-friendly flat
# columns" and the routes are shipped GETs this repository cannot enumerate the
# readers of -- a scheduled fetch, a CMDB import, a spreadsheet's own web query.
# A consumer keyed on header NAME is unaffected by anything added; one keyed on
# INDEX is unaffected only if new columns go on the END. Inserting `provenance`
# next to `os_family`, where it reads best, would silently shift five columns
# under every positional reader. So: new columns land here, at the bottom, and
# nothing above them ever moves.
DEVICE_EXPORT_COLUMNS = [
    "hostname",
    "os_family",
    "os_version",
    "architecture",
    "serial",
    "agent_status",
    "last_seen_at",
    # HOW THE ROW CAME TO EXIST, AND WHO STOOD BEHIND IT. Added 2026-08-26, the
    # day a `net.sweep` began recording every host that answers it: in a swept
    # estate most rows are machine-recorded -- a printer, a phone, a colleague's
    # laptop on the wrong VLAN, the coffee machine -- and an export that renders
    # them identically to the laptop somebody typed in is an export nobody can
    # act on. `provenance` is the whole answer to "which of these did a person
    # vouch for?" (`0078_device_provenance.py`, CLAUDE.md invariant 2a(d)).
    #
    # BOTH, not just `provenance`: `ck_devices_source_provenance` makes them one
    # fact in two columns (`agent`/`agent`, `manual`/`operator`,
    # `discovery`/`promoted|sweep`), and carrying half of it invites the reader
    # to infer the other half -- which is exactly the mistake that made
    # `source` insufficient for this question in the first place.
    "source",
    "provenance",
]

SOFTWARE_EXPORT_COLUMNS = ["hostname", "name", "version", "publisher"]

# WHICH COLUMNS CARRY A FORMULA PAYLOAD, decided per column and not per row.
# Read `app.core.csv_export`'s module docstring before changing either set:
# escaping a column the server itself formatted corrupts it for a machine
# reader, and leaving an agent-reported free-text column unescaped is the
# defect.
#
# `os_family` is excluded even though it comes off the same row: ingestion
# pattern-validates it to exactly `windows|linux|macos`
# (`app.modules.inventory.schema`), so it can never begin with a dangerous
# character. `agent_status` is a server-set enum and `last_seen_at` a
# timestamp the server isoformatted -- neither is operator- or
# agent-supplied text.
#
# `source` and `provenance` are excluded for the `os_family` reason, and the
# constraint is in the DATABASE rather than in a validator: 0078's
# `ck_devices_source_provenance` admits exactly the pairs `agent/agent`,
# `manual/operator` and `discovery/promoted|sweep`. No value in either column
# can begin with `=`, `+`, `-`, `@`, a tab or a carriage return, and none is
# ever supplied by an operator or an agent -- the server stamps both.
DEVICE_FREE_TEXT = frozenset({"hostname", "os_version", "architecture", "serial"})
# All four are unconstrained agent-reported free text
# (`app.models.inventory.InstalledSoftware`: `name`/`version`/`publisher`
# carry no pattern or enum constraint, and `hostname` is the same free-text
# device field escaped above).
SOFTWARE_FREE_TEXT = frozenset(SOFTWARE_EXPORT_COLUMNS)

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def _device_export_rows(session: AsyncSession) -> list[list[str]]:
    rows = (
        await session.execute(
            select(Device, Agent.status).join(Agent, isouter=True).order_by(Device.hostname)
        )
    ).all()
    return [
        [
            device.hostname or "",
            device.os_family or "",
            device.os_version or "",
            device.architecture or "",
            device.serial or "",
            agent_status or "",
            device.last_seen_at.isoformat() if device.last_seen_at else "",
            device.source,
            device.provenance,
        ]
        for device, agent_status in rows
    ]


async def _software_export_rows(session: AsyncSession) -> list[list[str]]:
    rows = (
        await session.execute(
            select(InstalledSoftware, Device.hostname)
            .join(Device, Device.id == InstalledSoftware.device_id)
            .order_by(Device.hostname, InstalledSoftware.name)
        )
    ).all()
    return [
        [hostname or "", software.name or "", software.version or "", software.publisher or ""]
        for software, hostname in rows
    ]


async def _record_export(
    session: AsyncSession, ctx: AuthContext, *, dataset: str, fmt: str, row_count: int
) -> None:
    """INVARIANT #4, and the CSV routes shipped without it.

    A whole-tenant inventory export is a privileged read of the entire estate
    leaving the platform as a file -- every hostname, serial, OS version and
    installed package the fleet has reported. That is the same act as
    `report.exported`, `patch.report_exported` and `bcp.report_exported`,
    each of which writes an audit event and cites this invariant; these two
    routes wrote nothing at all, so the estate could be walked out with no
    record that it happened.

    THE COUNT, NEVER THE ROWS. `audit_events` is append-only, hash-chained
    and unprunable by `app_user`, so a row of exported estate written into
    the metadata here could not be taken back.

    Written INSIDE the same `org_session` block as the read, so the file
    cannot leave without the record of it landing too (the discipline
    `app.modules.reporting.export` states in full).
    """
    await audit_service.record(
        session,
        org_id=ctx.org_id,
        actor_type="user",
        actor_id=ctx.user_id,
        action="inventory.exported",
        target_type="organization",
        target_id=str(ctx.org_id),
        metadata={"dataset": dataset, "format": fmt, "row_count": row_count},
    )


def _render_csv(columns: list[str], free_text: frozenset[str], rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(
            [
                escape_formula(value) if name in free_text else value
                for name, value in zip(columns, row, strict=True)
            ]
        )
    return buffer.getvalue()


def _write_text_cell(sheet: Worksheet, row: int, column: int, value: str, *, guard: bool) -> None:
    """One cell, written so that it can never be a formula.

    THIS IS WHERE A WORKBOOK IS MORE DANGEROUS THAN A CSV, NOT LESS.
    `escape_formula` defends a CSV against a spreadsheet's IMPORT PARSER
    deciding that a cell beginning `=`/`+`/`-`/`@` is a formula. A workbook
    never goes through that parser -- and openpyxl reads the value it is
    handed and writes a string beginning with `=` as `t="f"`, a real formula
    in a real sheet, evaluated the instant the file opens with no import
    dialog in between. A device is free to report its own hostname, so
    `=HYPERLINK("http://evil","x")` as a hostname is the whole attack.

    TWO THINGS, AND THE FIRST IS UNCONDITIONAL. Every cell is forced to the
    string type, for every column, whether or not the caller listed it as
    free text -- so a column added later by somebody who did not read this
    comment is still inert. Openpyxl's inference is the hazard; taking it out
    of the loop entirely is the defence, and `test_no_cell_in_either_workbook
    _is_a_formula` asserts it over every cell of both sheets.

    Second, on exactly the columns the CSV route escapes, `quotePrefix` is
    set. That is Excel's own "this cell is text, not a formula" marker --
    precisely what a leading `'` MEANS to a CSV import, expressed natively
    rather than by editing the value. So this keeps `csv_export`'s promise
    that the reader sees the exact original text, which prefixing the string
    would break here: a workbook stores what it stores, so a `'` added to the
    value would be displayed rather than consumed, and the operator would
    read a hostname that is not the hostname.
    """
    cell = sheet.cell(row=row, column=column, value=value)
    # AFTER the assignment: the value setter is what infers `f`, so the
    # override has to follow it.
    cell.data_type = "s"
    if guard and escape_formula(value) != value:
        cell.quotePrefix = True


def _render_xlsx(
    title: str, columns: list[str], free_text: frozenset[str], rows: list[list[str]]
) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = title
    for index, name in enumerate(columns, start=1):
        _write_text_cell(sheet, 1, index, name, guard=False)
    for row_index, row in enumerate(rows, start=2):
        for index, (name, value) in enumerate(zip(columns, row, strict=True), start=1):
            _write_text_cell(sheet, row_index, index, value, guard=name in free_text)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _xlsx_response(body: bytes, filename: str) -> Response:
    return Response(
        content=body,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/devices.csv", response_class=PlainTextResponse)
async def export_devices_csv(
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.read")),
) -> PlainTextResponse:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        rows = await _device_export_rows(session)
        await _record_export(session, ctx, dataset="devices", fmt="csv", row_count=len(rows))
    body = _render_csv(DEVICE_EXPORT_COLUMNS, DEVICE_FREE_TEXT, rows)
    return PlainTextResponse(body, media_type="text/csv")


@router.get("/export/devices.xlsx", response_class=Response)
async def export_devices_xlsx(
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.read")),
) -> Response:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        rows = await _device_export_rows(session)
        await _record_export(session, ctx, dataset="devices", fmt="xlsx", row_count=len(rows))
    body = _render_xlsx("Devices", DEVICE_EXPORT_COLUMNS, DEVICE_FREE_TEXT, rows)
    return _xlsx_response(body, "devices.xlsx")


@router.get("/export/software.csv", response_class=PlainTextResponse)
async def export_software_csv(
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.read")),
) -> PlainTextResponse:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        rows = await _software_export_rows(session)
        await _record_export(session, ctx, dataset="software", fmt="csv", row_count=len(rows))
    body = _render_csv(SOFTWARE_EXPORT_COLUMNS, SOFTWARE_FREE_TEXT, rows)
    return PlainTextResponse(body, media_type="text/csv")


@router.get("/export/software.xlsx", response_class=Response)
async def export_software_xlsx(
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.read")),
) -> Response:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        rows = await _software_export_rows(session)
        await _record_export(session, ctx, dataset="software", fmt="xlsx", row_count=len(rows))
    body = _render_xlsx("Software", SOFTWARE_EXPORT_COLUMNS, SOFTWARE_FREE_TEXT, rows)
    return _xlsx_response(body, "software.xlsx")
