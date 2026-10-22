"""Settings -> Email: `/api/v1/settings/email`.

Spec `docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §2.1.

THIS IS THE FEATURE THE OPERATOR ASKED FOR BY NAME. They spent an afternoon
looking for SMTP configuration and had to be told it lives in a different
product's admin console. The four properties that make this tab worth having,
and the test that would notice each one breaking:

1. **The password is never echoed.** It travels once, inbound, and the realm's
   own GET does not return it.
   `test_no_route_ever_echoes_the_password` drives all three routes and scans
   the RAW BODIES for the literal -- not for the absence of a key, which a
   renamed field would satisfy while still shipping the value.

   THAT TEST ALREADY EARNED ITS KEEP. The two body rules that cannot be
   expressed as field constraints were first written as a Pydantic
   model-level validator, and FastAPI reports a model-level failure with the
   WHOLE BODY echoed into the 422 -- password included. The sweep drives a
   refused save for that reason, and the rules live in the route
   (`_refuse_unusable`) rather than on the model.
2. **Every write is gated server-side and audited.** `orgs.manage`, checked
   before anything reads the provisioning secret off disk, and one
   `email.settings_updated` row per accepted save, carrying WHICH fields
   changed and none of their values.
3. **Unconfigured is a state, not an error.** The shipped realm has no
   `smtpServer` at all, so `GET` answers `configured: false` rather than 404 --
   the console renders an empty form and the Users tab shows its banner, which
   is how an operator finds out BEFORE an invitation silently fails.
4. **The save merges the realm.** The route's job is one key; the client's job
   is not losing the login theme and the WebAuthn policy along the way
   (`tests/test_realm_config.py`). This file pins that the route goes through
   that client rather than round-tripping the realm itself.

WHAT THIS CANNOT SHOW, and it is the same gap every Keycloak surface in this
tree declares: nothing here has run against a live identity provider.
`MockTransport` answers what these tests say. It proves what this application
SENDS, gates, and records.

WHY THE ROUTER IS INCLUDED BY A FIXTURE. This task owns
`app/api/v1/realm_settings.py` and not `app/main.py`; the controller wires the
router afterwards. `email_env` includes it into the per-test app only if it is
not already there, so these tests pass both before and after that wiring lands
and never register it twice.
"""

import json
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.v1.realm_settings import MAX_PASSWORD
from app.api.v1.realm_settings import router as email_router
from app.core.rbac import route_permission
from app.core.router_inspect import flatten
from app.models import AuditEvent
from app.modules.user_admin.api import keycloak_config
from app.modules.user_admin.kc_client import KeycloakAdminConfig
from app.modules.user_admin.realm_config import MASKED_SECRET

#: Captured at import, before any fixture swaps the name.
_REAL_CLIENT = httpx.AsyncClient

CONFIG = KeycloakAdminConfig(
    base_url="https://keycloak.test",
    realm="it-suite",
    client_id="it-suite-provisioning",
    client_secret="not-a-real-secret",
)

#: A password a MISBEHAVING realm hands back on the read. A live one masks it;
#: this platform does not get to rely on that, so the fixture is the hostile
#: case and the sweep below looks for this literal.
LEAKED = "hunter2-the-realm-should-not-have-sent-this"

#: What the operator types into the form. It must reach the realm verbatim and
#: appear nowhere else -- not in a response, not in an audit row.
TYPED = "s3cr3t-typed-once"


