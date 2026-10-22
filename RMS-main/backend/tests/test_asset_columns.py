"""`devices` becomes an ASSET table (docs/modules/asset-inventory/spec.md §2).

THE RISKY PART IS NOT THE MIGRATION. Adding seven columns is trivial; dropping
`NOT NULL` from `hostname` and `os_family` changes what every existing read path
may receive, and none of them was written expecting `None`. That audit is
`tests/test_asset_read_paths.py`. This file pins the SHAPE.

WHY THERE IS NO `management_state` COLUMN, asserted rather than remembered:
"does this record have an agent" is a join (`agents.device_id` is a unique FK to
`devices.id`), and storing it beside the `agents` row guarantees the two
eventually disagree, with nothing to notice that they have.
"""

from app.models.core import Device

LIFECYCLES = {"in_service", "stock", "unmanageable", "retired"}
ASSET_TYPES = {
    "computer",
    "server",
    "network",
    "printer",
    "phone",
    "appliance",
    "other",
}
SOURCES = {"agent", "manual", "discovery"}


def test_the_asset_columns_exist() -> None:
    columns = Device.__table__.columns
    for name in (
        "lifecycle",
        "asset_type",
        "description",
        "primary_ip",
        "owner",
        "location",
        "source",
    ):
        assert name in columns, sorted(columns.keys())


def test_identity_is_optional_because_a_boxed_laptop_has_none() -> None:
    """A switch has a name but no hostname in this sense, and no `os_family` at
    all; a laptop still in its box has neither yet. Both were NOT NULL, and
    that is exactly what kept them out of the inventory."""
    assert Device.__table__.columns["hostname"].nullable is True
    assert Device.__table__.columns["os_family"].nullable is True


def test_lifecycle_and_source_default_to_todays_meaning() -> None:
    """Every row that exists when 0061 runs came from an enrolling agent and is
    in service. A default that said anything else would silently reclassify the
    whole estate at migration time."""
    assert Device.__table__.columns["lifecycle"].default.arg == "in_service"
    assert Device.__table__.columns["source"].default.arg == "agent"
    assert Device.__table__.columns["asset_type"].default.arg == "computer"


def test_there_is_no_stored_management_state() -> None:
    """Spec §2: managed is a JOIN. These are the columns that must never appear.

    A single `management_state` is the tempting shape, and it conflates two
    facts that change independently: whether an `agents` row exists (derived,
    and it changes when a machine enrols or is revoked) and what the operator
    intends the asset to BE (stored, and nothing can derive it).
    """
    assert "management_state" not in Device.__table__.columns
    assert "is_managed" not in Device.__table__.columns


def test_the_manage_code_exists_and_read_is_deliberately_not_added() -> None:
    """`assets.read` is NOT a code, and that is a decision (spec §7).

    `devices.read` already answers "may this principal see the fleet list", and
    an asset IS a fleet row. A second read gate would mean every existing
    read-only role silently stops seeing part of that list on the day this
    ships, with nothing on screen to say why.
    """
    from app.core.rbac import PERMISSIONS

    assert "assets.manage" in PERMISSIONS
    assert "assets.read" not in PERMISSIONS
    # Part 2's code. Deliberately absent: a permission that exists before the
    # thing it guards is a permission nobody can test, and it would be granted
    # to roles for a capability that does nothing.
    assert "assets.discover" not in PERMISSIONS
