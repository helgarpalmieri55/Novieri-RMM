import csv
import io
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Device
from app.modules.inventory.ingest import ingest_inventory
from app.modules.inventory.schema import InventoryDocument
from tests.test_inventory_schema import FULL_DOC


async def _seed_inventory(api_env, org_id: UUID, hostname: str = "wks-042") -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, hostname=hostname, os_family="windows")
        session.add(device)
        await session.commit()
        device_id = device.id
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        loaded = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one()
        await ingest_inventory(
            session, loaded, InventoryDocument(**{**FULL_DOC, "hostname": hostname})
        )
    return device_id


async def test_device_inventory_detail(api_env) -> None:
    org_id = await api_env.create_org("iapi")
    device_id = await _seed_inventory(api_env, org_id)
    reader = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, reader)

    response = await api_env.client.get(f"/api/v1/devices/{device_id}/inventory", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["os_version"] == "Windows 11 Pro 23H2"
    assert {c["type"] for c in body["hardware"]} == {"disk", "nic"}
    assert {s["name"] for s in body["software"]} == {"7-Zip", "Google Chrome"}
    assert body["facts"]["disk_encryption"] is True
    assert body["snapshot_taken_at"] is not None


async def test_inventory_detail_404_for_foreign_org(api_env) -> None:
    org_a = await api_env.create_org("iapi")
    org_b = await api_env.create_org("iapi-b")
    foreign_device = await _seed_inventory(api_env, org_b)
    reader = await api_env.create_user(org_a, "read-only")
    headers = await api_env.headers(org_a, reader)
    response = await api_env.client.get(
        f"/api/v1/devices/{foreign_device}/inventory", headers=headers
    )
    assert response.status_code == 404


async def test_changes_endpoint_newest_first(api_env) -> None:
    org_id = await api_env.create_org("iapi")
    device_id = await _seed_inventory(api_env, org_id)
    # trigger changes with a second sweep
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one()
        await ingest_inventory(
            session,
            device,
            InventoryDocument(
                **{
                    **FULL_DOC,
                    "software": [{"name": "Google Chrome", "version": "999.0"}],
                }
            ),
        )
    reader = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, reader)
    response = await api_env.client.get(
        f"/api/v1/devices/{device_id}/inventory/changes", headers=headers
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2  # 7-Zip removed + Chrome updated
    types = {i["change_type"] for i in items}
    assert types == {"software_removed", "software_updated"}


async def test_software_search_aggregates(api_env) -> None:
    org_id = await api_env.create_org("iapi")
    await _seed_inventory(api_env, org_id, "host-a")
    await _seed_inventory(api_env, org_id, "host-b")
    reader = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, reader)

    response = await api_env.client.get("/api/v1/software?name=chrome", headers=headers)
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "Google Chrome"
    assert items[0]["device_count"] == 2

    devices = await api_env.client.get("/api/v1/software/Google Chrome/devices", headers=headers)
    assert devices.status_code == 200
    assert {d["hostname"] for d in devices.json()} == {"host-a", "host-b"}


async def test_rbac_and_csv_exports(api_env) -> None:
    org_id = await api_env.create_org("iapi")
    await _seed_inventory(api_env, org_id, "csv-host")
    nobody = await api_env.create_user(org_id)
    assert (
        await api_env.client.get(
            "/api/v1/export/devices.csv", headers=await api_env.headers(org_id, nobody)
        )
    ).status_code == 403

    reader = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, reader)

    devices_csv = await api_env.client.get("/api/v1/export/devices.csv", headers=headers)
    assert devices_csv.status_code == 200
    assert devices_csv.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(devices_csv.text)))
    # `source` AND `provenance` ARE APPENDED, NEVER INSERTED. The seven columns
    # before them keep their positions and their order, so a consumer reading by
    # index still reads what it read yesterday and one reading by header name
    # is unaffected either way (`docs/modules/inventory/spec.md` §8).
    assert rows[0] == [
        "hostname",
        "os_family",
        "os_version",
        "architecture",
        "serial",
        "agent_status",
        "last_seen_at",
        "source",
        "provenance",
    ]
    assert any(r[0] == "csv-host" for r in rows[1:])

    software_csv = await api_env.client.get("/api/v1/export/software.csv", headers=headers)
    rows = list(csv.reader(io.StringIO(software_csv.text)))
    assert rows[0] == ["hostname", "name", "version", "publisher"]
    assert any(r[:2] == ["csv-host", "7-Zip"] for r in rows[1:])