class FakeRealm:
    """A realm that records what it was asked and answers what the test says."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.representation: dict[str, Any] = {
            "realm": "it-suite",
            "loginTheme": "it-suite",
            "webAuthnPolicyPasswordlessRpEntityName": "ITMS",
            "smtpServer": {
                "host": "smtp.internal",
                "port": "587",
                "from": "itms@toeshee.com",
                "fromDisplayName": "ITMS",
                "starttls": "true",
                "auth": "true",
                "user": "itms",
                "password": LEAKED,
            },
        }
        self.service_account: dict[str, Any] = {"id": "sa-1", "username": "sa"}
        self.put_status = 204
        self.test_status = 204
        #: Off by default so the ordinary tests read exactly what they sent.
        self.normalises_host = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "tok-123"})
        self.seen.append(request)
        if path.endswith("/testSMTPConnection"):
            return httpx.Response(self.test_status)
        if path.endswith("/users") and request.method == "GET":
            return httpx.Response(200, json=[self.service_account])
        if "/users/" in path and request.method == "PUT":
            self.service_account = json.loads(request.content)
            return httpx.Response(204)
        if request.method == "GET":
            return httpx.Response(200, json=self.representation)
        if request.method == "PUT":
            # A live realm answers the PUT with no body; the route reads the
            # state back afterwards, so the fixture keeps what it was sent --
            # NORMALISED, the way a real one does. `normalises_host` is what
            # lets a test tell "the route re-read" from "the route echoed the
            # request back and called it saved".
            self.representation = json.loads(request.content)
            if self.normalises_host:
                self.representation["smtpServer"]["host"] = self.representation[
                    "smtpServer"
                ]["host"].lower()
            return httpx.Response(self.put_status)
        raise AssertionError(f"unexpected call: {request.method} {path}")

    @property
    def writes(self) -> list[httpx.Request]:
        return [r for r in self.seen if r.method == "PUT" and "/users/" not in r.url.path]

    @property
    def tests(self) -> list[httpx.Request]:
        return [r for r in self.seen if r.url.path.endswith("/testSMTPConnection")]


@pytest.fixture()
def email_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    paths = {path for path, _route in flatten(api_env.app.router)}
    if "/api/v1/settings/email" not in paths:
        api_env.app.include_router(email_router, prefix="/api/v1")

    realm = FakeRealm()

    def factory(**kw: Any) -> httpx.AsyncClient:
        return _REAL_CLIENT(transport=httpx.MockTransport(realm.handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    api_env.app.dependency_overrides[keycloak_config] = lambda: CONFIG
    api_env.realm = realm
    yield api_env
    api_env.app.dependency_overrides.pop(keycloak_config, None)


VALID_BODY: dict[str, Any] = {
    "host": "smtp.new.internal",
    "port": 465,
    "from_address": "itms@toeshee.com",
    "from_display_name": "ITMS",
    "reply_to": "support@toeshee.com",
    "encryption": "ssl",
    "username": "itms",
    "password": TYPED,
}


async def _audit(engine: AsyncEngine, org_id: UUID) -> list[AuditEvent]:
    """Through the ADMIN engine: an assertion about what a request recorded
    must not itself be filtered by the mechanism under test."""
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == org_id).order_by(AuditEvent.id)
                )
            ).scalars()
        )


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #


async def test_the_saved_settings_are_reported(email_env: Any) -> None:
    org = await email_env.create_org("email-read")
    admin = await email_env.create_user(org, "org-admin")

    response = await email_env.client.get(
        "/api/v1/settings/email", headers=await email_env.headers(org, admin)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["host"] == "smtp.internal"
    assert body["port"] == "587"
    assert body["from_address"] == "itms@toeshee.com"
    assert body["encryption"] == "starttls"
    assert body["username"] == "itms"
    assert body["authenticated"] is True


async def test_an_unconfigured_realm_reports_configured_false_rather_than_404(
    email_env: Any,
) -> None:
    """THE STATE THIS DEPLOYMENT IS ACTUALLY IN. `deploy/keycloak/
    realm-it-suite.json` ships with no `smtpServer`.

    A 404 would be read by the console as "this route is not deployed" -- the
    exact shape the Organization tab uses for a missing route -- and the Users
    tab's banner, which is the whole point of the flag, would never appear. An
    unconfigured state has to be visible where it bites.
    """
    email_env.realm.representation.pop("smtpServer")
    org = await email_env.create_org("email-empty")
    admin = await email_env.create_user(org, "org-admin")

    response = await email_env.client.get(
        "/api/v1/settings/email", headers=await email_env.headers(org, admin)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["host"] == ""
    assert body["authenticated"] is False


# --------------------------------------------------------------------------- #
# the password -- the reason this surface is written the way it is
# --------------------------------------------------------------------------- #


async def test_no_route_ever_echoes_the_password(email_env: Any) -> None:
    """THE LITERAL, IN THE RAW BODY, ON EVERY ROUTE.

    Asserting that a `password` KEY is missing is not this assertion: a field
    renamed `smtp_credential` would satisfy it while still shipping the value.
    Two literals are searched for -- the one the operator just typed, and one a
    misbehaving realm returned on the read -- because the failures differ. The
    first would mean the form round-trips what it was given; the second would
    mean this platform relies on the vendor to strip a secret it should be
    stripping itself.
    """
    org = await email_env.create_org("email-echo")
    admin = await email_env.create_user(org, "org-admin")
    headers = await email_env.headers(org, admin)

    bodies = [
        (await email_env.client.get("/api/v1/settings/email", headers=headers)).text,
        (
            await email_env.client.put(
                "/api/v1/settings/email", headers=headers, json=VALID_BODY
            )
        ).text,
        (
            await email_env.client.post("/api/v1/settings/email/test", headers=headers)
        ).text,
        # A REFUSED save, and this one found a real leak: a model-level Pydantic
        # rule reports the whole body back in the 422, password included. The
        # two body rules that cannot be field constraints live in the route for
        # that reason (`_refuse_unusable`), and this line is what keeps them
        # there.
        (
            await email_env.client.put(
                "/api/v1/settings/email", headers=headers, json={**VALID_BODY, "username": ""}
            )
        ).text,
        (
            await email_env.client.put(
                "/api/v1/settings/email",
                headers=headers,
                json={**VALID_BODY, "port": 70000},
            )
        ).text,
    ]

    for body in bodies:
        assert TYPED not in body
        assert LEAKED not in body
        assert MASKED_SECRET not in body


async def test_the_typed_password_reaches_the_realm_verbatim(email_env: Any) -> None:
    """Non-vacuity for the test above: "never echoed" must not be achieved by
    never sending it. This is the one stated exception to "no password enters
    the ITMS process" (spec §4), and the whole point is that it ARRIVES."""
    org = await email_env.create_org("email-through")
    admin = await email_env.create_user(org, "org-admin")

    response = await email_env.client.put(
        "/api/v1/settings/email",
        headers=await email_env.headers(org, admin),
        json=VALID_BODY,
    )

    assert response.status_code == 200
    (write,) = email_env.realm.writes
    assert json.loads(write.content)["smtpServer"]["password"] == TYPED


