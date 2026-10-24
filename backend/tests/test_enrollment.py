from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.models import AuditEvent, Device, EnrollmentToken
from app.services.enrollment import DeviceFacts


def make_csr_pem() -> str:
    key = Ed25519PrivateKey.generate()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "enrolling-agent")]))
        .sign(key, None)
    )
    return csr.public_bytes(Encoding.PEM).decode()


FACTS = {
    "hostname": "wks-001",
    "os_family": "linux",
    "os_version": "Ubuntu 24.04",
    "architecture": "x86_64",
}


async def _issue_token(api_env, org_id, sub, **kwargs):
    """POST one token. `max_uses` IS IN THE DEFAULT BODY BECAUSE THE ROUTE
    REQUIRES IT -- it used to default to null on the server, and null means
    unlimited. A helper that omitted it now gets a 422, which is exactly the
    loudness the change is for; every test below that does not care about the
    bound says `1`, the smallest credential that still enrols a machine.

    `site_id` IS IN IT FOR THE SAME REASON (MSP clients spec §1.5): the route
    requires it. The organisation's Principal site has the organisation's id."""
    headers = await api_env.headers(org_id, sub)
    body = {"name": "lab", "expires_in_hours": 24, "max_uses": 1, "site_id": str(org_id)} | kwargs
    return await api_env.client.post("/api/v1/enrollment-tokens", json=body, headers=headers)


async def test_token_create_requires_permission(api_env) -> None:
    org_id = await api_env.create_org("enr")
    tech = await api_env.create_user(org_id, "technician")
    response = await _issue_token(api_env, org_id, tech)
    assert response.status_code == 403


