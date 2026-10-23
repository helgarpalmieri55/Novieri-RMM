"""Tests for the outbound egress guard (spec §4, plan Task 4, Task 4b).

Fixture note: this module is pure-function / transport-level (no DB), so it
needs neither `db_session` nor `api_env` -- unlike the fixture pattern used by
the rest of this project's async, DB-backed tests.

Task 4b baseline-settings note: `post_json` now consults `get_settings()`
unconditionally (spec §4.1's platform-host denial, see the bottom of this
file), so every test in this file runs under an autouse `_baseline_settings`
fixture providing a harmless `Settings` instance via `override_settings` --
none of its host values (`db.internal`, and `gateway_url`/`gateway_relay_url`'s
class defaults of `localhost`) collide with any hostname used elsewhere in
this file. Tests that care about the platform-host check override settings
again, narrowly, inside their own `with override_settings(...)` block.

S1/S2 fix note: LAYER 2 (`app.core.egress._platform_layer2_denies`) caches
resolved platform-host addresses in a module-level dict with a TTL (see
`app.core.egress._platform_addr_cache`). The same autouse fixture clears it
before every test, so a platform hostname reused across tests with different
mocked `_resolve_all` behaviour (or different settings) never sees another
test's cached address.
"""

import asyncio
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import httpx
import pytest

import app.core.egress as egress_module
from app.core.config import Settings, override_settings
from app.core.egress import EgressBlocked, EgressResponse, check_host, post_json


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

# Spec §12.1 -- one case per denied range. Each entry is a real address an
# attacker would reach for, not a synthetic example.
DENIED = [
    ("10.0.0.1", "RFC1918 /8"),
    ("172.16.0.1", "RFC1918 /12"),
    ("192.168.1.1", "RFC1918 /16"),
    ("127.0.0.1", "loopback v4"),
    ("::1", "loopback v6"),
    ("169.254.169.254", "cloud metadata"),
    ("169.254.1.1", "link-local v4"),
    ("fe80::1", "link-local v6"),
    ("fc00::1", "unique-local v6"),
    ("224.0.0.1", "multicast"),
    ("0.0.0.0", "unspecified"),
    # Not in the plan's original table. A resolver an attacker controls (it's
    # their own domain's DNS) can hand back an IPv4-mapped IPv6 literal for
    # the metadata address. A check that only consults `ip.version` to pick
    # which denied table to scan (v4 table vs v6 table) never notices the
    # embedded v4 address unless it is unwrapped first -- a classic SSRF
    # filter bypass, not a synthetic one.
    ("::ffff:169.254.169.254", "IPv4-mapped v6 cloud metadata"),
    # Whole-branch review finding M4 (low practical value, added for
    # completeness): 240.0.0.1 and 255.255.255.255 both resolved past the
    # guard and reached the connect stage before this fix (demonstrated in
    # the review). 240.0.0.0/4 covers both; 192.0.0.0/24 and 198.18.0.0/15
    # were the other two gaps the review named.
    ("240.0.0.1", "reserved /4 (240.0.0.0/4)"),
    ("255.255.255.255", "limited broadcast"),
    ("192.0.0.1", "IETF protocol assignments (RFC 6890)"),
    ("198.18.0.1", "benchmarking (RFC 2544)"),
    # Teredo (RFC 4380) -- the fourth RFC-recognised IPv4-in-IPv6 embedding,
    # denied wholesale (the whole 2001::/32) rather than unwrapped, same
    # treatment as the IPv4-compatible form below. Not a demonstrated
    # bypass -- reaching a Teredo address needs a Teredo tunnel interface a
    # server does not normally have -- added for completeness.
    ("2001:0:4136:e378:8000:63bf:3fff:fdd2", "Teredo (RFC 4380)"),
]


@pytest.mark.parametrize("ip,label", DENIED)
def test_denied_ranges_are_blocked(ip: str, label: str):
    with pytest.raises(EgressBlocked) as exc:
        check_host("evil.example.com", [ip], allowlisted=False)
    assert exc.value.reason == "blocked_private", label


def test_public_address_is_allowed():
    assert check_host("hooks.example.com", ["93.184.216.34"], allowlisted=False) == "93.184.216.34"


