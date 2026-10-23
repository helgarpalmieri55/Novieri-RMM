"""Bearer tokens on browser WebSockets, carried in `Sec-WebSocket-Protocol`.

A browser cannot set an Authorization header on a WebSocket handshake, but the
subprotocol list it offers becomes a request header. Putting the token there
instead of the query string keeps it out of access logs, which record URLs
verbatim. Used by `/ws` and by the remote screen route.
"""

from fastapi import WebSocket

# The subprotocol the client offers first and the server echoes back. The
# token is the SECOND offered value; this name is the only thing that ever
# appears in a response header.
AUTH_SUBPROTOCOL = "bearer"


def token_from_subprotocols(websocket: WebSocket) -> str:
    """Recover the bearer token from the offered subprotocols, or "" .

    Exactly two values, `bearer` first: anything else is refused rather than
    searched. A permissive "find the JWT-looking one" reading would make the
    handshake's meaning depend on the shape of a credential, and the server
    must echo back exactly one of the values the client offered, so there is
    no ambiguity worth tolerating here.
    """
    # `scope["subprotocols"]` is typed loosely by the ASGI spec, so the values
    # are coerced here rather than trusted -- this is a credential read.
    offered = [str(value) for value in (websocket.scope.get("subprotocols") or [])]
    if len(offered) == 2 and offered[0] == AUTH_SUBPROTOCOL:
        return offered[1]
    return ""