async def test_enroll_happy_path(api_env) -> None:
    org_id = await api_env.create_org("enr")
    admin = await api_env.create_user(org_id, "org-admin")
    created = await _issue_token(api_env, org_id, admin)
    assert created.status_code == 201, created.text
    token = created.json()["token"]

    response = await api_env.client.post(
        "/enroll/v1", json={"token": token, "csr_pem": make_csr_pem(), "device_facts": FACTS}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    cert = x509.load_pem_x509_certificate(body["cert_pem"].encode())
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == body["device_id"]
    assert "BEGIN PUBLIC KEY" in body["job_signing_public_key_pem"]
    assert body["gateway_url"]
    assert "BEGIN CERTIFICATE" in body["ca_bundle_pem"]

    # device + agent rows exist; audit trail covers issue + enroll
    async with AsyncSession(api_env.admin_engine) as session:
        device = (
            await session.execute(select(Device).where(Device.org_id == org_id))
        ).scalar_one()
        assert device.hostname == "wks-001"
        actions = {
            r.action
            for r in (
                await session.execute(select(AuditEvent).where(AuditEvent.org_id == org_id))
            ).scalars()
        }
    assert "enrollment.token_created" in actions
    assert "enrollment.device_enrolled" in actions


async def test_enroll_rejects_exhausted_token(api_env) -> None:
    org_id = await api_env.create_org("enr")
    admin = await api_env.create_user(org_id, "org-admin")
    token = (await _issue_token(api_env, org_id, admin, max_uses=1)).json()["token"]

    first = await api_env.client.post(
        "/enroll/v1", json={"token": token, "csr_pem": make_csr_pem(), "device_facts": FACTS}
    )
    assert first.status_code == 201
    second = await api_env.client.post(
        "/enroll/v1", json={"token": token, "csr_pem": make_csr_pem(), "device_facts": FACTS}
    )
    assert second.status_code == 403


async def test_enroll_rejects_expired_token(api_env) -> None:
    org_id = await api_env.create_org("enr")
    admin = await api_env.create_user(org_id, "org-admin")
    token = (await _issue_token(api_env, org_id, admin)).json()["token"]
    async with AsyncSession(api_env.admin_engine) as session:
        row = (
            (
                await session.execute(
                    select(EnrollmentToken).where(EnrollmentToken.org_id == org_id)
                )
            )
            .scalars()
            .one()
        )
        row.expires_at = datetime.now(UTC) - timedelta(hours=1)
        session.add(row)
        await session.commit()
    response = await api_env.client.post(
        "/enroll/v1", json={"token": token, "csr_pem": make_csr_pem(), "device_facts": FACTS}
    )
    assert response.status_code == 403


async def test_enroll_rejects_revoked_and_garbage_token(api_env) -> None:
    org_id = await api_env.create_org("enr")
    admin = await api_env.create_user(org_id, "org-admin")
    created = (await _issue_token(api_env, org_id, admin)).json()
    headers = await api_env.headers(org_id, admin)
    revoke = await api_env.client.post(
        f"/api/v1/enrollment-tokens/{created['id']}/revoke", headers=headers
    )
    assert revoke.status_code == 200
    response = await api_env.client.post(
        "/enroll/v1",
        json={"token": created["token"], "csr_pem": make_csr_pem(), "device_facts": FACTS},
    )
    assert response.status_code == 403
    garbage = await api_env.client.post(
        "/enroll/v1", json={"token": "nope.nope", "csr_pem": make_csr_pem(), "device_facts": FACTS}
    )
    assert garbage.status_code == 403


async def _enroll_device(api_env, org_id, admin) -> str:
    token = (await _issue_token(api_env, org_id, admin)).json()["token"]
    body = (
        await api_env.client.post(
            "/enroll/v1",
            json={"token": token, "csr_pem": make_csr_pem(), "device_facts": FACTS},
        )
    ).json()
    return body["device_id"]


async def test_agent_revoke_sets_flag_updates_crl_and_disconnects(api_env) -> None:
    # Deferred finding B: an operator can revoke an enrolled agent; the DB flag,
    # CA CRL, and a live-disconnect event all fire.
    import json as _json

    import redis.asyncio as aioredis

    from app.gateway.dispatch import REVOKE_CHANNEL
    from app.models import Agent

    org_id = await api_env.create_org("rev")
    admin = await api_env.create_user(org_id, "org-admin")
    device_id = await _enroll_device(api_env, org_id, admin)
    headers = await api_env.headers(org_id, admin)

    # listen for the live-disconnect event the gateway would consume
    sub = aioredis.from_url(api_env.settings.redis_url, decode_responses=True)
    pubsub = sub.pubsub()
    await pubsub.subscribe(REVOKE_CHANNEL)
    await pubsub.get_message(timeout=1)  # drain the subscribe confirmation

    resp = await api_env.client.post(f"/api/v1/agents/{device_id}/revoke", headers=headers)
    assert resp.status_code == 200, resp.text

    # DB flag set
    async with AsyncSession(api_env.admin_engine) as session:
        agent = (
            await session.execute(select(Agent).where(Agent.device_id == device_id))
        ).scalar_one()
        assert agent.cert_revoked is True
        serial = agent.cert_serial
    # CA CRL updated
    assert api_env.app.state.ca.is_revoked(serial)
    # revoke event published for live disconnect
    event = None
    for _ in range(20):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if message:
            event = _json.loads(message["data"])
            break
    await pubsub.aclose()
    await sub.aclose()
    assert event == {"device_id": device_id}


async def test_agent_revoke_requires_permission(api_env) -> None:
    org_id = await api_env.create_org("rev")
    admin = await api_env.create_user(org_id, "org-admin")
    device_id = await _enroll_device(api_env, org_id, admin)
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    resp = await api_env.client.post(f"/api/v1/agents/{device_id}/revoke", headers=headers)
    assert resp.status_code == 403


async def test_agent_revoke_unknown_device_404(api_env) -> None:
    org_id = await api_env.create_org("rev")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    resp = await api_env.client.post(f"/api/v1/agents/{uuid4()}/revoke", headers=headers)
    assert resp.status_code == 404


async def test_token_list_never_returns_secrets(api_env) -> None:
    org_id = await api_env.create_org("enr")
    admin = await api_env.create_user(org_id, "org-admin")
    await _issue_token(api_env, org_id, admin)
    headers = await api_env.headers(org_id, admin)
    listing = await api_env.client.get("/api/v1/enrollment-tokens", headers=headers)
    assert listing.status_code == 200
    for item in listing.json()["items"]:
        assert "token" not in item and "token_hash" not in item


async def test_unauthenticated_requests_rejected(api_env) -> None:
    assert (await api_env.client.get("/api/v1/enrollment-tokens")).status_code == 401
    bad = {"Authorization": "Bearer garbage"}
    assert (
        await api_env.client.get("/api/v1/enrollment-tokens", headers=bad)
    ).status_code == 401


async def test_concurrent_enroll_respects_max_uses(api_env) -> None:
    """A single-use token must mint exactly one agent even under a concurrent
    burst (no TOCTOU / lost-update on the use count)."""
    import asyncio

    from sqlalchemy import func

    from app.models import Agent

    org_id = await api_env.create_org("enr")
    admin = await api_env.create_user(org_id, "org-admin")
    token = (await _issue_token(api_env, org_id, admin, max_uses=1)).json()["token"]

    async def attempt() -> int:
        response = await api_env.client.post(
            "/enroll/v1",
            json={"token": token, "csr_pem": make_csr_pem(), "device_facts": FACTS},
        )
        return response.status_code

    results = await asyncio.gather(*(attempt() for _ in range(8)))
    assert results.count(201) == 1
    assert results.count(403) == 7

    async with AsyncSession(api_env.admin_engine) as session:
        agents = (
            await session.execute(
                select(func.count()).select_from(Agent).where(Agent.org_id == org_id)
            )
        ).scalar_one()
    assert agents == 1


# ---------------------------------------------------------------------------
# C4: optional capability advertisement (agent plan Task 5)
# ---------------------------------------------------------------------------


async def _agent_row(admin_engine, device_id):
    from app.models import Agent

    async with AsyncSession(admin_engine) as session:
        return (
            await session.execute(select(Agent).where(Agent.device_id == device_id))
        ).scalar_one()


async def test_enroll_without_capabilities_stores_the_empty_default(api_env) -> None:
    """OLD AGENT -> NEW SERVER. Every agent in the field today sends three
    fields. The stored value must be exactly what it is today -- `{}` -- and not
    None, because `docs/modules/ipam/spec.md` §5.3's dispatcher reads the map."""
    org_id = await api_env.create_org("cap")
    admin = await api_env.create_user(org_id, "org-admin")
    token = (await _issue_token(api_env, org_id, admin)).json()["token"]

    response = await api_env.client.post(
        "/enroll/v1", json={"token": token, "csr_pem": make_csr_pem(), "device_facts": FACTS}
    )
    assert response.status_code == 201, response.text
    agent = await _agent_row(api_env.admin_engine, response.json()["device_id"])
    assert agent.capabilities == {}


async def test_enroll_stores_advertised_capabilities(api_env) -> None:
    """C4's gap closed: the column that nothing ever wrote now carries what the
    agent said it can run. Asserted on the STORED ROW, not on the 201."""
    org_id = await api_env.create_org("cap")
    admin = await api_env.create_user(org_id, "org-admin")
    token = (await _issue_token(api_env, org_id, admin)).json()["token"]

    advertised = {"inventory.collect": True, "health.check.now": True, "net.sweep": False}
    response = await api_env.client.post(
        "/enroll/v1",
        json={
            "token": token,
            "csr_pem": make_csr_pem(),
            "device_facts": FACTS,
            "capabilities": advertised,
        },
    )
    assert response.status_code == 201, response.text
    agent = await _agent_row(api_env.admin_engine, response.json()["device_id"])
    assert agent.capabilities == advertised


async def test_enroll_request_tolerates_an_unknown_field(api_env) -> None:
    """NEW AGENT -> OLD SERVER, asserted from the other side.

    This is the compatibility direction this repo has no precedent for. An
    agent built against a newer control plane will one day send a field this
    server has never heard of. Pydantic's default `extra` behaviour is
    "ignore", so it is dropped and enrollment succeeds.

    If this guard were deleted, the observation is an `EnrollRequest` tightened
    to `extra="forbid"` -- at which point every NEW agent gets a 422 from every
    OLD control plane, which is a fleet that cannot enroll during exactly the
    window when the two versions coexist. No other test in this repository
    sends an unknown key.
    """
    org_id = await api_env.create_org("cap")
    admin = await api_env.create_user(org_id, "org-admin")
    token = (await _issue_token(api_env, org_id, admin)).json()["token"]

    response = await api_env.client.post(
        "/enroll/v1",
        json={
            "token": token,
            "csr_pem": make_csr_pem(),
            "device_facts": FACTS,
            "capabilities": {"inventory.collect": True},
            "a_field_from_a_future_agent": {"nested": [1, 2, 3]},
        },
    )
    assert response.status_code == 201, (
        "an unknown request field was rejected; a newer agent could not enroll here"
    )
    agent = await _agent_row(api_env.admin_engine, response.json()["device_id"])
    assert agent.capabilities == {"inventory.collect": True}


async def test_capabilities_are_never_echoed_in_the_enroll_response(api_env) -> None:
    """The response is six fields (`EnrollResponse`) and the agent already knows
    what it advertised. Echoing it back would invite an agent to treat the
    server's copy as authoritative, and an advertisement is not an
    authorization."""
    org_id = await api_env.create_org("cap")
    admin = await api_env.create_user(org_id, "org-admin")
    token = (await _issue_token(api_env, org_id, admin)).json()["token"]

    response = await api_env.client.post(
        "/enroll/v1",
        json={
            "token": token,
            "csr_pem": make_csr_pem(),
            "device_facts": FACTS,
            "capabilities": {"inventory.collect": True},
        },
    )
    assert response.status_code == 201, response.text
    assert set(response.json()) == {
        "device_id",
        "cert_pem",
        "chain_pem",
        "ca_bundle_pem",
        "gateway_url",
        "job_signing_public_key_pem",
    }


async def test_an_agent_that_sends_no_macs_still_enrols(api_env) -> None:
    """THE WIRE-COMPATIBILITY CASE for asset-inventory §4's `macs` field.

    A fleet is always mixed-version: an agent in the field is older than the
    control plane it talks to and updates on its own schedule. A field a new
    server REQUIRES and an old agent does not send is an outage — and it is one
    no other test here would show, because every fixture in this file is
    written against today's server and would simply be updated alongside it.

    `FACTS` deliberately has no `macs` key at all, which is exactly what an
    agent built before 2026-08-18 puts on the wire.
    """
    org_id = await api_env.create_org("enr")
    admin = await api_env.create_user(org_id, "org-admin")
    created = await _issue_token(api_env, org_id, admin)
    token = created.json()["token"]

    assert "macs" not in FACTS
    response = await api_env.client.post(
        "/enroll/v1", json={"token": token, "csr_pem": make_csr_pem(), "device_facts": FACTS}
    )
    assert response.status_code == 201, response.text


def test_the_facts_model_actually_carries_macs() -> None:
    """NON-VACUITY for the request test below, and it is not optional.

    `DeviceFacts` does not forbid extra keys, so a request carrying `macs`
    is accepted whether the field exists or not -- a typo'd or deleted field
    name would leave the endpoint returning 201 and the addresses on the
    floor. This asserts the model parses them.
    """
    facts = DeviceFacts(hostname="x", os_family="linux", macs=["00:1b:21:aa:bb:cc"])
    assert facts.macs == ["00:1b:21:aa:bb:cc"]
    # Defaulted, not required -- the compatibility case above.
    assert DeviceFacts(hostname="x", os_family="linux").macs == []


async def test_an_agent_may_send_macs(api_env) -> None:
    """The other half: a current agent's request is accepted."""
    org_id = await api_env.create_org("enr")
    admin = await api_env.create_user(org_id, "org-admin")
    created = await _issue_token(api_env, org_id, admin)
    token = created.json()["token"]

    response = await api_env.client.post(
        "/enroll/v1",
        json={
            "token": token,
            "csr_pem": make_csr_pem(),
            "device_facts": {**FACTS, "macs": ["00:1b:21:aa:bb:cc"]},
        },
    )
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# Revoked entries are HIDDEN, never deleted.
#
# The operator's report was "revoked certificates are still present on the
# certificates page, there should be an option to remove them". Nothing here
# deletes a row, and the reasons are recorded at the two sites that would have
# had to: `app/services/enrollment.py::list_tokens` and this file's
# `test_a_revoked_agent_certificate_is_never_removed_from_the_record`. A
# revocation is the most auditable act on this screen (invariant 4) and the
# `agents` row is the ONLY mapping from a CRL serial back to a device, so the
# answer is a default filter and a visible count, not a DELETE.
# ---------------------------------------------------------------------------


async def _list_tokens(api_env, org_id, sub, **params):
    headers = await api_env.headers(org_id, sub)
    return await api_env.client.get(
        "/api/v1/enrollment-tokens", params=params, headers=headers
    )


async def test_the_token_list_hides_revoked_tokens_and_says_how_many(api_env) -> None:
    """The default response carries the LIVE tokens and a count of what it
    withheld. The count is the whole reason this is a filter and not a
    deletion: an operator has to be able to see that something was hidden."""
    org_id = await api_env.create_org("enr")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    kept = (await _issue_token(api_env, org_id, admin, name="kept")).json()
    gone = (await _issue_token(api_env, org_id, admin, name="gone")).json()
    assert (
        await api_env.client.post(
            f"/api/v1/enrollment-tokens/{gone['id']}/revoke", headers=headers
        )
    ).status_code == 200

    body = (await _list_tokens(api_env, org_id, admin)).json()
    assert [item["id"] for item in body["items"]] == [kept["id"]]
    assert body["total"] == 1
    assert body["revoked_count"] == 1, "a hidden row that is not counted has disappeared"


async def test_the_token_list_returns_revoked_tokens_when_asked(api_env) -> None:
    """`include_revoked=true`, the shape `GET /device-groups?include_archived`
    already uses. The row is still there; it was never deleted."""
    org_id = await api_env.create_org("enr")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    kept = (await _issue_token(api_env, org_id, admin, name="kept")).json()
    gone = (await _issue_token(api_env, org_id, admin, name="gone")).json()
    await api_env.client.post(
        f"/api/v1/enrollment-tokens/{gone['id']}/revoke", headers=headers
    )

    body = (await _list_tokens(api_env, org_id, admin, include_revoked="true")).json()
    assert {item["id"] for item in body["items"]} == {kept["id"], gone["id"]}
    assert body["total"] == 2
    assert body["revoked_count"] == 1
    assert [item for item in body["items"] if item["id"] == gone["id"]][0]["revoked"] is True


async def test_the_token_list_is_paged_and_the_total_is_not(api_env) -> None:
    """THE ROUTE WAS UNBOUNDED, and enrollment tokens only accumulate.

    One is minted per device onboarded, provisioning mints one per run, and
    nothing is ever deleted -- `list_tokens` records why a revocation has to
    outlive the credential. So the response grew forever on the one screen an
    operator is on while onboarding, until it timed out or exhausted the
    console's memory. `TokenPage.total` was already carried against exactly
    this day; this is the day.

    `limit`/`offset` with a capped `limit`, which is what `GET /patch/patches`,
    `GET /devices` and `GET /audit` already take. `total` counts the tokens
    MATCHING THE FILTER, not the ones on the page -- that is the whole point of
    the number, and a `total` that shrank to `len(items)` would tell a console
    it had reached the end of a list it had seen one page of.
    """
    org_id = await api_env.create_org("enr-page")
    admin = await api_env.create_user(org_id, "org-admin")
    issued = [
        (await _issue_token(api_env, org_id, admin, name=f"t{index}")).json()["id"]
        for index in range(5)
    ]
    # Newest first, which is the order `list_tokens` has always returned.
    newest_first = list(reversed(issued))

    first = (await _list_tokens(api_env, org_id, admin, limit=2)).json()
    assert [item["id"] for item in first["items"]] == newest_first[:2]
    assert first["total"] == 5, "the total must count the org's tokens, not the page"

    second = (await _list_tokens(api_env, org_id, admin, limit=2, offset=2)).json()
    assert [item["id"] for item in second["items"]] == newest_first[2:4]
    assert second["total"] == 5

    last = (await _list_tokens(api_env, org_id, admin, limit=2, offset=4)).json()
    assert [item["id"] for item in last["items"]] == newest_first[4:]
    assert last["total"] == 5

    # Past the end is an empty page, not an error: an operator who deep-links
    # an offset and then revokes their way below it must not get a 4xx.
    beyond = (await _list_tokens(api_env, org_id, admin, limit=2, offset=99)).json()
    assert beyond["items"] == []
    assert beyond["total"] == 5


async def test_the_token_list_refuses_an_uncapped_page(api_env) -> None:
    """THE CAP IS THE POINT OF THE CHANGE. A `limit` a caller can set to
    anything is the unbounded route with an extra parameter, so the ceiling is
    the route's, not the console's -- 500, matching every other paged list on
    this platform. `offset` cannot go negative."""
    org_id = await api_env.create_org("enr-cap")
    admin = await api_env.create_user(org_id, "org-admin")

    assert (await _list_tokens(api_env, org_id, admin, limit=501)).status_code == 422
    assert (await _list_tokens(api_env, org_id, admin, limit=0)).status_code == 422
    assert (await _list_tokens(api_env, org_id, admin, offset=-1)).status_code == 422
    assert (await _list_tokens(api_env, org_id, admin, limit=500)).status_code == 200


async def test_paging_the_token_list_still_counts_every_revocation(api_env) -> None:
    """`revoked_count` IS AN ORG-WIDE ANSWER AND MUST NOT LEARN ABOUT THE PAGE.

    It is the number the "Show revoked tokens" control offers to reveal, and it
    is exactly where an org-wide fact hides behind a correctly-paged list: a
    `revoked_count` computed from the page would say "1 hidden" on a screen
    withholding forty, and the operator has no way to tell from the page.
    """
    org_id = await api_env.create_org("enr-revcount")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    ids = [
        (await _issue_token(api_env, org_id, admin, name=f"t{index}")).json()["id"]
        for index in range(4)
    ]
    for token_id in ids[:3]:
        assert (
            await api_env.client.post(
                f"/api/v1/enrollment-tokens/{token_id}/revoke", headers=headers
            )
        ).status_code == 200

    page = (await _list_tokens(api_env, org_id, admin, limit=1)).json()
    assert len(page["items"]) == 1
    assert page["total"] == 1, "one live token, whatever the page size"
    assert page["revoked_count"] == 3

    shown = (
        await _list_tokens(api_env, org_id, admin, limit=1, include_revoked="true")
    ).json()
    assert len(shown["items"]) == 1
    assert shown["total"] == 4
    assert shown["revoked_count"] == 3


async def test_the_token_query_is_tenant_scoped_where_rls_is_not_the_thing_rejecting(
    bypassrls_session,
) -> None:
    """The service's OWN `org_id` predicates, on an engine that ignores RLS.

    Every assertion above runs through `org_session` on the `app_user` role,
    where Postgres rejects a cross-tenant row whether or not the query asked it
    to -- so deleting `WHERE org_id` from this service turns NONE of them red
    (`docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md`). Here the
    SQL is the only thing scoping anything.

    BOTH NUMBERS ARE CHECKED, not just the page. `revoked_count` is the one
    this cycle adds and it is an aggregate over the whole table -- exactly
    where an org-wide answer hides behind a correctly-filtered page. A
    neighbour tenant's revoked token inflating my "3 revoked hidden" badge is
    a cross-tenant read of a security event.
    """
    from app.models import Organization
    from app.services import enrollment as svc

    orgs: dict[str, UUID] = {}
    mine_live: UUID | None = None
    for key in ("mine", "theirs"):
        org = Organization(name=key, slug=f"enr-svc-{uuid4().hex[:8]}")
        bypassrls_session.add(org)
        await bypassrls_session.flush()
        orgs[key] = org.id
        live = EnrollmentToken(
            org_id=org.id,
            token_hash=uuid4().hex + uuid4().hex,
            name=f"live-{key}",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
            client_id=org.id,
            site_id=org.id,
        )
        bypassrls_session.add(live)
        bypassrls_session.add(
            EnrollmentToken(
                org_id=org.id,
                token_hash=uuid4().hex + uuid4().hex,
                name=f"dead-{key}",
                expires_at=datetime.now(UTC) + timedelta(hours=24),
                revoked=True,
                client_id=org.id,
                site_id=org.id,
            )
        )
        await bypassrls_session.flush()
        if key == "mine":
            mine_live = live.id
    await bypassrls_session.commit()

    page = await svc.list_tokens(bypassrls_session, org_id=orgs["mine"])
    assert [row.id for row in page.rows] == [mine_live]
    assert page.total == 1
    assert page.revoked_count == 1, "their revoked token is not in my hidden count"

    shown = await svc.list_tokens(bypassrls_session, org_id=orgs["mine"], include_revoked=True)
    assert len(shown.rows) == 2
    assert {row.name for row in shown.rows} == {"live-mine", "dead-mine"}


async def test_a_revoked_agent_certificate_is_visible_on_the_device_list(api_env) -> None:
    """`GET /devices` served `cert_state` and never `cert_revoked`, so a
    revoked certificate rendered as `ok` on the console's Certificates tab and
    still offered a Revoke button. "Still present" was literally true: the
    console could not say the certificate was dead."""
    org_id = await api_env.create_org("rev")
    admin = await api_env.create_user(org_id, "org-admin")
    device_id = await _enroll_device(api_env, org_id, admin)
    headers = await api_env.headers(org_id, admin)

    before = (await api_env.client.get("/api/v1/devices", headers=headers)).json()
    row = [item for item in before["items"] if item["id"] == device_id][0]
    assert row["cert_revoked"] is False
    assert row["cert_state"] == "ok"

    assert (
        await api_env.client.post(f"/api/v1/agents/{device_id}/revoke", headers=headers)
    ).status_code == 200

    after = (await api_env.client.get("/api/v1/devices", headers=headers)).json()
    row = [item for item in after["items"] if item["id"] == device_id][0]
    assert row["cert_revoked"] is True
    detail = (
        await api_env.client.get(f"/api/v1/devices/{device_id}", headers=headers)
    ).json()
    assert detail["cert_revoked"] is True


async def test_a_revoked_agent_certificate_is_never_removed_from_the_record(api_env) -> None:
    """NO ROUTE DELETES AN AGENT ROW, and this pins that it stays that way.

    `POST /agents/{id}/revoke` writes the serial into the CA's CRL
    (`app/core/ca.py`), which lives OUTSIDE the database and is keyed by serial
    alone. `agents.cert_serial` is the only mapping from a CRL entry back to a
    device, an org and a fingerprint, and `revoke_agent` deliberately commits
    the DB flag BEFORE the CRL so a failed CRL write can be retried. Delete the
    row and that retry is impossible while the certificate is still inside its
    validity window -- so the row stays and the console hides it instead.
    """
    from app.models import Agent

    org_id = await api_env.create_org("rev")
    admin = await api_env.create_user(org_id, "org-admin")
    device_id = await _enroll_device(api_env, org_id, admin)
    headers = await api_env.headers(org_id, admin)
    await api_env.client.post(f"/api/v1/agents/{device_id}/revoke", headers=headers)

    async with AsyncSession(api_env.admin_engine) as session:
        agent = (
            await session.execute(select(Agent).where(Agent.device_id == device_id))
        ).scalar_one()
    assert agent.cert_revoked is True
    assert agent.cert_serial, "the serial the CRL is keyed by is still attributable"

    # `flatten`, NOT `app.routes`. This codebase's `register()` wraps included
    # routers, so `app.routes` alone finds nothing under `/api/v1` -- a sweep
    # over an empty set is the shape of a guard that passes because it looked
    # nowhere (`app/core/router_inspect.py`, and the same warning at the top of
    # `test_ai_tool_surface.py`).
    from app.core.router_inspect import flatten

    paths = flatten(api_env.app.router)
    assert any("/agents/{device_id}/revoke" in path for path, _ in paths), (
        "the sweep below found no agent routes at all, so it proves nothing"
    )
    deleters = {
        path
        for path, route in paths
        if "DELETE" in (getattr(route, "methods", None) or set())
        and ("/agents" in path or "/enrollment-tokens" in path)
    }
    assert deleters == set(), f"a revoked record became deletable: {deleters}"


# --------------------------------------------------------------------------- #
# `max_uses`: required, capped, and grandfathered.
#
# The defect: `POST /enrollment-tokens` read
# `max_uses: int | None = Field(default=None, ge=1)`, and `None` means
# UNLIMITED inside `enroll()`'s guarded UPDATE. So the field an operator left
# alone minted a credential that turns any number of unmanaged machines into
# devices of this organisation, each with a certificate, until it expired.
# `docs/modules/agent-distribution/spec-managed-distribution.md` §3.5 recorded
# it as *found and not fixed*.
# --------------------------------------------------------------------------- #


async def test_a_token_cannot_be_minted_without_a_use_limit(api_env) -> None:
    """OMITTING `max_uses` IS A 422 NAMING THE FIELD, and it used to be an
    unlimited token.

    THE REFUSAL IS AT CREATION AND THAT IS THE WHOLE DESIGN. The alternative --
    re-reading a stored NULL as "one" -- fails at the ENDPOINT, inside a
    guarded UPDATE whose every rejection is deliberately the same detail-free
    403, so a fleet token that quietly became single-use presents to its
    operator exactly as a REVOKED one, on machines nobody is sitting at.
    """
    org_id = await api_env.create_org("enr-bound")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)

    omitted = await api_env.client.post(
        "/api/v1/enrollment-tokens",
        # The site is given, so the only thing missing is the bound.
        json={"name": "no-limit", "expires_in_hours": 24, "site_id": str(org_id)},
        headers=headers,
    )
    assert omitted.status_code == 422
    assert "max_uses" in omitted.text, "the refusal must name the field"

    explicit_null = await api_env.client.post(
        "/api/v1/enrollment-tokens",
        json={
            "name": "no-limit",
            "expires_in_hours": 24,
            "max_uses": None,
            "site_id": str(org_id),
        },
        headers=headers,
    )
    assert explicit_null.status_code == 422, (
        "spelling the unlimited case out loud must not be a way around the bound"
    )

    # And nothing was written on either refusal.
    listed = (await _list_tokens(api_env, org_id, admin)).json()
    assert listed["total"] == 0