def test_multi_record_dns_with_one_private_record_is_blocked():
    """Spec §12.3.

    Checking only the first record lets a multi-record answer walk past the
    guard: the check sees a public address, the connection uses a private one.
    """
    with pytest.raises(EgressBlocked) as exc:
        check_host("mixed.example.com", ["93.184.216.34", "10.0.0.5"], allowlisted=False)
    assert exc.value.reason == "blocked_private"


def test_allowlisted_host_permits_a_private_address():
    """Spec §12.5 -- the opt-back-in an on-prem LAN destination needs."""
    assert check_host("mattermost.lan", ["10.0.0.5"], allowlisted=True) == "10.0.0.5"


def test_allowlist_applies_only_to_the_named_host():
    # The allowlist is per-host. A different host resolving into the same range
    # is still blocked.
    with pytest.raises(EgressBlocked):
        check_host("other.lan", ["10.0.0.5"], allowlisted=False)


def test_empty_resolution_is_a_dns_failure():
    with pytest.raises(EgressBlocked) as exc:
        check_host("nowhere.example.com", [], allowlisted=False)
    assert exc.value.reason == "dns_failure"


def test_egress_blocked_reason_is_a_category_not_a_detail():
    """Spec §4.4 -- the exception message must not leak which network the
    scan hit; an admin reading delivery errors must not be able to map the
    internal network from them."""
    with pytest.raises(EgressBlocked) as exc:
        check_host("evil.example.com", ["10.0.0.1"], allowlisted=False)
    assert exc.value.reason == "blocked_private"
    assert "10.0.0.1" not in str(exc.value)


# ---------------------------------------------------------------------------
# Review Finding 1/2 -- every RFC-recognised IPv4-in-IPv6 embedding must be
# caught, not just the ::ffff: mapped form. Table-driven so a future edit
# cannot silently re-open one sibling encoding while leaving the others
# covered; the already-working mapped form is included deliberately so a
# regression there fails this table too, not just the one above.
# ---------------------------------------------------------------------------

IPV6_EMBEDDED_V4_DENIED = [
    ("::ffff:169.254.169.254", "IPv4-mapped (RFC 4291 2.5.5.2) cloud metadata"),
    ("::169.254.169.254", "IPv4-compatible (RFC 4291 2.5.5.1, deprecated) cloud metadata"),
    ("2002:a9fe:a9fe::", "6to4 (RFC 3056) encoding of cloud metadata"),
    ("64:ff9b::a9fe:a9fe", "NAT64 (RFC 6052) encoding of cloud metadata"),
]


@pytest.mark.parametrize("ip,label", IPV6_EMBEDDED_V4_DENIED)
def test_ipv6_embedded_ipv4_encodings_are_all_denied(ip: str, label: str):
    with pytest.raises(EgressBlocked) as exc:
        check_host("evil.example.com", [ip], allowlisted=False)
    assert exc.value.reason == "blocked_private", label


def test_nat64_embedding_a_public_address_is_allowed():
    """NAT64 is live on real IPv6-only enterprise networks (the on-prem LAN
    context this spec targets), unlike 6to4/IPv4-compatible tunnelling, which
    are essentially dead. Extract-then-check is the right treatment here: a
    NAT64 synthesis of a genuinely public destination must still be
    deliverable. 64:ff9b::5db8:d822 embeds 93.184.216.34 (0x5D 0xB8 0xD8 0x22)."""
    assert (
        check_host("hooks.example.com", ["64:ff9b::5db8:d822"], allowlisted=False)
        == "64:ff9b::5db8:d822"
    )


def test_ipv4_compatible_form_is_denied_wholesale_even_for_a_public_payload():
    """Judgement call (Finding 1): IPv4-compatible (::a.b.c.d, no ffff) is a
    deprecated RFC 4291 encoding with no legitimate modern producer. It is
    denied wholesale rather than unwrapped-and-checked -- an attacker who
    controls their own domain's DNS can pick ANY embedded payload, including a
    public-looking one, purely to make the address look benign to a check
    that only unwraps-and-checks; refusing the entire (dead) format closes
    that off instead of trusting the payload. ::93.184.216.34 embeds a
    genuinely public IP and must still be refused under this policy."""
    with pytest.raises(EgressBlocked) as exc:
        check_host("evil.example.com", ["::93.184.216.34"], allowlisted=False)
    assert exc.value.reason == "blocked_private"


