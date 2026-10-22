"""§11.4 and §0.10: **zero egress as a checked property, not a claim.**

Two halves, and both are needed.

BEHAVIOURAL. A complete turn -- several tool calls against real registry tools
on the real app, then a final answer -- is run with a spy on the provider
client's transport. The spy records the full URL of every request that would
have left the box. The assertion is that the set of `(scheme, host, port)` it
recorded is exactly the one configured host. **The loopback executor's requests
do not reach a transport at all** (`ASGITransport` calls the app object
in-process), which is precisely the property being demonstrated: the agent reads
the estate without a packet leaving.

Checking the *configured* host only would be a weaker test than it looks, so
this file checks WHAT ACTUALLY LEFT: every recorded URL, in full, plus a
redirect stub proving a `302` to another host is never followed. A base URL that
is server environment and never org-editable (§0.10) removes the SSRF primitive
rather than guarding it, but the guard against a host arriving some other way --
through a path, or through a redirect -- has to be an observation of the wire.

STRUCTURAL. A grep over every source file in the package for client
construction, `requests`, `urllib`, `smtplib`, `subprocess`, `app.core.egress`
and `app.core.approved_actions`. The construction-site assertion is an EQUALITY
against a named set of files, not an "assert no offenders": a link-preview
fetcher added later is observed as a third site and named, and a site that
disappears is observed too, because a scan that finds nothing where it expected
something is a broken scan rather than a clean bill of health.
"""

import ast
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.core.ai import providers as providers_module
from app.core.ai.orchestrator import run_turn
from app.core.ai.providers import ProviderUnavailableError, load_provider_configs
from app.core.ai.providers.openai_compatible import OpenAICompatibleProvider
from app.modules.ai_admin.profile import ADMIN_PROFILE
from tests.ai_admin_fakes import (
    OutboundSpy,
    ScriptedSseApp,
    StatusTransport,
    ai_user,
    install_outbound_spy,
    sse,
)

PACKAGE_ROOT = Path(providers_module.__file__).resolve().parent.parent

PROVIDER_HOST = "vllm.internal.test"
PROVIDER_PORT = 8000
BASE_URL = f"http://{PROVIDER_HOST}:{PROVIDER_PORT}/v1"


def _self_hosted() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        load_provider_configs(
            {
                "ITS_AI_PROVIDERS": "local",
                "ITS_AI_LOCAL_KIND": "openai_compatible",
                "ITS_AI_LOCAL_BASE_URL": BASE_URL,
                "ITS_AI_LOCAL_MODEL": "a-model-name",
            }
        )["local"]
    )


def _call_frame(index: int, call_id: str, name: str, arguments: str) -> str:
    escaped = arguments.replace("\\", "\\\\").replace('"', '\\"')
    return (
        f'data: {{"choices":[{{"delta":{{"tool_calls":[{{"index":{index},'
        f'"id":"{call_id}","type":"function","function":{{"name":"{name}",'
        f'"arguments":"{escaped}"}}}}]}}}}]}}'
    )


# --------------------------------------------------------------------------- #
# 1. A complete turn contacts exactly one outbound host.
# --------------------------------------------------------------------------- #


