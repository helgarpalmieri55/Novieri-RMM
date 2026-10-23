"""The realm's own configuration -- today, its SMTP block and nothing else.

Spec `docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §2.1.

WHY THIS EXISTS. Configuring email used to mean opening the identity provider's
admin console: a second product, a second password, a second mental model, and
an afternoon of an operator's life spent looking for a setting that lives
somewhere they had no reason to know about. It is now a tab in ITMS's own
Settings, and this module is the half that talks to the realm.

DELIBERATELY THIN, and modelled on `kc_client.py` line for line: config from the
same `KeycloakAdminConfig`, secret read from a file by that dataclass, one
client-credentials exchange per call, an explicit timeout on every request, and
errors RAISED rather than swallowed. Every decision about *whether* an operation
should happen -- the permission, the tenancy, the audit record -- is in
`app/api/v1/realm_settings.py`.

THE CREDENTIAL IS `manage-realm`, which is wider than `manage-users` and is
accounted for in spec §4 rather than assumed. The bargain: the human bootstrap
admin leaves daily operation entirely, so the platform holds one wider machine
credential instead of a standing human one, and every use of it is audited
through ITMS's own hash chain.

THE PASSWORD, WHICH IS THE WHOLE REASON THIS FILE IS CAREFUL
------------------------------------------------------------
It is the ONE stated exception to "no password ever enters the ITMS process"
(console-auth §1, restated in this cycle's plan). The accounting, in full:

  * It travels ONCE, INBOUND: browser -> API -> realm. It is never stored, never
    written to the vault, never logged (there is no logger in this module, and
    a test asserts that), and never returned.
  * The realm's own GET does NOT return it -- the identity provider replaces it
    with a fixed mask. `get_smtp` strips the key regardless of what came back,
    so a vendor that stopped masking cannot turn this into a credential in an
    API response.
  * An omitted password means "leave the stored one alone", expressed by
    sending the mask back exactly as it was received. That is the identity
    provider's own round-trip convention for a masked secret, and it is the
    only value available to send, because the real one was never disclosed to
    us. It is also what stops the common accident: editing the port must not
    clear the credential.

**Unverified against a live realm**, and stated here as the rest of this tree
states it: nothing in this module has run against a real identity provider. The
mask convention above is the vendor's documented behaviour, not something this
suite can prove; `MockTransport` answers whatever the tests say.

THE MERGE, WHICH IS THE OTHER WAY TO LOSE SOMETHING EXPENSIVE
--------------------------------------------------------------
`PUT /admin/realms/{realm}` REPLACES the realm representation. It is not a
patch. A bare `{"smtpServer": {...}}` PUT therefore does not mean "set the SMTP
block" -- it means "the realm is now this and nothing else", and on this
deployment that silently drops the login theme (the sign-in page this whole
cycle exists to keep) and the passwordless WebAuthn policy (passkey
authentication). So: read the realm, change one key, send the whole thing back.

Inside `smtpServer` the rule is the opposite and equally deliberate: the block
is REPLACED, because the ITMS form owns every field of it. A key the form did
not send is a key the operator cleared -- moving a relay from SSL to StartTLS
has to remove `ssl`, not leave both switched on. The masked secrets (`CARRIED_SECRETS`: the
password and the OAuth client secret) are the carried-over exceptions, for the
reason above.

THE TEST SEND, AND WHERE IT GOES
--------------------------------
The vendor's `testSMTPConnection` takes only the settings to test. It sends to
the email address of **whoever authenticated the call**, and that is this
platform's provisioning service account, never the operator who pressed the
button. A service account has no address, so until 2026-09-23 the vendor
answered every test with `Logged in user does not have an e-mail.` wrapped in
a bare 500. The button had never worked on any deployment. In production the
settings were fine: the same relay configuration sent mail from another host.

The recipient cannot be the operator's sign-in address. The only way to aim
this endpoint is to give the service account an address, and the realm refuses
two accounts with the same one (409 `User exists with same email`), so the
operator's own address is exactly the one it can never borrow. A tagged form
of it can be borrowed: `helgar+itms-test@toeshee.com` is a different account
address to the realm and the same inbox to Microsoft 365 and Gmail. That is
the console's suggestion.

**So the test goes to an address the operator picks, from a short list this
platform can vouch for**, and to the saved sending address when none is
picked:

  * the saved sending address itself;
  * the caller's OWN address, with or without a `+tag` -- the same local part
    and domain the caller's sign-in asserts;
  * any address in the organization's console sign-in domain
    (`ITS_CONSOLE_EMAIL_DOMAIN` or the org's own setting, resolved by
    `core.identity.policy`), when one is configured.

Until 2026-09-23 it could only ever go to the sending address, which answered
"does the relay accept this credential" and nothing an operator could check
from their own inbox. Why the list is this short:

  * An authenticated mail-sending primitive that took ANY address would send
    from the organization's own domain, through its own relay, to anyone. The
    content is the vendor's fixed test text, so the worst case is small, but it
    is a primitive this platform does not need to hold.
  * "The caller's DOMAIN" was the first version, and review showed why not: a
    caller signed in as `admin@gmail.com` would have opened all of gmail.com.
    The caller's own address is proof of one mailbox, not of a domain. The
    console sign-in domain is the only domain here that already means "this
    organization's people": it decides who may sign in at all.
  * The sending address's DOMAIN is not on the list for the same reason: it
    is one form field away from any value.
  * It is enforced in this function rather than only in the route, so no
    future caller can reach the vendor with an unchecked address.

The service account holds the address for the length of one call and is left
with NO address afterwards. It is cleared rather than left in place, because a
lingering address would make the realm refuse (409) to give a real person that
mailbox's address later, with an error nobody could trace to this button.

**Cleared, never "restored".** The service account's address exists only for
this call, so the right value afterwards is always empty, whatever it held
before. Saving and restoring the prior value was the first version of this
fix, and it was wrong: with two tests in flight, the second could read the
first's temporary address as "previous" and put it back PERMANENTLY, and every
later test would then preserve it too. Always clearing also repairs an address
left behind by a crash between the set and the clear.

Two tests pressed at the same moment can still race: one clears the address
while the other is sending, and the second fails with the vendor's 500. That
is rare, visible, and self-repairing: nothing is left behind, and pressing
again works. A lock that would have to span API workers is not worth the
complexity.

A success means the relay ACCEPTED the message, not that it arrived, which is
why the console's success copy names the mailbox to check and never shows a
green tick.
"""