# ---------------------------------------------------------------------------
# Transport-level tests, which need a stub.
# ---------------------------------------------------------------------------


def _client_returning(response: httpx.Response) -> Any:
    """A transport that returns a fixed response and records requests."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response

    return httpx.MockTransport(handler), calls


async def test_redirect_is_terminal_and_not_followed(monkeypatch):
    """Spec §12.4.

    A followed redirect is a second request to an UNVALIDATED destination --
    the classic guard bypass. Per-hop re-validation is easy to get subtly
    wrong, so a 3xx is simply terminal.
    """
    transport, calls = _client_returning(
        httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})
    )
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr("app.core.egress._build_transport", lambda ip: transport)

    with pytest.raises(EgressBlocked) as exc:
        await post_json(
            "https://hooks.example.com/x", {"a": 1}, headers={}, allowlisted_hosts=set()
        )
    assert exc.value.reason == "redirect_refused"
    assert len(calls) == 1, "the redirect must not be followed"


async def test_dns_rebinding_cannot_swap_the_address(monkeypatch):
    """Spec §12.2.

    A resolver that answers public-then-private must not result in a
    connection to the private address. The guard resolves once, validates that
    answer, and CONNECTS TO THE VALIDATED IP -- it never re-resolves.
    """
    answers = iter([["93.184.216.34"], ["169.254.169.254"]])
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: next(answers))

    connected_to: list[str] = []

    def build(ip: str):
        connected_to.append(ip)
        return httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))

    monkeypatch.setattr("app.core.egress._build_transport", build)

    await post_json(
        "https://hooks.example.com/x", {"a": 1}, headers={}, allowlisted_hosts=set()
    )
    assert connected_to == ["93.184.216.34"], "connected to a re-resolved address"


async def test_response_body_is_never_returned_in_full(monkeypatch):
    """Spec §4.4 -- the response must not become a read oracle."""
    body = "SECRET-INTERNAL-CONTENT " * 100
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr(
        "app.core.egress._build_transport",
        lambda ip: httpx.MockTransport(lambda r: httpx.Response(500, text=body)),
    )

    got = await post_json(
        "https://hooks.example.com/x", {"a": 1}, headers={}, allowlisted_hosts=set()
    )
    assert got.status_code == 500
    assert len(got.excerpt) <= 500
    assert len(got.excerpt) < len(body)


async def test_response_body_read_is_actually_bounded_off_the_wire(monkeypatch):
    """Spec §4.4 -- '64 KiB read bound' means bytes actually pulled off the
    wire, not just the excerpt truncated afterwards. A naive implementation
    that lets httpx buffer the full response before slicing the excerpt would
    still fully drain an arbitrarily large (or endless) body first -- an easy
    OOM against the control plane from a single compromised or malicious
    webhook endpoint. This proves the read itself stops early, using a lazy
    async body the client must pull chunk-by-chunk to prove it was never
    fully drained.
    """
    produced = 0

    async def endless_body():
        nonlocal produced
        chunk = b"x" * 4096
        # Far larger than the 64 KiB cap; bounded (not truly infinite) so a
        # regression hangs the test instead of deadlocking it.
        for _ in range(10_000):
            produced += len(chunk)
            yield chunk

    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr(
        "app.core.egress._build_transport",
        lambda ip: httpx.MockTransport(lambda r: httpx.Response(200, content=endless_body())),
    )

    got = await post_json(
        "https://hooks.example.com/x", {"a": 1}, headers={}, allowlisted_hosts=set()
    )
    assert got.status_code == 200
    assert len(got.excerpt) <= 500
    assert produced < 1_000_000, "the endless body was drained far past the 64 KiB read cap"


async def test_slow_trickle_is_bounded_by_total_timeout(monkeypatch):
    """Spec §4.4 -- 'the whole request is deadlined ... 30s total'.

    httpx's per-phase read timeout resets on every chunk received, so a
    server trickling one byte just under the read-timeout window, forever,
    never trips it -- exactly the 'endpoint that trickles bytes must not pin
    a worker' case the spec calls out. Only a hard wall-clock deadline around
    the whole exchange closes that hole. Uses a tiny timeout_s so the test
    itself stays fast.
    """
    import asyncio

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, text="too slow")

    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr(
        "app.core.egress._build_transport",
        lambda ip: httpx.MockTransport(slow_handler),
    )

    with pytest.raises(TimeoutError):
        await post_json(
            "https://hooks.example.com/x",
            {"a": 1},
            headers={},
            allowlisted_hosts=set(),
            timeout_s=0.05,
        )


async def test_plain_http_is_rejected_for_a_non_allowlisted_host(monkeypatch):
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    with pytest.raises(EgressBlocked) as exc:
        await post_json("http://hooks.example.com/x", {}, headers={}, allowlisted_hosts=set())
    assert exc.value.reason == "scheme_not_allowed"


async def test_plain_http_is_allowed_for_an_allowlisted_host(monkeypatch):
    # An internal Mattermost may legitimately have no TLS. Forcing https there
    # would push admins to disable the guard entirely.
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["10.0.0.5"])
    monkeypatch.setattr(
        "app.core.egress._build_transport",
        lambda ip: httpx.MockTransport(lambda r: httpx.Response(204)),
    )
    got = await post_json(
        "http://mattermost.lan/hook", {}, headers={}, allowlisted_hosts={("mattermost.lan", 80)}
    )
    assert got.status_code == 204


async def test_pinned_ip_is_used_not_the_hostname(monkeypatch):
    """Spec §4.2 -- the client must connect to the validated IP, with the
    original Host preserved, rather than letting the HTTP layer re-resolve
    the hostname itself."""
    seen_urls: list[str] = []
    seen_hosts: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        seen_hosts.append(request.headers.get("host"))
        return httpx.Response(200, text="ok")

    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr("app.core.egress._build_transport", lambda ip: httpx.MockTransport(handler))

    await post_json(
        "https://hooks.example.com/x", {"a": 1}, headers={}, allowlisted_hosts=set()
    )
    # httpx suppresses the default port (443) when rendering the URL back to
    # a string; what matters is the connection target is the pinned IP, not
    # the original hostname.
    assert seen_urls == ["https://93.184.216.34/x"]
    assert seen_hosts == ["hooks.example.com"]


def test_egress_response_is_frozen():
    resp = EgressResponse(status_code=200, excerpt="ok")
    with pytest.raises(FrozenInstanceError):
        resp.status_code = 500  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Review Finding 2 -- the wall-clock deadline must cover DNS resolution, not
# just the HTTP phase. An attacker-controlled resolver for their own webhook
# domain must not be able to stall the calling worker past `timeout_s`.
# ---------------------------------------------------------------------------


async def test_dns_resolution_is_inside_the_wall_clock_deadline(monkeypatch):
    def slow_resolve(host: str, port: int) -> list[str]:
        time.sleep(3)
        return ["10.0.0.5"]

    monkeypatch.setattr("app.core.egress._resolve_all", slow_resolve)

    start = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await post_json(
            "https://attacker-slow-dns.example.com/hook",
            {"a": 1},
            headers={},
            allowlisted_hosts=set(),
            timeout_s=0.5,
        )
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"DNS resolution was not bounded by the deadline, took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Review Finding 4 -- allowlisting a host must not grant every TCP port on it.
#
# Task 4b changes the shape of `allowlisted_hosts` from `set[str]` to
# `set[tuple[str, int]]`: an admin now allowlists a HOST AND PORT together
# (migration 0026, `webhook_allowlist_entries.port`). This is what lets a
# real on-prem Mattermost on :8065, a Gitea on :3000, a Jenkins on :8080 --
# spec §0.1's whole justification for the allowlist -- become reachable again
# without reopening "allowlisting a host grants every port on it".
# ---------------------------------------------------------------------------


async def test_allowlisted_host_does_not_grant_a_non_default_port():
    """Spec §2.6 justifies host entries as 'auditable and specific'; a host
    allowlisted for one port is not thereby reachable on a different,
    unlisted port -- the DB, a cache, an SMTP relay, whatever else shares the
    box."""
    with pytest.raises(EgressBlocked) as exc:
        await post_json(
            "http://127.0.0.1:15432/hook",
            {"secret": "leak-me"},
            headers={},
            allowlisted_hosts={("127.0.0.1", 443)},  # allowlisted, but not THIS port
        )
    assert exc.value.reason == "port_not_allowed"


async def test_non_allowlisted_host_on_a_non_default_port_is_still_refused():
    """A host with NO allowlist entry at all keeps today's behaviour: only the
    scheme's default port is reachable. Task 4b does not loosen this -- it
    only lets an admin explicitly widen it, per host, via an allowlist
    entry."""
    with pytest.raises(EgressBlocked) as exc:
        await post_json(
            "https://public.example.com:8443/hook",
            {"a": 1},
            headers={},
            allowlisted_hosts=set(),
        )
    assert exc.value.reason == "port_not_allowed"


async def test_allowlisted_host_on_its_default_port_still_works(monkeypatch):
    """Control case: the port constraint must not break the ordinary allowlist
    path -- only the scheme's default port -- from working end to end."""
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["10.0.0.5"])
    monkeypatch.setattr(
        "app.core.egress._build_transport",
        lambda ip: httpx.MockTransport(lambda r: httpx.Response(204)),
    )
    got = await post_json(
        "http://mattermost.lan:80/hook",
        {},
        headers={},
        allowlisted_hosts={("mattermost.lan", 80)},
    )
    assert got.status_code == 204


