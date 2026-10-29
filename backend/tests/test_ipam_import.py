"""CSV import and the documentation `network`-record converter
(`docs/modules/ipam/spec.md` §8.1, §8.3, §0.14, §0.18, §11.3).

WHAT THESE FIXTURES DELIBERATELY VARY:

* **"Preview writes nothing" is asserted as ROW COUNTS BEFORE AND AFTER**, over a
  file that WOULD write three rows if applied. A preview of an empty file, or a
  preview whose file is all errors, proves nothing about the write path.
* **The unparseable-row test counts the response against the FILE**, not against
  a constant: `len(rows) == len(file rows)` is the difference between "reported"
  and "silently skipped", and a fixture with only bad rows could not tell a
  reporting importer from one that dropped the good ones too.
* **The transactionality test fails the LAST row**, so a non-transactional
  implementation observes the earlier rows committed. Failing the first row
  would pass against an implementation with no transaction at all.
* **The "never authorizes, never enables sweeps" test uses a file whose rows
  fall inside a real registered subnet**, so the import genuinely succeeds --
  the negatives are about what an ACTUALLY APPLIED import did not do.
* **The converter's skip cases are each produced by a real record**, not by a
  hand-built list: an unparseable CIDR, a prefix already registered, and a
  gateway outside its own prefix.
"""

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import AuditEvent, DocRecord, IpamAddress, IpamScanAuthorization
from app.modules.ipam.errors import IpamValidationError
from app.modules.ipam.importer import (
    IMPORT_FIELDS,
    apply_import,
    convert_doc_records,
    file_digest,
    preview_digest,
    preview_import,
)
from app.modules.ipam.models import IpamSubnet
from tests.ipam_fixtures import seed_org, seed_subnet, seed_vlan, seed_vrf

MAPPING = {
    "IP Address": "address",
    "Host": "hostname",
    "Owner": "asset_label",
    "Notes": "description",
}

#: A VRF for the digest's pure-function tests: the digest names its target.
VRF = UUID(int=1)

GOOD_CSV = (
    b"IP Address,Host,Owner,Notes\n"
    b"10.70.0.10,printer-1,Printer A,ground floor\n"
    b"10.70.0.11,switch-1,Core switch,comms room\n"
    b"10.70.0.12,,,unclaimed but documented\n"
)


async def _fx(engine: AsyncEngine, tag: str):
    org = await seed_org(engine, tag)
    vrf_id = await seed_vrf(engine, org.org_id, name="global")
    subnet_id = await seed_subnet(
        engine, org.org_id, vrf_id=vrf_id, prefix="10.70.0.0/24"
    )
    return org, vrf_id, subnet_id


async def _address_count(engine: AsyncEngine, org_id: UUID) -> int:
    async with AsyncSession(engine) as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(IpamAddress).where(
                        IpamAddress.org_id == org_id
                    )
                )
            ).scalar_one()
        )


async def _addresses(engine: AsyncEngine, org_id: UUID) -> list[IpamAddress]:
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    select(IpamAddress)
                    .where(IpamAddress.org_id == org_id)
                    .order_by(IpamAddress.address)
                )
            )
            .scalars()
            .all()
        )


# --------------------------------------------------------------------------- #
# Preview writes nothing (§8.1)
# --------------------------------------------------------------------------- #


async def test_preview_writes_nothing_for_a_file_that_would_write_three_rows(
    admin_engine: AsyncEngine
) -> None:
    """§8.1 step one. Counted BEFORE AND AFTER, over a file that really would
    write: a preview of an empty file writes nothing against any implementation,
    including one with no preview step at all."""
    org, vrf_id, _ = await _fx(admin_engine, "prev")
    before = await _address_count(admin_engine, org.org_id)

    async with AsyncSession(admin_engine) as session:
        preview = await preview_import(
            session, org.org_id, client_id=org.org_id, content=GOOD_CSV, mapping=MAPPING
        )
        await session.commit()

    assert (preview.total_rows, preview.creates, preview.errors) == (3, 3, 0)
    # The digest a preview hands back covers the file AND the mapping, so it is
    # NOT the file's sha256 -- which is what the audit row keeps separately.
    assert preview.digest == preview_digest(GOOD_CSV, MAPPING, vrf_id)
    assert preview.digest != file_digest(GOOD_CSV)
    assert await _address_count(admin_engine, org.org_id) == before == 0


