from datetime import UTC, datetime
from typing import get_args
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import Device, DeviceGroup, Organization
from app.modules.inventory.rules import (
    OPS,
    Op,
    _condition_holds,
    evaluate_rules,
    matches_document,
)
from app.modules.inventory.schema import InventoryDocument
from tests.test_inventory_schema import FULL_DOC


async def _org(admin_engine: AsyncEngine) -> UUID:
    slug = f"rul-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


async def _group(
    app_engine, org_id: UUID, name: str, rules: dict | None, precedence: int = 0
) -> UUID:
    async with org_session(org_id, engine=app_engine) as session:
        group = DeviceGroup(
            org_id=org_id, name=name, rules=rules, precedence=precedence
        )
        session.add(group)
        await session.flush()
        return group.id


async def _device(app_engine, org_id: UUID, group_id: UUID | None = None) -> UUID:
    async with org_session(org_id, engine=app_engine) as session:
        device = Device(
            org_id=org_id, hostname="r1", os_family="windows", device_group_id=group_id
        )
        session.add(device)
        await session.flush()
        return device.id


async def _evaluate(app_engine, org_id: UUID, device_id: UUID, doc_dict: dict) -> UUID | None:
    async with org_session(org_id, engine=app_engine) as session:
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one()
        result = await evaluate_rules(
            session, device, InventoryDocument(**doc_dict), org_id=org_id
        )
        await session.flush()
        return result


async def test_ops_match_and_assign(admin_engine, app_engine) -> None:
    org_id = await _org(admin_engine)
    group_id = await _group(
        app_engine,
        org_id,
        "win-11-big-ram",
        {
            "all": [
                {"fact": "os_family", "op": "eq", "value": "windows"},
                {"fact": "os_version", "op": "contains", "value": "windows 11"},
                {"fact": "ram_bytes", "op": "gt", "value": 8_000_000_000},
                {"fact": "facts.domain_joined", "op": "eq", "value": True},
                {"fact": "hostname", "op": "ne", "value": "excluded-host"},
            ]
        },
    )
    device_id = await _device(app_engine, org_id)
    assert await _evaluate(app_engine, org_id, device_id, FULL_DOC) == group_id


async def test_software_fact_paths(admin_engine, app_engine) -> None:
    org_id = await _org(admin_engine)
    chrome_rule = {"all": [{"fact": "software:Google Chrome", "op": "contains", "value": "126."}]}
    has_chrome = await _group(app_engine, org_id, "a-chrome", chrome_rule)
    device_id = await _device(app_engine, org_id)
    assert await _evaluate(app_engine, org_id, device_id, FULL_DOC) == has_chrome

    # absent software: ne matches, eq does not
    no_slack_rule = {"all": [{"fact": "software:Slack", "op": "ne", "value": "*"}]}
    no_slack = await _group(app_engine, org_id, "0-no-slack", no_slack_rule)
    # "0-no-slack" sorts before "a-chrome" → first match wins deterministically
    assert await _evaluate(app_engine, org_id, device_id, FULL_DOC) == no_slack


async def test_no_match_preserves_manual_placement(admin_engine, app_engine) -> None:
    org_id = await _org(admin_engine)
    manual = await _group(app_engine, org_id, "manual", None)
    linux_rule = {"all": [{"fact": "os_family", "op": "eq", "value": "linux"}]}
    await _group(app_engine, org_id, "linux-only", linux_rule)
    device_id = await _device(app_engine, org_id, group_id=manual)
    assert await _evaluate(app_engine, org_id, device_id, FULL_DOC) is None
    async with org_session(org_id, engine=app_engine) as session:
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one()
        assert device.device_group_id == manual


async def test_malformed_rules_skipped(admin_engine, app_engine) -> None:
    org_id = await _org(admin_engine)
    await _group(app_engine, org_id, "broken", {"all": [{"fact": "os_family"}]})  # missing op
    works_rule = {"all": [{"fact": "os_family", "op": "eq", "value": "windows"}]}
    ok = await _group(app_engine, org_id, "works", works_rule)
    device_id = await _device(app_engine, org_id)
    assert await _evaluate(app_engine, org_id, device_id, FULL_DOC) == ok


async def test_lt_op(admin_engine, app_engine) -> None:
    org_id = await _org(admin_engine)
    small_rule = {"all": [{"fact": "ram_bytes", "op": "lt", "value": 1000}]}
    await _group(app_engine, org_id, "small-ram", small_rule)
    device_id = await _device(app_engine, org_id)
    assert await _evaluate(app_engine, org_id, device_id, FULL_DOC) is None  # 16 GiB not < 1000


