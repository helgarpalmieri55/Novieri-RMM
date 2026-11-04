"""`/.well-known/jwks.json`: the public halves of the platform's token signing
key (design §6.3; plan Task 2).

PUBLIC BY NECESSITY, AND SAFE TO BE: a JWK Set holds public keys only -- each
`{kty, crv, x, y, kid, alg, use}`, never `d` -- and is how anyone who receives
a token this platform signed checks it (the assistant's internal token now,
MCP's access tokens in phase 3). Outside `/api/v1`, so it is neither an MCP
tool nor subject to the console's door; Traefik routes the exact path to the
api (`deploy/docker-compose.yml`).
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.core.auth import keys
from app.core.i18n import tr

router = APIRouter()

#: Long enough to spare OpenBao, short against the five-minute life of the
#: tokens it verifies: a rotation keeps the previous key in the set anyway.
CACHE_CONTROL = "public, max-age=300"


@router.get("/.well-known/jwks.json", include_in_schema=False)
async def jwks_document() -> JSONResponse:
    try:
        document = await keys.jwks()
    except keys.AuthKeysUnavailable as exc:
        raise HTTPException(
            503, tr("The signing keys are not available right now; try again in a moment.")
        ) from exc
    return JSONResponse(document, headers={"Cache-Control": CACHE_CONTROL})