async def test_mattermost_on_its_real_non_default_port_is_now_reachable(monkeypatch):
    """THE regression fix (Task 4b, Problem 1). Before this fix,
    `post_json("http://mattermost.lan:8065/hooks/x", ..., allowlisted_hosts=
    {"mattermost.lan"})` raised `EgressBlocked(reason="port_not_allowed")` --
    breaking the entire on-prem-Mattermost case spec §0.1 exists for. An
    allowlist entry that explicitly names the port (8065, Mattermost's real
    default) must now be dialable."""
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["10.0.0.5"])
    monkeypatch.setattr(
        "app.core.egress._build_transport",
        lambda ip: httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    got = await post_json(
        "http://mattermost.lan:8065/hooks/x",
        {"text": "hello"},
        headers={},
        allowlisted_hosts={("mattermost.lan", 8065)},
    )
    assert got.status_code == 200


async def test_non_allowlisted_port_on_an_allowlisted_host_is_still_refused():
    """The host IS allowlisted -- but only for :8065. Requesting a different
    port on the same host must still be refused with `port_not_allowed`; the
    allowlist entry names a specific destination, not the whole host."""
    with pytest.raises(EgressBlocked) as exc:
        await post_json(
            "http://mattermost.lan:9999/hooks/x",
            {"text": "hello"},
            headers={},
            allowlisted_hosts={("mattermost.lan", 8065)},
        )
    assert exc.value.reason == "port_not_allowed"


# ---------------------------------------------------------------------------
# Task 4b, Problem 2 -- spec §4.1: "the platform's own gateway/vault/database
# hosts read from config" must be denied, by NAME, even when they resolve to
# a public address and even if an allowlist entry names them. This is what
# protects a deployment whose gateway sits behind a public hostname (a
# reverse-proxied vault, a cloud-fronted gateway) -- the case generic private-
# range denial does not cover.
# ---------------------------------------------------------------------------


async def test_configured_gateway_host_is_refused():
    with override_settings(_settings(gateway_url="wss://gateway.example.com:8443/agent")):
        with pytest.raises(EgressBlocked) as exc:
            await post_json(
                "https://gateway.example.com/hook",
                {"a": 1},
                headers={},
                allowlisted_hosts=set(),
            )
    assert exc.value.reason == "blocked_platform_host"


async def test_configured_gateway_relay_host_is_refused():
    with override_settings(_settings(gateway_relay_url="wss://relay.example.com:8443/relay")):
        with pytest.raises(EgressBlocked) as exc:
            await post_json(
                "https://relay.example.com/hook",
                {"a": 1},
                headers={},
                allowlisted_hosts=set(),
            )
    assert exc.value.reason == "blocked_platform_host"


async def test_configured_database_host_is_refused():
    settings = _settings(database_url="postgresql+asyncpg://u:p@db.acme.internal:5432/its")
    with override_settings(settings):
        with pytest.raises(EgressBlocked) as exc:
            await post_json(
                "https://db.acme.internal/hook",
                {"a": 1},
                headers={},
                allowlisted_hosts=set(),
            )
    assert exc.value.reason == "blocked_platform_host"


async def test_configured_vault_openbao_host_is_refused():
    with override_settings(_settings(openbao_addr="https://vault.acme.internal:8200")):
        with pytest.raises(EgressBlocked) as exc:
            await post_json(
                "https://vault.acme.internal/hook",
                {"a": 1},
                headers={},
                allowlisted_hosts=set(),
            )
    assert exc.value.reason == "blocked_platform_host"


async def test_configured_platform_host_is_refused_even_behind_a_public_hostname(monkeypatch):
    """The exact deployment shape Problem 2 names: a gateway reachable at a
    public-looking hostname. This must be refused by NAME, before DNS is ever
    consulted -- proving the check does not depend on IP-range
    classification. `_resolve_all` is patched to raise if ever called, so the
    test fails loudly (not just differently) if the short-circuit is lost."""

    def _must_not_resolve(host: str, port: int) -> list[str]:
        raise AssertionError("a platform host must be refused before DNS resolution")

    monkeypatch.setattr("app.core.egress._resolve_all", _must_not_resolve)
    with override_settings(
        _settings(gateway_url="wss://gw.customer-public-domain.com:8443/agent")
    ):
        with pytest.raises(EgressBlocked) as exc:
            await post_json(
                "https://gw.customer-public-domain.com/hook",
                {"a": 1},
                headers={},
                allowlisted_hosts=set(),
            )
    assert exc.value.reason == "blocked_platform_host"


async def test_configured_platform_host_cannot_be_allowlisted():
    """A deployment must not be able to allowlist its own vault: a platform
    host stays refused even when an admin (accidentally or otherwise) adds an
    allowlist entry naming its EXACT (host, port) -- the guarantee is about
    the platform service's own address+port specifically, not about every
    port on that hostname (see
    `test_platform_host_different_port_same_address_is_permitted_when_allowlisted`
    below, which is the S1 fix: a genuinely different port on the same
    hostname IS reachable once explicitly allowlisted)."""
    with override_settings(_settings(gateway_url="wss://gateway.example.com:8443/agent")):
        with pytest.raises(EgressBlocked) as exc:
            await post_json(
                "https://gateway.example.com:8443/hook",
                {"a": 1},
                headers={},
                allowlisted_hosts={("gateway.example.com", 8443)},
            )
    assert exc.value.reason == "blocked_platform_host"


# ---------------------------------------------------------------------------
# S1/S2 fix -- the platform-host denial matched on hostname STRING only, which
# pulled in two opposite directions:
#
#   S1 (too broad): denial ignored port, so a single-box on-prem deployment
#   where the database/vault/gateway and an internal webhook receiver share
#   one hostname could never be notified on ANY port -- not even one an
#   admin explicitly allowlisted.
#   S2 (too narrow): denial by name is defeated by reaching the identical
#   address under a different name.
#
# Both are closed by comparing the RESOLVED ADDRESS AND PORT, not the name --
# see `app.core.egress._platform_layer1_denies` (by name, no DNS -- the cheap
# fast path and the fail-closed backstop when resolution fails) and
# `app.core.egress._platform_layer2_denies` (by resolved address, after DNS
# -- unconditional, ignores allowlisting, exactly like the by-name check
# always has).
# ---------------------------------------------------------------------------


async def test_platform_host_different_port_same_address_is_permitted_when_allowlisted(
    monkeypatch,
):
    """S1, closed. The exact bug report: a single-box deployment where the
    database also has a webhook receiver on a different port -- reaching the
    SAME hostname, on a genuinely DIFFERENT port than the database's own,
    with an explicit allowlist entry naming that port, must now succeed. Must
    FAIL against the pre-fix code, which denied this hostname on every port
    unconditionally.
    """
    # Keyed by host, not a single universal answer -- the destination
    # ("db.example.com") and the baseline settings' OTHER platform hosts
    # (`localhost`, from the default gateway URLs, which happens to share
    # port 8443 with this test's destination) must resolve to DIFFERENT
    # addresses, or the test would accidentally exercise a real
    # gateway-address collision instead of the different-port-same-host case
    # it is meant to test.
    addrs_by_host = {"db.example.com": ["203.0.113.9"], "localhost": ["198.51.100.1"]}
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: addrs_by_host[h])
    monkeypatch.setattr(
        "app.core.egress._build_transport",
        lambda ip: httpx.MockTransport(lambda r: httpx.Response(204)),
    )
    with override_settings(
        _settings(database_url="postgresql+asyncpg://u:p@db.example.com:5432/x")
    ):
        got = await post_json(
            "https://db.example.com:8443/hook",
            {"a": 1},
            headers={},
            allowlisted_hosts={("db.example.com", 8443)},
        )
    assert got.status_code == 204