# --------------------------------------------------------------------------- #
# Precedence -- migration 0072, spec §10.1
#
# Before 0072 `evaluate_rules` ordered rule-bearing groups on `name` alone, so
# the alphabet WAS the precedence list and a rename moved machines between
# patch, backup, remote and deploy targets with no rule edited. These two tests
# are the pin on the new order and the proof the old one is gone; either fails
# on its own if `ORDER BY` here is reverted to `name`.
# --------------------------------------------------------------------------- #


async def test_precedence_decides_which_rule_group_claims_a_device(
    admin_engine, app_engine
) -> None:
    """Lower `precedence` wins, and it wins AGAINST the alphabet.

    `zeta` sorts last by name and first by precedence. Under the pre-0072
    ordering `alpha` claims the device; under `(precedence, name)` `zeta` does.
    """
    org_id = await _org(admin_engine)
    windows = {"all": [{"fact": "os_family", "op": "eq", "value": "windows"}]}
    await _group(app_engine, org_id, "alpha", windows, precedence=10)
    zeta = await _group(app_engine, org_id, "zeta", windows, precedence=0)

    device_id = await _device(app_engine, org_id)
    assert await _evaluate(app_engine, org_id, device_id, FULL_DOC) == zeta


async def test_a_rename_no_longer_decides_which_rule_wins(admin_engine, app_engine) -> None:
    """THE DEFECT 0072 EXISTS TO CLOSE, asserted as a before/after on one fleet.

    Two rule groups both matching the device, on DIFFERENT precedences. Under
    the pre-0072 evaluator, renaming the loser to something that sorts first
    handed it the device -- a fleet re-segmented by an edit to a label, with no
    rule touched. Under `(precedence, name)` the rename moves nothing, and the
    column that is supposed to decide still does.

    WHAT THIS DOES NOT CLAIM. `name` is the TIEBREAK, so two rule groups left
    on the same precedence are still ordered alphabetically and a rename
    between them still re-orders them. That is `(precedence, name)` working as
    specified, not the defect: the operator who wants a rename to be free
    states the order once, and after that names are labels.
    """
    org_id = await _org(admin_engine)
    windows = {"all": [{"fact": "os_family", "op": "eq", "value": "windows"}]}
    fallback = await _group(app_engine, org_id, "a-fallback", windows, precedence=0)
    other = await _group(app_engine, org_id, "m-other", windows, precedence=5)
    device_id = await _device(app_engine, org_id)
    assert await _evaluate(app_engine, org_id, device_id, FULL_DOC) == fallback

    # The rename that used to re-segment a fleet: "0-other" sorts before
    # "a-fallback" in every collation this database could be running.
    async with org_session(org_id, engine=app_engine) as session:
        group = (
            await session.execute(select(DeviceGroup).where(DeviceGroup.id == other))
        ).scalar_one()
        group.name = "0-other"
        await session.flush()
    assert await _evaluate(app_engine, org_id, device_id, FULL_DOC) == fallback, (
        "a rename moved a device between rule groups -- `name` is still deciding"
    )

    # And the column that IS supposed to decide, does -- so the assertion above
    # is not passing because nothing can move this device.
    async with org_session(org_id, engine=app_engine) as session:
        group = (
            await session.execute(select(DeviceGroup).where(DeviceGroup.id == other))
        ).scalar_one()
        group.precedence = -1
        await session.flush()
    assert await _evaluate(app_engine, org_id, device_id, FULL_DOC) == other


# --------------------------------------------------------------------------- #
# Cycle 2 -- `docs/superpowers/specs/2026-08-22-device-group-management-
# design.md` §5.1, §7.1 and §8.3.
#
# The evaluator gains three properties in this cycle, and each one is a hole
# that was open while nothing could author a rules document:
#
#   * an `org_id` PREDICATE (§8.3.2). Cycle 1's docstring called its absence a
#     standing debt "safe today only because its ONE caller runs inside
#     `org_session`". This cycle adds the second caller -- the re-evaluation
#     sweep -- in the worker, where four sibling sweeps hold a BYPASSRLS admin
#     engine. The test below drives the evaluator ON that engine, which is the
#     only way the predicate is the thing being tested rather than RLS.
#   * ARCHIVED GROUPS CLAIM NOTHING. §7.1's whole safety argument is that an
#     archived group "holds no devices ... so a job aimed at one resolves to
#     zero of them". An evaluator that kept assigning devices to an archived
#     rule group would falsify that sentence on the next sweep, and the group
#     would then be unarchivable-and-occupied with nothing on any screen
#     saying why. THE SPEC DOES NOT STATE THIS; it follows from §7.1 and is
#     recorded in the report for this cycle.
#   * ONE OP VOCABULARY (§5.1). The closed `Op` literal the API validates
#     against and the table `_condition_holds` dispatches on are the same
#     tuple, and the test below holds them equal in both directions.
# --------------------------------------------------------------------------- #


