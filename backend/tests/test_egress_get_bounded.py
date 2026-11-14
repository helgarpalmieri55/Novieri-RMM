"""`egress.get_bounded`: a GET under the egress guard's five properties, and
CIMD-03 §10.7's special-use addresses (autenticación propia plan 2026-11-06,
Task 3; design §5.5).

Same seams as `tests/test_egress_guard.py`: `_resolve_all` answers DNS and
`_build_transport` the connection, so nothing leaves the machine.
"""

import asyncio
import ipaddress
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

import app.core.egress as egress_module
from app.core.config import Settings, override_settings
from app.core.egress import EgressBlocked, get_bounded

PUBLIC = "93.184.216.34"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        database_url="postgresql+asyncpg://u:p@db.internal:5432/x",
        database_admin_url="postgresql+asyncpg://u:p@db.internal:5432/x",
        oidc_issuer="https://issuer.example.com",
        oidc_jwks_url="https://issuer.example.com/jwks",
        ca_dir=Path("/tmp/its-egress-test-ca"),
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _baseline_settings():
    egress_module._platform_addr_cache.clear()
    with override_settings(_settings()):
        yield


def _streamed(status: int, data: bytes = b"", **headers: str) -> httpx.Response:
    """An answer as a real transport gives one: a stream not yet read, so
    `aiter_raw` sees the bytes as they arrived (a `content=` response is read
    when it is built, and has no raw stream left)."""
    return httpx.Response(
        status,
        headers={k.replace("_", "-"): v for k, v in headers.items()},
        stream=httpx.ByteStream(data),
    )


def _serve(monkeypatch: pytest.MonkeyPatch, handler: Any, *, resolved: list[str] | None = None):
    seen: list[httpx.Request] = []

    def transport(_ip: str) -> httpx.AsyncBaseTransport:
        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        return httpx.MockTransport(wrapped)

    monkeypatch.setattr(egress_module, "_resolve_all", lambda host, port: resolved or [PUBLIC])
    monkeypatch.setattr(egress_module, "_build_transport", transport)
    return seen


async def test_a_document_is_read_through_the_pinned_address(monkeypatch) -> None:
    seen = _serve(
        monkeypatch,
        lambda r: _streamed(
            200,
            b'{"client_id": "x"}',
            Content_Type="application/json",
            Cache_Control="max-age=600",
        ),
    )
    body = await get_bounded(
        "https://client.example/oauth/metadata.json",
        max_bytes=5120,
        headers={"Accept": "application/json"},
    )
    assert body.status_code == 200
    assert body.content == b'{"client_id": "x"}'
    assert body.truncated is False
    assert body.content_type == "application/json"
    assert body.cache_control == "max-age=600"
    assert body.content_encoding == ""
    (request,) = seen
    assert request.method == "GET"
    assert request.url.host == PUBLIC  # pinned: the validated answer is the one dialled
    assert request.url.path == "/oauth/metadata.json"
    assert request.headers["host"] == "client.example"
    # The TLS name is the host's, not the pinned address's: the certificate is
    # checked against the name the URL gave (fix round 1d).
    assert request.extensions["sni_hostname"] == "client.example"
    assert request.headers["accept"] == "application/json"
    # What is measured is what is sent: nothing compressed (Task 3, decision 4).
    assert request.headers["accept-encoding"] == "identity"


async def test_an_ipv6_literal_host_is_bracketed_in_host_and_bare_in_tls(monkeypatch) -> None:
    """RFC 9110 §7.2's `Host = uri-host [ ":" port ]` is RFC 3986's host, where
    an IPv6 address is "distinguished by enclosing the IP literal within square
    brackets". The TLS name stays bare: `ssl` then checks the address against
    the certificate and sends no SNI (RFC 6066 §3: "Literal IPv4 and IPv6
    addresses are not permitted in "HostName"."), while a bracketed one would
    go out as a name (fix round 1d)."""
    address = "2606:2800:220:1::1"
    seen = _serve(monkeypatch, lambda r: _streamed(200, b"{}"), resolved=[address])
    await get_bounded(f"https://[{address}]/m.json", max_bytes=10)
    (request,) = seen
    assert request.headers["host"] == f"[{address}]"
    assert request.extensions["sni_hostname"] == address


