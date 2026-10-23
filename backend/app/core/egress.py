"""Outbound egress guard for webhook delivery (spec §4).

WHY THIS EXISTS. A per-org, admin-configurable webhook URL is a server-side
request forgery primitive by construction. This control plane is
domain-admin-equivalent over a fleet and runs on-prem, next to the agent
gateway, the vault, and the database. Without this guard, anyone holding
`notification.admin` can aim the server at 169.254.169.254, at the gateway, or
at any host on the customer's LAN, and read the outcome back through delivery
status and timing.

`app/core/http.py`, despite its name, is INBOUND request-body bounding. It is
not related to this module and does not guard anything outbound.

Five properties, each of which has a specific bypass it prevents:

1. Resolve ALL records and reject if ANY is denied. Checking only the first
   record lets a multi-record DNS answer walk past.
2. Connect to the VALIDATED IP, never re-resolve. Re-resolution is the
   DNS-rebinding hole: validate a public answer, connect to a private one.
3. Refuse redirects outright. A followed redirect is a second request to an
   unvalidated destination; per-hop re-validation is easy to get subtly
   wrong.
4. Never return the response body in full, and never read more than
   `_BODY_MAX_BYTES` off the wire while producing it. Truncating an
   already-fully-buffered body is not a read bound -- it still lets a
   compromised or merely slow endpoint hand back gigabytes and OOM the
   process before the excerpt is ever sliced. The read must stop early, not
   just get trimmed afterwards.
5. Deadline the whole exchange, not just its parts. httpx's per-phase
   timeouts (connect/read/write) each reset on progress, so a server that
   trickles one byte just under the read-timeout window forever never trips
   them -- exactly the "endpoint that trickles bytes must not pin a worker"
   case this module exists to close. A hard wall-clock deadline around the
   whole request is the only thing that actually bounds it.

Do NOT route SMTP through this guard -- see the plan's "Decided asymmetry".

TASK 4B ADDITIONS.

- The allowlist now names a HOST AND PORT together (`allowlisted_hosts:
  set[tuple[str, int]]`, migration 0026), not a bare host. A host allowlisted
  for one port is not thereby reachable on a different, unlisted one -- see
  the port-scope note on `post_json`.
- Spec §4.1's "the platform's own gateway/vault/database hosts read from
  config" is enforced unconditionally, by NAME, before the allowlist is ever
  consulted -- see `_platform_entries`. A deployment must not be able to
  allowlist its own vault.

S1/S2 FIX (platform-host denial by ADDRESS+PORT, not just by name).

Two real-world bugs, found by running the module, not by the test suite:

- S1 (too broad): the by-name check used to ignore port entirely, so a
  single-box on-prem deployment -- where the database, vault, gateway and an
  internal webhook receiver all share one hostname -- could never be
  notified on ANY port, even one an admin explicitly allowlisted.
- S2 (too narrow): the by-name check is defeated by reaching the identical
  address under a different name (a CNAME, an alias, a raw IP literal where
  the platform host was configured as a name, or vice versa).

Both share one root cause: matching on the hostname STRING alone. The fix is
two cooperating layers, both unconditional -- neither can be satisfied by an
allowlist entry naming the platform service's own port:

  LAYER 1 -- `_platform_layer1_denies` (by name, no DNS). Cheap, and the
  fail-closed backstop when resolution is unavailable: it needs no network
  access at all, so a platform host stays refused by its configured name
  even during a total DNS outage. A request naming the platform host's exact
  configured hostname is refused UNLESS an allowlist entry names that exact
  (host, port) pair AND that port is not the platform service's own port (a
  port that could not be derived for a setting is treated as "the platform's
  own port" for every port, wildcard -- see `_platform_entries` -- so that
  setting's host stays refused on every port, unchanged from before this
  fix). This is what lets a different, genuinely unrelated service on the
  same hostname become reachable on its own allowlisted port (S1), while an
  allowlist entry can never grant the platform service's own address+port.

  LAYER 2 -- `_platform_layer2_denies` (by resolved address, after DNS). Runs
  on the CONNECTED (pinned) address and the destination port, regardless of
  the name used to reach it and regardless of allowlisting (S2). Each
  platform setting's own host is resolved -- cached, with a modest TTL so a
  re-pointed platform host is picked up -- and compared against the pinned
  address. A resolution failure for a platform host is never cached and
  never treated as a match; that host's Layer-2 protection is simply
  unavailable until resolution recovers, which is safe because Layer 1 does
  not depend on resolution at all for the platform host's own name.
"""