async def test_the_save_answers_with_what_the_realm_HOLDS_not_what_was_typed(
    email_env: Any,
) -> None:
    """THE FORM SHOWS THE SERVER'S ANSWER, NEVER ITS OWN INPUT.

    The identity provider normalises what it stores. A route that echoed the
    request body back would show the operator a value that is not in the realm,
    and every "Saved." on that screen would be a claim this platform cannot
    support -- the same rule every other form on the Settings page follows.
    """
    email_env.realm.normalises_host = True
    org = await email_env.create_org("email-reread")
    admin = await email_env.create_user(org, "org-admin")

    response = await email_env.client.put(
        "/api/v1/settings/email",
        headers=await email_env.headers(org, admin),
        json={**VALID_BODY, "host": "SMTP.Shouty.Internal"},
    )

    assert response.status_code == 200
    assert response.json()["host"] == "smtp.shouty.internal"


async def test_a_save_without_a_password_keeps_the_stored_one(email_env: Any) -> None:
    """EDITING THE PORT MUST NOT CLEAR THE CREDENTIAL.

    The console cannot send a password it never received, so `password` is
    optional and absent means "leave it alone". If this route sent `""` for an
    omitted field, the first operator to correct a display name would break a
    working relay and nothing would say so.
    """
    org = await email_env.create_org("email-keep")
    admin = await email_env.create_user(org, "org-admin")
    body = {key: value for key, value in VALID_BODY.items() if key != "password"}

    response = await email_env.client.put(
        "/api/v1/settings/email", headers=await email_env.headers(org, admin), json=body
    )

    assert response.status_code == 200
    (write,) = email_env.realm.writes
    written = json.loads(write.content)["smtpServer"]
    assert written["password"] == LEAKED  # whatever the realm held, unchanged
    assert written["host"] == "smtp.new.internal"


async def test_clearing_the_username_clears_the_stored_credential(
    email_env: Any,
) -> None:
    """THE OTHER HALF OF "an absent password means keep it".

    Saving with no username says this relay needs no sign-in. Keeping the
    stored credential there would leave one behind that nothing uses and
    nobody rotates -- so this is the one case where an empty password is the
    correct thing to write, and it is written explicitly rather than by
    forgetting a key.
    """
    org = await email_env.create_org("email-noauth")
    admin = await email_env.create_user(org, "org-admin")
    body = {key: value for key, value in VALID_BODY.items() if key != "password"}

    response = await email_env.client.put(
        "/api/v1/settings/email",
        headers=await email_env.headers(org, admin),
        json={**body, "username": ""},
    )

    assert response.status_code == 200
    written = json.loads(email_env.realm.writes[-1].content)["smtpServer"]
    assert written["password"] == ""
    assert "auth" not in written
    assert "user" not in written