async def test_a_complete_turn_contacts_exactly_the_configured_provider_host(
    api_env: Any, monkeypatch: Any
) -> None:
    """§11.4's integration test, with a REAL registry tool and the real app.

    The turn: the model asks for `health.alerts`, then for a device's health,
    then answers. Both tool calls are issued by the loopback executor, which
    re-enters this app in-process. The only thing that reaches a transport is
    the provider exchange itself.

    The `>= 2` assertion on the executor's tape is what stops this test being
    vacuous. A turn that made no tool calls at all would also contact exactly
    one host, and would prove nothing about the reads.
    """
    org_id = await api_env.create_org("egress-org")
    _, sub = await ai_user(api_env, org_id, "ai.use", "health.read", "devices.read")
    headers = await api_env.headers(org_id, sub)

    provider_app = ScriptedSseApp(
        [
            sse(
                _call_frame(0, "call_1", "health.alerts", '{"limit": 3}'),
                "data: [DONE]",
            ),
            sse(
                _call_frame(
                    0,
                    "call_2",
                    "devices.health",
                    f'{{"device_id": "{uuid4()}"}}',
                ),
                "data: [DONE]",
            ),
            sse(
                'data: {"choices":[{"delta":{"content":"Nothing is alerting."}}]}',
                "data: [DONE]",
            ),
        ]
    )
    outbound = OutboundSpy(httpx.ASGITransport(app=provider_app))
    install_outbound_spy(monkeypatch, outbound)

    loopback: list[httpx.Request] = []

    class Tapped(httpx.AsyncBaseTransport):
        def __init__(self, app: Any) -> None:
            self.inner = httpx.ASGITransport(app=app)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            loopback.append(request)
            return await self.inner.handle_async_request(request)

    from app.core.ai import executor_staff as executor

    monkeypatch.setattr(executor, "_transport", Tapped)

    outcome = await run_turn(
        profile=ADMIN_PROFILE,
        provider=_self_hosted(),
        app=api_env.app,
        bearer=headers["Authorization"],
        messages=[],
        allowed_tools=["health.alerts", "devices.health"],
        deadline=time.monotonic() + 30.0,
    )

    assert outcome.answer.startswith("Nothing is alerting.")
    assert len(loopback) >= 2, f"the turn made {len(loopback)} tool calls; the test needs 2"
    assert outbound.urls, "the provider was never contacted at all"
    assert outbound.hosts == {("http", PROVIDER_HOST, PROVIDER_PORT)}, outbound.urls
    # WHAT ACTUALLY LEFT, in full -- not merely "the host we configured was
    # among them". A path-smuggled absolute URL changes this list even when it
    # resolves to the same host.
    assert set(outbound.urls) == {f"{BASE_URL}/chat/completions"}


async def test_a_redirect_to_another_host_is_never_followed(monkeypatch: Any) -> None:
    """The vector a "the configured host is correct" test cannot see.

    A provider (or anything that can answer as one) replying `302` to another
    host would, with `follow_redirects=True`, produce a second request to a host
    nobody configured. Asserted on the spy's tape: exactly one request left, and
    its host is the configured one.
    """
    outbound = OutboundSpy(
        StatusTransport(302, headers={"location": "http://attacker.test/collect"})
    )
    install_outbound_spy(monkeypatch, outbound)

    with pytest.raises(ProviderUnavailableError):
        async for _ in _self_hosted().stream_turn(
            system="s", messages=[], tools=[], deadline=time.monotonic() + 10.0
        ):
            pass

    assert outbound.hosts == {("http", PROVIDER_HOST, PROVIDER_PORT)}, outbound.urls
    assert len(outbound.urls) == 1


# --------------------------------------------------------------------------- #
# 2. The structural companion.
# --------------------------------------------------------------------------- #


def _source_files() -> list[Path]:
    files = sorted(p for p in PACKAGE_ROOT.rglob("*.py") if "__pycache__" not in p.parts)
    assert len(files) >= 10, f"the scan found only {len(files)} source files; it is vacuous"
    return files


def _relative(path: Path) -> str:
    return str(path.relative_to(PACKAGE_ROOT))


# The complete set of files in this module allowed to construct an HTTP client,
# and what each one is for. EQUALITY, not containment: a third entry is a new
# egress path, and a missing entry means the scan is looking at the wrong thing.
CLIENT_CONSTRUCTION_SITES = {
    # In-process only: `ASGITransport` calls the app object directly, so this
    # client never opens a socket (§0.1).
    "executor_staff.py",
    # **THE THIRD, ADDED DELIBERATELY, AND THIS TEST GOING RED IS THE MECHANISM
    # WORKING.** The tier-1 resolver serves a `PortalContext`, which
    # `app/modules/portal/principal.py` makes deliberately NOT an `AuthContext`,
    # so it cannot use the staff executor -- and
    # `docs/modules/ai-automation.md` §0.2 REJECTS the obvious alternative, "one
    # executor with a principal-kind branch", because a branch is how the staff
    # path and the portal path end up sharing a code path only one of them was
    # audited for. Two executors, two types, no branch, and one more counted
    # egress site. It is still in-process only, for the same `ASGITransport`
    # reason as the staff one.
    "executor_portal.py",
    # The module's ONE outbound path, shared by both provider clients.
    "providers/_http.py",
}