import asyncio
import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.config import get_settings

_EXCERPT_MAX = 500
_BODY_MAX_BYTES = 64 * 1024
_CONNECT_TIMEOUT_S = 10.0

_DENIED_V4 = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # includes cloud metadata
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    # Whole-branch review finding M4 (low practical value, added for
    # completeness): 240.0.0.0/4 is IANA "reserved for future use" and also
    # covers 255.255.255.255 (limited broadcast) -- both resolved past the
    # guard and reached the connect stage before this fix.
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("192.0.0.0/24"),  # IETF protocol assignments (RFC 6890)
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking (RFC 2544)
]
_DENIED_V6 = [
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("::/128"),
    # Whole-branch review finding M4: Teredo (RFC 4380) is the fourth
    # RFC-recognised IPv4-in-IPv6 embedding, alongside the mapped/6to4/NAT64
    # forms `_ipv4_embedded_addresses` already unwraps. Denied WHOLESALE here
    # (the entire /32), the same treatment `_is_ipv4_compatible` gives the
    # deprecated IPv4-compatible form, rather than unwrapped-and-checked: a
    # Teredo address's embedded client address is itself obfuscated
    # (XORed) in the low 32 bits, so "unwrap and check the embedded v4" is
    # not even the right operation here the way it is for the other three
    # forms. Not a demonstrated bypass -- reaching a Teredo address requires
    # a Teredo tunnel interface a server does not normally have -- added for
    # completeness, per the review.
    ipaddress.ip_network("2001::/32"),
]

# RFC 4291/3056/6052 IPv4-in-IPv6 embeddings. `ipaddress.IPv6Address` only
# recognises the mapped form (`.ipv4_mapped`) natively; the other two are
# recognised by their fixed prefix and a fixed byte offset for the embedded
# address.
_SIXTOFOUR_NET = ipaddress.ip_network("2002::/16")  # RFC 3056: embeds in bits 16-47
_NAT64_NET = ipaddress.ip_network("64:ff9b::/96")  # RFC 6052: embeds in the low 32 bits

# Only the scheme's default port may be dialled -- see the port-scope note on
# `post_json` (Finding 4 of the task-4 review). Allowlisting a host names a
# destination, not a blanket permit for every TCP port on it.
_DEFAULT_PORT_FOR_SCHEME = {"https": 443, "http": 80}