async def test_an_over_long_password_is_refused_without_repeating_it(
    email_env: Any,
) -> None:
    """The length bound is enforced in the route, not as a field constraint,
    for exactly this reason: a Pydantic field failure reports the offending
    INPUT back in the 422 body, and the input here is the password."""
    org = await email_env.create_org("email-long")
    admin = await email_env.create_user(org, "org-admin")
    over_long = "x" * (MAX_PASSWORD + 1)

    response = await email_env.client.put(
        "/api/v1/settings/email",
        headers=await email_env.headers(org, admin),
        json={**VALID_BODY, "password": over_long},
    )

    assert response.status_code == 422
    assert over_long not in response.text
    assert email_env.realm.writes == []


async def test_a_password_with_no_username_is_refused_rather_than_dropped(
    email_env: Any,
) -> None:
    """The save with no username CLEARS the credential, so a typed password
    would be discarded by a request that answered 200. An operator who believes
    they have just rotated a relay password and has not is worse off than one
    who was refused."""
    org = await email_env.create_org("email-passwordless")
    admin = await email_env.create_user(org, "org-admin")

    response = await email_env.client.put(
        "/api/v1/settings/email",
        headers=await email_env.headers(org, admin),
        json={**VALID_BODY, "username": ""},
    )

    assert response.status_code == 422
    assert email_env.realm.writes == []
    assert TYPED not in response.text


# --------------------------------------------------------------------------- #
# authorization, audit, tenancy
# --------------------------------------------------------------------------- #


async def test_every_route_on_this_surface_is_gated(email_env: Any) -> None:
    """Invariant 6, read off the router rather than off three status codes: a
    route added here later cannot join ungated without turning this red."""
    routes = [
        (path, route)
        for path, route in flatten(email_env.app.router)
        if isinstance(route, APIRoute) and path.startswith("/api/v1/settings/email")
    ]

    assert routes
    for path, route in routes:
        assert route_permission(route) == "orgs.manage", f"{path} is not gated"


async def test_a_write_without_the_permission_is_refused_and_changes_nothing(
    email_env: Any,
) -> None:
    """403, AND the realm untouched. A refusal that still performed the write
    would be a permission check that reports rather than enforces."""
    org = await email_env.create_org("email-rbac")
    nobody = await email_env.create_user(org, "read-only")
    headers = await email_env.headers(org, nobody)

    save = await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json=VALID_BODY
    )
    read = await email_env.client.get("/api/v1/settings/email", headers=headers)
    test = await email_env.client.post("/api/v1/settings/email/test", headers=headers)

    assert [save.status_code, read.status_code, test.status_code] == [403, 403, 403]
    assert email_env.realm.writes == []
    assert email_env.realm.tests == []


async def test_an_anonymous_caller_is_refused(email_env: Any) -> None:
    response = await email_env.client.get("/api/v1/settings/email")

    assert response.status_code == 401
    assert email_env.realm.seen == []


async def test_the_change_is_audited_with_the_fields_and_none_of_the_values(
    email_env: Any,
) -> None:
    """Invariant 4. WHICH fields changed answers "who broke email, and when";
    the values would put a credential into an append-only, tamper-evident log
    that by design cannot be redacted afterwards."""
    org = await email_env.create_org("email-audit")
    admin = await email_env.create_user(org, "org-admin")

    await email_env.client.put(
        "/api/v1/settings/email",
        headers=await email_env.headers(org, admin),
        json=VALID_BODY,
    )

    events = await _audit(email_env.admin_engine, org)
    written = [e for e in events if e.action == "email.settings_updated"]
    assert len(written) == 1
    metadata = written[0].meta
    assert "host" in metadata["fields"]
    assert "password" in metadata["fields"]  # the NAME, so a rotation is on record
    serialized = json.dumps(metadata)
    assert TYPED not in serialized
    assert "smtp.new.internal" not in serialized


async def test_a_refused_write_records_nothing(email_env: Any) -> None:
    """A log that says a change happened when it did not is worse than no log.
    The identity provider refusing the PUT must roll the audit row back with
    it."""
    email_env.realm.put_status = 403
    org = await email_env.create_org("email-audit-refused")
    admin = await email_env.create_user(org, "org-admin")

    response = await email_env.client.put(
        "/api/v1/settings/email",
        headers=await email_env.headers(org, admin),
        json=VALID_BODY,
    )

    assert response.status_code == 502
    events = await _audit(email_env.admin_engine, org)
    assert [e for e in events if e.action.startswith("email.")] == []


