"""Microsoft's endpoints, pinned in code, and the PKCE arithmetic.

The token exchange posts a refresh token and a client secret, and
`app/core/egress.py` does not guard it. A configurable token URL would let an
`email_intake.admin` holder send both to a host of their choosing, so the
operator supplies a directory (tenant) ID and the host is a constant.
Design: docs/superpowers/specs/2026-09-11-mail-oauth-design.md §4.6, §5.2.
"""

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest

from app.core.config import Settings
from app.modules.email_intake.microsoft import (
    CALLBACK_PATH,
    IMAP_SCOPE,
    MICROSOFT_HOST,
    MicrosoftNotConfigured,
    authorize_url,
    challenge_of,
    make_verifier,
    redirect_uri,
    token_url,
)


def _settings(public_console_url: str | None) -> Settings:
    return Settings.model_construct(public_console_url=public_console_url)


def test_the_token_host_is_microsofts() -> None:
    assert MICROSOFT_HOST == "https://login.microsoftonline.com"
    assert token_url("contoso.onmicrosoft.com") == (
        "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/token"
    )
    assert token_url("11111111-2222-3333-4444-555555555555").startswith(MICROSOFT_HOST + "/")


@pytest.mark.parametrize(
    "hostile",
    ["../../evil.example", "evil.example/x", "a?b", "a#b", "a/b", "", "@", "a b", "a\\b", ".a"],
)
def test_a_tenant_id_cannot_move_the_host(hostile: str) -> None:
    with pytest.raises(ValueError):
        token_url(hostile)
    with pytest.raises(ValueError):
        authorize_url(
            tenant_id=hostile,
            client_id="c",
            redirect_uri="https://x",
            state="s",
            code_challenge="c",
        )


def test_the_authorize_url_carries_pkce_the_offline_scope_and_select_account() -> None:
    url = authorize_url(
        tenant_id="contoso.onmicrosoft.com",
        client_id="cid",
        redirect_uri="https://itms.example/settings/email-intake/oauth/callback",
        state="st",
        code_challenge="ch",
    )
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}" == MICROSOFT_HOST
    assert parsed.path == "/contoso.onmicrosoft.com/oauth2/v2.0/authorize"
    query = parse_qs(parsed.query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["cid"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == ["ch"]
    assert query["state"] == ["st"]
    assert query["scope"] == [IMAP_SCOPE]
    assert query["redirect_uri"] == ["https://itms.example/settings/email-intake/oauth/callback"]
    # A second mailbox must not be silently connected as the signed-in account.
    assert query["prompt"] == ["select_account"]


def test_the_scope_asks_for_imap_and_a_refresh_token() -> None:
    assert IMAP_SCOPE == "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"


def test_the_challenge_is_s256_of_the_verifier() -> None:
    verifier = make_verifier()
    assert 43 <= len(verifier) <= 128
    assert make_verifier() != verifier
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    assert challenge_of(verifier) == expected


def test_the_redirect_uri_comes_from_config() -> None:
    assert CALLBACK_PATH == "/settings/email-intake/oauth/callback"
    assert redirect_uri(_settings("https://itms.example")) == (
        "https://itms.example/settings/email-intake/oauth/callback"
    )
    assert redirect_uri(_settings("https://itms.example/")) == (
        "https://itms.example/settings/email-intake/oauth/callback"
    )


@pytest.mark.parametrize("value", [None, "", "itms.example", "ftp://x", "https://x/?a=b"])
def test_an_unset_or_unusable_console_address_is_not_configured(value: str | None) -> None:
    with pytest.raises(MicrosoftNotConfigured, match="ITS_PUBLIC_CONSOLE_URL"):
        redirect_uri(_settings(value))