async def test_a_body_over_the_cap_is_cut_and_says_so(monkeypatch) -> None:
    _serve(monkeypatch, lambda r: _streamed(200, b"x" * 9000))
    body = await get_bounded("https://client.example/m.json", max_bytes=5120)
    assert body.truncated is True
    assert len(body.content) == 5120


async def test_a_compressed_body_is_counted_as_it_arrived_and_named(monkeypatch) -> None:
    import gzip

    packed = gzip.compress(b"x" * 100_000)
    _serve(
        monkeypatch,
        lambda r: _streamed(200, packed, Content_Encoding="gzip"),
    )
    body = await get_bounded("https://client.example/m.json", max_bytes=5120)
    assert body.content_encoding == "gzip"
    assert len(body.content) <= 5120
    assert body.content == packed[:5120]


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://client.example/m.json", "scheme_not_allowed"),
        ("ftp://client.example/m.json", "scheme_not_allowed"),
        ("https://client.example:8443/m.json", "port_not_allowed"),
        ("https://client.example:99999/m.json", "invalid_url"),
        ("https:///m.json", "invalid_url"),
        ("https://db.internal/m.json", "blocked_platform_host"),
    ],
)
async def test_what_the_guard_refuses_before_any_connection(
    monkeypatch, url: str, reason: str
) -> None:
    seen = _serve(monkeypatch, lambda r: httpx.Response(200))
    with pytest.raises(EgressBlocked) as refused:
        await get_bounded(url, max_bytes=10)
    assert refused.value.reason == reason
    assert seen == []


SPECIAL_USE = [
    "10.0.0.5",
    "127.0.0.1",
    "169.254.169.254",
    "::1",
    # CIMD-03 §10.7: RFC 6890's special-use addresses, which `post_json`'s
    # table lets through (Task 3, decision 3).
    "0.1.2.3",
    "192.0.2.1",
    "198.51.100.1",
    "203.0.113.1",
    "2001:db8::1",
    "2001:2::1",
    "2001:10::1",
    "100::1",
    "64:ff9b:1::a00:5",
    "64:ff9b::a00:5",
    # NAT64 of TEST-NET-1: `is_global` counts it as global; only the
    # embedded-IPv4 branch of `_is_special_use` refuses it (re-review
    # f3-1, Minor 2).
    "64:ff9b::c000:201",
    # Fix round 1: what `is_global` let through. IPv6 outside 2000::/3 --
    # deprecated site-local, SRv6 SIDs (RFC 9602), the dummy prefix (RFC
    # 9780) and unallocated space -- and the documentation prefix inside it
    # that Python 3.12.3 counts as global (RFC 9637).
    "fec0::1",
    "5f00::1",
    "100:0:0:1::1",
    "4000::1",
    "8000::1",
    "e000::1",
    "3fff::1",
    # SIIT's IPv4-translated form of 10.0.0.5 (`::ffff:0:0:0/96`), which is
    # neither IPv4-mapped nor NAT64, so nothing unwrapped it.
    "::ffff:0:a00:5",
    # The deprecated 6to4 relay anycast block of the IPv4 registry.
    "192.88.99.1",
]
GLOBAL = ["2606:2800:220:1::1", "::ffff:93.184.216.34", "64:ff9b::5db8:d822", PUBLIC]


@pytest.mark.parametrize("address", SPECIAL_USE)
async def test_a_special_use_answer_is_never_dialled(monkeypatch, address: str) -> None:
    seen = _serve(monkeypatch, lambda r: httpx.Response(200), resolved=[PUBLIC, address])
    with pytest.raises(EgressBlocked) as refused:
        await get_bounded("https://client.example/m.json", max_bytes=10)
    assert refused.value.reason == "blocked_private"
    assert seen == []


@pytest.mark.parametrize("address", GLOBAL)
async def test_a_global_answer_is_dialled(monkeypatch, address: str) -> None:
    seen = _serve(monkeypatch, lambda r: _streamed(200, b"{}"), resolved=[address])
    body = await get_bounded("https://client.example/m.json", max_bytes=10)
    assert body.status_code == 200
    assert len(seen) == 1


