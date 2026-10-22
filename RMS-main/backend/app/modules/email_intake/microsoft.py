"""Microsoft's sign-in endpoints for email intake, and the PKCE arithmetic.

The host is a constant and the operator supplies only a directory (tenant) ID.
The token exchange carries a refresh token and a client secret and
`app/core/egress.py` does not guard it, so this pin is the whole control
against an `email_intake.admin` holder pointing it at their own host
(design §4.6). Settings → Email takes a free-form token URL because that value
is stored in the realm and dialled by the realm, not by this process.

No token is obtained here: `oauth.py` (start/complete) and `tokens.py`
(refresh) make the exchanges, inside a session they can audit into.
"""

import base64
import hashlib
import re
import secrets
from urllib.parse import urlencode, urlsplit

import httpx

from app.core.config import Settings
from app.core.i18n import tr

__all__ = [
    "CALLBACK_PATH",
    "IMAP_SCOPE",
    "MICROSOFT_HOST",
    "MicrosoftNotConfigured",
    "SMTP_APP_SCOPE",
    "SMTP_HOST",
    "SMTP_PORT",
    "authorize_url",
    "challenge_of",
    "http_client",
    "make_verifier",
    "redirect_uri",
    "token_url",
]

MICROSOFT_HOST = "https://login.microsoftonline.com"

#: `offline_access` is what yields a refresh token; without it the connection
#: works for one hour and cannot be renewed.
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"

#: Client-credentials scope for SMTP.SendAsApp (notification email).
SMTP_APP_SCOPE = "https://outlook.office365.com/.default"

#: Where notification email signs in with that token. Pinned, like the token
#: host: the token can send as the mailbox, so it only ever goes to Microsoft.
SMTP_HOST = "smtp.office365.com"
SMTP_PORT = 587

#: A console (SPA) route, not an API route: the completing call is the
#: console's own authenticated request afterwards (design §4.3).
CALLBACK_PATH = "/settings/email-intake/oauth/callback"

#: A directory GUID or a verified domain. It is interpolated into a URL path,
#: so anything that could add a segment, a query or a fragment is refused.
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,199}$")


class MicrosoftNotConfigured(Exception):
    """`ITS_PUBLIC_CONSOLE_URL` is unset or unusable. A supported state for an
    install that never uses Microsoft; the routes answer 503 naming it."""


def _tenant(tenant_id: str) -> str:
    if not _TENANT_RE.match(tenant_id):
        raise ValueError(tr("the directory (tenant) ID is not a valid ID or domain"))
    return tenant_id


def token_url(tenant_id: str) -> str:
    return f"{MICROSOFT_HOST}/{_tenant(tenant_id)}/oauth2/v2.0/token"


def authorize_url(
    *,
    tenant_id: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": IMAP_SCOPE,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # Otherwise a second mailbox is silently connected as whoever is
            # already signed in to Microsoft in this browser.
            "prompt": "select_account",
        }
    )
    return f"{MICROSOFT_HOST}/{_tenant(tenant_id)}/oauth2/v2.0/authorize?{query}"


#: Seconds, per request. A wedged token endpoint must not hold a worker.
HTTP_TIMEOUT_S = 15.0


def http_client() -> httpx.AsyncClient:
    """The client every token exchange uses; tests replace this one seam."""
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)


def make_verifier() -> str:
    """86 characters of unreserved ASCII (RFC 7636 allows 43-128)."""
    return secrets.token_urlsafe(64)


def challenge_of(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def redirect_uri(settings: Settings) -> str:
    """From configuration, never from `Host` or `X-Forwarded-Host`: a redirect
    URI taken from the request is host-header injection (design §5.2)."""
    base = (getattr(settings, "public_console_url", None) or "").strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
        raise MicrosoftNotConfigured(
            tr(
                "Microsoft sign-in is not available on this server: ITS_PUBLIC_CONSOLE_URL "
                "must be set to the https address people open the console at. That is a "
                "server setting, not something this screen can fix."
            )
        )
    return f"{base}{CALLBACK_PATH}"