async def test_platform_host_reached_by_different_name_same_address_and_port_is_refused(
    monkeypatch,
):
    """S2, closed. A webhook aimed at a DIFFERENT hostname that resolves to
    the exact same address as the configured platform host, on that platform
    service's own port, must be refused even with an explicit allowlist entry
    naming the alias -- naming it differently must not defeat the guard, and
    an allowlist entry must never grant access to a platform service's own
    address+port. Must FAIL against the pre-fix code, which only ever
    compared hostname strings and never noticed the alias resolved to the
    platform's own address.
    """
    addrs_by_host = {
        "gw.example.com": ["203.0.113.9"],
        "alias.example.com": ["203.0.113.9"],
    }
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: addrs_by_host[h])
    with override_settings(_settings(gateway_url="wss://gw.example.com:8443/agent")):
        with pytest.raises(EgressBlocked) as exc:
            await post_json(
                "https://alias.example.com:8443/hook",
                {"a": 1},
                headers={},
                allowlisted_hosts={("alias.example.com", 8443)},
            )
    assert exc.value.reason == "blocked_platform_host"


async def test_platform_host_resolution_failure_still_denies_by_name(monkeypatch):
    """Fail-closed, not fail-open. LAYER 1 (by name) needs no DNS at all, so
    the platform host's own configured name must stay refused even if
    resolution is completely broken -- proving the by-name check is not
    merely a fast path but the guarantee of last resort when resolution is
    unavailable.
    """

    def _boom(host: str, port: int) -> list[str]:
        raise AssertionError(
            "a platform host reached by its own configured name must be refused "
            "before DNS resolution is ever attempted"
        )

    monkeypatch.setattr("app.core.egress._resolve_all", _boom)
    with override_settings(_settings(gateway_url="wss://gateway.example.com:8443/agent")):
        with pytest.raises(EgressBlocked) as exc:
            await post_json(
                "https://gateway.example.com/hook",
                {"a": 1},
                headers={},
                allowlisted_hosts=set(),
            )
    assert exc.value.reason == "blocked_platform_host"


