"""Client ID Metadata Documents (autenticación propia design §5.5; plan
2026-11-06, Task 3; draft-ietf-oauth-client-id-metadata-document-03, the
current revision, where it is stricter than the -00 MCP cites [S29]).

`egress.get_bounded` is replaced by a fake that answers what each case needs:
the guard itself is `tests/test_egress_get_bounded.py`'s.
"""

import json
import logging
from datetime import timedelta
from typing import Any

import httpx
import pytest

from app.core import egress
from app.core.auth import loopback
from app.core.auth.oauth import cimd
from app.core.egress import EgressBlocked, EgressBody

URL = "https://claude.example/oauth/client-metadata.json"
GOOD: dict[str, Any] = {
    "client_id": URL,
    "client_name": "Claude Code",
    "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
    "token_endpoint_auth_method": "none",
    "grant_types": ["authorization_code", "refresh_token"],
}


@pytest.fixture(autouse=True)
def _fresh_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cimd, "_refusals", loopback.RefusalThrottle())


def _answer(
    monkeypatch: pytest.MonkeyPatch,
    document: object = GOOD,
    *,
    status: int = 200,
    truncated: bool = False,
    cache_control: str = "max-age=3600",
    encoding: str = "",
    raw: bytes | None = None,
    content_type: str = "application/json",
) -> list[str]:
    asked: list[str] = []

    async def fake(url: str, **kwargs: Any) -> EgressBody:
        asked.append(url)
        assert kwargs["max_bytes"] == cimd.MAX_DOCUMENT_BYTES
        content = raw if raw is not None else json.dumps(document).encode()
        return EgressBody(
            status_code=status,
            content=content,
            truncated=truncated,
            content_type=content_type,
            cache_control=cache_control,
            content_encoding=encoding,
        )

    monkeypatch.setattr(egress, "get_bounded", fake)
    return asked


@pytest.mark.parametrize(
    ("client_id", "ok"),
    [
        (URL, True),
        ("https://claude.example/c", True),
        ("http://claude.example/c.json", False),
        ("https://claude.example", False),
        ("https://claude.example/", False),
        ("https://claude.example/a/../c.json", False),
        ("https://claude.example/a/%2e%2e/c.json", False),
        ("https://claude.example/./c.json", False),
        ("https://claude.example/c.json#x", False),
        ("https://claude.example/c.json?x=1", False),
        ("https://user@claude.example/c.json", False),
        ("https://claude.example/c json", False),
        # ASCII only, as a redirect (Task 1, decision 3): no lookalike host, and
        # no 500 from a Host header httpx cannot encode.
        ("https://сlaude.example/c.json", False),
        ("https://bücher.example/c.json", False),
        ("itms_0123456789abcdef0123456789abcd", False),
        ("https://claude.example/" + "a" * 300, False),
    ],
)
def test_which_client_ids_name_a_document(client_id: str, ok: bool) -> None:
    """CIMD-03 §3: https, a path other than "/", no dot segment, no fragment,
    no user name; a query is refused here (§3: "SHOULD NOT")."""
    assert cimd.is_document_client_id(client_id) is ok