@pytest.mark.parametrize("host", ["0x7f000001", "2130706433", "127.1", "0177.0.0.1", "0x0a000005"])
async def test_a_host_spelled_as_a_number_is_judged_by_the_address_it_resolves_to(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """Nothing before DNS reads the host as an address: only the platform's
    own names are compared as strings. The REAL resolver runs here (only the
    transport is replaced), because the point is what it does with these
    spellings: getaddrinfo reads them as inet_aton does (`0x7f000001` is
    127.0.0.1, `0x0a000005` is 10.0.0.5) without asking DNS, and the address
    it returns is the one refused (fix round 1b)."""
    seen: list[httpx.Request] = []

    def transport(_ip: str) -> httpx.AsyncBaseTransport:
        return httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200))

    monkeypatch.setattr(egress_module, "_build_transport", transport)
    with pytest.raises(EgressBlocked) as refused:
        await get_bounded(f"https://{host}/m.json", max_bytes=10)
    assert refused.value.reason == "blocked_private"
    assert seen == []


@pytest.mark.parametrize("pretend", [True, False])
def test_the_verdict_does_not_read_pythons_own_table(
    monkeypatch: pytest.MonkeyPatch, pretend: bool
) -> None:
    """`is_global` and `is_private` follow a table that changes between
    Python releases (3.12.3 calls `3fff::1` global, 3.13.16 does not), and
    production and CI run a floating 3.12. The verdict is the IANA registry's,
    written in `egress.py`, so Python's answer -- whatever it is -- changes
    nothing (fix round 1)."""
    for cls in (ipaddress.IPv4Address, ipaddress.IPv6Address):
        for name in ("is_global", "is_private", "is_reserved", "is_site_local"):
            monkeypatch.setattr(cls, name, property(lambda self: pretend), raising=False)
    assert [a for a in SPECIAL_USE if not egress_module._is_special_use(a)] == []
    assert [a for a in GLOBAL if egress_module._is_special_use(a)] == []


async def test_a_redirect_is_refused_and_never_followed(monkeypatch) -> None:
    seen = _serve(
        monkeypatch, lambda r: httpx.Response(302, headers={"Location": "https://169.254.169.254/"})
    )
    with pytest.raises(EgressBlocked) as refused:
        await get_bounded("https://client.example/m.json", max_bytes=10)
    assert refused.value.reason == "redirect_refused"
    assert len(seen) == 1


async def test_the_whole_exchange_has_one_deadline(monkeypatch) -> None:
    def slow(_host: str, _port: int) -> list[str]:
        import time

        time.sleep(0.5)
        return [PUBLIC]

    monkeypatch.setattr(egress_module, "_resolve_all", slow)
    with pytest.raises(TimeoutError):
        await get_bounded("https://client.example/m.json", max_bytes=10, timeout_s=0.1)


async def test_a_lookup_that_never_answers_holds_a_thread_of_its_own(monkeypatch) -> None:
    """The deadline returns, but `getaddrinfo` cannot be cancelled: its thread
    runs on. A signed-in person opening consent pages for a domain whose DNS
    never answers would otherwise fill the default executor, which password
    hashing shares (`app/core/auth/passwords.py`; plan Task 6, coordinator's
    note). So a document's host is resolved on `DOCUMENT_RESOLVER_THREADS`
    threads of its own: lookups past them queue and are dropped at their
    deadline, never started, and the default executor stays free."""
    release = threading.Event()
    lock = threading.Lock()
    inside = most = 0

    def stuck(_host: str, _port: int) -> list[str]:
        nonlocal inside, most
        with lock:
            inside += 1
            most = max(most, inside)
        release.wait(10)
        with lock:
            inside -= 1
        return [PUBLIC]

    threads = egress_module.DOCUMENT_RESOLVER_THREADS
    # Every thread of the pool free first: an earlier test's lookup may still
    # be running out past its deadline -- the very property under test.
    every_thread = threading.Barrier(threads)
    loop = asyncio.get_running_loop()
    await asyncio.gather(
        *(
            loop.run_in_executor(egress_module._document_resolver, every_thread.wait, 10)
            for _ in range(threads)
        )
    )
    monkeypatch.setattr(egress_module, "_resolve_all", stuck)
    try:
        answers = await asyncio.gather(
            *(
                get_bounded(f"https://c{n}.example/m.json", max_bytes=10, timeout_s=0.3)
                for n in range(threads * 3)
            ),
            return_exceptions=True,
        )
        assert all(isinstance(answer, TimeoutError) for answer in answers), answers
        assert most == threads
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), 2.0) == "free"
    finally:
        release.set()
