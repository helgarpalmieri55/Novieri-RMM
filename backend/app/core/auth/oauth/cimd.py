"""OAuth Client ID Metadata Documents (autenticación propia design §5.5; MCP
[S29]; draft-ietf-oauth-client-id-metadata-document-03, the current revision,
https://www.ietf.org/archive/id/draft-ietf-oauth-client-id-metadata-document-03.txt,
followed where it is stricter than the -00 MCP cites; plan B3-14).

A client that names itself by an https URL instead of registering. Accepted
whenever MCP is on (operator decision C2, 2026-11-06): no opt-in, because the
document is read only inside a signed-in person's consent (Task 8), through
`egress.get_bounded`, the SSRF guard (design §5.5; CIMD-03 §10.7). It is used
only when:

- the URL is a client identifier CIMD-03 §3 allows -- https, a path other than
  "/", no dot segment, no fragment, no user name or password -- with no query
  (§3: "SHOULD NOT"), made of a redirect's characters and with a host that
  passes `valid_host` (as a redirect, Task 1; `is_document_client_id`);
- the answer is a 200 JSON object of at most 5 KiB (§10.11: "The recommended
  maximum size to read is 5 kilobytes."), not compressed (Task 3, decision
  4), whatever its content type (below);
- its `client_id` is the URL, exactly ("MUST match the URL of the document
  using simple string comparison"; MCP: "MUST validate" [S29]);
- it has `client_name` and `redirect_uris` (MCP: "MUST include at least the
  following properties: client_id, client_name, redirect_uris" [S29]), each
  redirect one this server would register (`redirects.check_registered`);
- its `grant_types` and `response_types`, RFC 7591's defaults when absent,
  meet this server's: CIMD-03 §6, "MUST filter … and find the intersection …
  If the intersection of either of these two properties is empty, the
  Authorization Server MUST reject the request with invalid_client". An
  intersection without `authorization_code` is refused too;
- it names no shared secret: `token_endpoint_auth_method` absent or `none`,
  no `client_secret`, no `client_secret_expires_at`. This server has public
  clients only (plan D4, B3-20), so `private_key_jwt` is refused too.

THE CONTENT TYPE IS NOT JUDGED (Task 3 fix round 1). CIMD-03, at the URL
above, asks for a JSON document, not for a media type. §4: "The Client ID
Metadata Document is a JSON ([RFC8259]) document containing the metadata of
the client." and "The Client ID Metadata Document MAY also be served with
more specific content types as long as the response is JSON and conforms to
application/<AS-defined>+json." §6 asks only the authorization server for a
header: "The Authorization Server SHOULD send an Accept header of
application/json as described in Section 12.5.1 of [RFC9110] when fetching
the client metadata." `_read` sends it. No sentence of the draft requires a
Content-Type, so what the body parses to decides, and `EgressBody.
content_type` is not read here.

CACHED FOR `clamp(max-age or 0, 5 min, 24 h)`, WHATEVER ELSE THE HEADERS SAY
(plan B3-03; CIMD-03 §6.2: "SHOULD enforce a maximum effective cache duration
regardless of the cache directives … SHOULD also enforce a minimum effective
cache duration when none is otherwise indicated", and "MAY implement rate
limiting … even if that results in fetching the URL less often than requested
by the cache headers"). A refused document is never cached: the caller stores
only what `fetch_document` returned (`clients.resolve_client`, Task 6).

A TRANSIENT FAILURE IS NOT A REFUSAL (CIMD-03 §6.1: "A transient failure
SHOULD NOT by itself invalidate a Client"): `unavailable` -- a timeout, a
network error, a 5xx or a 429 -- is `transient`, and the caller keeps a valid
copy it already has. Everything else is for good.

A REFUSAL IS ONE LOG LINE, the reason and the host -- never the URL's path,
never the body -- at most one a minute per process, saying how many were held
(`loopback.RefusalThrottle`, the C-M8 precedent; plan B3-32).
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from urllib.parse import SplitResult, unquote, urlsplit

import httpx

from app.core import egress
from app.core.auth.loopback import RefusalThrottle
from app.core.auth.oauth.policy import (
    GRANT_TYPES,
    MAX_CLIENT_ID,
    MAX_REDIRECT_URIS,
    RESPONSE_TYPES,
    display_name,
)
from app.core.auth.oauth.redirects import (
    RedirectRejected,
    check_registered,
    host_of,
    valid_uri_characters,
)

logger = logging.getLogger("its.auth.oauth.cimd")

MAX_DOCUMENT_BYTES = 5 * 1024
FETCH_TIMEOUT_S = 5.0
MIN_CACHE = timedelta(minutes=5)
MAX_CACHE = timedelta(hours=24)
_MIN_CACHE_S = int(MIN_CACHE.total_seconds())
_MAX_CACHE_S = int(MAX_CACHE.total_seconds())
#: RFC 9111 §1.2.2's delta-seconds, ASCII digits only (`cache_lifetime`).
_DELTA_SECONDS = re.compile(r"[0-9]+")
#: Significant digits `cache_lifetime` parses: ten hold 9,999,999,999 s, far
#: past `MAX_CACHE`. A longer value is `_SATURATED`, RFC 9111 §1.2.2's 2^31.
_MAX_DIGITS = 10
_SATURATED = 2**31

Reason = Literal[
    "not_a_document_url",
    "own_origin",
    "blocked",
    "unavailable",
    "not_ok",
    "too_large",
    "encoded",
    "not_json",
    "mismatch",
    "incomplete",
    "bad_redirect",
    "secret",
    "unsupported",
]
#: The reasons that say nothing about the document, only about now.
TRANSIENT: frozenset[Reason] = frozenset({"unavailable"})

#: Per process, as `loopback._network_refusals`.
_refusals = RefusalThrottle()


class DocumentRefused(Exception):
    """The document cannot be this client's. `reason` is a code, never shown;
    `transient` says a later attempt may succeed."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason

    @property
    def transient(self) -> bool:
        return self.reason in TRANSIENT


