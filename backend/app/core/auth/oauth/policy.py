"""The OAuth server's numbers and addresses (autenticación propia design §5.1-§5.4).

Every number is the design's, or a rule's (plan Appendix B):

- an access token lives 10 minutes (§5.3) and an authorization code 60
  seconds (§5.2);
- a refresh token's family dies after 30 days without use, and so does a
  registration nobody used (§5.3, §5.4; RFC 7591 lets a server "expire
  registrations for existing clients meeting certain criteria" [S19]);
- registration is limited (B3-02; RFC 7591 §3: "MAY be rate-limited or
  otherwise limited"): 20 an hour from one network, an IPv6 network being its
  /64, and at most 200 live registrations, the cap the Keycloak realm had
  (MCP design §3.3, "Max Clients: 200");
- the token and revocation endpoints are limited per network as well (B3-24;
  RFC 7009 §5), generously: hosted AI apps all call from their provider's
  addresses, and the limit is against floods, not guesses;
- the only scope is `mcp`, the one the protected-resource metadata has always
  named (`app/mcp/server.py`). `offline_access` is advertised beside it
  because Claude asks for a refresh token only when it is (plan D7, [S33]).

THE ISSUER IS THE CONSOLE'S ADDRESS (plan D1): `ITS_PUBLIC_CONSOLE_URL` with
no trailing slash, the same base `mcp_resource` puts `/mcp` after. RFC 8414
§2 wants "a URL that uses the "https" scheme and has no query or fragment
components", RFC 9207 §2 the same of `iss`; a console address with a path
would move the metadata to a path-inserted URL Traefik does not route. So an
address that is not https, has a path, a query, a fragment or a user name
names no issuer, and every OAuth route and `/mcp` answer 503 (B3-18).
"""

import ipaddress
from datetime import timedelta
from urllib.parse import urlsplit

ACCESS_TOKEN_TTL_S = 600
CODE_TTL_S = 60
REFRESH_IDLE = timedelta(days=30)
CLIENT_IDLE = timedelta(days=30)
SCOPE = "mcp"
SCOPES_SUPPORTED = (SCOPE, "offline_access")
GRANT_TYPES = ("authorization_code", "refresh_token")
RESPONSE_TYPES = ("code",)
#: RFC 9068 §2.1: "the "typ" value used SHOULD be "at+jwt"" (plan D5).
ACCESS_TYP = "at+jwt"

AUTHORIZE_PATH = "/oauth/authorize"
TOKEN_PATH = "/oauth/token"
REGISTER_PATH = "/oauth/register"
REVOKE_PATH = "/oauth/revoke"
METADATA_PATH = "/.well-known/oauth-authorization-server"
JWKS_PATH = "/.well-known/jwks.json"

#: What a registration may hold (plan D9).
MAX_REDIRECT_URIS = 10
MAX_URI_LENGTH = 2000
MAX_CLIENT_NAME = 200
MAX_CLIENT_ID = 255
#: Registrations one network may make per hour, and live registrations one
#: organization may hold (B3-02).
REGISTRATIONS_PER_HOUR = 20
MAX_REGISTRATIONS = 200
#: The prefix one IPv6 network is counted by (B3-02): a host owns its /64.
IPV6_NETWORK_BITS = 64
#: Requests to the token and revocation endpoints one network may make per
#: minute (B3-24).
TOKEN_REQUESTS_PER_MINUTE = 600
#: Bytes a registration, token or revocation request may carry.
MAX_REQUEST_BYTES = 16 * 1024


def issuer(settings: object) -> str | None:
    """The issuer identifier, or None when the console address cannot be one."""
    raw = (getattr(settings, "public_console_url", None) or "").strip().rstrip("/")
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname:
        return None
    if parts.path or parts.query or parts.fragment or parts.username is not None:
        return None
    return raw


def endpoint(settings: object, path: str) -> str | None:
    """`issuer + path`, or None with no issuer."""
    base = issuer(settings)
    return None if base is None else base + path


def registration_open(settings: object) -> bool:
    """`ITS_MCP_REGISTRATION=open`, the exact word: the operator's opt-in to
    anonymous dynamic registration (`POST /oauth/register`), and to nothing
    else. A client metadata document needs no opt-in: it is read only while
    MCP is on, inside a signed-in person's consent (operator decision C2,
    2026-11-06; plan D15)."""
    return (getattr(settings, "mcp_registration", "") or "").strip() == "open"


def network_of(address: ipaddress.IPv4Address | ipaddress.IPv6Address | None) -> str:
    """The network a per-address limit counts (B3-02): an IPv4 address as
    itself, an IPv6 one by the /64 it belongs to -- a host owns its whole /64
    and could otherwise turn one bucket into 2^64 -- and "unknown" without
    one, the bucket every unplaceable request shares."""
    if address is None:
        return "ip:unknown"
    if address.version == 6:
        network = ipaddress.ip_network(f"{address}/{IPV6_NETWORK_BITS}", strict=False)
        return f"net:{network}"
    return f"ip:{address}"


def display_name(raw: str) -> str | None:
    """A client's name as a person may be shown it: printable text, runs of
    white space as one space, at most `MAX_CLIENT_NAME` characters; None when
    nothing is left. Still the app's own claim (RFC 7591 §5): the consent
    screen names the host first (plan B3-10)."""
    printable = "".join(
        " " if ch.isspace() else ch for ch in raw if ch.isprintable() or ch.isspace()
    )
    text = " ".join(printable.split())[:MAX_CLIENT_NAME].strip()
    return text or None