async def test_the_use_limit_has_a_ceiling_and_it_is_the_platforms(api_env) -> None:
    """`ge=1` ALONE IS NOT A BOUND -- a caller could type `2**31 - 1` and call
    it finite. The ceiling is `MAX_TOKEN_USES`, which is the managed
    distribution spec's number for the same reason it is that module's."""
    from app.services.enrollment import MAX_TOKEN_USES

    org_id = await api_env.create_org("enr-ceiling")
    admin = await api_env.create_user(org_id, "org-admin")

    assert (
        await _issue_token(api_env, org_id, admin, max_uses=MAX_TOKEN_USES)
    ).status_code == 201
    assert (
        await _issue_token(api_env, org_id, admin, max_uses=MAX_TOKEN_USES + 1)
    ).status_code == 422
    assert (await _issue_token(api_env, org_id, admin, max_uses=0)).status_code == 422
    assert (await _issue_token(api_env, org_id, admin, max_uses=-1)).status_code == 422


def test_the_kernels_ceiling_and_the_distribution_modules_are_one_number() -> None:
    """HELD EQUAL, NOT IMPORTED. The kernel must not depend on a module
    (CLAUDE.md: "modules register into the kernel; they do not fork it"), so
    `MAX_TOKEN_USES` is defined in `services/enrollment.py` and this is what
    stops the two copies disagreeing -- the same arrangement
    `device_groups.MAX_GROUP_DEPTH` has with health's."""
    from app.modules.agent_distribution.managed import MAX_DEPLOYMENT_USES
    from app.services.enrollment import MAX_TOKEN_USES

    assert MAX_TOKEN_USES == MAX_DEPLOYMENT_USES