async def test_apply_writes_exactly_what_preview_predicted(
    admin_engine: AsyncEngine
) -> None:
    """The pair to the test above. `origin='imported'` (§8.1); a row with a
    claimant lands `assigned` and one without lands `reserved` -- "imported and
    assigned to nothing" is precisely what §2.6's exactly-one-claimant CHECK
    exists to keep out of a plan of record."""
    org, vrf_id, _ = await _fx(admin_engine, "apply")
    async with AsyncSession(admin_engine) as session:
        result = await apply_import(
            session, org.org_id, client_id=org.org_id, actor_id=org.user_id, content=GOOD_CSV,
            mapping=MAPPING, digest=preview_digest(GOOD_CSV, MAPPING, vrf_id),
        )
        await session.commit()

    assert (result.total_rows, result.applied, result.errors) == (3, 3, 0)
    rows = await _addresses(admin_engine, org.org_id)
    assert [str(r.address) for r in rows] == ["10.70.0.10", "10.70.0.11", "10.70.0.12"]
    assert [r.state for r in rows] == ["assigned", "assigned", "reserved"]
    assert [r.origin for r in rows] == ["imported"] * 3
    assert [r.asset_label for r in rows] == ["Printer A", "Core switch", ""]
    assert [r.hostname for r in rows] == ["printer-1", "switch-1", ""]


async def test_apply_refuses_a_file_that_is_not_the_one_previewed(
    admin_engine: AsyncEngine
) -> None:
    """The digest is what makes the two steps be about the SAME file. Without
    it, "preview this, apply that" is one paste away -- and the diff the operator
    approved described a different sheet.

    The stored rows are asserted first: a mutation test that only observed
    `DID NOT RAISE` could not tell a refused apply from an accepted one.
    """
    org, vrf_id, _ = await _fx(admin_engine, "digest")
    other = GOOD_CSV.replace(b"10.70.0.10", b"10.70.0.99")
    # THE STORED ROWS ARE THE OBSERVATION, NOT THE EXCEPTION. On the accepting
    # path this COMMITS, so a mutation that drops the digest check writes three
    # addresses and is caught by the count -- rather than by a bare
    # `DID NOT RAISE`, which says nothing about what the wrong file did.
    raised = False
    async with AsyncSession(admin_engine) as session:
        try:
            await apply_import(
                session, org.org_id, client_id=org.org_id, actor_id=org.user_id, content=other,
                mapping=MAPPING, digest=preview_digest(GOOD_CSV, MAPPING, vrf_id),
            )
        except IpamValidationError:
            raised = True
            await session.rollback()
        else:
            await session.commit()
    assert await _address_count(admin_engine, org.org_id) == 0, (
        "a file that was never previewed was applied"
    )
    assert raised


async def test_apply_refuses_the_previewed_file_under_a_different_mapping(
    admin_engine: AsyncEngine
) -> None:
    """**THE SAME BYTES UNDER TWO MAPPINGS ARE TWO DIFFERENT IMPORTS**, and a
    digest over the bytes alone accepted the second one carrying the first one's
    digest.

    Same file, previewed with `Owner -> asset_label`, applied with
    `Owner -> hostname`. The two produce visibly different rows: the previewed
    mapping ASSIGNS `10.70.0.10` to "Printer A", the applied one RESERVES it
    with the hostname "Printer A" and no claimant at all -- a different state,
    a different §2.6 claimant, a different audit trail.

    *If the digest did not cover the mapping* -- the shipped behaviour until
    now -- this observes three `reserved` rows carrying owner names in the
    hostname column, applied under a digest whose preview described three
    `assigned` ones.
    """
    org, _, _ = await _fx(admin_engine, "remap")
    previewed = MAPPING
    remapped = {"IP Address": "address", "Owner": "hostname", "Notes": "description"}

    async with AsyncSession(admin_engine) as session:
        preview = await preview_import(
            session, org.org_id, client_id=org.org_id, content=GOOD_CSV, mapping=previewed
        )
        await session.commit()
    assert [row.action for row in preview.rows] == ["create"] * 3

    raised = False
    async with AsyncSession(admin_engine) as session:
        try:
            await apply_import(
                session, org.org_id, client_id=org.org_id, actor_id=org.user_id, content=GOOD_CSV,
                mapping=remapped, digest=preview.digest,
            )
        except IpamValidationError as exc:
            raised = True
            assert "mapping" in str(exc)
            await session.rollback()
        else:
            await session.commit()

    assert await _address_count(admin_engine, org.org_id) == 0, (
        "a mapping that was never previewed was applied"
    )
    assert raised