async def test_platform_host_own_resolution_failure_does_not_crash_layer2(monkeypatch):
    """LAYER 2's own resolution of a platform host can fail (a transient DNS
    blip for the database, say) without that failure escaping as a raw,
    uncategorized exception, and without it being treated as a match. The
    destination shares the platform database's port (so LAYER 2 actually
    attempts, and must survive, resolving the platform host) but is a
    genuinely different, allowlisted host -- it must still complete
    normally while the platform host's own DNS is unavailable."""

    def _resolve(host: str, port: int) -> list[str]:
        if host == "db.example.com":
            raise EgressBlocked("dns_failure", "simulated outage")
        return ["93.184.216.34"]

    monkeypatch.setattr("app.core.egress._resolve_all", _resolve)
    monkeypatch.setattr(
        "app.core.egress._build_transport",
        lambda ip: httpx.MockTransport(lambda r: httpx.Response(204)),
    )
    with override_settings(
        _settings(database_url="postgresql+asyncpg://u:p@db.example.com:5432/x")
    ):
        got = await post_json(
            "https://unrelated.example.com:5432/hook",
            {"a": 1},
            headers={},
            allowlisted_hosts={("unrelated.example.com", 5432)},
        )
    assert got.status_code == 204


# ---------------------------------------------------------------------------
# Review Finding 5 -- resolution/parse failures must become EgressBlocked with
# a reason category, not escape as a raw, uncategorized exception. Task 7's
# terminal-vs-retryable classification keys on `EgressBlocked.reason`.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Finding I1 -- a trailing root dot ("gw.example.com.") must not bypass the
# by-name platform-host denial. DNS treats the dotted and bare forms as the
# exact same name; `urlsplit(...).hostname` lowercases but does NOT strip the
# trailing dot, so the by-name string-equality check used to miss it entirely.
# ---------------------------------------------------------------------------