async def test_the_bound_is_the_services_and_not_only_the_routes(api_env) -> None:
    """FOUR CALL SITES MINT TOKENS AND ONE HAS A PYDANTIC MODEL IN FRONT OF IT.

    The kernel route, `agent_distribution` twice and `provisioning` all reach
    `create_token` directly. A bound that lived only in `TokenCreateRequest`
    would be a bound the next caller did not inherit, so the service refuses
    too -- before a secret is generated.
    """
    import pytest

    from app.core.tenancy import org_session
    from app.services.enrollment import MAX_TOKEN_USES, TokenBoundsError, create_token

    org_id = await api_env.create_org("enr-svc-bound")
    admin = await api_env.create_user(org_id, "org-admin")
    async with org_session(
        org_id, engine=api_env.app.state.app_engine, clients=ClientScope.all()
    ) as session:
        for bad in (None, 0, -1, MAX_TOKEN_USES + 1):
            with pytest.raises(TokenBoundsError):
                await create_token(
                    session,
                    org_id=org_id,
                    site_id=org_id,
                    created_by=admin,
                    name="direct",
                    expires_in_hours=1,
                    max_uses=bad,  # type: ignore[arg-type]
                )

    # Nothing was written by any of the four refusals.
    listed = (await _list_tokens(api_env, org_id, admin)).json()
    assert listed["total"] == 0