def test_the_digest_does_not_depend_on_the_order_a_mapping_arrives_in() -> None:
    """The mapping reaches the server as JSON in a query parameter, so key order
    is whatever the client and the parser happened to produce. Two spellings of
    the SAME mapping must not be two different imports.

    *If the mapping were hashed unsorted* an operator who re-picked the same
    columns in a different order would be told their file did not match itself.
    """
    forwards = {"IP Address": "address", "Host": "hostname", "Owner": "asset_label"}
    backwards = {"Owner": "asset_label", "Host": "hostname", "IP Address": "address"}
    assert preview_digest(GOOD_CSV, forwards, VRF) == preview_digest(GOOD_CSV, backwards, VRF)
    assert preview_digest(GOOD_CSV, forwards, VRF) != preview_digest(
        GOOD_CSV, {"IP Address": "address", "Host": "asset_label", "Owner": "hostname"}, VRF
    )


def test_the_digest_moves_when_any_of_its_three_inputs_moves() -> None:
    """Every input is load-bearing, and the check is a check in every
    direction: a digest that only tracked the file would accept a re-mapped
    apply (the test above), one that only tracked the mapping would accept a
    swapped file, and one that did not track the VRF would accept a sheet
    previewed into one client's network and applied into another's (MSP
    clients plan 2026-10-26 Task 11 re-review, I-R1)."""
    mapping = {"A": "address"}
    assert preview_digest(b"ab", mapping, VRF) != preview_digest(b"b", mapping, VRF)
    assert preview_digest(b"", mapping, VRF) != preview_digest(b"", {"A": "hostname"}, VRF)
    assert preview_digest(b"ab", mapping, VRF) == preview_digest(b"ab", dict(mapping), VRF)
    assert preview_digest(b"ab", mapping, VRF) != preview_digest(b"ab", mapping, UUID(int=2))


async def test_an_apply_that_never_previewed_is_accepted_and_that_is_documented(
    admin_engine: AsyncEngine
) -> None:
    """**THE LIMIT OF THE DIGEST, PINNED AS A TEST RATHER THAN LEFT AS A
    COMMENT.**

    No preview is persisted, so a caller that skipped step one computes the
    digest itself -- one line -- and the apply succeeds. The module docstring now
    says exactly that, and this test is what stops it drifting back to the
    stronger claim it used to make ("the digest is what makes the two steps be
    about the same file").

    The trade is stated in `importer.py`: the caller holds `ipam.manage`, is in
    their own org, and is authorized to write every row this writes; the apply is
    one transaction; every row comes back with a verdict; and
    `ipam.import_applied` records the file's sha256, the mapping and the counts,
    so a never-previewed import is fully visible afterwards. **If this test ever
    starts failing** -- because a preview record was added -- the docstrings must
    change with it, not the test.
    """
    org, vrf_id, _ = await _fx(admin_engine, "nopreview")
    async with AsyncSession(admin_engine) as session:
        result = await apply_import(
            session, org.org_id, client_id=org.org_id, actor_id=org.user_id, content=GOOD_CSV,
            mapping=MAPPING, digest=preview_digest(GOOD_CSV, MAPPING, vrf_id),
        )
        await session.commit()
    assert result.applied == 3
    assert await _address_count(admin_engine, org.org_id) == 3


# --------------------------------------------------------------------------- #
# Unparseable rows are REPORTED, never silently skipped
# --------------------------------------------------------------------------- #


