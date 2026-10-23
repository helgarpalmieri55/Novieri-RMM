from types import SimpleNamespace

import pytest

from app.core import mcp_channel
from app.core.mcp_channel import (
    McpCall,
    mcp_call_from_scope,
    mcp_call_var,
    mcp_resource,
    use_mcp_call,
)


def test_the_resource_is_the_console_url_plus_mcp() -> None:
    settings = SimpleNamespace(public_console_url="https://itms.example.com/")
    assert mcp_resource(settings) == "https://itms.example.com/mcp"
    assert mcp_resource(SimpleNamespace(public_console_url=None)) is None
    assert mcp_resource(SimpleNamespace(public_console_url="  ")) is None


def test_a_scope_without_the_extension_is_not_mcp() -> None:
    assert mcp_call_from_scope({"type": "http", "extensions": {}}) is None
    assert mcp_call_from_scope({"type": "http"}) is None


def test_a_marked_scope_names_the_app_and_tool() -> None:
    marker = {"azp": "claude", "tool": "devices_list"}
    scope = {"type": "http", "extensions": {mcp_channel.EXTENSION_KEY: marker}}
    assert mcp_call_from_scope(scope) == McpCall(azp="claude", tool="devices_list")


def test_a_malformed_marker_is_refused_rather_than_trusted() -> None:
    with pytest.raises(ValueError):
        mcp_call_from_scope({"extensions": {mcp_channel.EXTENSION_KEY: {"azp": 3}}})


@pytest.mark.parametrize("marker", ["claude", ["claude", "t"], 3, ""])
def test_a_marker_that_is_not_a_mapping_is_refused_as_malformed(marker: object) -> None:
    with pytest.raises(ValueError):
        mcp_call_from_scope({"extensions": {mcp_channel.EXTENSION_KEY: marker}})


def test_use_mcp_call_sets_and_resets() -> None:
    assert mcp_call_var.get() is None
    with use_mcp_call(McpCall("a", "t")):
        assert mcp_call_var.get() == McpCall("a", "t")
    assert mcp_call_var.get() is None


def test_the_forbidden_codes_are_the_in_app_ais() -> None:
    from app.core.ai.tools.registry import _FORBIDDEN_GATES

    assert mcp_channel.MCP_FORBIDDEN_CODES == _FORBIDDEN_GATES