async def test_a_token_issued_before_the_bound_still_enrols(api_env) -> None:
    """GRANDFATHERED, AND THIS IS THE FALSIFIER FOR "NO SILENT REVOCATION".

    No migration rewrites `enrollment_tokens`. A row with `max_uses IS NULL`
    was issued under a meaning -- unlimited -- and `enroll()` still honours it,
    twice here, because the failure mode of changing that meaning is invisible:
    the same 403 an operator reads as "somebody revoked my token".

    If a future change makes `enroll()` treat NULL as anything else, THIS goes
    red rather than a fleet does.
    """
    org_id = await api_env.create_org("enr-grandfather")

    # Written the way the old route wrote it: `max_uses` left at NULL.
    async with AsyncSession(api_env.admin_engine) as session:
        import hashlib
        import secrets as pysecrets

        secret = pysecrets.token_urlsafe(32)
        row = EnrollmentToken(
            org_id=org_id,
            token_hash=hashlib.sha256(secret.encode()).hexdigest(),
            name="legacy-unlimited",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
            max_uses=None,
            client_id=org_id,
            site_id=org_id,
        )
        session.add(row)
        # The id is read BEFORE the commit expires the instance: `uuid7_pk`'s
        # default is client-side, so it is populated at flush, and touching an
        # expired attribute afterwards is a lazy IO in a sync context.
        await session.flush()
        token_id = row.id
        await session.commit()
        token = f"{token_id}.{secret}"

    for hostname in ("legacy-a", "legacy-b", "legacy-c"):
        response = await api_env.client.post(
            "/enroll/v1",
            json={
                "token": token,
                "csr_pem": make_csr_pem(),
                "device_facts": FACTS | {"hostname": hostname},
            },
        )
        assert response.status_code == 201, (
            f"{hostname} was refused: a stored NULL stopped meaning unlimited, "
            "which is the silent misdiagnosable failure this test exists for"
        )


