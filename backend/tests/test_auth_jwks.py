"""`GET /.well-known/jwks.json` (design §6.3; plan Task 2): public, serves
public keys only, verifies what the platform signs, and is reachable through
Traefik -- an exact path, so nothing else under `/.well-known/` moves."""

import time
from typing import Any

import httpx
import jwt

from app.core.auth import keys
from app.core.auth.keys import OpenBaoAuthKeys
from tests.test_deploy_keycloak_registration_edge import _winner


async def test_the_set_is_public_and_verifies_what_the_platform_signs(api_env: Any) -> None:
    """No header at all: no session, no token. `create_app` configured the
    local backend from `ca_dir`, which is also what `sign_jwt` uses."""
    response = await api_env.client.get("/.well-known/jwks.json")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "public, max-age=300"
    document = response.json()
    assert len(document["keys"]) == 1
    (key,) = document["keys"]
    assert set(key) == {"kty", "crv", "x", "y", "kid", "alg", "use"}  # never "d"

    now = int(time.time())
    token = await keys.sign_jwt({"sub": "u-1", "aud": "loopback", "exp": now + 300})
    public = jwt.PyJWKSet.from_dict(document)[jwt.get_unverified_header(token)["kid"]].key
    assert jwt.decode(token, public, algorithms=["ES256"], audience="loopback")["sub"] == "u-1"


async def test_an_unreachable_openbao_is_a_503(api_env: Any) -> None:
    def sealed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"errors": ["Vault is sealed"]})

    keys.set_backend(
        OpenBaoAuthKeys(
            "https://bao.test",
            "t",
            client=httpx.AsyncClient(transport=httpx.MockTransport(sealed)),
        )
    )
    try:
        response = await api_env.client.get(
            "/.well-known/jwks.json", headers={"Accept-Language": "es"}
        )
    finally:
        keys.set_backend(None)  # process-wide: never leave it installed
    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Las claves de firma no están disponibles ahora mismo; vuelve a intentarlo en un "
        "momento."
    )


def test_traefik_sends_the_set_to_the_api_and_nothing_else_moves() -> None:
    assert _winner("/.well-known/jwks.json") == "api"
    assert _winner("/.well-known/jwks.json.bak") != "api"
    assert _winner("/.well-known/security.txt") != "api"
