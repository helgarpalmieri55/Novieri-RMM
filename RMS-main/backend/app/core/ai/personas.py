"""A persona is a frozen profile, not a package (docs/modules/ai-automation.md
§0.1, §2.2).

The turn loop, the provider clients, the envelope, the scrubber, the injection
heuristic, the loopback executor, the transcript store and the retention sweep
are KERNEL code under `app/core/ai/`, and none of them knows any persona's name.
A persona contributes exactly three things: this profile, a router mounted on
its own surface, and a frontend feature folder.

**THE PROFILE IS A FROZEN CONSTANT COMPILED INTO THE IMAGE**, and `PERSONAS` is
a `MappingProxyType` over the registry -- the same immutability posture
`app/core/ai/tools/registry.py` already takes, for the same reason: whatever
compromises the database must not get to choose the agent's capabilities, its
system prompt, or whether it may propose an act. A profile assembled from rows
would put all three in reach of a SQL injection in an unrelated module.

`register_persona` is called at IMPORT time from each persona package, so the
registry is complete once the application has been built and is never written to
again. There is no unregister and no replace: a second registration under one
name raises, because the failure it prevents -- a later import quietly replacing
the admin persona's tool set -- is silent otherwise.
"""

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol

from app.core.ai.types import ReadTool, ToolResult

__all__ = [
    "PERSONAS",
    "LoopbackExecutor",
    "PersonaProfile",
    "ReplayPolicy",
    "register_persona",
]

#: How a persona's own prior ASSISTANT turns are replayed into a new turn
#: (§0.8, §7.4).
#:
#: `verbatim` is read mode's behaviour and is correct there: a read-mode turn
#: cannot act, so laundered instructions replayed back into context reach a
#: frozen GET allowlist and nothing else.
#:
#: `neutralize_flagged` is what an ACT-CAPABLE session needs, and the difference
#: is not caution -- it is that the premise read mode's defence rests on is gone.
#: The vocabulary is declared here so a profile cannot name a policy that does
#: not exist.
ReplayPolicy = Literal["verbatim", "neutralize_flagged"]


class LoopbackExecutor(Protocol):
    """The signature both loopback executors satisfy (§0.2).

    There are two, and there is deliberately NO "principal kind" branch inside
    one: the staff executor carries the caller's own bearer token back through
    the whole ASGI stack so `require_permission` re-runs verbatim, and a portal
    principal has no `AuthContext` to build such a request from. A branch is how
    the staff path and the portal path end up sharing a code path only one of
    them was audited for.
    """

    async def __call__(
        self,
        *,
        tool_name: str,
        raw_args: Mapping[str, Any],
        app: Any,
        bearer: str,
        deadline: float,
        registry: Mapping[str, ReadTool],
    ) -> ToolResult | Any: ...


@dataclass(frozen=True)
class PersonaProfile:
    """§2.2's facts about one persona, and nothing else.

    `system_prompt` is a MODULE-LEVEL CONSTANT in the persona's own package,
    never assembled from data (§8.1): a prompt built per turn is a prompt an
    attacker can hope to influence one field of.

    `may_propose` is the only place act capability is expressed. It is `False`
    by default, so a persona added without thinking about it cannot act -- the
    same direction `ReadTool.response_fields` takes for the same reason.
    """

    name: str
    #: The dotted path of the persona package this profile lives in
    #: (`app.modules.ai_admin`).
    #:
    #: RECORDED DEVIATION FROM SPEC §2.2, which names five fields and not this
    #: one. The source-scanning boundary tests derive the list of persona
    #: packages FROM THIS REGISTRY rather than from a literal, because a
    #: hardcoded list means the fourth persona is unscanned -- the same defect
    #: as a denylist, and invisible in exactly the same way. A package has to be
    #: written down somewhere for that to be possible; here it is written down
    #: on the profile, and `register_persona` refuses a value that does not
    #: contain the module doing the registering, so it cannot be quietly wrong.
    package: str
    system_prompt: str
    executor: LoopbackExecutor
    tools: Mapping[str, ReadTool]
    may_propose: bool = False
    replay_policy: ReplayPolicy = "verbatim"


_REGISTRY: dict[str, PersonaProfile] = {}

#: Every registered persona, read-only at runtime. Widening it requires a
#: `register_persona` call in a persona package, which is a reviewed code
#: change; `PERSONAS[name] = profile` raises `TypeError`.
PERSONAS: Mapping[str, PersonaProfile] = MappingProxyType(_REGISTRY)


def register_persona(profile: PersonaProfile) -> PersonaProfile:
    """Register `profile` under its own name.

    `profile.package` IS CHECKED AGAINST THE CALLER, not trusted. A profile that
    named the wrong package would make the source-scanning boundary tests scan
    the wrong directory and PASS, which is the worst of the three possible
    outcomes: a green build asserting nothing.
    """
    existing = _REGISTRY.get(profile.name)
    if existing is profile:
        # Module reimport under pytest re-executes the persona package. Nothing
        # to check and nothing to change: this is the object already stored.
        return profile
    if existing is not None:
        raise ValueError(  # i18n-exempt: import-time registration check, logged
            f"persona {profile.name!r} is already registered with a different profile; "
            f"a persona has exactly one profile, compiled into the image"
        )
    caller = sys._getframe(1).f_globals.get("__name__", "")
    if not (caller == profile.package or caller.startswith(profile.package + ".")):
        raise ValueError(  # i18n-exempt: import-time registration check, logged
            f"persona {profile.name!r} declares package {profile.package!r} but is being "
            f"registered from {caller!r}; the boundary scans derive their roots from this "
            f"field and would scan the wrong directory"
        )
    _REGISTRY[profile.name] = profile
    return profile