async def test_the_test_send_is_audited_and_asks_the_identity_provider(
    email_env: Any,
) -> None:
    """The button sends real mail through the org's own relay, so it is a
    privileged action and it is on the record like every other one."""
    org = await email_env.create_org("email-test-send")
    admin = await email_env.create_user(org, "org-admin")

    response = await email_env.client.post(
        "/api/v1/settings/email/test", headers=await email_env.headers(org, admin)
    )

    assert response.status_code == 200
    assert response.json() == {"sent": True, "recipient": "itms@toeshee.com"}
    assert len(email_env.realm.tests) == 1
    events = await _audit(email_env.admin_engine, org)
    sent = [e for e in events if e.action.startswith("email.")]
    assert [e.action for e in sent] == ["email.test_requested"]
    assert sent[0].meta == {"recipient": "itms@toeshee.com"}


async def test_a_test_send_the_relay_refuses_is_reported_not_swallowed(
    email_env: Any,
) -> None:
    """The failing case is the reason the button exists. A green tick over a
    refused connection is worse than no button at all."""
    email_env.realm.test_status = 500
    org = await email_env.create_org("email-test-refused")
    admin = await email_env.create_user(org, "org-admin")

    response = await email_env.client.post(
        "/api/v1/settings/email/test", headers=await email_env.headers(org, admin)
    )

    assert response.status_code == 502


async def test_the_test_send_goes_to_the_address_asked_for_and_audits_it(
    email_env: Any,
) -> None:
    org = await email_env.create_org("email-test-chosen")
    admin = await email_env.create_user(org, "org-admin")
    tagged = f"{admin}+itms-test@test.local"

    response = await email_env.client.post(
        "/api/v1/settings/email/test",
        json={"recipient": tagged},
        headers=await email_env.headers(org, admin),
    )

    assert response.status_code == 200
    assert response.json() == {"sent": True, "recipient": tagged}
    events = await _audit(email_env.admin_engine, org)
    sent = [e for e in events if e.action.startswith("email.")]
    assert sent[0].meta == {"recipient": tagged}


async def test_a_colleague_at_the_callers_domain_is_refused_without_a_sign_in_domain(
    email_env: Any,
) -> None:
    """The caller's own address proves one mailbox, not a domain."""
    org = await email_env.create_org("email-test-own-domain")
    admin = await email_env.create_user(org, "org-admin")

    response = await email_env.client.post(
        "/api/v1/settings/email/test",
        json={"recipient": "someone@test.local"},
        headers=await email_env.headers(org, admin),
    )

    assert response.status_code == 409
    assert email_env.realm.tests == []


async def test_a_test_send_to_an_outside_address_is_refused_and_sends_nothing(
    email_env: Any,
) -> None:
    org = await email_env.create_org("email-test-outside")
    admin = await email_env.create_user(org, "org-admin")

    response = await email_env.client.post(
        "/api/v1/settings/email/test",
        json={"recipient": "victim@elsewhere.com"},
        headers=await email_env.headers(org, admin),
    )

    assert response.status_code == 409
    assert "your own address" in response.json()["detail"]
    assert email_env.realm.tests == []
    events = await _audit(email_env.admin_engine, org)
    assert [e for e in events if e.action.startswith("email.")] == []


async def test_the_settings_suggest_a_tagged_form_of_the_callers_address(
    email_env: Any,
) -> None:
    org = await email_env.create_org("email-test-suggest")
    admin = await email_env.create_user(org, "org-admin")

    response = await email_env.client.get(
        "/api/v1/settings/email", headers=await email_env.headers(org, admin)
    )

    assert response.json()["suggested_test_recipient"] == f"{admin}+itms-test@test.local"


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("helgar@toeshee.com", "helgar+itms-test@toeshee.com"),
        ("helgar+old@Toeshee.com", "helgar+itms-test@toeshee.com"),
        ("", ""),
        ("not-an-address", ""),
        ("a@b@c.com", ""),
    ],
)
def test_the_suggestion_replaces_a_tag_rather_than_stacking_one(
    email: str, expected: str
) -> None:
    from app.api.v1.realm_settings import suggested_test_recipient

    assert suggested_test_recipient(email) == expected


# --------------------------------------------------------------------------- #
# request shape
# --------------------------------------------------------------------------- #


