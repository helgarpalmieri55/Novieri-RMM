"""One or two English sentences per exposed MCP tool, keyed `"METHOD /api/v1/path"`
(MCP server design §4.1). Read by an AI client, so not translated.

Split by area into four parts only so they can be written and reviewed apart;
a key appearing in two parts is a build error, not a silent override.
"""

from app.mcp.descriptions.part_a import PART_A
from app.mcp.descriptions.part_b import PART_B
from app.mcp.descriptions.part_c import PART_C
from app.mcp.descriptions.part_d import PART_D


def _merge(*parts: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for part in parts:
        clash = merged.keys() & part.keys()
        if clash:
            raise RuntimeError(f"MCP tool described twice: {sorted(clash)}")  # i18n-exempt: build
        merged.update(part)
    return merged


DESCRIPTIONS: dict[str, str] = _merge(PART_A, PART_B, PART_C, PART_D)
