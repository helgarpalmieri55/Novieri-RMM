"""Talking to Keycloak's admin API for user provisioning, and nothing else.

DELIBERATELY THIN, and modelled line for line on
`app/modules/keycloak_events/client.py`. This module exchanges a client
credential for a token and performs exactly six operations. Every decision
about *whether* an operation should happen -- the rails, the tenancy, the audit
-- is in `service.py`, which is what lets the interesting part be tested without
an HTTP server.

THE CREDENTIAL IS `manage-users`, ON A SEPARATE SERVICE-ACCOUNT CLIENT from
`it-suite-events` (`keycloak_admin_client_id`, default `it-suite-provisioning`).
Stated plainly because it is the cost of the feature (spec §2.1): `manage-users`
has no finer split in Keycloak, so whoever owns ITMS owns every identity in the
realm. The credential stays broader than the code. What is bounded is the code:

    create a user | enable/disable a user | execute-actions-email | delete a user
    | set or clear the caller's own `locale` attribute
    | remove one AI app's consent for one user (MCP revoke)

IMPERSONATION IS ALSO INSIDE `manage-users` AND IS NEVER CALLED. There is no
function here that reaches `/admin/realms/{realm}/users/{id}/impersonation`, and
`tests/test_user_admin_client.py` asserts that path does not appear in this
source, so adding one is a deliberate, reviewed act rather than a line in a
patch. Impersonation would let an ITMS operator obtain a session as any user in
the realm, which is the one capability that would make every audit actor id in
this platform unreliable.

NO PASSWORD EVER TRANSITS ITMS (spec §2.2 rule 1, invariant 3). Nothing here
generates, stores, reads, returns or transmits a password. "Reset the password"
means `execute-actions-email` with `UPDATE_PASSWORD` -- Keycloak mails the user
a link and ITMS never learns anything. The user representation this module POSTs
carries no `credentials` array, and a test asserts the serialized body contains
neither "password" nor "credentials".

EVERY CALL HAS A TIMEOUT. A Keycloak that is slow or wedged must not hold a
request worker open. Errors are RAISED rather than swallowed, because the
failures on this path -- a realm with no SMTP, a revoked credential, a taken
email address -- all look like success if they are quiet, and each of them
leaves an account somebody cannot sign into.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

__all__ = [
    "ENROLMENT_ACTIONS",
    "PASSKEY_REQUIRED_ACTION",
    "PASSWORD_RESET_ACTIONS",
    "KeycloakAdminConfig",
    "KeycloakAdminConnection",
    "KeycloakAdminError",
    "create_user",
    "delete_user",
    "execute_actions_email",
    "revoke_consent",
    "set_enabled",
    "set_locale_attribute",
]

#: Seconds. Generous enough for a busy Keycloak, short enough that a wedged one
#: does not occupy a request worker until the client gives up.
_TIMEOUT = 15.0

#: Spec §4. The first thing an ITMS-created user does is enrol a passkey; the
#: realm already carries the passwordless WebAuthn policy and this required
#: action, so this is the one line that connects the two.
PASSKEY_REQUIRED_ACTION = "webauthn-register-passwordless"

#: "Reset password", in full. There is no other branch, here or anywhere.
PASSWORD_RESET_ACTIONS: tuple[str, ...] = ("UPDATE_PASSWORD",)

#: First contact. A user created by ITMS has NO credential at all, so the
#: enrolment mail has to carry both halves: set something you can sign in with,
#: then enrol the passkey that replaces it.
ENROLMENT_ACTIONS: tuple[str, ...] = ("UPDATE_PASSWORD", PASSKEY_REQUIRED_ACTION)


class KeycloakAdminError(RuntimeError):
    """Keycloak answered in a shape this client cannot act on.

    Distinct from `httpx.HTTPStatusError` (a refusal, which carries a status the
    service maps) -- this is a successful response that did not contain what the
    protocol says it contains, e.g. a 201 with no `Location`.
    """


@dataclass(frozen=True)
class KeycloakAdminConfig:
    base_url: str
    realm: str
    client_id: str
    client_secret: str

    @classmethod
    def from_settings(cls, settings: Any) -> "KeycloakAdminConfig | None":
        """The config, or `None` when this deployment has not set provisioning up.

        `None` IS A SUPPORTED ANSWER, not an error -- an operator who has not
        configured the provisioning client has not broken anything, and the
        lifecycle routes refuse with a 503 naming the missing setting. But
        "configured with a missing or empty secret" is NOT that state: the
        operator believes provisioning is running, so it raises. The same
        three-way contract as `KeycloakEventsConfig.from_settings`.
        """
        url = getattr(settings, "keycloak_admin_url", None)
        secret_path = getattr(settings, "keycloak_admin_secret_path", None)
        if not url or not secret_path:
            return None
        path = Path(secret_path)
        if not path.is_file():
            raise FileNotFoundError(  # i18n-exempt: config error, logged; a plain 500
                f"keycloak_admin_secret_path points at {path}, which does not exist"
            )
        secret = path.read_text().strip()
        if not secret:
            raise ValueError(f"{path} is empty")  # i18n-exempt: config error, logged; a plain 500
        return cls(
            base_url=str(url).rstrip("/"),
            realm=getattr(settings, "keycloak_realm", "it-suite"),
            client_id=getattr(
                settings, "keycloak_admin_client_id", "it-suite-provisioning"
            ),
            client_secret=secret,
        )


async def _token(client: httpx.AsyncClient, config: KeycloakAdminConfig) -> str:
    """One client-credentials exchange.

    Its own `raise_for_status` because each leg needs one: a token call that
    succeeds and an admin call that 403s is exactly what "the client exists but
    nobody assigned `manage-users`" looks like, and a single check on the second
    leg alone cannot tell those apart from a bad secret.
    """
    response = await client.post(
        f"{config.base_url}/realms/{config.realm}/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": config.client_id,
            "client_secret": config.client_secret,
        },
    )
    response.raise_for_status()
    token: str = response.json()["access_token"]
    return token


def _users_url(config: KeycloakAdminConfig) -> str:
    return f"{config.base_url}/admin/realms/{config.realm}/users"


def _path_segment(value: str) -> str:
    """One id, as exactly one path segment, or a refusal.

    `quote(value, safe="")` is NOT enough on its own: `.` and `..` are
    unreserved characters it leaves alone, and httpx resolves dot segments
    before sending, so `.../users/{id}/consents/..` goes out as
    `DELETE .../users/{id}` -- the person's whole identity, not a consent.
    So an id that is empty, `.` or `..`, or that carries a `/` or `\\`, is
    refused BEFORE a URL is built and before anything is sent.
    """
    if not value or value in (".", "..") or "/" in value or "\\" in value:
        raise KeycloakAdminError(  # i18n-exempt: callers log a static template, never forwarded
            "refusing a Keycloak id that is not exactly one path segment"
        )
    return quote(value, safe="")


def _user_url(config: KeycloakAdminConfig, user_id: str, *tail: str) -> str:
    """`{realm}/users/{user_id}[/{tail}...]`, THE ONLY WAY an id reaches a URL
    path in this module.

    Two layers. Every segment passes `_path_segment`; and then the URL httpx
    will actually send is checked to end with exactly those quoted segments,
    so a segment the first layer missed cannot resolve into a different path.
    Either refusal raises `KeycloakAdminError` before a request is made.
    """
    segments = [_path_segment(user_id), *(_path_segment(part) for part in tail)]
    url = f"{_users_url(config)}/{'/'.join(segments)}"
    expected = "/users/" + "/".join(segments)
    if not httpx.URL(url).raw_path.decode("ascii").endswith(expected):
        raise KeycloakAdminError(  # i18n-exempt: callers log a static template, never forwarded
            "the Keycloak admin URL does not end with the path that was built"
        )
    return url


class KeycloakAdminConnection:
    """One HTTP client and one admin token, for a batch of calls.

    Transport, not an operation: it performs only the token exchange every
    operation already performs. A revoke for a whole organization passes one
    to `revoke_consent` per person instead of exchanging a token per person.

        async with KeycloakAdminConnection(config) as connection:
            await revoke_consent(config, user_id=..., client_id=..., connection=connection)
    """

    def __init__(self, config: KeycloakAdminConfig) -> None:
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._token = ""

    async def __aenter__(self) -> "KeycloakAdminConnection":
        client = httpx.AsyncClient(timeout=_TIMEOUT)
        try:
            self._token = await _token(client, self.config)
        except BaseException:
            await client.aclose()
            raise
        self._client = client
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise KeycloakAdminError(  # i18n-exempt: programmer error, never forwarded
                "the Keycloak admin connection is not open"
            )
        return self._client

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}


def _split_name(display_name: str) -> tuple[str, str]:
    """Keycloak's user representation has `firstName`/`lastName`; ITMS has one
    `display_name`. Split at the LAST space, so "Ada King Lovelace" gives
    ("Ada King", "Lovelace") and a mononym gives ("Ada", "").

    This is presentation only. Nothing in this platform keys on either half --
    `display_name` in ITMS is the authoritative presentation value (spec §7.1),
    and `deps.py` deliberately does not refresh it from the token.
    """
    first, _, last = display_name.strip().rpartition(" ")
    return (first, last) if first else (last, "")


async def create_user(
    config: KeycloakAdminConfig, *, email: str, display_name: str
) -> str:
    """Create the identity and return its Keycloak id, which IS the `sub` claim.

    THE ID COMES BACK IN `Location`, NOT IN A BODY. Keycloak answers 201 with an
    empty body and the new user's id as the last segment of the `Location`
    header. Reading it is what lets the ITMS row be written with the correct
    `external_subject` immediately (spec §2.2 rule 3) instead of waiting for the
    user's first login to reconcile.

    `username` is the email address: this realm authenticates by email, and a
    separate username would be a second identifier nobody maintains.

    NO CREDENTIAL IS SET. The account is created unable to sign in, and the
    enrolment mail is what makes it usable -- which is why a mail failure is
    reported rather than swallowed (spec §2.3).
    """
    first, last = _split_name(display_name)
    representation: dict[str, Any] = {
        "username": email,
        "email": email,
        "firstName": first,
        "lastName": last,
        "enabled": True,
        "emailVerified": False,
        "requiredActions": [PASSKEY_REQUIRED_ACTION],
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        token = await _token(client, config)
        response = await client.post(
            _users_url(config),
            json=representation,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
    location = response.headers.get("Location")
    if not location:
        raise KeycloakAdminError(  # i18n-exempt: _translating logs it, never forwarded
            "Keycloak accepted the user but returned no Location header, so the "
            "new subject id is unknown"
        )
    user_id: str = location.rstrip("/").rpartition("/")[2]
    if not user_id:
        raise KeycloakAdminError(  # i18n-exempt: _translating logs it, never forwarded
            f"Location header carried no user id: {location!r}"
        )
    return user_id


async def set_enabled(
    config: KeycloakAdminConfig, *, user_id: str, enabled: bool
) -> None:
    """Turn the identity on or off in Keycloak.

    A PARTIAL representation, deliberately: Keycloak merges it, so this cannot
    clobber an attribute ITMS does not know about. Note what disabling here does
    NOT do -- it stops the NEXT token being issued and does not revoke the one
    already in the operator's browser. That window is closed by `users.status`
    at the three admission doors, which is why the service writes both.
    """
    url = _user_url(config, user_id)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        token = await _token(client, config)
        response = await client.put(
            url,
            json={"enabled": enabled},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()


#: The fields of the user representation a locale write sends back. Under the
#: declarative user profile a PUT that omits these is read as clearing them, so
#: they travel (as the GET returned them); nothing else does.
_PROFILE_FIELDS: tuple[str, ...] = ("username", "email", "firstName", "lastName")


async def set_locale_attribute(
    config: KeycloakAdminConfig, *, user_id: str, locale: str | None
) -> None:
    """Mirror a person's saved language to their Keycloak `locale` attribute, so
    Keycloak's own pages and emails follow it (spec §3.5), and CHECK that it
    stuck.

    WHAT IS SENT. `PUT /users/{id}` under the declarative user profile
    (Keycloak 24+) reads a missing `email`, `firstName` or `lastName` as
    cleared, and it REPLACES the whole `attributes` map. So the body carries
    `username`, `email`, `firstName`, `lastName` exactly as the GET returned
    them, plus `attributes` with `locale` set (to `[locale]`) or removed (when
    `locale` is None) and every other attribute kept. It does NOT carry
    `enabled`, `requiredActions`, `emailVerified`, `notBefore`, `totp`,
    `access`, `credentials` or any other field: Keycloak leaves a field it is
    not sent untouched, so a language save cannot re-enable a user an admin
    disabled between our read and our write, or restore a required action
    somebody just cleared. Sending `{"attributes": {"locale": [..]}}` alone
    would erase `org`, the claim every tenancy decision here reads, and a
    profile field missing from the GET is left out rather than sent as null.

    WHAT THIS DOES NOT CLOSE. `attributes` is still replaced whole, from a
    GET that is a separate request. An administrator who changes `org` (or any
    other attribute) in Keycloak after our read and before our write has that
    change overwritten with the value we read. The window is one HTTP round
    trip on a healthy network and as long as the caller's latency budget on an
    unhealthy one; Keycloak offers no conditional update to close it. The
    field this platform means to change is `locale`; the others are carried,
    not owned.

    VERIFIED, NOT ASSUMED. A realm without internationalization answers the
    PUT 204 and drops `locale`, and an answer of 204 says nothing about what
    was kept. After the PUT the user is read again, on the same client and
    token, and `attributes["locale"]` must equal `[locale]` (or be absent when
    `locale` is None); otherwise this raises `KeycloakAdminError`, so the
    caller reports `false` rather than a success that did not happen.

    Mirrors exactly what is stored. Keycloak matches the value against the
    realm's supported locales, so a stored `en-GB` falls back to the default.

    RAISES on any failure and never logs: the caller decides that a Keycloak
    error must not fail the console's own save, and the user representation is
    personal data that does not belong in a log.
    """
    url = _user_url(config, user_id)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        token = await _token(client, config)
        auth = {"Authorization": f"Bearer {token}"}

        got = await client.get(url, headers=auth)
        got.raise_for_status()
        representation = got.json()
        if not isinstance(representation, dict):
            raise KeycloakAdminError(  # i18n-exempt: caller logs a static template, never forwarded
                "Keycloak returned a user that is not a JSON object"
            )
        body: dict[str, Any] = {
            field: representation[field]
            for field in _PROFILE_FIELDS
            if representation.get(field) is not None
        }
        raw = representation.get("attributes")
        attributes: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        if locale is None:
            attributes.pop("locale", None)
        else:
            attributes["locale"] = [locale]
        body["attributes"] = attributes

        put = await client.put(url, json=body, headers=auth)
        put.raise_for_status()

        check = await client.get(url, headers=auth)
        check.raise_for_status()
        kept = check.json()
        kept_attributes = kept.get("attributes") if isinstance(kept, dict) else None
        stored = kept_attributes.get("locale") if isinstance(kept_attributes, dict) else None
        wanted = None if locale is None else [locale]
        if stored != wanted:
            raise KeycloakAdminError(  # i18n-exempt: caller logs a static template, never forwarded
                "Keycloak accepted the locale update but did not keep it"
            )


async def execute_actions_email(
    config: KeycloakAdminConfig, *, user_id: str, actions: Sequence[str]
) -> None:
    """Ask Keycloak to mail the user a link that performs `actions`.

    THIS IS THE WHOLE OF "reset password" AND THE WHOLE OF "enrol" (spec §2.2
    rule 1, §2.3). ITMS asks; Keycloak generates the token, sends the mail, and
    validates the link. Nothing secret crosses this boundary in either
    direction, and the response body is empty.

    A realm with no `smtpServer` -- which is the state of
    `deploy/keycloak/realm-it-suite.json` today -- fails here with a 500, and
    that raise is load-bearing: the caller reports it so the operator knows the
    account is unreachable and can fix SMTP and resend.
    """
    url = _user_url(config, user_id, "execute-actions-email")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        token = await _token(client, config)
        response = await client.put(
            url,
            json=list(actions),
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()


async def delete_user(config: KeycloakAdminConfig, *, user_id: str) -> None:
    """Remove the Keycloak identity.

    THE KEYCLOAK IDENTITY ONLY (spec §2.2 rule 2). The ITMS `users` row is
    retained and marked `status='deleted'` by the service: 89 foreign-key
    columns across 21 tables reference `users.id`, audit `actor_id` among them,
    so a hard delete either fails on the constraints or shreds the history that
    makes the audit log evidence.
    """
    url = _user_url(config, user_id)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        token = await _token(client, config)
        response = await client.delete(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()


async def revoke_consent(
    config: KeycloakAdminConfig,
    *,
    user_id: str,
    client_id: str,
    connection: KeycloakAdminConnection | None = None,
) -> None:
    """Remove one AI app's consent for one person (MCP server design §3.4).

    `DELETE /users/{user_id}/consents/{client_id}` -- `client_id` is the
    OAuth client id (the token's `azp`), not Keycloak's internal id. Keycloak
    drops the consent, revokes that client's offline tokens for the user and,
    when a consent existed, logs the user's sessions out of that client, so
    the app's refresh token stops working and it cannot mint a new access
    token. Registered MCP apps always carry a consent: the realm's anonymous
    registration policy is Consent Required (`deploy/keycloak/init-clients.py`).
    The person has to connect the app again and consent again. The access token
    already in the app's hands is a signed JWT this cannot recall -- the
    `mcp_revocation` row is what refuses it, which is why the caller writes
    that row first and calls this after.

    WITHIN `manage-users`, the role this client already holds: consents are
    part of the user resource in Keycloak's admin API, so this needs no new
    grant.

    BOTH IDS GO THROUGH `_user_url`. Quoting alone does not make an id one
    segment -- `..` survives `quote(..., safe="")` and httpx resolves it, which
    turned `.../consents/..` into a DELETE of the user. An id that is empty,
    `.` or `..`, or carries `/` or `\\`, is refused before anything is sent, and
    the URL is checked to end with exactly `/users/{user}/consents/{client}`.

    `connection`, when given, is an open `KeycloakAdminConnection` whose client
    and token are reused: one token exchange for a whole organization's revoke.

    404 IS DONE: Keycloak answers it when there is neither a consent nor an
    offline token to remove (the person never connected the app, or already
    revoked it), or when the user or client no longer exists -- each is the
    state this call exists to reach. Anything else raises, and the caller
    counts it.
    """
    url = _user_url(config, user_id, "consents", client_id)
    if connection is None:
        async with KeycloakAdminConnection(config) as own:
            await _delete_consent(own, url)
        return
    await _delete_consent(connection, url)


async def _delete_consent(connection: KeycloakAdminConnection, url: str) -> None:
    response = await connection.client.delete(url, headers=connection.auth)
    if response.status_code == 404:
        return
    response.raise_for_status()
