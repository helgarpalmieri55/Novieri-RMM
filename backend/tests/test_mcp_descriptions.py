"""Every exposed MCP tool carries a short, true description (MCP server design §4.1)."""

import pytest

from app.mcp.catalog import Tool, build_catalog
from app.mcp.descriptions import DESCRIPTIONS
from tests.tool_surface import build_router_only_app

#: Which part describes which areas. Every area of the catalogue is in exactly one.
PARTS: dict[str, frozenset[str]] = {
    "a": frozenset({"ipam", "budget", "docs", "service-desk", "portal", "tickets"}),
    "b": frozenset(
        {"health", "agent", "deploy", "agent-push", "patch", "backup", "site-servers"}
    ),
    "c": frozenset(
        {"changes", "licenses", "notifications", "people", "devices", "bcp", "sla",
         "allocations", "seats"}
    ),
    "d": frozenset(
        {"subscriptions", "ai", "provisioning", "users", "email-intake", "device-groups",
         "approved-actions", "jobs", "platform", "roles", "reports", "remote", "vendors",
         "dashboard", "orgs", "settings", "enrollment-tokens", "me", "audit", "software",
         "security-events", "agents", "sites", "permissions", "clients", "contacts"}
    ),
}

#: Parts whose descriptions are written. Each part's commit adds itself.
DONE_PARTS: frozenset[str] = frozenset(PARTS)


@pytest.fixture(scope="module")
def catalog() -> dict[str, Tool]:
    return build_catalog(build_router_only_app())


def test_every_area_is_in_exactly_one_part(catalog: dict[str, Tool]) -> None:
    areas = {t.area for t in catalog.values()}
    listed = [a for part in PARTS.values() for a in part]
    assert len(listed) == len(set(listed)), "an area is in two parts"
    assert areas == set(listed), (sorted(areas - set(listed)), sorted(set(listed) - areas))


@pytest.mark.parametrize("part", sorted(PARTS))
def test_every_exposed_tool_in_a_done_part_has_a_description(
    catalog: dict[str, Tool], part: str
) -> None:
    if part not in DONE_PARTS:
        pytest.skip(f"part {part} not written yet")
    missing = sorted(
        f"{t.method} {t.path}"
        for t in catalog.values()
        if t.area in PARTS[part] and not t.description.strip()
    )
    assert not missing, missing


def test_no_description_names_an_unexposed_route(catalog: dict[str, Tool]) -> None:
    exposed = {f"{t.method} {t.path}" for t in catalog.values()}
    assert not set(DESCRIPTIONS) - exposed, sorted(set(DESCRIPTIONS) - exposed)


def test_descriptions_are_short_and_plain() -> None:
    for key, text in DESCRIPTIONS.items():
        assert 0 < len(text) <= 300, key
        assert text == text.strip() and "\n" not in text, key
        assert text[0].isupper() and text.endswith("."), key