async def test_every_file_row_comes_back_with_a_verdict(
    admin_engine: AsyncEngine
) -> None:
    """§8.1. SILENT SKIPPING IS HOW AN IMPORT "SUCCEEDS" AND LOSES A THIRD OF THE
    SHEET, and the operator finds out a quarter later.

    Five rows in, five verdicts out -- counted against the FILE rather than
    against a constant. *If bad rows were dropped* this observes 3 rows where 5
    belong, and the two unparseable addresses vanish without a trace.
    """
    org, _, _ = await _fx(admin_engine, "report")
    csv_bytes = (
        b"IP Address,Host,Owner,Notes\n"
        b"10.70.0.10,a,Printer A,x\n"
        b"not-an-address,b,Printer B,x\n"
        b"10.70.0.11,c,Printer C,x\n"
        b"10.70.0.999,d,Printer D,x\n"
        b"10.70.0.12,e,Printer E,x\n"
    )
    file_rows = len(csv_bytes.decode().strip().splitlines()) - 1

    async with AsyncSession(admin_engine) as session:
        preview = await preview_import(
            session, org.org_id, client_id=org.org_id, content=csv_bytes, mapping=MAPPING
        )
        await session.commit()

    assert len(preview.rows) == file_rows == 5
    assert preview.errors == 2
    bad = [row for row in preview.rows if row.action == "error"]
    assert [row.line for row in bad] == [3, 5]
    assert all(row.error for row in bad), "a reported row with no reason is a skip"
    assert [row.address for row in bad] == ["not-an-address", "10.70.0.999"]


async def test_apply_skips_the_bad_rows_but_still_reports_them(
    admin_engine: AsyncEngine
) -> None:
    """A file with two bad rows applies the three good ones and returns all five
    verdicts. Reporting and applying are different questions and the answer to
    one must not be inferred from the other."""
    org, vrf_id, _ = await _fx(admin_engine, "partial")
    csv_bytes = (
        b"IP Address,Host,Owner,Notes\n"
        b"10.70.0.10,a,Printer A,x\n"
        b"not-an-address,b,Printer B,x\n"
        b"10.70.0.11,c,Printer C,x\n"
        b"zzz,d,Printer D,x\n"
        b"10.70.0.12,e,Printer E,x\n"
    )
    async with AsyncSession(admin_engine) as session:
        result = await apply_import(
            session, org.org_id, client_id=org.org_id, actor_id=org.user_id, content=csv_bytes,
            mapping=MAPPING, digest=preview_digest(csv_bytes, MAPPING, vrf_id),
        )
        await session.commit()
    assert (result.total_rows, result.applied, result.errors) == (5, 3, 2)
    assert len(result.rows) == 5
    assert await _address_count(admin_engine, org.org_id) == 3


async def test_apply_is_one_transaction_and_a_late_failure_writes_nothing(
    admin_engine: AsyncEngine
) -> None:
    """§8.1: applied in ONE transaction.

    THE FAILURE IS FORCED ON THE LAST ROW, so a non-transactional implementation
    observes the two earlier addresses committed. Failing the first row would
    pass against an implementation with no transaction at all.

    The failure is a real one: `10.70.0.0` is the network address of its subnet,
    which §3.2 refuses -- so this is the ordinary "one row in the sheet is wrong"
    case, not a contrived exception.
    """
    org, vrf_id, _ = await _fx(admin_engine, "atomic")
    csv_bytes = (
        b"IP Address,Host,Owner,Notes\n"
        b"10.70.0.10,a,Printer A,x\n"
        b"10.70.0.11,b,Printer B,x\n"
        b"10.70.0.0,c,Printer C,x\n"
    )
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamValidationError):
            await apply_import(
                session, org.org_id, client_id=org.org_id, actor_id=org.user_id, content=csv_bytes,
                mapping=MAPPING, digest=preview_digest(csv_bytes, MAPPING, vrf_id),
            )
        await session.rollback()
    assert await _address_count(admin_engine, org.org_id) == 0


# --------------------------------------------------------------------------- #
# The two negatives that matter (§8.1)
# --------------------------------------------------------------------------- #