def test_a_document_on_the_issuers_own_origin_is_known_as_such() -> None:
    """CIMD-03 §10.1: "SHOULD reject a Client Identifier URL that is equal …
    to its own issuer identifier"."""
    assert cimd.on_origin("https://itms.example.com/mcp", "https://itms.example.com")
    assert cimd.on_origin("https://ITMS.example.com/x.json", "https://itms.example.com")
    # The default port written out, and the root dot (re-review f3-1, Minor 3).
    assert cimd.on_origin("https://itms.example.com:443/x.json", "https://itms.example.com")
    assert cimd.on_origin("https://itms.example.com./x.json", "https://itms.example.com")
    assert not cimd.on_origin(URL, "https://itms.example.com")
    assert not cimd.on_origin("https://itms.example.com:8443/x", "https://itms.example.com")


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("max-age=3600", 3600),
        ("public, max-age=300", 300),  # Claude Code's own document
        ("public, max-age=60", 300),  # the 5-minute floor
        ("max-age=604800", 86400),  # the 24-hour ceiling
        ('max-age="900"', 900),
        ("max-age=0", 300),
        ("no-store", 300),
        ("no-cache, max-age=3600", 3600),
        ("", 300),
        ("max-age=abc", 300),
        # Fix round 1: what the document's server writes cannot raise. A value
        # too large to hold is RFC 9111 §1.2.2's "2147483648 (2^31)", so the
        # ceiling, however many digits it has; a digit that is not ASCII is
        # not RFC 5234's DIGIT, so no max-age at all; leading zeros are not
        # size.
        ("max-age=100000000000000", 86400),
        pytest.param("max-age=" + "9" * 5000, 86400, id="max-age=9{5000}"),
        ("max-age=²", 300),
        ("max-age=٣٦٠٠", 300),
        ("max-age=" + "0" * 40 + "600", 600),
    ],
)
def test_a_valid_document_is_cached_within_bounds_whatever_its_headers(
    header: str, seconds: int
) -> None:
    """B3-03: `clamp(max-age or 0, 5 min, 24 h)` for every valid document
    (CIMD-03 §6.2: a maximum "regardless of the cache directives", a minimum
    "when none is otherwise indicated", and fetching "less often than
    requested by the cache headers" allowed)."""
    assert cimd.cache_lifetime(header) == timedelta(seconds=seconds)


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("max-age=100000000000000", 86400),
        pytest.param("max-age=" + "9" * 5000, 86400, id="max-age=9{5000}"),
        ("max-age=²", 300),
    ],
)
async def test_a_cache_header_never_escapes_as_an_error(
    monkeypatch, header: str, seconds: int
) -> None:
    """`fetch_document` returns a `ClientDocument` or raises `DocumentRefused`,
    nothing else: a `Cache-Control` the document's own server chose must not
    come out as an `OverflowError` or a `ValueError` -- a 500 at the consent
    screen, past the throttled log line (fix round 1)."""
    _answer(monkeypatch, cache_control=header)
    document = await cimd.fetch_document(URL)
    assert document.client_id == URL
    assert document.cache_for == timedelta(seconds=seconds)


@pytest.mark.parametrize("content_type", ["text/plain", "application/octet-stream", ""])
async def test_the_content_type_is_not_judged(monkeypatch, content_type: str) -> None:
    """CIMD-03 wants a JSON document, not a media type (`cimd.py`'s docstring
    quotes it): what the body parses to decides (fix round 1)."""
    _answer(monkeypatch, content_type=content_type)
    assert (await cimd.fetch_document(URL)).client_id == URL


async def test_a_valid_document_is_its_client(monkeypatch) -> None:
    asked = _answer(monkeypatch)
    document = await cimd.fetch_document(URL)
    assert asked == [URL]
    assert document.client_id == URL
    assert document.client_name == "Claude Code"
    assert document.redirect_uris == ("http://localhost/callback", "http://127.0.0.1/callback")
    assert document.cache_for == timedelta(hours=1)


async def test_without_grant_or_response_types_rfc_7591s_defaults_apply(monkeypatch) -> None:
    document = {k: v for k, v in GOOD.items() if k != "grant_types"}
    _answer(monkeypatch, document)
    assert (await cimd.fetch_document(URL)).client_id == URL


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"client_id": URL + "x"}, "mismatch"),
        ({"client_id": None}, "mismatch"),
        ({"client_name": ""}, "incomplete"),
        ({"client_name": None}, "incomplete"),
        ({"redirect_uris": []}, "incomplete"),
        ({"redirect_uris": "http://localhost/cb"}, "incomplete"),
        ({"redirect_uris": [f"https://c.example/{n}" for n in range(11)]}, "incomplete"),
        ({"redirect_uris": ["http://evil.example/cb"]}, "bad_redirect"),
        ({"redirect_uris": ["https://сlaude.ai/cb"]}, "bad_redirect"),
        ({"token_endpoint_auth_method": "client_secret_basic"}, "secret"),
        ({"token_endpoint_auth_method": "private_key_jwt"}, "secret"),
        ({"client_secret": "s3cret"}, "secret"),
        ({"client_secret_expires_at": 0}, "secret"),
        ({"grant_types": ["client_credentials"]}, "unsupported"),
        ({"grant_types": ["refresh_token"]}, "unsupported"),
        ({"grant_types": "authorization_code"}, "incomplete"),
        ({"response_types": ["token"]}, "unsupported"),
    ],
)
async def test_a_document_this_server_cannot_use_is_refused(
    monkeypatch, changes: dict[str, Any], reason: str
) -> None:
    _answer(monkeypatch, {**GOOD, **changes})
    with pytest.raises(cimd.DocumentRefused) as refused:
        await cimd.fetch_document(URL)
    assert refused.value.reason == reason
    assert refused.value.transient is False