from typing import Any

import httpx

from app.core.i18n import tr
from app.core.identity.email_domain import is_allowed_domain
from app.modules.user_admin.kc_client import KeycloakAdminConfig, KeycloakAdminError

__all__ = [
    "CARRIED_SECRETS",
    "MASKED_SECRET",
    "SMTP_FIELDS",
    "get_smtp",
    "send_test_email",
    "set_smtp",
]

#: Seconds. The same budget `kc_client` uses, for the same reason: generous
#: enough for a busy identity provider, short enough that a wedged one does not
#: occupy a request worker until the client gives up.
_TIMEOUT = 15.0

#: What the identity provider returns in place of a stored secret. Sending it
#: back unchanged is how "leave the password alone" is expressed; it is never
#: shown to a user and never returned by the API.
MASKED_SECRET = "**********"

#: The realm's own key names for the SMTP block, in the order the form shows
#: them. Named here so the API layer maps ITMS field names onto vendor ones in
#: exactly one place, and so a key that is not one of these cannot be smuggled
#: into the realm by a request body.
#:
#: The last five are Keycloak 26.2's token-based SMTP sign-in (XOAUTH2).
#: Keycloak performs the client-credentials exchange against `authTokenUrl`
#: itself and presents the token on its own SMTP connection, so this platform
#: never obtains or holds that token (mail-oauth design §3.1).
SMTP_FIELDS: tuple[str, ...] = (
    "host",
    "port",
    "from",
    "fromDisplayName",
    "replyTo",
    "envelopeFrom",
    "ssl",
    "starttls",
    "auth",
    "user",
    "password",
    "authType",
    "authTokenUrl",
    "authTokenScope",
    "authTokenClientId",
    "authTokenClientSecret",
)

#: Keys the realm masks on read, so an absent key in a write means "leave the
#: stored value alone". Both are credentials that travel once, inbound.
CARRIED_SECRETS: tuple[str, ...] = ("password", "authTokenClientSecret")

