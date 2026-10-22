"""Reading and writing the realm's SMTP block through the admin API.

Spec `docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §2.1.

MIRRORS `tests/test_user_admin_client.py` EXACTLY in shape -- `MockTransport`, a
factory captured at import, and assertions on the request this code SENDS. It
proves nothing about what a real identity provider replies; that gap is stated
in the spec and lives on the operator's deployment, as every other Keycloak
surface here does.

THE PASSWORD IS THE INTERESTING PART. It travels once, on the way in, and is
never read back: the realm's GET returns the SMTP block WITHOUT the password --
the identity provider replaces it with a fixed mask -- and `get_smtp` must not
invent one. A form that round-tripped a password it never received would write
an empty string over a working credential the first time somebody edited the
port.

THE SECOND PROPERTY IS THE MERGE. `PUT /admin/realms/{realm}` REPLACES the realm
representation. A bare `{"smtpServer": ...}` PUT is therefore not "set the SMTP
block" -- it is "make the realm be this", and on this deployment that would drop
the login theme and the passwordless WebAuthn policy, which is every operator's
ability to sign in. The realm is read, one key is changed, and the whole
representation goes back.
"""

import json
from typing import Any

import httpx
import pytest

from app.modules.user_admin.kc_client import KeycloakAdminConfig, KeycloakAdminError
from app.modules.user_admin.realm_config import (
    MASKED_SECRET,
    get_smtp,
    send_test_email,
    set_smtp,
)

# `asyncio_mode = "auto"` (pyproject.toml), so async tests need no marker.

#: Captured at import, before any fixture swaps the name -- a factory that
#: resolved `httpx.AsyncClient` by name would recurse into itself.
_REAL_CLIENT = httpx.AsyncClient

CONFIG = KeycloakAdminConfig(
    base_url="https://keycloak.test",
    realm="it-suite",
    client_id="it-suite-provisioning",
    client_secret="not-a-real-secret",
)

SERVICE_ACCOUNT_ID = "3f0a2b41-9c1e-4c0e-9a2b-0d1e2f3a4b5c"

#: The two realm settings this deployment would lose to a bare
#: `{"smtpServer": ...}` PUT. Named rather than represented by a generic
#: "some other key", because these are the ones that actually cost something:
#: the theme is the sign-in page the whole cycle exists to keep, and the policy
#: is passkey authentication.
REALM_REPRESENTATION: dict[str, Any] = {
    "id": "it-suite",
    "realm": "it-suite",
    "enabled": True,
    "loginTheme": "it-suite",
    "webAuthnPolicyPasswordlessRpEntityName": "ITMS",
    "webAuthnPolicyPasswordlessUserVerificationRequirement": "required",
    "smtpServer": {
        "host": "smtp.old.internal",
        "port": "25",
        "from": "old@toeshee.com",
        "auth": "true",
        "user": "old-user",
        # What a live realm answers with: the stored value is masked, never
        # returned. The fixture asserts this code copes with the mask rather
        # than with a convenient absence.
        "password": MASKED_SECRET,
    },
}