async def test_a_body_carrying_an_unknown_field_is_refused(email_env: Any) -> None:
    """`extra="forbid"`: a caller who sends `smtpServer` or `org_id` gets a 422
    rather than a 200 that silently ignored it. It is also what stops a body
    from reaching the realm with keys the form does not own."""
    org = await email_env.create_org("email-extra")
    admin = await email_env.create_user(org, "org-admin")

    response = await email_env.client.put(
        "/api/v1/settings/email",
        headers=await email_env.headers(org, admin),
        json={**VALID_BODY, "smtpServer": {"host": "evil"}},
    )

    assert response.status_code == 422
    assert email_env.realm.writes == []


async def test_an_impossible_port_is_refused(email_env: Any) -> None:
    org = await email_env.create_org("email-port")
    admin = await email_env.create_user(org, "org-admin")

    response = await email_env.client.put(
        "/api/v1/settings/email",
        headers=await email_env.headers(org, admin),
        json={**VALID_BODY, "port": 70000},
    )

    assert response.status_code == 422


async def test_the_tls_mode_is_a_closed_set(email_env: Any) -> None:
    """One of three, and never both flags at once: a realm carrying `ssl` and
    `starttls` together is a configuration the relay decides the meaning of."""
    org = await email_env.create_org("email-tls")
    admin = await email_env.create_user(org, "org-admin")
    headers = await email_env.headers(org, admin)

    refused = await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json={**VALID_BODY, "encryption": "both"}
    )
    assert refused.status_code == 422

    await email_env.client.put(
        "/api/v1/settings/email",
        headers=headers,
        json={**VALID_BODY, "encryption": "starttls"},
    )
    written = json.loads(email_env.realm.writes[-1].content)["smtpServer"]
    assert written["starttls"] == "true"
    assert "ssl" not in written


async def test_authorization_runs_before_the_provisioning_secret_is_read(
    email_env: Any, tmp_path: Any
) -> None:
    """THE ORDER OF THE DEPENDENCIES IS A SECURITY PROPERTY, not a style point.

    `keycloak_config` reads the provisioning client secret OFF DISK, and
    FastAPI resolves dependencies in declaration order. Declared ahead of the
    gate, every unauthenticated request to these three routes would open that
    credential before anything established who was asking.

    Proved by pointing the setting at a path that does not exist:
    `from_settings` raises on a configured-but-missing secret, so if the read
    is reached at all the request cannot answer 401 or 403. The final
    `pytest.raises` is the non-vacuity half -- an AUTHORIZED caller does reach
    the read and does blow up on it, which is what makes the refusals above
    evidence of ordering rather than evidence that the setting was ignored.
    """
    org = await email_env.create_org("email-dep-order")
    email_env.app.dependency_overrides.pop(keycloak_config, None)
    settings = email_env.app.state.settings
    settings.keycloak_admin_url = "https://keycloak.test"
    settings.keycloak_admin_secret_path = tmp_path / "absent" / "provisioning.secret"

    nobody = await email_env.create_user(org, "read-only")
    unprivileged = await email_env.headers(org, nobody)

    anonymous = await email_env.client.get("/api/v1/settings/email")
    refused = await email_env.client.put(
        "/api/v1/settings/email", headers=unprivileged, json=VALID_BODY
    )
    tested = await email_env.client.post("/api/v1/settings/email/test", headers=unprivileged)

    assert [anonymous.status_code, refused.status_code, tested.status_code] == [401, 403, 403]

    admin = await email_env.create_user(org, "org-admin")
    with pytest.raises(FileNotFoundError):
        await email_env.client.get(
            "/api/v1/settings/email", headers=await email_env.headers(org, admin)
        )


async def test_a_deployment_without_provisioning_is_a_503_that_does_not_name_the_vendor(
    email_env: Any,
) -> None:
    """`None` is a supported deployment state, and it is not a crash.

    CORRECTED 2026-08-19. This test used to assert the opposite: `assert
    "KEYCLOAK" in response.text.upper()`, on the reasoning that an operator
    who has not configured provisioning "is told which setting is missing".
    The setting names are `ITS_KEYCLOAK_ADMIN_URL` and
    `ITS_KEYCLOAK_ADMIN_SECRET_PATH`, so satisfying that assertion put the
    identity vendor's name in a response body a console user reads -- which
    `docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md`
    §1.3 forbids and `tests/test_no_vendor_names_backend.py` now enforces. The
    test was pinning the defect.

    What replaces it is the property that actually matters here: a supported
    absence answers 503 rather than 500, and says what the reader can do about
    it. The setting names stay named in `realm_settings.py`'s comment and in
    `deploy/.env.example`, for the person who configures the deployment.
    """
    email_env.app.dependency_overrides[keycloak_config] = lambda: None
    org = await email_env.create_org("email-unconfigured")
    admin = await email_env.create_user(org, "org-admin")

    response = await email_env.client.get(
        "/api/v1/settings/email", headers=await email_env.headers(org, admin)
    )

    assert response.status_code == 503
    assert "KEYCLOAK" not in response.text.upper()
    detail = response.json()["detail"]
    assert "not configured" in detail and "server setting" in detail