# The `httpx` module-level helpers matter as much as the client constructors.
# `httpx.get(url)` is a complete egress path in ONE EXPRESSION and constructs no
# client the reader can see; a scan looking only for `AsyncClient(` walks past
# it. This list was widened after exactly that mutation was tried.
_CLIENT_MARKERS = (
    "httpx.AsyncClient(",
    "httpx.Client(",
    "httpx.get(",
    "httpx.post(",
    "httpx.put(",
    "httpx.delete(",
    "httpx.request(",
    "httpx.stream(",
    "aiohttp",
    "requests.get(",
)


def test_the_module_constructs_an_http_client_in_exactly_three_places() -> None:
    """**THE MUTATION THIS EXISTS FOR IS A SECOND EGRESS PATH.**

    A URL-fetch tool, a link-preview fetcher, a "just ping the provider's health
    endpoint" helper: each arrives as one line in a file nobody thinks of as
    networked. This observes the third site and names the file.
    """
    found = {
        _relative(path)
        for path in _source_files()
        for marker in _CLIENT_MARKERS
        if marker in path.read_text()
    }
    assert found == CLIENT_CONSTRUCTION_SITES, (
        f"client construction sites changed: {sorted(found)} != "
        f"{sorted(CLIENT_CONSTRUCTION_SITES)}"
    )


_FORBIDDEN_IMPORTS = (
    # §0.10: written for webhook delivery -- refuses redirects, caps the body,
    # never returns the body in full, all wrong for a streaming completion. Its
    # presence would mean an arbitrary-URL feature had been reintroduced and
    # then guarded.
    "app.core.egress",
    # `app.core.approved_actions` USED TO BE ON THIS LIST and has been removed
    # deliberately, with the claim it carried moved rather than dropped.
    #
    # Its justification was "§12.3: read mode does not read the approved-action
    # registry AT ALL" -- a statement about the READ-MODE MODULE, which is now
    # `app/modules/ai_admin/` and is scanned by
    # `test_ai_admin_boundaries.py::test_no_source_file_imports_the_approved_action_registry`,
    # where it still holds and is still asserted. THIS file's root became
    # `app/core/ai/` when the orchestration machinery relocated, and that
    # package now contains `act.py`, whose entire job is to resolve an
    # `approved_action` row (docs/modules/ai-automation.md §0.7). Keeping the
    # ban here would forbid the one import act mode is built on, in a file whose
    # subject is EGRESS -- a registry import opens no socket and starts no
    # process, and it was only ever here by adjacency.
    # Anything that opens a socket or a process by another route.
    #
    # `urllib.request` and `urllib.error` only -- NOT `urllib` whole.
    # `executor.py` imports `urllib.parse.quote` to escape a validated path
    # parameter, which is pure string manipulation and opens nothing. Banning
    # the whole package would have made this test red on landing for a reason
    # that has nothing to do with egress, and a test that has to be weakened on
    # its first run gets weakened wholesale.
    "requests",
    "urllib.request",
    "urllib.error",
    "smtplib",
    "subprocess",
    "socket",
    "aiohttp",
    # Provider SDKs (§11.1). `httpx` only; no vendor SDK is a dependency.
    "openai",
    "litellm",
    "langchain",
)


def test_no_source_file_imports_another_way_out_of_the_process() -> None:
    imported: list[str] = []
    for path in _source_files():
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if any(
                    name == bad or name.startswith(f"{bad}.") for bad in _FORBIDDEN_IMPORTS
                ):
                    imported.append(f"{_relative(path)}: import {name}")
    assert imported == [], "; ".join(imported)


def test_the_outbound_client_never_follows_a_redirect() -> None:
    """Pinned in the SOURCE as well as on the wire, because the wire test above
    can only see the stub it was given. `follow_redirects=True` anywhere in the
    outbound path turns a compromised or misconfigured provider into an
    arbitrary-host client."""
    source = (PACKAGE_ROOT / "providers" / "_http.py").read_text()
    assert "follow_redirects=False" in source
    assert "follow_redirects=True" not in source