def test_the_validators_op_set_is_the_evaluators_op_set_in_both_directions() -> None:
    """§5.1: two independently maintained lists is how a rule the API accepts
    becomes a rule the ingest skips -- and the only evidence would be a
    `logger.warning` nobody reads.

    FALSIFIED BOTH WAYS: adding an entry to `_OPS` without adding it to `Op`
    fails the first assertion; adding one to `Op` without a dispatch entry
    fails the loop, because `_condition_holds` raises on an op it cannot
    dispatch.
    """
    assert set(get_args(Op)) == set(OPS)
    for op in get_args(Op):
        # Not an assertion about the RESULT -- only that the op dispatches. The
        # operands are numeric because `gt`/`lt` call `float()` on both, which
        # is the very coupling validator 2 exists to enforce at authorship.
        _condition_holds(1, op, 1)
    with pytest.raises(ValueError, match="unknown op"):
        _condition_holds(1, "startswith", 1)


def test_a_typod_fact_under_ne_still_matches_every_device() -> None:
    """The evaluator property the API validator exists to make unreachable.

    `_condition_holds` returns True for `ne` on an ABSENT fact, so
    `{"fact": "os_familly", "op": "ne", "value": "windows"}` claims every
    device in the org and reads like a reasonable rule. This cycle does not
    change that -- it refuses the document at the point of authorship instead
    (see `tests/test_device_group_rules.py`). Pinned here so that a future
    change to the evaluator cannot quietly make the validator's whole reason
    for existing stale.
    """
    doc = InventoryDocument(**FULL_DOC)
    assert matches_document(
        {"all": [{"fact": "os_familly", "op": "ne", "value": "windows"}]}, doc
    )


async def test_an_archived_rule_group_claims_nothing(admin_engine, app_engine) -> None:
    """§7.1's guarantee, held at the one place that could break it.

    An archived group is empty and unreferenced BY CONSTRUCTION -- that is the
    sentence six modules are allowed to stay ignorant of `archived_at` on. A
    rule-bearing group can be archived (the refusal only counts live
    references, and an empty rule group has none), so without this filter the
    very next sweep would re-populate it.
    """
    org_id = await _org(admin_engine)
    windows = {"all": [{"fact": "os_family", "op": "eq", "value": "windows"}]}
    live = await _group(app_engine, org_id, "live-rule", windows, precedence=10)
    archived = await _group(app_engine, org_id, "archived-rule", windows, precedence=0)
    async with org_session(org_id, engine=app_engine) as session:
        group = (
            await session.execute(select(DeviceGroup).where(DeviceGroup.id == archived))
        ).scalar_one()
        group.archived_at = datetime.now(UTC)
        await session.flush()

    device_id = await _device(app_engine, org_id)
    # `archived` has the LOWER precedence, so it would win if it were eligible.
    assert await _evaluate(app_engine, org_id, device_id, FULL_DOC) == live


async def test_the_org_id_predicate_is_the_only_scope_on_the_admin_engine(
    admin_engine,
) -> None:
    """THE TENANCY TEST THAT CAN ACTUALLY FAIL (audit idiom, adapted).

    `docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md` §0: everything
    served through `org_session` runs as `app_user` (NOBYPASSRLS), so deleting
    the application's `WHERE org_id` turns no `org_session`-driven test red --
    RLS silently covers for it. This test runs `evaluate_rules` on the BYPASSRLS
    admin engine, which is exactly the engine the re-evaluation sweep's four
    sibling sweeps hold, so the predicate is the ONLY thing between one tenant's
    rule and another tenant's device.

    FALSIFIED: delete `DeviceGroup.org_id == org_id` from `evaluate_rules` and
    this device is claimed by the NEIGHBOUR's group.
    """
    theirs = await _org(admin_engine)
    mine = await _org(admin_engine)
    windows = {"all": [{"fact": "os_family", "op": "eq", "value": "windows"}]}

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        # The neighbour's rule sorts FIRST, so an unscoped evaluator picks it.
        neighbour = DeviceGroup(
            org_id=theirs, name="aaa-theirs", rules=windows, precedence=-100
        )
        ours = DeviceGroup(org_id=mine, name="zzz-mine", rules=windows, precedence=0)
        device = Device(org_id=mine, hostname="r1", os_family="windows")
        session.add_all([neighbour, ours, device])
        await session.commit()
        neighbour_id, our_id, device_id = neighbour.id, ours.id, device.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one()
        claimed = await evaluate_rules(
            session, row, InventoryDocument(**FULL_DOC), org_id=mine
        )
        await session.commit()

    assert claimed == our_id, "the evaluator crossed a tenant boundary"
    assert claimed != neighbour_id