#: The key the realm holds the SMTP block under.
_SMTP = "smtpServer"


async def _token(client: httpx.AsyncClient, config: KeycloakAdminConfig) -> str:
    """One client-credentials exchange, with its own `raise_for_status`.

    Each leg needs one: a token call that succeeds and a realm call that 403s is
    exactly what "the client exists but nobody granted `manage-realm`" looks
    like, and a single check on the second leg cannot tell that from a bad
    secret.
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


def _realm_url(config: KeycloakAdminConfig) -> str:
    return f"{config.base_url}/admin/realms/{config.realm}"


async def _read_realm(
    client: httpx.AsyncClient, config: KeycloakAdminConfig, token: str
) -> dict[str, Any]:
    response = await client.get(
        _realm_url(config), headers={"Authorization": f"Bearer {token}"}
    )
    response.raise_for_status()
    realm: dict[str, Any] = response.json()
    return realm


def _stored_smtp(realm: dict[str, Any]) -> dict[str, str]:
    block = realm.get(_SMTP)
    return dict(block) if isinstance(block, dict) else {}


async def get_smtp(config: KeycloakAdminConfig) -> dict[str, str]:
    """The realm's SMTP block, WITHOUT the password, in the realm's own keys.

    An empty dict means the realm has no `smtpServer` at all -- the state of
    `deploy/keycloak/realm-it-suite.json` as shipped. That is an unconfigured
    deployment, not a fault, and the console renders it as "email is not set up"
    rather than as an error: telling those two apart is the difference between
    "finish the setup" and "call the vendor".

    The password is stripped whatever came back, mask or not. This platform does
    not get to rely on the vendor stripping it (invariant 3).
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        token = await _token(client, config)
        realm = await _read_realm(client, config, token)
    values = _stored_smtp(realm)
    for key in CARRIED_SECRETS:
        values.pop(key, None)
    return values


async def set_smtp(config: KeycloakAdminConfig, values: dict[str, str]) -> None:
    """Write `values` as the realm's SMTP block, changing nothing else.

    `values` uses the realm's own key names (`SMTP_FIELDS`). It REPLACES the
    block; the realm around it is merged. Omit `password` (or
    `authTokenClientSecret`) to keep whatever is stored -- see the module
    docstring for why that is the mask and not an empty string.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        token = await _token(client, config)
        realm = await _read_realm(client, config, token)

        block = {key: value for key, value in values.items() if key in SMTP_FIELDS}
        stored = _stored_smtp(realm)
        for key in CARRIED_SECRETS:
            held = stored.get(key)
            if key not in block and held is not None:
                # The value we were given by the GET, sent back unchanged. On a
                # live realm that is the mask, which the identity provider
                # reads as "unchanged"; there is no other value here, because
                # the real one was never disclosed to this process.
                block[key] = held

        # THE WHOLE REPRESENTATION, one key changed. See the module docstring:
        # this PUT is a replace, and a partial body silently drops the login
        # theme and the WebAuthn policy.
        realm[_SMTP] = block
        response = await client.put(
            _realm_url(config),
            json=realm,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()


#: The vendor's own username for a client's service account. The exact-match
#: search below needs only `manage-users`, which the provisioning client holds;
#: looking the client up by id would also need `view-clients`, which it does not.
_SERVICE_ACCOUNT_PREFIX = "service-account-"


async def _service_account(
    client: httpx.AsyncClient, config: KeycloakAdminConfig, headers: dict[str, str]
) -> dict[str, Any]:
    """The provisioning client's own user record, which is the principal the
    vendor's test endpoint sends to."""
    response = await client.get(
        f"{_realm_url(config)}/users",
        params={
            "username": f"{_SERVICE_ACCOUNT_PREFIX}{config.client_id}".lower(),
            "exact": "true",
            "briefRepresentation": "false",
        },
        headers=headers,
    )
    response.raise_for_status()
    found = response.json()
    if not isinstance(found, list) or len(found) != 1:
        raise KeycloakAdminError(
            tr(
                "the identity provider did not return this platform's service "
                "account, so a test message has no sender to go out as"
            )
        )
    user: dict[str, Any] = found[0]
    return user


