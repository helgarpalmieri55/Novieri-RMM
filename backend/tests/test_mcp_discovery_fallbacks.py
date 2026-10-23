"""The discovery fallbacks Traefik routes to the api answer 404 JSON.

An MCP client given `authorization_servers: [".../realms/it-suite"]` tries
`/.well-known/oauth-authorization-server/realms/it-suite` and
`/.well-known/openid-configuration/realms/it-suite` before the realm's own
documents, moves on after a 4xx, and gives up on a 200 it cannot parse.
`deploy/docker-compose.yml` routes both prefixes to the api for exactly this
(`test_deploy_mcp_route.py` holds the routing); this holds the api's half:
they are a JSON 404, never the console's 200 page.
"""

import pytest

PATHS = (
    "/.well-known/oauth-authorization-server/realms/it-suite",
    "/.well-known/openid-configuration/realms/it-suite",
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
)


@pytest.mark.parametrize("path", PATHS)
async def test_a_discovery_fallback_is_a_json_404(api_env, path: str) -> None:
    api_env.settings.public_console_url = "https://itms.example.com"

    resp = await api_env.client.get(path)

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"detail": "Not Found"}
