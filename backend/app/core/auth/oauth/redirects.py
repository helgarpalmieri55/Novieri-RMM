"""Redirect URIs: which may be registered, and when a request's matches
(design §5.2; plan D8).

- ASCII only: RFC 6749 §3.1.2 wants "an absolute URI as defined by [RFC3986]",
  and RFC 3986 §2 makes a URI of "a limited set of characters". So a host that
  only looks like another (`сlaude.ai`, with a Cyrillic "с") is not one the
  consent screen can show as `claude.ai` (plan B3-10);
- https to any host, or http to the machine itself. MCP: "All redirect URIs
  MUST be either localhost or use HTTPS" [S30];
- no fragment: RFC 6749 §3.1.2, "The endpoint URI MUST NOT include a fragment
  component";
- no response parameter in the query: OAuth 2.1-17 §2.3, "The query string
  component MUST NOT include any parameters defined for use in the
  authorization response";
- no user name or password, no `*`, no white space or control character, at
  most `MAX_URI_LENGTH` characters;
- compared EXACTLY, as strings, but for the port of a loopback redirect
  (OAuth 2.1-17 §4.1.1: "simple string comparison … The only exception is
  loopback … variable port numbers"; RFC 8252 §7.3: "MUST allow any port to be
  specified at the time of the request for loopback IP redirect URIs").
  `localhost`, `127.0.0.1` and `[::1]` are each matched as registered, never as
  one another, and `localhost` is allowed although RFC 8252 §8.3 calls it
  "NOT RECOMMENDED": Claude Code registers it (plan B3-19, [S33]).

The answer keeps the redirect's own query as written (OAuth 2.1-17 §2.3: "MUST
be retained when adding additional query parameters").
"""

import ipaddress
from collections.abc import Mapping, Sequence
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.auth.oauth.policy import MAX_URI_LENGTH

#: `urlsplit` gives an IPv6 literal without its brackets.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
#: The parameters of an authorization response (RFC 6749 §4.1.2, §4.1.2.1;
#: RFC 9207 §2), which a registered redirect's query may not hold.
RESPONSE_PARAMETERS = frozenset(
    {"code", "state", "iss", "error", "error_description", "error_uri"}
)


class RedirectRejected(ValueError):
    """Why a redirect cannot be registered: `malformed`, `fragment`,
    `credentials`, `query` or `scheme`. A reason code, never shown to a
    person."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _split(uri: str) -> SplitResult:
    try:
        parts = urlsplit(uri)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError as exc:
        raise RedirectRejected("malformed") from exc  # i18n-exempt: a reason code
    return parts


def check_registered(uri: object) -> str:
    """`uri` unchanged when it may be registered; `RedirectRejected` if not."""
    if not isinstance(uri, str) or not uri or len(uri) > MAX_URI_LENGTH:
        raise RedirectRejected("malformed")  # i18n-exempt: a reason code
    if any(ord(ch) <= 0x20 or ord(ch) > 0x7E for ch in uri) or "*" in uri:
        raise RedirectRejected("malformed")  # i18n-exempt: a reason code
    if "#" in uri:
        raise RedirectRejected("fragment")  # i18n-exempt: a reason code
    parts = _split(uri)
    if parts.username is not None or parts.password is not None:
        raise RedirectRejected("credentials")  # i18n-exempt: a reason code
    names = {name for name, _ in parse_qsl(parts.query, keep_blank_values=True)}
    if names & RESPONSE_PARAMETERS:
        raise RedirectRejected("query")  # i18n-exempt: a reason code
    host = parts.hostname or ""
    if parts.scheme == "https" and host:
        return uri
    if parts.scheme == "http" and host in LOOPBACK_HOSTS:
        return uri
    raise RedirectRejected("scheme")  # i18n-exempt: a reason code


def is_loopback(uri: str) -> bool:
    """http to `127.0.0.1`, `[::1]` or `localhost`: a redirect whose port may
    vary."""
    try:
        parts = _split(uri)
    except RedirectRejected:
        return False
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS


def stays_on_this_computer(uri: str) -> bool:
    """The answer goes to the person's own machine, whatever the scheme and
    however it is spelled -- `localhost` (with or without its root dot) or
    any loopback address, `127.0.0.0/8` and `::1`: the consent screen's
    warning (MCP [S30]: "SHOULD display additional warnings for
    localhost-only redirect URIs")."""
    host = host_of(uri).rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _without_port(uri: str) -> tuple[str, str, str, str]:
    """Scheme and host AS WRITTEN (only the port goes), path and query."""
    parts = _split(uri)
    netloc = parts.netloc
    if parts.port is not None:
        netloc = netloc[: netloc.rfind(":")]
    return uri[: uri.index(":")], netloc, parts.path, parts.query


def matches(registered: Sequence[str], requested: str) -> bool:
    """`requested` is one of `registered`, or the same loopback redirect on
    another port."""
    if requested in registered:
        return True
    if not is_loopback(requested) or "#" in requested:
        return False
    want = _split(requested)
    if want.username is not None or want.password is not None:
        return False
    shape = _without_port(requested)
    return any(is_loopback(uri) and _without_port(uri) == shape for uri in registered)


def host_of(uri: str) -> str:
    """The host name, as the consent screen shows it ("" if there is none).
    ASCII, because a registered redirect is (`check_registered`)."""
    try:
        return _split(uri).hostname or ""
    except RedirectRejected:
        return ""


def with_params(uri: str, params: Mapping[str, str]) -> str:
    """`uri` with `params` added after its own query, which is kept exactly as
    written."""
    parts = urlsplit(uri)
    added = urlencode(list(params.items()))
    query = f"{parts.query}&{added}" if parts.query else added
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
