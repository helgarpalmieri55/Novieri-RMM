"""The source address of a request (install-link spec §1.1).

The header is honoured only from a peer inside ITS_TRUSTED_PROXY_CIDRS, and
then only its RIGHTMOST entry -- the one Traefik wrote. Everything to the left
of it is whatever the client sent.
"""

import ipaddress

import pytest
from starlette.requests import Request

from app.core.client_address import (
    TrustedProxyConfigError,
    address_in,
    client_address,
    parse_trusted_proxies,
)

TRUSTED = parse_trusted_proxies("172.19.240.0/24")


def _request(peer: str | None, *xff: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-forwarded-for", value.encode()) for value in xff],
        "client": (peer, 5000) if peer is not None else None,
    }
    return Request(scope)


def test_a_peer_outside_the_trusted_set_is_the_source_and_its_header_is_ignored() -> None:
    got = client_address(_request("198.51.100.4", "10.50.0.9"), TRUSTED)
    assert got == ipaddress.ip_address("198.51.100.4")


def test_from_traefik_the_rightmost_entry_is_the_source() -> None:
    # The client claimed 10.50.0.9; Traefik appended the real 203.0.113.7.
    got = client_address(_request("172.19.240.3", "10.50.0.9, 203.0.113.7"), TRUSTED)
    assert got == ipaddress.ip_address("203.0.113.7")


def test_two_header_lines_are_one_list_and_the_last_entry_wins() -> None:
    got = client_address(_request("172.19.240.3", "10.50.0.9", "192.0.2.10"), TRUSTED)
    assert got == ipaddress.ip_address("192.0.2.10")


def test_a_trusted_peer_with_no_header_is_itself() -> None:
    assert client_address(_request("172.19.240.3"), TRUSTED) == ipaddress.ip_address(
        "172.19.240.3"
    )


def test_an_unparseable_rightmost_entry_is_no_address_at_all() -> None:
    assert client_address(_request("172.19.240.3", "10.0.0.1, not-an-ip"), TRUSTED) is None


def test_an_ipv4_mapped_peer_is_compared_as_ipv4() -> None:
    got = client_address(_request("::ffff:172.19.240.3", "10.50.0.9"), TRUSTED)
    assert got == ipaddress.ip_address("10.50.0.9")


def test_no_client_is_no_address() -> None:
    assert client_address(_request(None), TRUSTED) is None


def test_the_setting_refuses_a_prefix_with_host_bits() -> None:
    with pytest.raises(TrustedProxyConfigError):
        parse_trusted_proxies("172.19.240.1/24")


def test_the_setting_takes_several_prefixes_and_ignores_blanks() -> None:
    nets = parse_trusted_proxies(" 10.0.0.0/8, ,fd00::/8 ")
    assert [str(n) for n in nets] == ["10.0.0.0/8", "fd00::/8"]


def test_containment_across_versions_is_false_not_an_error() -> None:
    v6 = ipaddress.ip_address("fd00::1")
    assert address_in(v6, ["10.0.0.0/8"]) is False
    assert address_in(v6, ["fd00::/8"]) is True
    assert address_in(None, ["0.0.0.0/0"]) is False