# --------------------------------------------------------------------------- #
# sign-in mode: password or Microsoft (token)
# docs/superpowers/specs/2026-09-11-mail-oauth-design.md §3.2-3.3, §5.1
# --------------------------------------------------------------------------- #

CLIENT_SECRET = "s3cr3t-client-value"

TOKEN_BODY: dict[str, Any] = {
    "host": "smtp.office365.com",
    "port": 587,
    "from_address": "itms@toeshee.com",
    "from_display_name": "ITMS",
    "reply_to": "",
    "encryption": "starttls",
    "username": "itms@toeshee.com",
    "auth_type": "token",
    "token_url": "https://login.microsoftonline.com/tid/oauth2/v2.0/token",
    "token_scope": "https://outlook.office365.com/.default",
    "token_client_id": "client-abc",
    "token_client_secret": CLIENT_SECRET,
}


async def _admin_headers(env: Any, name: str) -> dict[str, str]:
    org = await env.create_org(name)
    admin = await env.create_user(org, "org-admin")
    headers: dict[str, str] = await env.headers(org, admin)
    return headers


async def test_password_mode_writes_the_block_a_password_deployment_needs(
    email_env: Any,
) -> None:
    """The compatibility guarantee, as a whole-dict comparison.

    `authType: password` is the vendor's own default, and the four emptied
    token keys are what makes switching back from Microsoft clear the old
    application: an omitted key would not reach the realm to clear anything.
    """
    headers = await _admin_headers(email_env, "email-mode-password")

    response = await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json=VALID_BODY
    )

    assert response.status_code == 200
    (write,) = email_env.realm.writes
    assert json.loads(write.content)["smtpServer"] == {
        "host": "smtp.new.internal",
        "port": "465",
        "from": "itms@toeshee.com",
        "fromDisplayName": "ITMS",
        "replyTo": "support@toeshee.com",
        "ssl": "true",
        "auth": "true",
        "user": "itms",
        "password": TYPED,
        "authType": "password",
        "authTokenUrl": "",
        "authTokenScope": "",
        "authTokenClientId": "",
        "authTokenClientSecret": "",
    }


async def test_token_mode_writes_the_token_keys_and_clears_the_password(
    email_env: Any,
) -> None:
    headers = await _admin_headers(email_env, "email-mode-token")

    response = await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json=TOKEN_BODY
    )

    assert response.status_code == 200
    block = json.loads(email_env.realm.writes[-1].content)["smtpServer"]
    assert block["authType"] == "token"
    assert block["authTokenUrl"] == TOKEN_BODY["token_url"]
    assert block["authTokenScope"] == TOKEN_BODY["token_scope"]
    assert block["authTokenClientId"] == "client-abc"
    assert block["authTokenClientSecret"] == CLIENT_SECRET
    # A realm carrying both leaves the relay to decide what was meant.
    assert block["password"] == ""
    # Read back from the realm, so a later edit does not have to retype them.
    assert response.json()["auth_type"] == "token"
    assert response.json()["microsoft_client_id"] == "client-abc"
    assert response.json()["microsoft_sign_in_url"] == TOKEN_BODY["token_url"]
    assert response.json()["microsoft_scope"] == TOKEN_BODY["token_scope"]


async def test_switching_back_to_password_clears_the_token_keys(
    email_env: Any,
) -> None:
    headers = await _admin_headers(email_env, "email-mode-back")

    await email_env.client.put("/api/v1/settings/email", headers=headers, json=TOKEN_BODY)
    await email_env.client.put("/api/v1/settings/email", headers=headers, json=VALID_BODY)

    block = json.loads(email_env.realm.writes[-1].content)["smtpServer"]
    for key in ("authTokenUrl", "authTokenScope", "authTokenClientId", "authTokenClientSecret"):
        assert block[key] == ""
    assert block["authType"] == "password"
    assert block["password"] == TYPED


