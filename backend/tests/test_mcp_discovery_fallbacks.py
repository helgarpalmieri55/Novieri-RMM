"""The discovery fallbacks Traefik routes to the api answer 404 JSON, and the
platform's own authorization server metadata is the document.

Until own authentication phase 3, `authorization_servers` named the realm, and
an MCP client tried `/.well-known/oauth-authorization-server/realms/it-suite`
and `/.well-known/openid-configuration/realms/it-suite` before the realm's
own documents: both had to be a JSON 404, never the console's 200 page. Since
phase 3 the platform is its own issuer at the console's address (plan
2026-11-06, D1), so `/.well-known/oauth-authorization-server` at the root is
its RFC 8414 document; the other three stay a JSON 404, because a client
that tries them first must still move on rather than parse the console's
HTML. `test_deploy_mcp_route.py` holds the routing.
"""

import pytest

FALLBACKS = (
    "/.well-known/oauth-authorization-server/realms/it-suite",
    "/.well-known/openid-configuration/realms/it-suite",
    "/.well-known/openid-configuration",
)


@pytest.mark.parametrize("path", FALLBACKS)
async def test_a_discovery_fallback_is_a_json_404(api_env, path: str) -> None:
    api_env.settings.public_console_url = "https://itms.example.com"

    resp = await api_env.client.get(path)

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"detail": "Not Found"}


async def test_the_root_metadata_is_the_platforms_own_document(api_env) -> None:
    api_env.settings.public_console_url = "https://itms.example.com"

    resp = await api_env.client.get("/.well-known/oauth-authorization-server")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["issuer"] == "https://itms.example.com"