async def _set_email(
    client: httpx.AsyncClient,
    config: KeycloakAdminConfig,
    headers: dict[str, str],
    user: dict[str, Any],
    email: str,
) -> httpx.Response:
    # The whole representation, one key changed: the same rule `set_smtp`
    # follows for the realm, for the same reason.
    return await client.put(
        f"{_realm_url(config)}/users/{user['id']}",
        json={**user, "email": email},
        headers=headers,
    )


def _split(address: str) -> tuple[str, str] | None:
    """(local part without any `+tag`, lower-cased domain) of a plausible single
    address; None otherwise.

    Deliberately small: the relay is the authority on whether an address
    exists. This only has to say which mailbox an address names, and refuse
    anything that is not one address.
    """
    address = address.strip()
    if (
        address.count("@") != 1
        or any(ch.isspace() or ch in ',;<>"' for ch in address)
        or len(address) > 254
    ):
        return None
    local, domain = address.split("@")
    domain = domain.lower().rstrip(".")
    base = local.split("+", 1)[0].lower()
    if not base or "." not in domain or domain.startswith("."):
        return None
    return base, domain


def _permitted(address: str, sender: str, caller_email: str, org_domain: str | None) -> bool:
    wanted = _split(address)
    if wanted is None:
        return False
    if address.strip().lower() == sender.strip().lower():
        return True
    if wanted == _split(caller_email):
        return True
    root = (org_domain or "").strip()
    return bool(root) and is_allowed_domain(address.strip(), root)


async def send_test_email(
    config: KeycloakAdminConfig,
    recipient: str | None = None,
    *,
    caller_email: str | None = None,
    org_domain: str | None = None,
) -> str:
    """Ask the identity provider to test the SAVED SMTP settings, and return the
    address the test message went to.

    `recipient` defaults to the saved sending address. When given, it must be
    that address, `caller_email` with or without a `+tag`, or an address in
    `org_domain` -- see the module docstring for why. Raises
    `KeycloakAdminError` for any other address, when there is nothing saved to
    test, or when the address belongs to a sign-in account. Lets any HTTP
    failure out, because a relay that refuses the connection is the whole
    point of the button.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        token = await _token(client, config)
        headers = {"Authorization": f"Bearer {token}"}

        realm = await _read_realm(client, config, token)
        settings = _stored_smtp(realm)
        if not settings.get("host"):
            raise KeycloakAdminError(
                tr("no email settings are saved yet, so there is nothing to test")
            )
        sender = settings.get("from", "")
        if recipient is None or not recipient.strip():
            if not sender:
                raise KeycloakAdminError(
                    tr(
                        "no sending address is saved, so there is nowhere to send a test "
                        "message"
                    )
                )
            address = sender
        else:
            address = recipient.strip()
            if _split(address) is None:
                raise KeycloakAdminError(tr("{address} is not an email address", address=address))
            if not _permitted(address, sender, caller_email or "", org_domain):
                root = (org_domain or "").strip()
                if root:
                    raise KeycloakAdminError(
                        tr(
                            "a test can only go to the sending address, to your own address "
                            "(a +tag is fine), or any address at {domain}",
                            domain=root,
                        )
                    )
                raise KeycloakAdminError(
                    tr(
                        "a test can only go to the sending address, to your own address "
                        "(a +tag is fine)"
                    )
                )

        account = await _service_account(client, config, headers)
        response = await _set_email(client, config, headers, account, address)
        if response.status_code == httpx.codes.CONFLICT:
            local, _, domain = address.partition("@")
            raise KeycloakAdminError(
                tr(
                    "{address} is the address of a sign-in account, and the identity "
                    "provider will not send a test to it. Add a tag before the @ to "
                    "reach the same inbox, for example {example}",
                    address=address,
                    example=f"{local.split('+')[0]}+itms-test@{domain}",
                )
            )
        response.raise_for_status()
        try:
            response = await client.post(
                f"{_realm_url(config)}/testSMTPConnection",
                json=settings,
                headers=headers,
            )
            response.raise_for_status()
        finally:
            cleared = await _set_email(client, config, headers, account, "")
            cleared.raise_for_status()
    return address