async def test_an_import_never_creates_a_scan_authorization(
    admin_engine: AsyncEngine
) -> None:
    """§8.1, and it is a SECURITY property, not a tidiness one.

    If an import could authorize a prefix, it would be a way to authorize
    scanning without holding `ipam.scope.manage` -- the one grant §11.2 keeps
    away from technicians because it decides where this platform emits network
    traffic.

    The import genuinely succeeds (three rows written), so this is a statement
    about what an APPLIED import did not do.
    """
    org, vrf_id, _ = await _fx(admin_engine, "noauth")
    async with AsyncSession(admin_engine) as session:
        result = await apply_import(
            session, org.org_id, client_id=org.org_id, actor_id=org.user_id, content=GOOD_CSV,
            mapping=MAPPING, digest=preview_digest(GOOD_CSV, MAPPING, vrf_id),
        )
        await session.commit()
    assert result.applied == 3

    async with AsyncSession(admin_engine) as session:
        scopes = (
            (
                await session.execute(
                    select(IpamScanAuthorization).where(
                        IpamScanAuthorization.org_id == org.org_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert scopes == []


async def test_an_import_never_enables_a_sweep(admin_engine: AsyncEngine) -> None:
    """§8.1's other negative. `sweep_enabled` is an operator act gated on
    `ipam.sweep.manage`; a sheet is not one.

    The subnet is asserted to be unchanged in all three sweep columns, not
    merely `sweep_enabled` -- an import that set an interval or designated an
    agent would be the same defect in a quieter form.
    """
    org, vrf_id, subnet_id = await _fx(admin_engine, "nosweep")
    async with AsyncSession(admin_engine) as session:
        await apply_import(
            session, org.org_id, client_id=org.org_id, actor_id=org.user_id, content=GOOD_CSV,
            mapping=MAPPING, digest=preview_digest(GOOD_CSV, MAPPING, vrf_id),
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        subnet = (
            await session.execute(select(IpamSubnet).where(IpamSubnet.id == subnet_id))
        ).scalar_one()
    assert (subnet.sweep_enabled, subnet.sweep_interval_hours, subnet.sweep_device_id) == (
        False, None, None
    )


async def test_apply_is_audited_with_the_counts_and_the_file_digest(
    admin_engine: AsyncEngine
) -> None:
    """§11.3: the import is reproducible from the audit log alone -- which is
    what the digest is for."""
    org, vrf_id, _ = await _fx(admin_engine, "auditimp")
    async with AsyncSession(admin_engine) as session:
        await apply_import(
            session, org.org_id, client_id=org.org_id, actor_id=org.user_id, content=GOOD_CSV,
            mapping=MAPPING, digest=preview_digest(GOOD_CSV, MAPPING, vrf_id),
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org.org_id,
                    AuditEvent.action == "ipam.import_applied",
                )
            )
        ).scalar_one()
    assert row.target_type == "organization"
    assert row.meta["file_digest"] == file_digest(GOOD_CSV)
    assert row.meta["applied"] == 3
    assert row.meta["total_rows"] == 3
    assert row.meta["mapping"] == MAPPING
    # AND WHERE IT WENT: the sheet, the mapping and the VRF are the whole
    # import, and the digest covers all three (Task 11 re-review, M-R1).
    assert row.meta["vrf_id"] == str(vrf_id)
    assert row.meta["preview_digest"] == preview_digest(GOOD_CSV, MAPPING, vrf_id)


async def test_a_conflicting_row_is_reported_and_not_applied(
    admin_engine: AsyncEngine
) -> None:
    """§8.1's diff. A `conflict` is specifically "already assigned to SOMEBODY
    ELSE" -- re-importing the sheet that produced the plan must be an `update`,
    or every second import is a wall of conflicts nobody reads.

    Both classifications appear in one fixture, so the test observes the
    distinction rather than "something was refused".
    """
    org, vrf_id, _ = await _fx(admin_engine, "conflict")
    async with AsyncSession(admin_engine) as session:
        await apply_import(
            session, org.org_id, client_id=org.org_id, actor_id=org.user_id, content=GOOD_CSV,
            mapping=MAPPING, digest=preview_digest(GOOD_CSV, MAPPING, vrf_id),
        )
        await session.commit()

    second = (
        b"IP Address,Host,Owner,Notes\n"
        b"10.70.0.10,printer-1,Printer A,unchanged owner\n"
        b"10.70.0.11,switch-1,SOMEBODY ELSE,taken over\n"
    )
    async with AsyncSession(admin_engine) as session:
        preview = await preview_import(
            session, org.org_id, client_id=org.org_id, content=second, mapping=MAPPING
        )
        await session.commit()
    assert [row.action for row in preview.rows] == ["update", "conflict"]
    assert preview.conflicts == 1

    async with AsyncSession(admin_engine) as session:
        result = await apply_import(
            session, org.org_id, client_id=org.org_id, actor_id=org.user_id, content=second,
            mapping=MAPPING, digest=preview_digest(second, MAPPING, vrf_id),
        )
        await session.commit()
    assert result.applied == 1

    rows = {str(r.address): r for r in await _addresses(admin_engine, org.org_id)}
    assert rows["10.70.0.11"].asset_label == "Core switch", (
        "a conflicting row overwrote an existing claimant"
    )


async def test_the_mapping_is_validated_before_anything_is_parsed(
    admin_engine: AsyncEngine
) -> None:
    """A mapping naming `state` would let a spreadsheet write the one column
    §0.6 reserves for a human decision made through an audited transition."""
    org, _, _ = await _fx(admin_engine, "mapping")
    async with AsyncSession(admin_engine) as session:
        for bad in (
            {},
            {"Host": "hostname"},  # no `address` column
            {"IP Address": "address", "S": "state"},
            {"A": "address", "B": "address"},
        ):
            with pytest.raises(IpamValidationError):
                await preview_import(
                    session, org.org_id, client_id=org.org_id, content=GOOD_CSV, mapping=bad
                )
    assert "state" not in IMPORT_FIELDS


# --------------------------------------------------------------------------- #
# §8.3 -- the documentation `network`-record converter
# --------------------------------------------------------------------------- #


async def _network_record(
    engine: AsyncEngine, org_id: UUID, name: str, fields: dict[str, Any]
) -> UUID:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        row = DocRecord(
            org_id=org_id, record_type="network", name=name, fields=fields
        )
        session.add(row)
        await session.commit()
        return row.id


async def test_the_converter_dry_runs_by_default_and_writes_nothing(
    admin_engine: AsyncEngine
) -> None:
    """§8.3: **`dry_run=True` IS THE DEFAULT, AND THE DEFAULT IS THE SAFE ONE.**

    *If the default were inverted* this observes converted subnets and an
    archived source record from a call that asked for a report. The counts are
    read from the database, not from the response, for exactly that reason.
    """
    org, vrf_id, _ = await _fx(admin_engine, "dryrun")
    record_id = await _network_record(
        admin_engine, org.org_id, "Manila users",
        {"cidr": "10.80.0.0/24", "gateway": "10.80.0.1", "dns": "10.80.0.2, 10.80.0.3"},
    )

    async with AsyncSession(admin_engine) as session:
        conversion = await convert_doc_records(
            session, org.org_id, actor_id=org.user_id
        )
        await session.commit()

    assert conversion.dry_run is True
    assert [row.prefix for row in conversion.converted] == ["10.80.0.0/24"]
    assert conversion.converted[0].subnet_id is None

    async with AsyncSession(admin_engine) as session:
        subnets = (
            (
                await session.execute(
                    select(IpamSubnet).where(
                        IpamSubnet.org_id == org.org_id,
                        IpamSubnet.prefix == "10.80.0.0/24",
                    )
                )
            )
            .scalars()
            .all()
        )
        record = (
            await session.execute(select(DocRecord).where(DocRecord.id == record_id))
        ).scalar_one()
    assert subnets == []
    assert record.archived is False


async def test_the_converter_archives_the_source_and_sets_doc_record_id(
    admin_engine: AsyncEngine
) -> None:
    """§8.3: **ARCHIVED, NOT DELETED.** `doc_versions.record_id` is a real
    foreign key and `doc_links` point at the row, so a delete would take the
    article links and the version history with it.

    `doc_record_id` on the new subnet is the provenance of the absorbed record --
    without it, "where did this subnet come from" is unanswerable the day after.
    """
    org, _, _ = await _fx(admin_engine, "convert")
    vlan_id = await seed_vlan(admin_engine, org.org_id, vid=42)
    record_id = await _network_record(
        admin_engine, org.org_id, "Manila users",
        {"cidr": "10.80.0.5/24", "vlan": 42, "gateway": "10.80.0.1",
         "dns": "10.80.0.2, 10.80.0.3"},
    )

    async with AsyncSession(admin_engine) as session:
        conversion = await convert_doc_records(
            session, org.org_id, actor_id=org.user_id, dry_run=False
        )
        await session.commit()

    assert conversion.dry_run is False
    assert len(conversion.converted) == 1
    # `strict=False`, the same call `NetworkFields._valid_cidr` already makes:
    # an operator who typed a host address meant the network.
    assert conversion.converted[0].prefix == "10.80.0.0/24"

    async with AsyncSession(admin_engine) as session:
        subnet = (
            await session.execute(
                select(IpamSubnet).where(
                    IpamSubnet.org_id == org.org_id,
                    IpamSubnet.prefix == "10.80.0.0/24",
                )
            )
        ).scalar_one()
        record = (
            await session.execute(select(DocRecord).where(DocRecord.id == record_id))
        ).scalar_one()
    assert subnet.name == "Manila users"
    assert subnet.doc_record_id == record_id
    assert subnet.vlan_id == vlan_id
    assert str(subnet.gateway) == "10.80.0.1"
    assert subnet.dns_servers == ["10.80.0.2", "10.80.0.3"]
    assert subnet.kind == "assignable"
    assert record.archived is True


async def test_every_skip_reason_is_produced_by_a_real_case(
    admin_engine: AsyncEngine
) -> None:
    """§8.3: rows that do not convert come back in `skipped` WITH REASONS.
    Nothing is guessed.

    Three real records, one per reason, plus a fourth that DOES convert -- so
    the test observes a partition rather than "everything was skipped".
    """
    org, _, _ = await _fx(admin_engine, "skips")
    await _network_record(admin_engine, org.org_id, "garbage", {"cidr": "not a cidr"})
    await _network_record(admin_engine, org.org_id, "no cidr at all", {"vlan": 7})
    await _network_record(
        admin_engine, org.org_id, "already registered", {"cidr": "10.70.0.0/24"}
    )
    await _network_record(admin_engine, org.org_id, "fine", {"cidr": "10.81.0.0/24"})

    async with AsyncSession(admin_engine) as session:
        conversion = await convert_doc_records(
            session, org.org_id, actor_id=org.user_id, dry_run=False
        )
        await session.commit()

    assert [row.name for row in conversion.converted] == ["fine"]
    reasons = {row.name: row.reason for row in conversion.skipped}
    assert set(reasons) == {"garbage", "no cidr at all", "already registered"}
    assert "unparseable cidr" in reasons["garbage"]
    assert "no cidr" in reasons["no cidr at all"]
    assert "10.70.0.0/24" in reasons["already registered"]


async def test_a_gateway_outside_its_prefix_is_dropped_with_a_warning(
    admin_engine: AsyncEngine
) -> None:
    """§8.3: nothing is guessed. A gateway outside its own prefix is a typo, and
    it is the typo that makes provisioning hand out a broken static config -- so
    the subnet is created WITHOUT it and the operator is told.

    *If the gateway were copied across unchecked*, `ck_ipam_subnet_gateway_in`
    would refuse the insert and the whole record would land in `skipped` with a
    constraint name for a reason.

    THE READ-BACK IS SCOPED TO THIS ORG. It was not: it selected
    `prefix == '10.82.0.0/24'` across the WHOLE database on the admin engine,
    while `test_ipam_sweep_dispatch.py` seeds that same prefix in a different
    org. Alphabetical collection order kept it green; reordered, `scalar_one`
    raises `MultipleResultsFound`. Worse than the flake: a query with no org
    predicate is a query that could never DETECT another org's row, which is the
    opposite of what every other assertion in this suite is built to do.
    """
    org, _, _ = await _fx(admin_engine, "badgw")
    await _network_record(
        admin_engine, org.org_id, "typo", {"cidr": "10.82.0.0/24", "gateway": "10.99.0.1"}
    )
    async with AsyncSession(admin_engine) as session:
        conversion = await convert_doc_records(
            session, org.org_id, actor_id=org.user_id, dry_run=False
        )
        await session.commit()

    assert len(conversion.converted) == 1
    assert any("not inside" in w for w in conversion.converted[0].warnings)

    async with AsyncSession(admin_engine) as session:
        subnet = (
            await session.execute(
                select(IpamSubnet).where(
                    IpamSubnet.org_id == org.org_id,
                    IpamSubnet.prefix == "10.82.0.0/24",
                )
            )
        ).scalar_one()
    assert subnet.gateway is None


async def test_a_vlan_id_that_names_no_vlan_is_reported_not_invented(
    admin_engine: AsyncEngine
) -> None:
    """§8.3: `vlan_id` is left null AND REPORTED. Creating the VLAN would be the
    converter guessing that a number in a jsonb blob is a VLAN this org runs."""
    org, _, _ = await _fx(admin_engine, "novlan")
    await _network_record(
        admin_engine, org.org_id, "vlan 999", {"cidr": "10.83.0.0/24", "vlan": 999}
    )
    async with AsyncSession(admin_engine) as session:
        conversion = await convert_doc_records(
            session, org.org_id, actor_id=org.user_id, dry_run=False
        )
        await session.commit()
    assert conversion.converted[0].vlan_id is None
    assert any("999" in w for w in conversion.converted[0].warnings)


async def test_an_archived_network_record_is_left_alone(
    admin_engine: AsyncEngine
) -> None:
    """Only NON-ARCHIVED records convert (§8.3). An archived one is a record
    somebody already retired -- resurrecting it as a subnet would undo that
    decision silently.

    A convertible record is present too, so this observes a choice.
    """
    org, _, _ = await _fx(admin_engine, "archived")
    record_id = await _network_record(
        admin_engine, org.org_id, "retired", {"cidr": "10.84.0.0/24"}
    )
    await _network_record(admin_engine, org.org_id, "live", {"cidr": "10.85.0.0/24"})
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(select(DocRecord).where(DocRecord.id == record_id))
        ).scalar_one()
        row.archived = True
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        conversion = await convert_doc_records(
            session, org.org_id, actor_id=org.user_id, dry_run=False
        )
        await session.commit()
    assert [row.name for row in conversion.converted] == ["live"]
    assert conversion.skipped == []


async def test_the_converter_is_org_scoped_on_the_admin_engine(
    admin_engine: AsyncEngine
) -> None:
    """PINNED ON THE ADMIN ENGINE (invariant #5): through `org_session` RLS
    rejects first, so a cross-org test driven that way passes with the service's
    own `org_id` predicate deleted.

    Org B holds a perfectly convertible record, so "org A's run converted
    nothing of B's" is a count that stays at zero rather than an absence that
    would hold for a converter that did nothing at all.
    """
    a, _, _ = await _fx(admin_engine, "conv-a")
    b, _, _ = await _fx(admin_engine, "conv-b")
    await _network_record(admin_engine, a.org_id, "a-net", {"cidr": "10.86.0.0/24"})
    await _network_record(admin_engine, b.org_id, "b-net", {"cidr": "10.87.0.0/24"})

    async with AsyncSession(admin_engine) as session:
        conversion = await convert_doc_records(
            session, a.org_id, actor_id=a.user_id, dry_run=False
        )
        await session.commit()
    assert [row.name for row in conversion.converted] == ["a-net"]

    async with AsyncSession(admin_engine) as session:
        count = (
            await session.execute(
                select(func.count()).select_from(IpamSubnet).where(
                    IpamSubnet.org_id == b.org_id
                )
            )
        ).scalar_one()
        b_record = (
            await session.execute(
                select(DocRecord).where(
                    DocRecord.org_id == b.org_id, DocRecord.name == "b-net"
                )
            )
        ).scalar_one()
    assert count == 1, "org B still has only its own seeded subnet"
    assert b_record.archived is False