async def test_an_omitted_client_secret_keeps_the_stored_one(email_env: Any) -> None:
    """Correcting the scope must not clear a working client secret."""
    email_env.realm.representation["smtpServer"] = {
        "host": "smtp.office365.com",
        "authType": "token",
        "authTokenClientId": "client-abc",
        "authTokenClientSecret": MASKED_SECRET,
    }
    headers = await _admin_headers(email_env, "email-mode-keep-secret")
    body = {k: v for k, v in TOKEN_BODY.items() if k != "token_client_secret"}

    response = await email_env.client.put("/api/v1/settings/email", headers=headers, json=body)

    assert response.status_code == 200
    block = json.loads(email_env.realm.writes[-1].content)["smtpServer"]
    assert block["authTokenClientSecret"] == MASKED_SECRET


@pytest.mark.parametrize(
    ("change", "phrase"),
    [
        ({"token_client_id": ""}, "client id"),
        ({"token_url": ""}, "token url"),
        ({"username": ""}, "username"),
    ],
)
async def test_an_incomplete_microsoft_sign_in_is_refused(
    email_env: Any, change: dict[str, str], phrase: str
) -> None:
    headers = await _admin_headers(email_env, f"email-mode-incomplete-{phrase[:5]}")

    response = await email_env.client.put(
        "/api/v1/settings/email", headers=headers, json={**TOKEN_BODY, **change}
    )

    assert response.status_code == 422
    assert phrase in response.json()["detail"].lower()
    # The refusal names the rule and never repeats the value.
    assert CLIENT_SECRET not in response.text
    assert email_env.realm.writes == []


async def test_a_client_secret_without_a_client_id_is_refused_in_password_mode(
    email_env: Any,
) -> None:
    """The mirror of "a password needs a username": a secret that would be
    discarded must be refused, not answered with 200."""
    headers = await _admin_headers(email_env, "email-mode-stray-secret")

    response = await email_env.client.put(
        "/api/v1/settings/email",
        headers=headers,
        json={**VALID_BODY, "token_client_secret": CLIENT_SECRET},
    )

    assert response.status_code == 422
    assert CLIENT_SECRET not in response.text
    assert email_env.realm.writes == []


async def test_an_over_long_client_secret_is_refused_without_repeating_it(
    email_env: Any,
) -> None:
    from app.api.v1.realm_settings import MAX_CLIENT_SECRET

    headers = await _admin_headers(email_env, "email-mode-long-secret")
    over_long = "y" * (MAX_CLIENT_SECRET + 1)

    response = await email_env.client.put(
        "/api/v1/settings/email",
        headers=headers,
        json={**TOKEN_BODY, "token_client_secret": over_long},
    )

    assert response.status_code == 422
    assert over_long not in response.text


async def test_no_route_ever_returns_the_client_secret(email_env: Any) -> None:
    """Raw bytes, not a key check: a renamed field would satisfy the weaker
    assertion while still shipping the value."""
    headers = await _admin_headers(email_env, "email-mode-echo")
    email_env.realm.representation["smtpServer"]["authTokenClientSecret"] = LEAKED

    responses = [
        await email_env.client.put("/api/v1/settings/email", headers=headers, json=TOKEN_BODY),
        await email_env.client.get("/api/v1/settings/email", headers=headers),
    ]

    for response in responses:
        assert CLIENT_SECRET not in response.text
        assert LEAKED not in response.text
        assert MASKED_SECRET not in response.text


async def test_the_mode_change_is_audited_with_field_names_only(email_env: Any) -> None:
    org = await email_env.create_org("email-mode-audit")
    admin = await email_env.create_user(org, "org-admin")

    await email_env.client.put(
        "/api/v1/settings/email",
        headers=await email_env.headers(org, admin),
        json=TOKEN_BODY,
    )

    events = await _audit(email_env.admin_engine, org)
    (written,) = [e for e in events if e.action == "email.settings_updated"]
    assert "token_client_secret" in written.meta["fields"]
    assert "auth_type" in written.meta["fields"]
    assert CLIENT_SECRET not in json.dumps(written.meta)


async def test_an_existing_password_relay_reads_as_password_mode(email_env: Any) -> None:
    """A realm saved before this change has no `authType`; it is a password
    relay, not an unknown one."""
    headers = await _admin_headers(email_env, "email-mode-legacy")

    body = (await email_env.client.get("/api/v1/settings/email", headers=headers)).json()

    assert body["auth_type"] == "password"
    assert body["microsoft_client_id"] == ""
    assert body["microsoft_sign_in_url"] == ""
