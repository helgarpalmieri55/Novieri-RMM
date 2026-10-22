"""The agent's optional self-report on the WebSocket upgrade request
(spec-self-update.md §3.3).

Additive: an old agent sends neither header and nothing changes. Both are
read only after the client certificate has authenticated the agent, and are
used for display and eligibility, never for authorization, so a lying agent
can only misdescribe itself.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "HEADER_CAPABILITIES",
    "HEADER_VERSION",
    "MAX_CAPABILITIES_HEADER",
    "AgentSelfReport",
    "parse_agent_headers",
]

HEADER_VERSION = "X-ITS-Agent-Version"
HEADER_CAPABILITIES = "X-ITS-Agent-Capabilities"
MAX_CAPABILITIES_HEADER = 4096

# agents.agent_version is varchar(50). A git SHA or "dev" is stored as
# reported; eligibility parses it as semver separately.
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,49}")
_CAPABILITY = re.compile(r"[a-z][a-z0-9._-]{0,63}")


@dataclass(frozen=True)
class AgentSelfReport:
    version: str | None
    capabilities: dict[str, bool] | None


def parse_agent_headers(headers: Mapping[str, str]) -> AgentSelfReport:
    raw_version = headers.get(HEADER_VERSION)
    version = None
    if raw_version is not None and _VERSION.fullmatch(raw_version.strip()):
        version = raw_version.strip()

    raw_caps = headers.get(HEADER_CAPABILITIES)
    capabilities = None
    if raw_caps is not None and 0 < len(raw_caps) <= MAX_CAPABILITIES_HEADER:
        names = [n.strip() for n in raw_caps.split(",")]
        if all(_CAPABILITY.fullmatch(n) for n in names):
            capabilities = dict.fromkeys(names, True)
    return AgentSelfReport(version=version, capabilities=capabilities)