async def test_the_console_can_count_and_find_the_unlimited_tokens_left(api_env) -> None:
    """THE ANSWER TO A GRANDFATHERED CREDENTIAL IS A HUMAN WITH A REVOKE
    BUTTON, and this is what puts them in front of one.

    `unlimited_count` counts what is STILL REDEEMABLE and unbounded -- not
    revoked, not expired -- because a banner that also counted history would
    cry wolf on every deployment that ever issued one. `unlimited_only=true`
    lists exactly the rows that number counted, using the same predicate, so
    the control and the list it opens cannot disagree about which ones.
    """
    org_id = await api_env.create_org("enr-unlimited")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)

    bounded = (await _issue_token(api_env, org_id, admin, name="bounded")).json()
    async with AsyncSession(api_env.admin_engine) as session:
        rows = {
            "live-unlimited": (datetime.now(UTC) + timedelta(hours=24), False),
            "expired-unlimited": (datetime.now(UTC) - timedelta(hours=1), False),
            "revoked-unlimited": (datetime.now(UTC) + timedelta(hours=24), True),
        }
        made: dict[str, UUID] = {}
        for name, (expires_at, revoked) in rows.items():
            row = EnrollmentToken(
                org_id=org_id,
                token_hash=uuid4().hex + uuid4().hex,
                name=name,
                expires_at=expires_at,
                max_uses=None,
                revoked=revoked,
                client_id=org_id,
                site_id=org_id,
            )
            session.add(row)
            await session.flush()
            made[name] = row.id
        await session.commit()

    body = (await _list_tokens(api_env, org_id, admin)).json()
    assert body["unlimited_count"] == 1, (
        "only the redeemable one is a standing risk; the expired and the "
        "revoked are history"
    )

    only = (await _list_tokens(api_env, org_id, admin, unlimited_only="true")).json()
    assert [item["id"] for item in only["items"]] == [str(made["live-unlimited"])]
    assert only["total"] == 1
    assert only["unlimited_count"] == 1
    assert bounded["id"] not in {item["id"] for item in only["items"]}

    # Revoking it is what makes the number fall -- there is no other way, and
    # that is the point.
    await api_env.client.post(
        f"/api/v1/enrollment-tokens/{made['live-unlimited']}/revoke", headers=headers
    )
    after = (await _list_tokens(api_env, org_id, admin)).json()
    assert after["unlimited_count"] == 0