@dataclass(frozen=True)
class ClientDocument:
    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    cache_for: timedelta


def is_document_client_id(client_id: str) -> bool:
    """CIMD-03 §3, held to a redirect's rules: no query, and a host every
    reader reads alike.

    - Every character is one `check_registered` allows
      (`redirects.valid_uri_characters`): RFC 3986's, `%` only as a `%XX`
      escape, and no `*` (fix round 1c).
    - The host is `redirects.host_of`'s, the same `urlsplit` reading the
      consent screen names, so it passes `valid_host` (Task 1) and is never a
      bracketed name such as the IPvFuture `[v1.claude.ai]`: no backslash,
      `%` escape, quote, `_` or sub-delimiter, and no number a browser reads
      as an IPv4 address (`0x7f000001`) (fix rounds 1b and 1c)."""
    if not isinstance(client_id, str) or len(client_id) > MAX_CLIENT_ID:
        return False
    if not valid_uri_characters(client_id):
        return False
    if "#" in client_id or "?" in client_id:
        return False
    try:
        parts = urlsplit(client_id)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname:
        return False
    if not host_of(client_id):
        return False
    if parts.username is not None or parts.password is not None:
        return False
    if parts.path in ("", "/"):
        return False
    return not any(unquote(segment) in (".", "..") for segment in parts.path.split("/"))


def on_origin(client_id: str, issuer: str) -> bool:
    """`client_id` is on the issuer's own origin (CIMD-03 §10.1): the MCP
    resource is, and so is anything else this server answers."""
    try:
        mine, theirs = urlsplit(issuer), urlsplit(client_id)
        return _origin(theirs) == _origin(mine)
    except ValueError:
        return False


def _origin(parts: SplitResult) -> tuple[str, str, int]:
    """Scheme, host without its root dot, and port with https's default
    filled in: `https://host:443/` and `https://host./` are `https://host/`."""
    return parts.scheme, (parts.hostname or "").rstrip("."), parts.port or 443


def cache_lifetime(cache_control: str) -> timedelta:
    """How long a valid document is reused (module docstring): a `timedelta`
    in [`MIN_CACHE`, `MAX_CACHE`] whatever the document's server sent, and
    never an exception (Task 3 fix round 1).

    A max-age is RFC 9111 §1.2.2's `delta-seconds = 1*DIGIT`, DIGIT being
    ASCII (RFC 5234): `str.isdigit` also accepts "²", which `int` then
    refuses. One too large to hold is saturated, never parsed, as §1.2.2
    requires: "the cache MUST consider the value to be 2147483648 (2^31) or
    the greatest positive integer it can conveniently represent", which the
    clamp makes the ceiling. The clamp is applied to the integer, before the
    `timedelta` exists."""
    seconds = 0
    for part in cache_control.split(","):
        name, _, value = part.strip().partition("=")
        if name.strip().lower() == "max-age":
            raw = value.strip().strip('"')
            if _DELTA_SECONDS.fullmatch(raw):
                digits = raw.lstrip("0") or "0"
                seconds = int(digits) if len(digits) <= _MAX_DIGITS else _SATURATED
            else:
                seconds = 0
    return timedelta(seconds=min(max(seconds, _MIN_CACHE_S), _MAX_CACHE_S))


