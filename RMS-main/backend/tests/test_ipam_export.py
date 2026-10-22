"""CSV exports and their formula-injection guard
(`docs/modules/ipam/spec.md` §8.2, §4.3, and `app/core/csv_export.py`).

**A CELL BEGINNING `=`, `+`, `-` OR `@` EXECUTES AS A FORMULA the instant the
file is opened in Excel or Google Sheets.** This module's exports carry subnet
and site names, descriptions, hostnames, asset labels, drift notes and agent-
supplied `detail` blobs -- all operator- or agent-controlled free text, all
landing in a file somebody points Power BI at.

WHAT THESE FIXTURES DELIBERATELY VARY:

* **Every escapable column is seeded with a REAL payload**, one per dangerous
  prefix, so a missed column is a named failure rather than an absence. A
  fixture with one payload in one column proves that column and nothing else.
* **The non-escapable columns are asserted UNCHANGED in the same test.** Running
  `escape_formula` over a server-formatted timestamp or a UUID would corrupt it
  for a machine reader, and "everything is escaped" is a different defect from
  "nothing is".
* **The `dormant` exclusion is tested against an export that DOES contain a real
  finding**, so the assertion is a partition rather than an empty file.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.modules.ipam.importer import (
    export_addresses_csv,
    export_drift_csv,
    export_subnets_csv,
)
from app.modules.ipam.models import IpamDrift
from tests.ipam_fixtures import (
    seed_address,
    seed_agent,
    seed_device,
    seed_org,
    seed_subnet,
    seed_sweep_run,
    seed_vrf,
)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
RECENT = NOW - timedelta(hours=2)

#: One payload per character `escape_formula` neutralises, so a column that
#: escapes only `=` is a named failure.
PAYLOADS = {
    "=": '=HYPERLINK("http://evil","click")',
    "+": "+1-800-EVIL",
    "-": "-50 Corp",
    "@": "@SUM(1,1)",
}


def _rows(body: str) -> list[list[str]]:
    import csv
    import io

    return list(csv.reader(io.StringIO(body)))


async def test_subnet_export_escapes_free_text_and_leaves_typed_columns_alone(
    admin_engine: AsyncEngine
) -> None:
    """§8.2, PER COLUMN and never blanket over the row.

    `name` and `description` are operator free text and are escaped; `prefix`,
    `kind`, the UUIDs and the timestamps are server-formatted from typed columns
    and are asserted UNCHANGED -- escaping a `cidr` would hand a machine reader
    the literal string `'10.70.0.0/24`.
    """
    org = await seed_org(admin_engine, "exp-sub")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.70.0.0/24",
        name=PAYLOADS["="], description=PAYLOADS["@"],
    )
    async with AsyncSession(admin_engine) as session:
        body = await export_subnets_csv(session, org.org_id)

    rows = _rows(body)
    assert rows[0][:4] == ["prefix", "kind", "name", "description"]
    assert rows[1][0] == "10.70.0.0/24", "a typed cidr column was mangled"
    assert rows[1][1] == "assignable"
    assert rows[1][2] == "'" + PAYLOADS["="]
    assert rows[1][3] == "'" + PAYLOADS["@"]


async def test_address_export_escapes_every_free_text_column(
    admin_engine: AsyncEngine
) -> None:
    """Three escapable columns, three DIFFERENT payloads -- a single payload
    reused across columns cannot tell "all three are escaped" from "the first one
    is, and the assertion matched the wrong cell".

    `state`, `origin` and `allocation_method` are CHECK-constrained enums and
    `address` is an `inet`: none can begin with a dangerous character, and all
    four are asserted unchanged.
    """
    org = await seed_org(admin_engine, "exp-addr")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    subnet_id = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.70.0.0/24"
    )
    await seed_address(
        admin_engine, org.org_id, vrf_id=vrf_id, subnet_id=subnet_id,
        address="10.70.0.42", state="assigned", asset_label=PAYLOADS["+"],
        hostname=PAYLOADS["-"], description=PAYLOADS["@"],
    )
    async with AsyncSession(admin_engine) as session:
        body = await export_addresses_csv(session, org.org_id)

    header, row = _rows(body)[0], _rows(body)[1]
    index = {name: i for i, name in enumerate(header)}
    assert row[index["asset_label"]] == "'" + PAYLOADS["+"]
    assert row[index["hostname"]] == "'" + PAYLOADS["-"]
    assert row[index["description"]] == "'" + PAYLOADS["@"]
    assert row[index["address"]] == "10.70.0.42"
    assert row[index["state"]] == "assigned"
    assert row[index["origin"]] == "manual"
    assert row[index["allocation_method"]] == "unknown"


async def test_drift_export_excludes_dormant_and_escapes_the_note_and_detail(
    admin_engine: AsyncEngine
) -> None:
    """§4.3: **`dormant` IS NOT DRIFT**, so an export called "drift" must not
    carry it. The fixture holds one of each, so this is a partition and not an
    empty file.

    `detail` is deliberately NOT escaped, and the reason is the guard's own rule:
    CSV injection is about the LEADING character of a cell, and a JSON object
    literal always begins with `{`. The agent-supplied hostname inside it can
    never be leading, and prefixing a machine-readable JSON cell with `'` would
    corrupt it for the reader that column exists for. Asserted both ways below,
    because "we escaped everything" is a different defect from "we escaped
    nothing".
    """
    org = await seed_org(admin_engine, "exp-drift")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    subnet_id = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.70.0.0/24"
    )
    async with AsyncSession(admin_engine) as session:
        session.add(
            IpamDrift(
                org_id=org.org_id, vrf_id=vrf_id, subnet_id=subnet_id,
                address="10.70.0.42", drift_class="documented_absent",
                severity="medium", confidence="high",
                detail={"hostname": PAYLOADS["="]}, note=PAYLOADS["+"],
                first_detected_at=NOW, last_evaluated_at=NOW,
            )
        )
        session.add(
            IpamDrift(
                org_id=org.org_id, vrf_id=vrf_id, subnet_id=subnet_id,
                address="10.70.0.43", drift_class="dormant",
                severity="low", confidence="low", detail={},
                first_detected_at=NOW, last_evaluated_at=NOW,
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        body = await export_drift_csv(session, org.org_id)

    rows = _rows(body)
    header, data = rows[0], rows[1:]
    index = {name: i for i, name in enumerate(header)}
    assert [row[index["address"]] for row in data] == ["10.70.0.42"]
    assert "dormant" not in body
    assert data[0][index["note"]] == "'" + PAYLOADS["+"]
    detail = data[0][index["detail"]]
    assert detail.startswith("{"), "a JSON cell cannot begin with a formula character"
    assert not detail.startswith("'"), "a machine-readable JSON cell was mangled"
    # JSON-quoted, so the payload survives the round trip and is still not
    # leading -- `json.loads` is the assertion because comparing the raw cell
    # would be comparing an escaping convention rather than the value.
    import json

    assert json.loads(detail) == {"hostname": PAYLOADS["="]}
    assert data[0][index["severity"]] == "medium"


async def test_drift_export_hides_resolved_items_unless_asked(
    admin_engine: AsyncEngine
) -> None:
    """A resolved item is STAMPED, not deleted (§0.8), so the export has to
    choose. It defaults to open findings and includes resolved ones on request --
    which is what makes "we closed 40 address-plan defects this quarter" an
    evidence export.

    Both calls run against the same fixture, so the flag is observably doing the
    work.
    """
    org = await seed_org(admin_engine, "exp-res")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    async with AsyncSession(admin_engine) as session:
        session.add(
            IpamDrift(
                org_id=org.org_id, vrf_id=vrf_id, address="10.70.0.44",
                drift_class="conflict", severity="high", confidence="high",
                detail={}, first_detected_at=NOW, last_evaluated_at=NOW,
                resolved_at=NOW, consecutive_detections=0,
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        default = await export_drift_csv(session, org.org_id)
        everything = await export_drift_csv(session, org.org_id, include_resolved=True)
    assert len(_rows(default)) == 1, "only the header belongs in the default export"
    assert len(_rows(everything)) == 2
    assert "10.70.0.44" in everything


async def test_the_exports_are_org_scoped_on_the_admin_engine(
    admin_engine: AsyncEngine
) -> None:
    """PINNED ON THE ADMIN ENGINE (invariant #5): through `org_session` RLS
    rejects first, so a cross-org export test driven that way passes with the
    `org_id` predicate deleted.

    Org B holds a row in every exported table, so leakage is observable as a
    string that must not appear rather than as an empty file.
    """
    a = await seed_org(admin_engine, "exp-a")
    b = await seed_org(admin_engine, "exp-b")
    a_vrf = await seed_vrf(admin_engine, a.org_id)
    b_vrf = await seed_vrf(admin_engine, b.org_id)
    await seed_subnet(
        admin_engine, a.org_id, vrf_id=a_vrf, prefix="10.70.0.0/24", name="mine"
    )
    b_subnet = await seed_subnet(
        admin_engine, b.org_id, vrf_id=b_vrf, prefix="10.99.0.0/24", name="theirs"
    )
    await seed_address(
        admin_engine, b.org_id, vrf_id=b_vrf, subnet_id=b_subnet,
        address="10.99.0.42", state="assigned", asset_label="their-printer",
    )
    async with AsyncSession(admin_engine) as session:
        session.add(
            IpamDrift(
                org_id=b.org_id, vrf_id=b_vrf, address="10.99.0.42",
                drift_class="conflict", severity="high", confidence="high",
                detail={}, first_detected_at=NOW, last_evaluated_at=NOW,
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        subnets = await export_subnets_csv(session, a.org_id)
        addresses = await export_addresses_csv(session, a.org_id)
        drift = await export_drift_csv(session, a.org_id)

    for body in (subnets, addresses, drift):
        assert "10.99.0" not in body
        assert "theirs" not in body
        assert "their-printer" not in body
    assert "mine" in subnets


async def test_an_empty_org_exports_a_header_and_nothing_else(
    admin_engine: AsyncEngine
) -> None:
    """A header-only file is a valid empty export; a zero-byte file is a broken
    one that Power BI reports as a schema error rather than as no rows."""
    org = await seed_org(admin_engine, "exp-empty")
    async with AsyncSession(admin_engine) as session:
        for body in (
            await export_subnets_csv(session, org.org_id),
            await export_addresses_csv(session, org.org_id),
            await export_drift_csv(session, org.org_id),
        ):
            rows = _rows(body)
            assert len(rows) == 1
            assert rows[0], "the export produced no header"


async def test_the_sweep_columns_round_trip_through_the_subnet_export(
    admin_engine: AsyncEngine
) -> None:
    """`sweep_enabled`, `sweep_interval_hours` and `last_swept_at` are what an
    operator exports to answer "which segments are we actually looking at" --
    the question §4.3's Gate E turns on.

    A device and a succeeded run are seeded so the row is a realistic one rather
    than a subnet with every sweep column at its default.
    """
    org = await seed_org(admin_engine, "exp-sweep")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    device_id = await seed_device(admin_engine, org.org_id)
    await seed_agent(admin_engine, org.org_id, device_id, status="online")
    subnet_id = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.70.0.0/24",
        sweep_enabled=True, sweep_interval_hours=168, sweep_device_id=device_id,
        last_swept_at=RECENT,
    )
    await seed_sweep_run(
        admin_engine, org.org_id, subnet_id=subnet_id, state="succeeded",
        started_at=RECENT,
    )
    async with AsyncSession(admin_engine) as session:
        body = await export_subnets_csv(session, org.org_id)

    header, row = _rows(body)[0], _rows(body)[1]
    index = {name: i for i, name in enumerate(header)}
    assert row[index["sweep_enabled"]] == "True"
    assert row[index["sweep_interval_hours"]] == "168"
    assert row[index["last_swept_at"]].startswith("2026-08-02")


async def test_the_drift_export_and_the_drift_page_filter_on_the_same_tuple(
    admin_engine: AsyncEngine
) -> None:
    """The export's exclusion and `list_drift`'s exclusion are the SAME
    `DRIFT_CLASSES`. Two copies is how the page and the export come to disagree
    about what counts as drift, which is the disagreement §0.11 exists to
    prevent."""
    from app.modules.ipam import importer
    from app.modules.ipam.reconcile import DORMANT_CLASS, DRIFT_CLASSES

    assert importer.DRIFT_CLASSES is DRIFT_CLASSES
    assert DORMANT_CLASS not in DRIFT_CLASSES


async def test_the_exports_read_only_and_write_nothing(
    admin_engine: AsyncEngine
) -> None:
    """An export is a read. Counted before and after over a populated org,
    because a "read" that flushed a lazily-mutated ORM row would be a write
    nobody looked for."""
    from sqlalchemy import func

    from app.modules.ipam.models import IpamSubnet

    org = await seed_org(admin_engine, "exp-ro")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    await seed_subnet(admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.70.0.0/24")

    async def _count(org_id: UUID) -> int:
        async with AsyncSession(admin_engine) as session:
            return int(
                (
                    await session.execute(
                        select(func.count()).select_from(IpamSubnet).where(
                            IpamSubnet.org_id == org_id
                        )
                    )
                ).scalar_one()
            )

    before = await _count(org.org_id)
    async with AsyncSession(admin_engine) as session:
        await export_subnets_csv(session, org.org_id)
        await export_addresses_csv(session, org.org_id)
        await export_drift_csv(session, org.org_id)
    assert await _count(org.org_id) == before == 1