# THE `EgressBlocked("<reason>", <exemption comment>` CALLS KEEP THE REASON LITERAL ON
# THE CALL'S OWN LINE, and the comment after it, on purpose. Two checks read this
# file as text/AST: `tests/test_notification_delivery.py` finds every reason with the
# regex `EgressBlocked\(\s*"([a-z_]+)"`, and `tests/test_i18n_catalog.py` looks for
# the exemption on the call's first line. A reformat that moves the reason to its own
# line, or the comment before it, breaks one of them silently.
class EgressBlocked(Exception):
    """Raised when a destination is refused. `reason` is a CATEGORY, never a
    per-IP detail -- an admin reading delivery errors must not be able to map
    the internal network from them (spec §4.4)."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True)
class EgressResponse:
    status_code: int
    excerpt: str


def _ipv4_embedded_addresses(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """Every IPv4 address embedded in `ip` under a recognised RFC embedding --
    mapped, 6to4, or NAT64 -- regardless of which one it is.

    This covers every IPv4-in-IPv6 embedding this module unwraps-and-checks
    (task-4 review, Findings 1-2; whole-branch review finding M4 softened the
    claim below after a fourth embedding, Teredo, was found absent -- see
    `_DENIED_V6`, which denies the whole Teredo `/32` WHOLESALE instead of
    unwrapping it, for why Teredo is not a sibling entry in this list): a
    resolver the attacker controls (their own webhook domain's DNS) can hand
    back any of the forms handled HERE for a denied v4 address, and a
    classifier that only ever consults `ip.version` to pick a table never
    notices the embedded address unless it is unwrapped first. A future
    sibling encoding that genuinely needs unwrap-then-check (not
    deny-wholesale) treatment is handled by adding one entry HERE, not by
    adding another parallel branch in `_is_denied` -- that is the whack-a-mole
    this helper exists to close off for the forms it does cover; it is no
    longer claimed to be the entire bug class by itself.

    Deliberately excludes the IPv4-compatible form (`::a.b.c.d`, no `ffff`):
    see `_is_ipv4_compatible` for why that one is denied wholesale instead of
    unwrapped.
    """
    embedded: list[ipaddress.IPv4Address] = []
    mapped = ip.ipv4_mapped  # ::ffff:a.b.c.d (RFC 4291 §2.5.5.2)
    if mapped is not None:
        embedded.append(mapped)
    if ip in _SIXTOFOUR_NET:  # 2002:AABB:CCDD::/16 (RFC 3056)
        embedded.append(ipaddress.IPv4Address(ip.packed[2:6]))
    if ip in _NAT64_NET:  # 64:ff9b::a.b.c.d (RFC 6052)
        embedded.append(ipaddress.IPv4Address(ip.packed[12:16]))
    return embedded


def _is_ipv4_compatible(ip: ipaddress.IPv6Address) -> bool:
    """RFC 4291 §2.5.5.1 IPv4-compatible form (`::a.b.c.d`, no `ffff`).

    Judgement call (task-4 review, Finding 1): unlike the mapped form and
    NAT64 -- both still legitimately produced on real networks -- nothing on
    a modern network legitimately hands this back for a real destination; the
    form has been formally deprecated since RFC 4291 itself and automatic
    IPv4-compatible tunnelling is dead on current OSes. Denied WHOLESALE
    rather than unwrapped-and-checked: an attacker who controls the resolver
    for their own domain can choose any embedded payload at will, including a
    public-looking one, purely to slip past an unwrap-then-check filter --
    unwrapping it would mean trusting a self-reported payload as if it proved
    something about the destination, which it does not. Refusing the whole
    (dead) encoding is strictly safer than trusting what it claims to embed.
    """
    return ip.packed[:12] == b"\x00" * 12


def _is_denied(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)
    if isinstance(ip, ipaddress.IPv6Address):
        if _is_ipv4_compatible(ip):
            return True
        for embedded in _ipv4_embedded_addresses(ip):
            if any(embedded in net for net in _DENIED_V4):
                return True
        return any(ip in net for net in _DENIED_V6)
    return any(ip in net for net in _DENIED_V4)


@dataclass(frozen=True)
class _PlatformEntry:
    """One platform setting's (host, port) as derived from its configured
    URL. `port=None` means the port could not be determined for this
    setting -- see `_platform_entries` -- and is treated as a wildcard: this
    entry matches EVERY destination port, the fail-closed choice, exactly
    what denying "that host on all ports" (today's by-name-only behaviour)
    means for an entry we cannot scope more precisely.
    """

    host: str
    port: int | None


# Per-scheme default port, used only when a platform setting's URL omits an
# explicit port. `urlsplit` has no notion of a default port for `postgresql`
# (or its SQLAlchemy driver-suffixed forms like `postgresql+asyncpg`) -- it
# only special-cases schemes real browsers know -- so that one is supplied
# here from the well-known Postgres default. `ws`/`wss` (the gateway's
# schemes) follow the same default-port convention as `http`/`https`.
_SCHEME_DEFAULT_PORTS: dict[str, int] = {
    "https": 443,
    "http": 80,
    "wss": 443,
    "ws": 80,
    "postgresql": 5432,
    "postgres": 5432,
}


def _platform_entries() -> list[_PlatformEntry]:
    """Spec §4.1: the platform's own gateway/vault/database hosts, each as a
    (host, port) pair -- not a bare hostname -- so the guard can tell "the
    platform service's own port" apart from "some other, genuinely different
    service that happens to share the box" (S1), while still refusing the
    platform service's own address+port no matter what name or allowlist
    entry is used to reach it (S2).

    Read from the existing `Settings` object; no new setting keys were
    invented -- these are already the fields the rest of the codebase uses to
    reach each of these three hosts:
      - `database_url` -- the app's own Postgres (`app/core/db.py`). Almost
        always carries an explicit port; falls back to Postgres's well-known
        5432 when it does not.
      - `gateway_url` / `gateway_relay_url` -- the agent gateway: signed-job
        dispatch and remote-control media tunnels
        (`app/services/enrollment.py`, `app/api/v1/enrollment.py`). `wss`/`ws`
        URLs; falls back to the scheme's usual default port (443/80) when no
        port is given.
      - `openbao_addr` -- the shared network endpoint for the vault, CA, and
        signing backends when any of them run in `openbao` mode
        (`app/core/vault.py`, `app/core/ca.py`, `app/core/signing.py`).
        Unset (`None`) in `local` mode, which has no network host to deny.

    PORT DERIVATION RULE, per setting: use the URL's explicit port if it has
    one; otherwise look up the scheme's default in `_SCHEME_DEFAULT_PORTS`
    (stripping a SQLAlchemy `+driver` suffix first, e.g. `postgresql+asyncpg`
    -> `postgresql`). If the port is STILL undetermined -- an unrecognised
    scheme with no explicit port, or a malformed port that fails to parse --
    the entry's `port` is `None`: a wildcard that matches every destination
    port for that host, i.e. FAIL CLOSED to today's "deny this host on all
    ports" rather than silently denying nothing for it.

    Called on every `post_json`, unconditionally -- there is no parameter to
    opt out, because the whole point is that neither layer built on this can
    be satisfied by an allowlist entry (a deployment must not be able to
    allowlist its own vault).
    """
    settings = get_settings()
    entries: list[_PlatformEntry] = []
    for url in (
        settings.database_url,
        settings.gateway_url,
        settings.gateway_relay_url,
        settings.openbao_addr,
    ):
        if not url:
            continue
        parts = urlsplit(url)
        hostname = parts.hostname
        if not hostname:
            continue
        # Same normalisation as `post_json`'s own `host` (Finding I1) --
        # applied here too so a platform URL configured with a trailing dot
        # (unusual, but not implausible for a hand-edited config) matches
        # either way.
        host = hostname.lower().rstrip(".")
        try:
            explicit_port = parts.port
        except ValueError:
            # e.g. a port outside 0-65535 in a hand-edited config value.
            # Fail closed: wildcard, deny this host on every port.
            entries.append(_PlatformEntry(host, None))
            continue
        if explicit_port is not None:
            entries.append(_PlatformEntry(host, explicit_port))
            continue
        scheme_base = parts.scheme.split("+", 1)[0].lower()
        # `.get(...)` returning `None` here (unrecognised scheme, no explicit
        # port) IS the fail-closed wildcard case -- deliberately not defaulted
        # to anything else.
        entries.append(_PlatformEntry(host, _SCHEME_DEFAULT_PORTS.get(scheme_base)))
    return entries


def _platform_layer1_denies(
    host: str, port: int, allowlisted_hosts: set[tuple[str, int]]
) -> bool:
    """LAYER 1 -- by name, no DNS. See the module docstring's "S1/S2 FIX"
    section for the full rationale; this is the cheap fast path AND the
    fail-closed behaviour when resolution is unavailable, since it never
    resolves anything.

    A request naming a platform host's exact configured hostname is refused
    UNLESS an allowlist entry names this exact (host, port) pair AND that
    port is not the platform service's own port (or wildcard) -- an
    allowlist entry can never grant the platform service's own address+port,
    only a genuinely different, explicitly-named port on the same box (S1).
    """
    entries = [e for e in _platform_entries() if e.host == host]
    if not entries:
        return False
    if (host, port) not in allowlisted_hosts:
        return True
    # Explicitly allowlisted for this exact (host, port) -- but the allowlist
    # can never grant the platform's own port, nor a fail-closed (port
    # undeterminable) entry.
    return any(e.port is None or e.port == port for e in entries)


# LAYER 2's cache: platform hosts rarely number more than three or four, so
# this stays tiny. A modest 60s TTL bounds the DNS latency this check adds to
# every delivery (the common case is a cache hit) while still noticing a
# re-pointed platform host within a minute -- short enough that a deployment
# migrating its database or gateway to a new address is not left with a stale
# denial (or, worse, a stale ALLOW) for long, long enough that this does not
# amount to resolving on every single call. A resolution failure is never
# written into the cache -- see `_resolve_platform_addresses` -- so a
# transient DNS blip cannot poison it into "no addresses" for the full TTL.
_PLATFORM_ADDR_CACHE_TTL_S = 60.0
_platform_addr_cache: dict[str, tuple[float, frozenset[str]]] = {}


async def _resolve_platform_addresses(host: str) -> frozenset[str]:
    """Resolve a platform host's own address(es) for LAYER 2, cached per the
    TTL above. A resolution failure returns an empty set for THIS call and is
    never cached -- never cache a failure as a success, and never cache a
    failure as a stable "no addresses" answer either, since the very next
    call should simply try again. This is safe to leave unresolved: LAYER 1
    (by name) does not depend on resolution at all, so the platform host's
    own name stays refused throughout any outage; only the address-based
    cross-check for a DIFFERENTLY-named alias (S2) is unavailable until
    resolution recovers.
    """
    now = time.monotonic()
    cached = _platform_addr_cache.get(host)
    if cached is not None and now - cached[0] < _PLATFORM_ADDR_CACHE_TTL_S:
        return cached[1]
    try:
        addrs = frozenset(await asyncio.to_thread(_resolve_all, host, 0))
    except EgressBlocked:
        return frozenset()
    _platform_addr_cache[host] = (now, addrs)
    return addrs


def _canonical_addr(ip_str: str) -> str:
    """Canonicalise an address for LAYER 2 comparison, unwrapping the same
    IPv4-in-IPv6 embeddings `_is_denied` unwraps (mapped, 6to4, NAT64) so an
    attacker-controlled resolver cannot dodge the address+port cross-check by
    handing back an encoded form of a platform host's real address for their
    own domain. The deprecated IPv4-compatible form is deliberately NOT
    unwrapped here either, for the identical reason `_is_ipv4_compatible`
    gives: unwrapping it means trusting a self-reported payload as if it
    proved something about the destination.
    """
    ip = ipaddress.ip_address(ip_str)
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _ipv4_embedded_addresses(ip)
        if embedded:
            return str(embedded[0])
    return str(ip)


async def _platform_layer2_denies(addr: str, port: int) -> bool:
    """LAYER 2 -- by resolved address, after DNS. See the module docstring's
    "S1/S2 FIX" section. Runs on the CONNECTED (pinned) address and the
    destination port -- the address actually dialled, not merely one of
    possibly several DNS answers -- regardless of the name used to reach it
    and regardless of allowlisting (S2): an allowlist entry must never grant
    access to a platform service's own address+port.

    Cheap in the common case: a platform entry whose OWN port does not match
    the destination port is skipped without ever resolving it, so a request
    to an unrelated host:port never pays for platform DNS lookups at all
    (this also keeps single-record-per-call DNS test doubles elsewhere in
    the test suite from being consulted for calls this layer has no reason
    to make). A wildcard entry (`port=None`, fail-closed) is never skipped by
    port, since it must match on address alone regardless of port.
    """
    target = _canonical_addr(addr)
    for entry in _platform_entries():
        if entry.port is not None and entry.port != port:
            continue
        platform_addrs = await _resolve_platform_addresses(entry.host)
        if any(_canonical_addr(a) == target for a in platform_addrs):
            return True
    return False


def check_host(host: str, resolved: list[str], *, allowlisted: bool) -> str:
    """Validate every resolved record; return the IP to connect to.

    Returns the FIRST record, which is also the one every record was validated
    alongside -- the caller must connect to exactly this address.
    """
    if not resolved:
        raise EgressBlocked("dns_failure",  # i18n-exempt: machine code, the console words it
            f"{host} did not resolve")
    if not allowlisted:
        for ip_str in resolved:
            if _is_denied(ip_str):
                raise EgressBlocked("blocked_private",  # i18n-exempt: machine code
                    f"{host} resolves into denied address space")
    return resolved[0]


def _resolve_all(host: str, port: int) -> list[str]:
    """Blocking; always called through `resolve()` off the event loop."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        # UnicodeError: an oversized/malformed hostname label fails IDNA
        # encoding before the resolver is ever consulted (e.g. a 300-char
        # label). Fail-closed either way, but it must still surface as the
        # same categorized `dns_failure` an EgressBlocked caller expects
        # (task-4 review, Finding 5), not an uncaught raw exception.
        raise EgressBlocked("dns_failure",  # i18n-exempt: machine code, the console words it
            f"{host} did not resolve") from exc
    return [str(info[4][0]) for info in infos]


async def resolve(host: str, port: int) -> list[str]:
    """Async public entry point for DNS resolution.

    `socket.getaddrinfo` is a blocking call; this process also serves the
    agent gateway's WebSocket connections and the API on the same event loop,
    so resolving a webhook host inline here would stall every other
    connection for as long as DNS takes. Offloaded to a worker thread; the
    blocking implementation stays in `_resolve_all` as the seam tests patch.
    """
    return await asyncio.to_thread(_resolve_all, host, port)


def _build_transport(_pinned_ip: str) -> httpx.AsyncBaseTransport:
    """A plain, no-retry transport -- deliberately NOT where pinning happens.

    Whole-branch review finding M12: an earlier reader could mistake the
    parameter for "the IP this transport connects to", as though pinning were
    implemented here. It is not: pinning is done by `post_json` rewriting the
    request URL to the validated IP and passing `extensions={"sni_hostname":
    host}` (see `post_json`'s own comments on `pinned_url` and the `sni_
    hostname` extension) -- this function builds an otherwise ordinary
    transport that connects to whatever URL it is given. The parameter is
    kept, underscore-prefixed and unused, purely because this is the seam
    every test in this module patches (`monkeypatch.setattr("app.core.egress.
    _build_transport", lambda ip: ...)`) to swap in a mock transport per
    resolved address; removing it would mean threading the pinned IP through
    a different call shape for no behavioural gain.
    """
    return httpx.AsyncHTTPTransport(retries=0)


async def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    allowlisted_hosts: set[tuple[str, int]],
    timeout_s: float = 30.0,
) -> EgressResponse:
    parts = urlsplit(url)
    # `.hostname` already lowercases (urlsplit's own behaviour); a trailing
    # root dot ("gw.example.com.") is NOT stripped by it, and DNS treats that
    # form and the bare form as the exact same name (task-4 review follow-up,
    # Finding I1: `gw.example.com` was correctly refused as a platform host
    # while `gw.example.com.` sailed through to a real connection, resolving
    # identically). Stripped here, ONCE, before every use of `host` below --
    # the platform-host check, the allowlist membership check, DNS
    # resolution, and the Host/SNI values -- so a single normalised value is
    # what every one of those decisions sees, and none of them can be
    # satisfied by a request that merely spells the same host differently.
    host = (parts.hostname or "").rstrip(".")

    if parts.scheme not in ("https", "http"):
        raise EgressBlocked("scheme_not_allowed",  # i18n-exempt: machine code, the console words it
            f"scheme {parts.scheme!r}")

    default_port = _DEFAULT_PORT_FOR_SCHEME[parts.scheme]
    try:
        explicit_port = parts.port
    except ValueError as exc:
        # e.g. `https://host:999999/x` -- out of the 0-65535 range. Fail
        # closed as a categorized EgressBlocked (task-4 review, Finding 5),
        # not an uncaught ValueError.
        raise EgressBlocked("invalid_url",  # i18n-exempt: machine code, the console words it
            "malformed port") from exc
    port = explicit_port or default_port

    # Spec §4.1, LAYER 1: the platform's own gateway/vault/database hosts,
    # read from config -- refused by NAME, before the allowlist can grant the
    # platform service's own port (S1/S2 fix -- see the module docstring's
    # "S1/S2 FIX" section). Deliberately evaluated before the allowlist/port
    # scoping below and needs no DNS at all, so it stays in force as a
    # fail-closed backstop even during a total DNS outage.
    if _platform_layer1_denies(host, port, allowlisted_hosts):
        raise EgressBlocked("blocked_platform_host",  # i18n-exempt: machine code
            "refused: this is one of the platform's own hosts")

    # Port scope (task-4 review Finding 4; task 4b restores what the review's
    # own fix over-corrected -- see fix-egress-report.md). An allowlist entry
    # now names a HOST AND PORT together (spec §2.6, migration 0026):
    # allowlisting `mattermost.lan` for :8065 does not thereby grant every
    # other port on that host, and it does not, on its own, grant the
    # scheme's default port unless that port was *also* named. A host with NO
    # allowlist entry at all keeps today's behaviour: only the scheme's
    # default port is reachable, unchanged from before this fix.
    allowlisted = (host, port) in allowlisted_hosts
    host_has_other_allowlisted_port = not allowlisted and any(
        h == host for h, _ in allowlisted_hosts
    )
    if not allowlisted:
        if host_has_other_allowlisted_port:
            raise EgressBlocked("port_not_allowed",  # i18n-exempt: machine code
                "this host is allowlisted, but not for this port")
        if port != default_port:
            raise EgressBlocked("port_not_allowed",  # i18n-exempt: machine code
                "only the scheme's default port is permitted for a non-allowlisted host")

    if parts.scheme == "http" and not allowlisted:
        raise EgressBlocked("scheme_not_allowed",  # i18n-exempt: machine code, the console words it
            "plain http requires an allowlisted host:port")

    # Only the connect phase gets its own sub-budget; the wall-clock deadline
    # for the whole exchange -- DNS resolution included (property 5 above,
    # task-4 review Finding 3) -- is enforced below via asyncio.wait_for.
    # httpx's own read/write timeouts reset on every chunk and do not bound
    # total elapsed time, and resolution happening before any httpx call is
    # entered is exactly how a slow attacker-controlled resolver used to
    # escape the budget entirely.
    timeout = httpx.Timeout(timeout_s, connect=_CONNECT_TIMEOUT_S)

    async def _do_request() -> EgressResponse:
        pinned = check_host(host, await resolve(host, port), allowlisted=allowlisted)

        # Spec §4.1, LAYER 2: the platform's own gateway/vault/database
        # hosts, refused by RESOLVED ADDRESS+PORT -- the address actually
        # dialled -- regardless of the name used to reach it and regardless
        # of allowlisting (S2 fix). An allowlist entry must never grant
        # access to a platform service's own address+port; LAYER 1 above
        # only ever exempts a genuinely different port on the same name.
        if await _platform_layer2_denies(pinned, port):
            raise EgressBlocked("blocked_platform_host",  # i18n-exempt: machine code
                "refused: resolves to one of the platform's own hosts")

        # Connect to the pinned IP while preserving Host and TLS SNI, so the
        # validated answer is the one actually used (property 2 above).
        pinned_netloc = f"[{pinned}]" if ":" in pinned else pinned
        pinned_url = parts._replace(netloc=f"{pinned_netloc}:{port}").geturl()
        request_headers = {**headers, "Host": host, "Content-Type": "application/json"}

        async with httpx.AsyncClient(
            transport=_build_transport(pinned),
            timeout=timeout,
            follow_redirects=False,  # property 3
        ) as client:
            async with client.stream(
                "POST",
                pinned_url,
                content=json.dumps(payload).encode(),
                headers=request_headers,
                extensions={"sni_hostname": host},
            ) as response:
                if 300 <= response.status_code < 400:
                    raise EgressBlocked("redirect_refused",  # i18n-exempt: machine code
                        f"destination returned {response.status_code}")

                # Bounded read (property 4): stop pulling chunks the instant
                # the cap is reached instead of letting the client buffer an
                # unbounded body first and trimming it afterwards.
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) >= _BODY_MAX_BYTES:
                        break

            raw = bytes(chunks[:_BODY_MAX_BYTES]).decode("utf-8", errors="replace")
            excerpt = "".join(ch for ch in raw if ch.isprintable())[:_EXCERPT_MAX]
            return EgressResponse(status_code=response.status_code, excerpt=excerpt)

    return await asyncio.wait_for(_do_request(), timeout=timeout_s)