@pytest.mark.parametrize(
    ("kwargs", "reason", "transient"),
    [
        ({"status": 404}, "not_ok", False),
        ({"status": 410}, "not_ok", False),
        ({"status": 500}, "unavailable", True),
        ({"status": 503}, "unavailable", True),
        ({"status": 429}, "unavailable", True),
        ({"truncated": True}, "too_large", False),
        ({"encoding": "gzip"}, "encoded", False),
        ({"raw": b"<html>"}, "not_json", False),
        ({"raw": b"[1, 2]"}, "not_json", False),
        ({"raw": b"\xff\xfe"}, "not_json", False),
    ],
)
async def test_an_answer_that_is_not_a_document_is_refused(
    monkeypatch, kwargs: dict[str, Any], reason: str, transient: bool
) -> None:
    _answer(monkeypatch, **kwargs)
    with pytest.raises(cimd.DocumentRefused) as refused:
        await cimd.fetch_document(URL)
    assert refused.value.reason == reason
    assert refused.value.transient is transient


@pytest.mark.parametrize(
    ("guard", "reason", "transient"),
    [
        ("blocked_private", "blocked", False),
        ("port_not_allowed", "blocked", False),
        # A resolver that did not answer retrieved nothing (CIMD-03 §6.1).
        ("dns_failure", "unavailable", True),
    ],
)
async def test_what_the_guard_refuses_is_blocked_or_transient(
    monkeypatch, guard: str, reason: str, transient: bool
) -> None:
    async def blocked(url: str, **kwargs: Any) -> EgressBody:
        raise EgressBlocked(guard, "no")

    monkeypatch.setattr(egress, "get_bounded", blocked)
    with pytest.raises(cimd.DocumentRefused) as refused:
        await cimd.fetch_document(URL)
    assert (refused.value.reason, refused.value.transient) == (reason, transient)


@pytest.mark.parametrize("error", [TimeoutError(), httpx.ConnectError("down"), OSError("reset")])
async def test_a_network_failure_is_transient(monkeypatch, error: Exception) -> None:
    async def failing(url: str, **kwargs: Any) -> EgressBody:
        raise error

    monkeypatch.setattr(egress, "get_bounded", failing)
    with pytest.raises(cimd.DocumentRefused) as refused:
        await cimd.fetch_document(URL)
    assert (refused.value.reason, refused.value.transient) == ("unavailable", True)


async def test_a_client_id_that_names_no_document_is_never_fetched(monkeypatch) -> None:
    asked = _answer(monkeypatch)
    with pytest.raises(cimd.DocumentRefused) as refused:
        await cimd.fetch_document("http://claude.example/c.json")
    assert refused.value.reason == "not_a_document_url"
    assert asked == []


async def test_a_refusal_is_logged_with_the_reason_and_the_host_only(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    _answer(monkeypatch, {**GOOD, "client_secret": "s3cret"})
    with caplog.at_level(logging.WARNING, logger="its.auth.oauth.cimd"):
        with pytest.raises(cimd.DocumentRefused):
            await cimd.fetch_document(URL)
    (record,) = [r for r in caplog.records if r.name == "its.auth.oauth.cimd"]
    assert record.getMessage() == "client metadata document refused"
    assert record.__dict__["reason"] == "secret"
    assert record.__dict__["host"] == "claude.example"
    assert record.__dict__["unlogged"] == 0
    assert "s3cret" not in caplog.text
    assert URL not in caplog.text


async def test_refusals_are_logged_at_most_once_a_minute_with_a_count(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """B3-32, as C-M8 for the internal token: anybody with a session can make
    the server read a document, so the line cannot be written at request rate."""
    clock = [1000.0]
    monkeypatch.setattr(loopback, "_monotonic", lambda: clock[0])
    _answer(monkeypatch, {**GOOD, "client_secret": "s3cret"})
    with caplog.at_level(logging.WARNING, logger="its.auth.oauth.cimd"):
        for _ in range(5):
            with pytest.raises(cimd.DocumentRefused):
                await cimd.fetch_document(URL)
        clock[0] += loopback.NETWORK_REFUSAL_EVERY_S
        with pytest.raises(cimd.DocumentRefused):
            await cimd.fetch_document(URL)
    records = [r for r in caplog.records if r.name == "its.auth.oauth.cimd"]
    assert [r.__dict__["unlogged"] for r in records] == [0, 4]