async def test_the_device_export_says_who_stood_behind_each_row(api_env) -> None:
    """**AN EXPORT THAT CANNOT SAY WHICH ROWS A PERSON VOUCHED FOR IS AN EXPORT
    NOBODY CAN ACT ON.**

    Since the operator's ruling of 2026-08-26 a `net.sweep` records every host
    that answers it, so in a swept estate MOST `devices` rows are machine-
    recorded -- a printer, a phone, a colleague's laptop on the wrong VLAN, the
    coffee machine. `DEVICE_EXPORT_COLUMNS` predates both `source` and
    `provenance`, so the workbook rendered all of them identically to the
    warehouse laptop somebody typed in. `devices.provenance` is the whole
    answer to "which of these did a person vouch for?" and it was the one
    column the export could not carry.

    THREE ROWS THAT DIFFER ONLY IN PROVENANCE, and the fixture is the point: a
    test with one row passes against an implementation that hardcodes `agent`.
    The pairing constraint `ck_devices_source_provenance` is why `source` rides
    along -- the two are one fact in two columns and an export carrying half of
    it would invite the reader to infer the other half wrongly.
    """
    org_id = await api_env.create_org("iapi")
    await _seed_inventory(api_env, org_id, "enrolled-host")
    async with AsyncSession(api_env.admin_engine) as session:
        session.add(
            Device(
                org_id=org_id, hostname="typed-in-host",
                source="manual", provenance="operator",
            )
        )
        session.add(
            Device(
                org_id=org_id, hostname="swept-host",
                source="discovery", provenance="sweep",
            )
        )
        await session.commit()

    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "read-only"))
    devices_csv = await api_env.client.get("/api/v1/export/devices.csv", headers=headers)
    assert devices_csv.status_code == 200
    rows = list(csv.reader(io.StringIO(devices_csv.text)))
    header = rows[0]
    by_host = {r[header.index("hostname")]: r for r in rows[1:]}
    assert (
        by_host["enrolled-host"][header.index("source")],
        by_host["enrolled-host"][header.index("provenance")],
    ) == ("agent", "agent")
    assert (
        by_host["typed-in-host"][header.index("source")],
        by_host["typed-in-host"][header.index("provenance")],
    ) == ("manual", "operator")
    assert (
        by_host["swept-host"][header.index("source")],
        by_host["swept-host"][header.index("provenance")],
    ) == ("discovery", "sweep")


async def test_csv_exports_escape_formula_injection_in_free_text_columns(api_env) -> None:
    """A device's self-reported `hostname`/`serial` and a software item's
    `name`/`version`/`publisher` are all agent-supplied free text with no
    pattern constraint -- each is a candidate for CSV formula injection
    (a device or a piece of installed software genuinely named
    `=HYPERLINK(...)` is executed as a live formula the moment this export
    is opened in Excel). `os_family` is pattern-validated to exactly
    `windows|linux|macos` (`app.modules.inventory.schema`) and `agent_status`/
    `last_seen_at` are server-set/-formatted, so none of those three can
    carry a payload and none should be escaped."""
    org_id = await api_env.create_org("iapi")
    device_id = await _seed_inventory(
        api_env,
        org_id,
        # `_seed_inventory` doesn't expose overriding serial/software, so
        # seed directly with a doctored document.
    )
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one()
        await ingest_inventory(
            session,
            device,
            InventoryDocument(
                **{
                    **FULL_DOC,
                    "hostname": "=cmd|' /C calc'!A1",
                    "serial": "-2+3+cmd|/c calc",
                    "software": [
                        {"name": "@SUM(1,1)", "version": "+1", "publisher": "-Acme"},
                    ],
                }
            ),
        )
        await session.commit()

    reader = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, reader)

    devices_csv = await api_env.client.get("/api/v1/export/devices.csv", headers=headers)
    assert devices_csv.status_code == 200, devices_csv.text
    rows = list(csv.reader(io.StringIO(devices_csv.text)))
    header = rows[0]
    device_row = next(r for r in rows[1:] if r[header.index("serial")].endswith("cmd|/c calc"))
    assert device_row[header.index("hostname")] == "'=cmd|' /C calc'!A1"
    assert device_row[header.index("serial")] == "'-2+3+cmd|/c calc"
    # `os_family` is a validated enum -- never escaped even though it sits
    # right next to two escaped columns in the same row.
    assert device_row[header.index("os_family")] == "windows"

    software_csv = await api_env.client.get("/api/v1/export/software.csv", headers=headers)
    assert software_csv.status_code == 200, software_csv.text
    srows = list(csv.reader(io.StringIO(software_csv.text)))
    sheader = srows[0]
    software_row = next(r for r in srows[1:] if r[sheader.index("name")] == "'@SUM(1,1)")
    assert software_row[sheader.index("hostname")] == "'=cmd|' /C calc'!A1"
    assert software_row[sheader.index("version")] == "'+1"
    assert software_row[sheader.index("publisher")] == "'-Acme"
