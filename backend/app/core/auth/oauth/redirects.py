"""Redirect URIs: which may be registered, and when a request's matches
(design §5.2; plan D8).

- RFC 3986 characters only: RFC 6749 §3.1.2 wants "an absolute URI as defined
  by [RFC3986]", and RFC 3986 §2 makes a URI of "a limited set of characters"
  (unreserved, reserved, and `%XX` escapes). So a host that only looks like
  another (`сlaude.ai`, with a Cyrillic "с") is not one the consent screen can
  show as `claude.ai` (plan B3-10), and a backslash, which a browser reads as
  the end of an https host, never registers;
- a host both readings agree on (`policy.valid_host`, re-exported here): a DNS
  name of LDH labels, a bracketed IPv6 literal or a canonical dotted-quad, and
  never a name a browser reads as an IPv4 number (`0x7f000001`, `127.1`);
- https to any host, or http to the machine itself. MCP: "All redirect URIs
  MUST be either localhost or use HTTPS" [S30];
- no fragment: RFC 6749 §3.1.2, "The endpoint URI MUST NOT include a fragment
  component";
- no response parameter in the query: OAuth 2.1-17 §2.3, "The query string
  component MUST NOT include any parameters defined for use in the
  authorization response";
- no user name or password, no `*`, at
  most `MAX_URI_LENGTH` characters;
- compared EXACTLY, as strings, but for the port of a loopback redirect
  (OAuth 2.1-17 §4.1.1: "simple string comparison … The only exception is
  loopback … variable port numbers"; RFC 8252 §7.3: "MUST allow any port to be
  specified at the time of the request for loopback IP redirect URIs").
  `localhost`, `127.0.0.1` and `[::1]` are each matched as registered, never as
  one another, and `localhost` is allowed although RFC 8252 §8.3 calls it
  "NOT RECOMMENDED": Claude Code registers it (plan B3-19, [S33]). The
  requested redirect is held to the same RFC 3986 characters first, and port 0
  never matches (`matches`; Task 8 review, fix round 1) nor registers
  (`check_registered`, fix round 2): the same rule at both ends.

The answer keeps the redirect's own query as written (OAuth 2.1-17 §2.3: "MUST
be retained when adding additional query parameters").
"""

import ipaddress
import re
from collections.abc import Mapping, Sequence
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.auth.oauth.policy import MAX_URI_LENGTH
from app.core.auth.oauth.policy import valid_host as valid_host

#: `urlsplit` gives an IPv6 literal without its brackets.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
#: The parameters of an authorization response (RFC 6749 §4.1.2, §4.1.2.1;
#: RFC 9207 §2), which a registered redirect's query may not hold.
RESPONSE_PARAMETERS = frozenset({"code", "state", "iss", "error", "error_description", "error_uri"})


#: RFC 3986's characters (unreserved, reserved, and `%` only as a `%XX`
#: escape). Not backslash, quotes, `< > ^ ` { | }` nor `*`: a browser's URL
#: parser reads some of those differently from `urlsplit` (a backslash ends an
#: https host).
_URI_CHARS = re.compile(r"(?:[A-Za-z0-9\-._~:/?#\[\]@!$&'()+,;=]|%[0-9A-Fa-f]{2})*")


def valid_uri_characters(uri: str) -> bool:
    """`uri` is made only of `_URI_CHARS`: the rule a registered redirect is
    held to, and a client metadata document URL too (`cimd`, Task 3 fix round
    1c)."""
    return _URI_CHARS.fullmatch(uri) is not None


def _bracketed_name(parts: SplitResult) -> bool:
    """The authority's host is bracketed but is not an IPv6 literal: an
    IPvFuture literal such as `[v1.claude.ai]` (RFC 3986 §3.2.2). `urlsplit`
    accepts it and unbrackets it into a name, `v1.claude.ai`; a WHATWG URL
    parser puts only an IPv6 address in brackets, so a browser never uses
    that name (Task 3 fix round 1c)."""
    return "[" in parts.netloc.rpartition("@")[2] and ":" not in (parts.hostname or "")


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
    if not valid_uri_characters(uri):
        raise RedirectRejected("malformed")  # i18n-exempt: a reason code
    if "#" in uri:
        raise RedirectRejected("fragment")  # i18n-exempt: a reason code
    parts = _split(uri)
    if parts.port == 0:  # no browser connects to it; `matches` refuses it too
        raise RedirectRejected("malformed")  # i18n-exempt: a reason code
    if parts.username is not None or parts.password is not None:
        raise RedirectRejected("credentials")  # i18n-exempt: a reason code
    names = {name for name, _ in parse_qsl(parts.query, keep_blank_values=True)}
    if names & RESPONSE_PARAMETERS:
        raise RedirectRejected("query")  # i18n-exempt: a reason code
    host = parts.hostname or ""
    if host and not valid_host(host):
        raise RedirectRejected("malformed")  # i18n-exempt: a reason code
    if _bracketed_name(parts):  # IPvFuture, `[v1.claude.ai]`
        raise RedirectRejected("malformed")  # i18n-exempt: a reason code
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
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return False


def _without_port(uri: str) -> tuple[str, str, str, str]:
    """Scheme and host AS WRITTEN (only the port goes), path and query."""
    parts = _split(uri)
    netloc = parts.netloc
    if parts.port is not None:
        netloc = netloc[: netloc.rfind(":")]
    return uri[: uri.index(":")], netloc, parts.path, parts.query


def _port_zero(uri: str) -> bool:
    """`uri` names port 0 (`:0`, `:00`, ...), which no browser connects to."""
    try:
        return _split(uri).port == 0
    except RedirectRejected:
        return False


def matches(registered: Sequence[str], requested: str) -> bool:
    """`requested` is one of `registered`, or the same loopback redirect on
    another port.

    `requested` is first held to the characters a registered redirect is
    (`valid_uri_characters`; Task 8 review, fix round 1): `urlsplit` silently
    drops TAB, CR and LF, so a loopback request with one inside compared
    equal to the registered shape, and the code was bound to that string
    while the answer (`with_params`, which splits it again) sent the browser
    to the one without it. Held to RFC 3986, "exact but for the port" is
    literally true. And port 0 never matches, registered or not: no browser
    connects to it, so a code bound to it could only be lost."""
    if not valid_uri_characters(requested) or _port_zero(requested):
        return False
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
    ASCII, because a registered redirect is (`check_registered`). Never a
    host a browser would not use, whatever the caller checked before: one
    that fails `valid_host`, or a bracketed one that is not IPv6
    (`_bracketed_name`), is "" (Task 3 fix round 1c)."""
    try:
        parts = _split(uri)
    except RedirectRejected:
        return ""
    host = parts.hostname or ""
    if not valid_host(host) or _bracketed_name(parts):
        return ""
    if ":" in host:
        return ipaddress.ip_address(host).compressed
    return host


def with_params(uri: str, params: Mapping[str, str]) -> str:
    """`uri` with `params` added after its own query, which is kept exactly as
    written."""
    parts = urlsplit(uri)
    added = urlencode(list(params.items()))
    query = f"{parts.query}&{added}" if parts.query else added
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
