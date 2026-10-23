"""Traefik routes the MCP server to the api, and nothing else is stolen by it.

MCP spec 2026-10-09 §2. The api serves `/mcp` (the protocol endpoint) and
`/.well-known/oauth-protected-resource[/mcp]` (RFC 9728 metadata, which names
the realm as the authorization server). Neither lives under `/api`, so the api
router has to name them; without that Traefik hands both to `web`, whose nginx
answers every unknown path with the SPA's `index.html` and a 200.

THAT 200 IS ALSO WHY THE DISCOVERY FALLBACKS ARE ROUTED. An MCP client given
`authorization_servers: ["https://host/realms/it-suite"]` tries RFC 8414's
path-insertion form first -- `/.well-known/oauth-authorization-server/realms/
it-suite` -- then OIDC's `/.well-known/openid-configuration/realms/it-suite`,
and only then the realm's own `/realms/it-suite/.well-known/...`. Clients move
on after a 4xx; given a 200 they parse the body, and an HTML page fails that
parse and ends the sign-in. Routed to the api, both answer FastAPI's 404 and the
client falls through to Keycloak's real document.

The router model (priority, then first match) is the one
`test_deploy_keycloak_registration_edge.py` already holds to the compose file.
"""

import re
from pathlib import Path

import yaml

from app.mcp.invoke import TOOL_TIMEOUT_S
from app.mcp.server import _SHUTDOWN_GRACE_S
from tests.test_deploy_keycloak_registration_edge import _winner

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "deploy" / "docker-compose.yml"
ROUTES = REPO / "frontend" / "scripts" / "routes.mjs"

#: What the api must receive, and what the SPA must never be asked for.
MCP_PATHS = (
    "/mcp",
    "/mcp/",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    # The two discovery forms that sort before the realm's own (see above).
    "/.well-known/oauth-authorization-server/realms/it-suite",
    "/.well-known/openid-configuration/realms/it-suite",
)


def _api() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]["api"]


def test_the_api_router_names_the_mcp_prefixes() -> None:
    rule = str(_api()["labels"]["traefik.http.routers.api.rule"])
    for prefix in (
        "/mcp",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
        "/.well-known/openid-configuration",
    ):
        assert f"PathPrefix(`{prefix}`)" in rule, prefix
    # And the existing three are still there.
    for prefix in ("/api", "/enroll/", "/i/"):
        assert f"PathPrefix(`{prefix}`)" in rule, prefix


def test_the_mcp_paths_reach_the_api_and_not_the_spa() -> None:
    for path in MCP_PATHS:
        assert _winner(path) == "api", (path, _winner(path))


def test_the_realm_keeps_its_own_discovery_documents() -> None:
    """The realm's documents live under `/realms/<realm>/.well-known/`, which
    begins `/realms` -- no new prefix can take them."""
    for path in (
        "/realms/it-suite/.well-known/openid-configuration",
        "/realms/it-suite/.well-known/oauth-authorization-server",
        "/realms/it-suite/protocol/openid-connect/auth",
        "/realms/it-suite/clients-registrations/openid-connect",
    ):
        assert _winner(path) in {"kc", "kc-registration-oidc"}, (path, _winner(path))


def test_the_console_keeps_its_pages() -> None:
    for path in ("/", "/devices", "/settings", "/settings/ai-connections", "/auth/callback"):
        assert _winner(path) == "web", (path, _winner(path))


def test_the_console_has_no_route_the_new_prefixes_would_steal() -> None:
    """`PathPrefix` is a raw string prefix (see the `/enroll/` comment in the
    compose file): `/mcp` would also take a console page called `/mcp-anything`."""
    routes = ROUTES.read_text()
    assert not re.search(r"""["']/mcp""", routes)
    assert not re.search(r"""["']/\.well-known""", routes)


def _seconds(duration: str) -> float:
    match = re.fullmatch(r"(\d+)(s|m)", duration)
    assert match, duration
    return int(match.group(1)) * (60 if match.group(2) == "m" else 1)


def test_the_api_is_given_longer_to_stop_than_a_running_write() -> None:
    """A write tool call can hold its request for up to `TOOL_TIMEOUT_S`
    before the server's lifespan shutdown even begins, and THEN gets
    `_SHUTDOWN_GRACE_S` to finish. Docker's default stop is 10 s and then
    SIGKILL, which would cut that write off part-way."""
    grace = _api().get("stop_grace_period")
    assert grace is not None, "api has no stop_grace_period: Docker kills it at 10 s"
    assert _seconds(str(grace)) >= TOOL_TIMEOUT_S + _SHUTDOWN_GRACE_S + 5