async def test_configured_platform_host_with_a_trailing_dot_is_still_refused():
    """The exact bypass the review demonstrated: `gw.example.com` was
    correctly refused, `gw.example.com.` (identical per DNS) was not."""
    with override_settings(
        _settings(gateway_url="wss://gw.customer-public-domain.com:8443/agent")
    ):
        with pytest.raises(EgressBlocked) as exc:
            await post_json(
                "https://gw.customer-public-domain.com./hook",
                {"a": 1},
                headers={},
                allowlisted_hosts=set(),
            )
    assert exc.value.reason == "blocked_platform_host"


async def test_configured_platform_host_with_trailing_dot_and_mixed_case_is_still_refused():
    with override_settings(
        _settings(gateway_url="wss://gw.customer-public-domain.com:8443/agent")
    ):
        with pytest.raises(EgressBlocked) as exc:
            await post_json(
                "https://GW.Customer-Public-Domain.COM./hook",
                {"a": 1},
                headers={},
                allowlisted_hosts=set(),
            )
    assert exc.value.reason == "blocked_platform_host"


async def test_trailing_dot_does_not_bypass_the_platform_host_check_even_when_allowlisted():
    """The platform-host check must not be satisfiable via the allowlist
    either -- `test_configured_platform_host_cannot_be_allowlisted`'s
    trailing-dot counterpart. Same (host, port) as the platform's own
    configured gateway -- the address+port collision case -- not merely a
    different port on the same name."""
    with override_settings(_settings(gateway_url="wss://gateway.example.com:8443/agent")):
        with pytest.raises(EgressBlocked) as exc:
            await post_json(
                "https://gateway.example.com.:8443/hook",
                {"a": 1},
                headers={},
                allowlisted_hosts={("gateway.example.com.", 8443), ("gateway.example.com", 8443)},
            )
    assert exc.value.reason == "blocked_platform_host"