def _types(document: dict[str, object], name: str, default: tuple[str, ...]) -> set[str]:
    value = document.get(name, list(default))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DocumentRefused("incomplete")  # i18n-exempt: a reason code
    return set(value)


async def _read(client_id: str) -> ClientDocument:
    if not is_document_client_id(client_id):
        raise DocumentRefused("not_a_document_url")  # i18n-exempt: a reason code
    try:
        answer = await egress.get_bounded(
            client_id,
            max_bytes=MAX_DOCUMENT_BYTES,
            timeout_s=FETCH_TIMEOUT_S,
            headers={"Accept": "application/json"},
        )
    except egress.EgressBlocked as exc:
        # A name that did not resolve retrieved nothing: transient (CIMD-03
        # §6.1, "a network error or timeout"); anything else the guard refused
        # is for good (re-review f3-1, Minor 1).
        reason: Reason = "unavailable" if exc.reason == "dns_failure" else "blocked"
        raise DocumentRefused(reason) from exc  # i18n-exempt: a reason code
    except (httpx.HTTPError, TimeoutError, OSError) as exc:
        raise DocumentRefused("unavailable") from exc  # i18n-exempt: a reason code
    if answer.status_code >= 500 or answer.status_code == 429:
        raise DocumentRefused("unavailable")  # i18n-exempt: a reason code
    if answer.status_code != 200:
        raise DocumentRefused("not_ok")  # i18n-exempt: a reason code
    if answer.truncated:
        raise DocumentRefused("too_large")  # i18n-exempt: a reason code
    if answer.content_encoding not in ("", "identity"):
        raise DocumentRefused("encoded")  # i18n-exempt: a reason code
    try:
        document = json.loads(answer.content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DocumentRefused("not_json") from exc  # i18n-exempt: a reason code
    if not isinstance(document, dict):
        raise DocumentRefused("not_json")  # i18n-exempt: a reason code
    if document.get("client_id") != client_id:
        raise DocumentRefused("mismatch")  # i18n-exempt: a reason code
    name = document.get("client_name")
    uris = document.get("redirect_uris")
    shown = display_name(name) if isinstance(name, str) else None
    if shown is None or not isinstance(uris, list) or not 1 <= len(uris) <= MAX_REDIRECT_URIS:
        raise DocumentRefused("incomplete")  # i18n-exempt: a reason code
    try:
        checked = tuple(dict.fromkeys(check_registered(uri) for uri in uris))
    except RedirectRejected as exc:
        raise DocumentRefused("bad_redirect") from exc  # i18n-exempt: a reason code
    if document.get("token_endpoint_auth_method", "none") != "none" or (
        "client_secret" in document or "client_secret_expires_at" in document
    ):
        raise DocumentRefused("secret")  # i18n-exempt: a reason code
    grants = _types(document, "grant_types", ("authorization_code",))
    responses = _types(document, "response_types", ("code",))
    if "authorization_code" not in grants & set(GRANT_TYPES) or not responses & set(
        RESPONSE_TYPES
    ):
        raise DocumentRefused("unsupported")  # i18n-exempt: a reason code
    return ClientDocument(
        client_id=client_id,
        client_name=shown,
        redirect_uris=checked,
        cache_for=cache_lifetime(answer.cache_control),
    )


async def fetch_document(client_id: str) -> ClientDocument:
    """The client `client_id` names, or `DocumentRefused` (logged, throttled)."""
    try:
        return await _read(client_id)
    except DocumentRefused as refused:
        unlogged = _refusals.admit()
        if unlogged is not None:
            try:
                host = urlsplit(client_id).hostname or ""
            except ValueError:
                host = ""
            logger.warning(
                "client metadata document refused",
                extra={"reason": refused.reason, "host": host, "unlogged": unlogged},
            )
        raise
