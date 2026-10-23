"""Where a request came from, for decisions that depend on it.

Install-link spec 2026-09-28 §1.1. The link answers only requests from networks
an operator listed, so the source address is a security input, and the API sits
behind Traefik: the TCP peer is Traefik, and the client's address is in
`X-Forwarded-For`.

THE HEADER IS BELIEVED ONLY FROM A TRUSTED PEER, AND ONLY ITS RIGHTMOST ENTRY.
Traefik APPENDS the address it saw; everything to the left of that is whatever
the client sent, so a client that writes `X-Forwarded-For: 10.50.0.9` into its
own request cannot claim an allowed network -- its claim is not the rightmost
entry by the time the request arrives. From a peer outside
`ITS_TRUSTED_PROXY_CIDRS` the header is ignored entirely: anybody who can reach
the API port directly could otherwise write it.

UVICORN MUST NOT REWRITE `request.client` ITSELF. It would take the LEFTMOST
entry. `tests/test_deploy_install_link.py` pins that the compose command does not
pass `--forwarded-allow-ips` / `--proxy-headers`.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence

from starlette.requests import Request

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class TrustedProxyConfigError(ValueError):
    """`ITS_TRUSTED_PROXY_CIDRS` holds something that is not a network prefix."""


def parse_trusted_proxies(value: str) -> tuple[IPNetwork, ...]:
    """The configured prefixes, strictly: a prefix with host bits set is refused,
    because `172.19.240.1/24` is far more often a typo than an intention."""
    networks: list[IPNetwork] = []
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=True))
        except ValueError as exc:
            raise TrustedProxyConfigError(  # i18n-exempt: startup config error, logged
                f"ITS_TRUSTED_PROXY_CIDRS: {item!r} is not a network prefix ({exc})"
            ) from exc
    return tuple(networks)


def _address(value: str) -> IPAddress | None:
    try:
        parsed = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def address_in(address: IPAddress | None, networks: Iterable[object]) -> bool:
    """Is `address` inside any of `networks`? Accepts `ip_network` objects or
    their strings (asyncpg hands `cidr[]` back as objects, the API as strings).
    No address is inside nothing; a v6 address is never inside a v4 prefix."""
    if address is None:
        return False
    for raw in networks:
        network = ipaddress.ip_network(str(raw), strict=False)
        if network.version == address.version and address in network:
            return True
    return False


def client_address(request: Request, trusted: Sequence[IPNetwork]) -> IPAddress | None:
    """The request's source address, or `None` when it cannot be established.

    `None` is a refusal at every caller: the install link answers it with the
    same 404 as a link that does not exist.
    """
    if request.client is None:
        return None
    peer = _address(request.client.host)
    if peer is None or not address_in(peer, trusted):
        return peer
    forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
    entries = [entry.strip() for entry in forwarded.split(",") if entry.strip()]
    if not entries:
        return peer
    return _address(entries[-1])