class FakeRealm:
    """A realm that records what it was asked and answers what the test says."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.representation: dict[str, Any] = json.loads(json.dumps(REALM_REPRESENTATION))
        #: The provisioning client's own user. It has no address, as on a
        #: live realm, and it is the principal the test endpoint sends to.
        self.service_account: dict[str, Any] = {
            "id": SERVICE_ACCOUNT_ID,
            "username": "service-account-it-suite-provisioning",
            "enabled": True,
        }
        self.get_status = 200
        self.put_status = 204
        self.test_status = 204
        self.user_put_status = 204
        #: The service account's address at the moment the test was asked for,
        #: which is where the vendor sends it.
        self.recipient_at_test: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "tok-123"})
        self.seen.append(request)
        if path.endswith("/testSMTPConnection"):
            self.recipient_at_test = self.service_account.get("email")
            return httpx.Response(self.test_status)
        if path == "/admin/realms/it-suite/users" and request.method == "GET":
            return httpx.Response(200, json=[self.service_account])
        if path == f"/admin/realms/it-suite/users/{SERVICE_ACCOUNT_ID}":
            assert request.method == "PUT", f"unexpected {request.method} {path}"
            if self.user_put_status < 300:
                self.service_account = json.loads(request.content)
            return httpx.Response(self.user_put_status)
        if request.method == "GET":
            return httpx.Response(self.get_status, json=self.representation)
        if request.method == "PUT":
            return httpx.Response(self.put_status)
        raise AssertionError(f"unexpected call: {request.method} {path}")

    def calls(self, method: str, suffix: str = "") -> list[httpx.Request]:
        return [r for r in self.seen if r.method == method and r.url.path.endswith(suffix)]


@pytest.fixture()
def realm(monkeypatch: pytest.MonkeyPatch) -> Any:
    state = FakeRealm()

    def factory(**kw: Any) -> httpx.AsyncClient:
        state.kwargs = kw  # type: ignore[attr-defined]
        return _REAL_CLIENT(transport=httpx.MockTransport(state.handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return state


def _put_body(realm: FakeRealm) -> dict[str, Any]:
    (put,) = [r for r in realm.calls("PUT") if "/users/" not in r.url.path]
    body: dict[str, Any] = json.loads(put.content)
    return body


# --------------------------------------------------------------------------- #
# get_smtp
# --------------------------------------------------------------------------- #


async def test_get_smtp_reads_the_realm_block(realm: Any) -> None:
    """One GET, against the realm itself -- there is no SMTP-only endpoint."""
    values = await get_smtp(CONFIG)

    (read,) = realm.calls("GET")
    assert read.url.path == "/admin/realms/it-suite"
    assert values["host"] == "smtp.old.internal"
    assert values["port"] == "25"
    assert values["from"] == "old@toeshee.com"
    assert values["user"] == "old-user"


async def test_get_smtp_never_returns_a_password(realm: Any) -> None:
    """NEITHER the mask NOR a value a misbehaving realm handed back.

    Two cases, because they fail differently. The mask is what a live realm
    sends, and passing it on would put a fixed string in the form that the next
    save writes back as the password. A REAL value would be a credential in an
    API response (invariant 3) -- this platform does not get to rely on the
    vendor stripping it.
    """
    values = await get_smtp(CONFIG)
    assert "password" not in values
    assert MASKED_SECRET not in json.dumps(values)

    realm.representation["smtpServer"]["password"] = "hunter2-the-real-one"
    values = await get_smtp(CONFIG)
    assert "password" not in values
    assert "hunter2-the-real-one" not in json.dumps(values)


async def test_get_smtp_on_a_realm_with_no_smtp_is_empty_rather_than_an_error(
    realm: Any,
) -> None:
    """The shipped realm has no `smtpServer` at all (spec §2.3). That is an
    unconfigured deployment, not a fault: the console renders an empty form and
    the Users tab shows its banner. Raising here would make "not set up yet"
    indistinguishable from "the identity provider is down"."""
    realm.representation.pop("smtpServer")

    assert await get_smtp(CONFIG) == {}


async def test_a_refused_read_raises(realm: Any) -> None:
    """`manage-realm` may not be granted (spec §4 widens the client, and an
    operator upgrading an existing deployment has not re-run the init job). A
    403 swallowed into an empty form reads as "no email configured" on a
    deployment that has some, and the next save would overwrite it."""
    realm.get_status = 403

    with pytest.raises(httpx.HTTPStatusError):
        await get_smtp(CONFIG)


# --------------------------------------------------------------------------- #
# set_smtp -- the merge, and the one value that travels inbound only
# --------------------------------------------------------------------------- #


async def test_set_smtp_merges_rather_than_replacing_the_realm(realm: Any) -> None:
    """THE PUT CARRIES THE WHOLE REALM with only `smtpServer` changed.

    `PUT /admin/realms/{realm}` is a replace, not a patch. A bare
    `{"smtpServer": ...}` would take the login theme and the passwordless
    WebAuthn policy with it -- on this deployment, the sign-in page and passkey
    authentication -- and every one of those losses is silent.
    """
    await set_smtp(CONFIG, {"host": "smtp.new.internal", "port": "587"})

    body = _put_body(realm)
    assert body["loginTheme"] == "it-suite"
    assert body["webAuthnPolicyPasswordlessRpEntityName"] == "ITMS"
    assert body["webAuthnPolicyPasswordlessUserVerificationRequirement"] == "required"
    assert body["realm"] == "it-suite"
    assert body["smtpServer"]["host"] == "smtp.new.internal"
    assert body["smtpServer"]["port"] == "587"


async def test_set_smtp_replaces_the_smtp_block_rather_than_layering_over_it(
    realm: Any,
) -> None:
    """Inside `smtpServer`, the supplied values are the WHOLE block.

    The form owns every field of it, so a key it did not send is a key the
    operator cleared -- switching a relay from SSL to StartTLS has to remove
    `ssl`, not leave both set. The realm around it is merged; the block itself
    is not.
    """
    realm.representation["smtpServer"]["ssl"] = "true"

    await set_smtp(CONFIG, {"host": "smtp.new.internal", "starttls": "true"})

    block = _put_body(realm)["smtpServer"]
    assert "ssl" not in block
    assert block["starttls"] == "true"
    assert block["host"] == "smtp.new.internal"


async def test_a_supplied_password_is_written_through_verbatim(realm: Any) -> None:
    """THE ONE STATED EXCEPTION (spec §4, plan's global constraints). The
    password crosses this process on its way to the realm and is not stored,
    logged or read back. What it must not do is arrive changed."""
    await set_smtp(CONFIG, {"host": "smtp.new.internal", "password": "s3cr3t"})

    assert _put_body(realm)["smtpServer"]["password"] == "s3cr3t"


async def test_an_omitted_password_keeps_the_stored_one(realm: Any) -> None:
    """EDITING THE PORT MUST NOT CLEAR THE CREDENTIAL.

    The console cannot send a password it was never given, so "absent" means
    "leave it alone". The mask the realm returned is what goes back -- the
    identity provider reads its own sentinel as "unchanged", and it is the only
    value available, because the real one was never disclosed. Writing `""`
    here, or dropping the key, is the failure this test exists for: a working
    relay broken by somebody correcting a typo in the display name.
    """
    await set_smtp(CONFIG, {"host": "smtp.old.internal", "port": "587"})

    assert _put_body(realm)["smtpServer"]["password"] == MASKED_SECRET


async def test_an_omitted_password_on_a_realm_that_has_none_stays_absent(
    realm: Any,
) -> None:
    """Non-vacuity for the rule above: "carry the stored one" must not become
    "always send the mask", which would configure an unauthenticated relay with
    a literal password of ten asterisks."""
    realm.representation["smtpServer"].pop("password")

    await set_smtp(CONFIG, {"host": "smtp.new.internal"})

    assert "password" not in _put_body(realm)["smtpServer"]


async def test_a_refused_write_raises_so_the_console_does_not_report_a_save(
    realm: Any,
) -> None:
    """A 403 here is the provisioning client lacking `manage-realm`. Swallowed,
    the console says "Saved." over a realm that did not change, and the operator
    goes looking for the invitation that never arrives."""
    realm.put_status = 403

    with pytest.raises(httpx.HTTPStatusError):
        await set_smtp(CONFIG, {"host": "smtp.new.internal"})


# --------------------------------------------------------------------------- #
# send_test_email
# --------------------------------------------------------------------------- #


async def test_the_test_goes_to_the_saved_sending_address(realm: Any) -> None:
    """THE VENDOR SENDS ITS TEST TO WHOEVER MADE THE CALL, which is the service
    account, which has no address. Until 2026-09-23 this function did not
    account for that, and every press of the button got the vendor's 500
    `Logged in user does not have an e-mail.`

    So the service account is given the saved sending address for the length
    of the call. This test asserts that the address was in place WHEN THE TEST
    RAN, since a check afterwards would pass on an implementation that never
    set it.
    """
    recipient = await send_test_email(CONFIG)

    assert recipient == "old@toeshee.com"
    assert realm.recipient_at_test == "old@toeshee.com"


async def test_the_callers_own_address_with_a_tag_is_used(realm: Any) -> None:
    recipient = await send_test_email(
        CONFIG, "helgar+itms-test@toeshee.com", caller_email="helgar@toeshee.com"
    )

    assert recipient == "helgar+itms-test@toeshee.com"
    assert realm.recipient_at_test == "helgar+itms-test@toeshee.com"


async def test_the_sending_address_itself_may_be_named(realm: Any) -> None:
    assert await send_test_email(CONFIG, "old@toeshee.com") == "old@toeshee.com"


async def test_any_address_in_the_organizations_sign_in_domain_is_used(realm: Any) -> None:
    recipient = await send_test_email(
        CONFIG,
        "it-team@mail.toeshee.com",
        caller_email="helgar@toeshee.com",
        org_domain="toeshee.com",
    )

    assert recipient == "it-team@mail.toeshee.com"


@pytest.mark.parametrize(
    "address",
    [
        "someone@elsewhere.com",
        # The sending address's DOMAIN is not enough: it is one form field
        # away from any value.
        "colleague@toeshee.com",
        # The caller's DOMAIN is not enough either: a caller signed in as
        # admin@gmail.com must not open all of gmail.com.
        "stranger@gmail.com",
        # A look-alike suffix is a different domain.
        "helgar@toeshee.com.evil.test",
        "helgar@eviltoeshee.com",
        # Not one address.
        "a@toeshee.com, b@elsewhere.com",
        "a@b@toeshee.com",
        "no-at-sign",
        "Name <helgar@gmail.com>",
    ],
)
async def test_an_address_this_platform_cannot_vouch_for_never_reaches_the_realm(
    realm: Any, address: str
) -> None:
    """A test send that accepted ANY address would let anyone holding the
    permission send mail from the organization's own domain, through its own
    relay, to whoever they chose. The rule lives in this function, so no route
    can skip it -- and the refusal happens before the service account is given
    anything. No org sign-in domain is configured here, so only the sending
    address and the caller's own mailbox are allowed."""
    with pytest.raises(KeycloakAdminError):
        await send_test_email(CONFIG, address, caller_email="helgar@gmail.com")

    assert realm.recipient_at_test is None
    assert not [r for r in realm.seen if "/users/" in r.url.path and r.method == "PUT"]


async def test_a_sign_in_accounts_address_is_refused_with_the_tagged_form_to_use(
    realm: Any,
) -> None:
    realm.user_put_status = 409

    with pytest.raises(KeycloakAdminError, match=r"helgar\+itms-test@toeshee\.com"):
        await send_test_email(CONFIG, "helgar@toeshee.com", caller_email="helgar@toeshee.com")


async def test_the_service_account_is_left_with_no_address(realm: Any) -> None:
    """If the address stayed on the service account, the realm would later
    refuse (409) to give that address to a real person, with an error nobody
    could trace back to this button."""
    await send_test_email(CONFIG)

    assert realm.service_account.get("email") == ""


async def test_an_address_left_behind_is_cleared_not_preserved(realm: Any) -> None:
    """THE RACE THE FIRST VERSION LOST. It saved the service account's address
    before the test and put it back afterwards. With two tests in flight, the
    second read the first's temporary address as "previous" and restored it
    permanently, and every later test preserved it in turn. The address exists
    only for one call, so the value afterwards is always empty, whatever was
    there before. That also repairs a leftover from a crash."""
    realm.service_account["email"] = "old@toeshee.com"

    await send_test_email(CONFIG)

    assert realm.recipient_at_test == "old@toeshee.com"
    assert realm.service_account["email"] == ""


async def test_the_address_is_put_back_even_when_the_relay_refuses(realm: Any) -> None:
    """The failing case is the common one while someone is setting email up,
    so it is the one most likely to leave the address behind if cleanup only
    ran on success."""
    realm.test_status = 500

    with pytest.raises(httpx.HTTPStatusError):
        await send_test_email(CONFIG)

    assert realm.service_account.get("email") == ""


async def test_the_service_account_is_found_by_its_exact_username(realm: Any) -> None:
    """An exact-match search needs only `manage-users`. Looking the client up
    by id would also need `view-clients`, which the provisioning client does
    not hold. A prefix search could return another account."""
    await send_test_email(CONFIG)

    (search,) = [r for r in realm.calls("GET") if r.url.path.endswith("/users")]
    assert search.url.params["username"] == "service-account-it-suite-provisioning"
    assert search.url.params["exact"] == "true"


async def test_a_sending_address_a_person_already_holds_is_refused_plainly(
    realm: Any,
) -> None:
    """The realm will not give two accounts one address. When the sending
    mailbox is also somebody's login, the operator is told that, along with
    what to do instead, rather than getting a bare upstream error."""
    realm.user_put_status = 409

    with pytest.raises(KeycloakAdminError, match="old@toeshee.com"):
        await send_test_email(CONFIG)

    assert realm.calls("POST", "/testSMTPConnection") == []


async def test_send_test_email_asks_with_the_settings_the_realm_holds(
    realm: Any,
) -> None:
    """The stored block is what is tested, not something reconstructed here.

    Testing a configuration the realm does not hold answers a question nobody
    asked -- the operator wants to know whether SAVED email works.
    """
    await send_test_email(CONFIG)

    (test,) = realm.calls("POST", "/testSMTPConnection")
    assert test.url.path == "/admin/realms/it-suite/testSMTPConnection"
    settings = json.loads(test.content)
    assert settings["host"] == "smtp.old.internal"
    assert settings["from"] == "old@toeshee.com"


@pytest.mark.parametrize("missing", ["host", "from"])
async def test_nothing_is_touched_without_a_saved_host_and_sender(
    realm: Any, missing: str
) -> None:
    del realm.representation["smtpServer"][missing]

    with pytest.raises(KeycloakAdminError):
        await send_test_email(CONFIG)

    assert realm.calls("PUT") == []
    assert realm.calls("POST", "/testSMTPConnection") == []


async def test_a_refused_test_send_raises_rather_than_reporting_success(
    realm: Any,
) -> None:
    """A relay that refuses the connection is the entire point of the button."""
    realm.test_status = 500

    with pytest.raises(httpx.HTTPStatusError):
        await send_test_email(CONFIG)


# --------------------------------------------------------------------------- #
# properties that hold across every call
# --------------------------------------------------------------------------- #


async def test_the_token_is_a_bearer_header_and_the_secret_is_never_in_a_url(
    realm: Any,
) -> None:
    """A credential in a URL is a credential in every proxy log between here
    and the identity provider."""
    await get_smtp(CONFIG)
    await set_smtp(CONFIG, {"host": "smtp.new.internal"})
    await send_test_email(CONFIG)

    assert realm.seen
    for request in realm.seen:
        assert request.headers["Authorization"] == "Bearer tok-123"
        assert CONFIG.client_secret not in str(request.url)
        assert "tok-123" not in str(request.url)


async def test_every_call_carries_a_timeout(realm: Any) -> None:
    """A wedged identity provider must not hold a request worker open.
    Asserted on the client's configuration rather than by waiting one out."""
    from app.modules.user_admin.realm_config import _TIMEOUT

    await get_smtp(CONFIG)

    assert _TIMEOUT > 0
    assert realm.kwargs["timeout"] == _TIMEOUT


def test_the_module_never_logs_what_it_writes() -> None:
    """No logger in this module, at any level.

    The one value that crosses this process is the SMTP password. A debug line
    that dumped the request body -- the ordinary way to make an integration
    like this diagnosable -- would put it in the platform's logs for ever, and
    a log file is exactly the "stored" the invariant is about. Diagnosis is the
    caller's audit record, which carries WHICH fields changed and none of their
    values.
    """
    from pathlib import Path

    from app.modules.user_admin import realm_config

    source = Path(realm_config.__file__).read_text()
    assert "logging" not in source
    assert "print(" not in source


def test_a_config_object_is_required_rather_than_settings(realm: Any) -> None:
    """The same `KeycloakAdminConfig` the user-lifecycle client takes, so the
    secret is read off disk once, by the dependency that already does it, and
    the routes here inherit its "not configured is a 503, not a crash"
    contract."""
    import inspect

    from app.modules.user_admin import realm_config

    for name in ("get_smtp", "set_smtp", "send_test_email"):
        first = list(inspect.signature(getattr(realm_config, name)).parameters)[0]
        assert first == "config", f"{name} does not take the admin config first"


# --------------------------------------------------------------------------- #
# token-based SMTP sign-in (Keycloak 26.2 XOAUTH2)
# docs/superpowers/specs/2026-09-11-mail-oauth-design.md §3
# --------------------------------------------------------------------------- #

TOKEN_KEYS = (
    "authType",
    "authTokenUrl",
    "authTokenScope",
    "authTokenClientId",
    "authTokenClientSecret",
)


def test_the_five_token_keys_are_allowlisted() -> None:
    from app.modules.user_admin.realm_config import CARRIED_SECRETS, SMTP_FIELDS

    for key in TOKEN_KEYS:
        assert key in SMTP_FIELDS
    assert set(CARRIED_SECRETS) == {"password", "authTokenClientSecret"}


async def test_token_mode_writes_all_five_keys(realm: Any) -> None:
    await set_smtp(
        CONFIG,
        {
            "host": "smtp.office365.com",
            "port": "587",
            "from": "itms@toeshee.com",
            "starttls": "true",
            "auth": "true",
            "user": "itms@toeshee.com",
            "authType": "token",
            "authTokenUrl": "https://login.microsoftonline.com/t/oauth2/v2.0/token",
            "authTokenScope": "https://outlook.office365.com/.default",
            "authTokenClientId": "client-abc",
            "authTokenClientSecret": "s3cr3t",
        },
    )

    block = _put_body(realm)["smtpServer"]
    assert block["authType"] == "token"
    assert block["authTokenUrl"].startswith("https://login.microsoftonline.com/")
    assert block["authTokenScope"] == "https://outlook.office365.com/.default"
    assert block["authTokenClientId"] == "client-abc"
    assert block["authTokenClientSecret"] == "s3cr3t"


async def test_an_absent_token_secret_keeps_the_stored_one(realm: Any) -> None:
    """The password's rule: the console cannot send back a value it was never
    given, so correcting the scope must not clear a working client secret."""
    realm.representation["smtpServer"] = {
        "host": "smtp.office365.com",
        "authType": "token",
        "authTokenClientSecret": MASKED_SECRET,
    }

    await set_smtp(CONFIG, {"host": "smtp.office365.com", "authType": "token"})

    assert _put_body(realm)["smtpServer"]["authTokenClientSecret"] == MASKED_SECRET


async def test_an_absent_token_secret_on_a_realm_that_has_none_stays_absent(
    realm: Any,
) -> None:
    await set_smtp(CONFIG, {"host": "smtp.new.internal"})

    assert "authTokenClientSecret" not in _put_body(realm)["smtpServer"]


async def test_get_smtp_never_returns_the_token_secret(realm: Any) -> None:
    realm.representation["smtpServer"]["authTokenClientSecret"] = "leaked-client-secret"

    values = await get_smtp(CONFIG)

    assert "authTokenClientSecret" not in values
    assert "leaked-client-secret" not in json.dumps(values)


async def test_a_key_outside_the_allowlist_cannot_reach_the_realm(realm: Any) -> None:
    await set_smtp(CONFIG, {"host": "h", "authTokenUrlEvil": "https://attacker.example"})

    assert "authTokenUrlEvil" not in _put_body(realm)["smtpServer"]