async def test_allowlisted_host_matches_despite_a_trailing_dot(monkeypatch):
    """The other side of the same normalisation: a genuinely allowlisted
    on-prem host must still match when a caller's URL happens to spell it
    with a trailing dot -- the allowlist comparison uses the SAME normalised
    `host` the platform check does, not a second, unnormalised one."""
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["10.0.0.5"])
    monkeypatch.setattr(
        "app.core.egress._build_transport",
        lambda ip: httpx.MockTransport(lambda r: httpx.Response(204)),
    )
    got = await post_json(
        "http://mattermost.lan./hook",
        {},
        headers={},
        allowlisted_hosts={("mattermost.lan", 80)},
    )
    assert got.status_code == 204


async def test_overlong_hostname_is_a_categorized_dns_failure():
    url = f"https://{'a' * 300}/x"
    with pytest.raises(EgressBlocked) as exc:
        await post_json(url, {"a": 1}, headers={}, allowlisted_hosts=set())
    assert exc.value.reason == "dns_failure"


async def test_malformed_port_is_a_categorized_egress_block():
    with pytest.raises(EgressBlocked) as exc:
        await post_json(
            "https://host.example.com:999999/x", {"a": 1}, headers={}, allowlisted_hosts=set()
        )
    assert exc.value.reason == "invalid_url"