async def test_the_unlimited_count_is_tenant_scoped_where_rls_is_not_rejecting(
    bypassrls_session,
) -> None:
    """`unlimited_count` IS AN AGGREGATE OVER THE WHOLE TABLE, which is exactly
    where a neighbour's row hides behind a correctly-filtered page.

    Everything a request touches runs as `app_user` (NOBYPASSRLS), so deleting
    `EnrollmentToken.org_id == org_id` from `_unlimited_predicates` turns no
    route test red. On this session Postgres stops filtering and the
    application's predicate is the only thing scoping the count.

    THE FALSIFIER IS PROVEN, not asserted: the neighbour org below holds TWO
    live unlimited tokens and this org holds one, so a missing predicate reads
    3 rather than 1.
    """
    from app.models import Organization
    from app.services import enrollment as svc

    orgs: dict[str, UUID] = {}
    for key, unlimited in (("mine", 1), ("theirs", 2)):
        org = Organization(name=key, slug=f"enr-unl-{uuid4().hex[:8]}")
        bypassrls_session.add(org)
        await bypassrls_session.flush()
        orgs[key] = org.id
        for index in range(unlimited):
            bypassrls_session.add(
                EnrollmentToken(
                    org_id=org.id,
                    token_hash=uuid4().hex + uuid4().hex,
                    name=f"unlimited-{key}-{index}",
                    expires_at=datetime.now(UTC) + timedelta(hours=24),
                    max_uses=None,
                    client_id=org.id,
                    site_id=org.id,
                )
            )
    await bypassrls_session.commit()

    page = await svc.list_tokens(bypassrls_session, org_id=orgs["mine"])
    assert page.unlimited_count == 1, "the neighbour's two are not my standing risk"

    only = await svc.list_tokens(
        bypassrls_session, org_id=orgs["mine"], unlimited_only=True
    )
    assert {row.name for row in only.rows} == {"unlimited-mine-0"}
