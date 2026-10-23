"""Manual asset entry (docs/modules/asset-inventory/spec.md §2, §7).

THE OPERATOR'S ASK, VERBATIM: "I should be able to manually add an asset with
name, description, IP address" -- for stock in the warehouse and for devices
that can never run an agent, like networking gear. So the create route must
accept a record with NO hostname and NO OS, which is exactly what 0061's
`NOT NULL` drops made expressible.

WHAT THESE TESTS REFUSE TO LET HAPPEN, and both are quiet failures rather than
loud ones:

  * a manual row claiming to be agent-managed. `source` is stamped by the
    route, never taken from the body, because it is the first thing an operator
    checks when a row looks wrong -- and a wrong answer there sends them to
    look at the agent instead of at whoever typed it.
  * an edit to a field the MACHINE owns. The next inventory run overwrites
    hostname, OS and serial, so accepting the edit and silently reverting it
    hours later is worse than refusing it now.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Agent, AuditEvent, Device
from tests.conftest import ApiEnv

_SWITCH: dict[str, Any] = {
    "hostname": "core-sw-01",
    "description": "core switch, rack 3",
    "primary_ip": "172.22.104.1",
    "asset_type": "network",
    "lifecycle": "unmanageable",
}


async def _enrolled_device(api_env: ApiEnv, org_id: UUID) -> UUID:
    """A device that HAS an agent, for the edit-refusal cases."""
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        device = Device(org_id=org_id, hostname="managed-01", os_family="linux")
        session.add(device)
        await session.flush()
        device_id = device.id
        session.add(
            Agent(
                org_id=org_id,
                device_id=device_id,
                cert_fingerprint=f"fp-{device_id.hex[:16]}",
                cert_serial="01",
                cert_not_after=datetime.now(UTC) + timedelta(days=365),
                enrolled_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return device_id


async def test_creating_an_asset_requires_manage(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("assets")
    reader = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.post(
        "/api/v1/devices", json=_SWITCH, headers=await api_env.headers(org_id, reader)
    )
    assert resp.status_code == 403, resp.text


async def test_an_operator_can_add_a_boxed_laptop_with_no_hostname_at_all(
    api_env: ApiEnv,
) -> None:
    """The warehouse case: a record whose whole identity is a description."""
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    resp = await api_env.client.post(
        "/api/v1/devices",
        json={"description": "boxed laptop, PO 4471", "lifecycle": "stock"},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["hostname"] is None
    assert body["os_family"] is None
    assert body["lifecycle"] == "stock"
    assert body["description"] == "boxed laptop, PO 4471"


async def test_a_manual_asset_is_marked_manual_and_cannot_claim_otherwise(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    resp = await api_env.client.post(
        "/api/v1/devices",
        json={**_SWITCH, "source": "agent"},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 422, resp.text

    ok = await api_env.client.post(
        "/api/v1/devices", json=_SWITCH, headers=await api_env.headers(org_id, admin)
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["source"] == "manual"
    # Managed is a JOIN, and there is no agent row -- so this must be null no
    # matter what the caller sent.
    assert ok.json()["agent_status"] is None


async def test_an_unknown_lifecycle_is_refused_where_the_operator_can_see_it(
    api_env: ApiEnv,
) -> None:
    """422 from the request model, not a 500 from the CHECK constraint. The
    operator typed this, and the message has to reach them."""
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    resp = await api_env.client.post(
        "/api/v1/devices",
        json={**_SWITCH, "lifecycle": "decommissioned"},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 422, resp.text


async def test_creating_and_editing_an_asset_are_both_audited(
    api_env: ApiEnv,
) -> None:
    """Invariant 4. Changing what an asset IS -- retiring it, marking it
    unmanageable -- changes what every sweep and alert does with it, which
    makes it a privileged action rather than a note."""
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    created = (
        await api_env.client.post("/api/v1/devices", json=_SWITCH, headers=headers)
    ).json()

    resp = await api_env.client.patch(
        f"/api/v1/devices/{created['id']}",
        json={"lifecycle": "retired", "location": "rack 3"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["lifecycle"] == "retired"
    assert resp.json()["location"] == "rack 3"

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        actions = list(
            (
                await session.execute(select(AuditEvent.action).order_by(AuditEvent.id))
            ).scalars()
        )
    assert "asset.created" in actions
    assert "asset.updated" in actions


async def test_a_managed_device_cannot_be_renamed_by_hand(api_env: ApiEnv) -> None:
    """A hostname on a managed device is REPORTED BY THE MACHINE, and the next
    inventory run overwrites whatever a human typed. Accepting the edit and
    reverting it silently hours later is the worse failure."""
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    device_id = await _enrolled_device(api_env, org_id)

    resp = await api_env.client.patch(
        f"/api/v1/devices/{device_id}",
        json={"hostname": "renamed-by-hand"},
        headers=headers,
    )
    assert resp.status_code == 409, resp.text
    assert "hostname" in resp.json()["detail"]


async def test_owner_and_location_stay_editable_on_a_managed_device(
    api_env: ApiEnv,
) -> None:
    """Non-vacuity for the refusal above: a route that rejected every PATCH on
    a managed device would pass that test and make the feature useless on the
    fleet. Owner and location are facts about the ORGANISATION -- no inventory
    run will ever overwrite them."""
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    device_id = await _enrolled_device(api_env, org_id)

    resp = await api_env.client.patch(
        f"/api/v1/devices/{device_id}",
        json={"owner": "helpdesk", "location": "HQ floor 2"},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["owner"] == "helpdesk"


async def test_editing_someone_elses_asset_is_a_404(api_env: ApiEnv) -> None:
    org_a = await api_env.create_org("assets-a")
    org_b = await api_env.create_org("assets-b")
    admin_a = await api_env.create_user(org_a, "org-admin")
    admin_b = await api_env.create_user(org_b, "org-admin")
    created = (
        await api_env.client.post(
            "/api/v1/devices", json=_SWITCH, headers=await api_env.headers(org_a, admin_a)
        )
    ).json()

    resp = await api_env.client.patch(
        f"/api/v1/devices/{created['id']}",
        json={"location": "somewhere else"},
        headers=await api_env.headers(org_b, admin_b),
    )
    assert resp.status_code == 404, resp.text


async def test_a_created_asset_belongs_to_the_callers_org(api_env: ApiEnv) -> None:
    """Falsifier 9 on the write path, asserted on the BYPASSRLS ADMIN engine.

    Through a tenant-scoped session a row written into the wrong org would be
    invisible rather than absent, and those two look identical from the API --
    which is exactly how a missing predicate survives a test suite.
    """
    org_a = await api_env.create_org("assets-a")
    org_b = await api_env.create_org("assets-b")
    admin_a = await api_env.create_user(org_a, "org-admin")
    created = (
        await api_env.client.post(
            "/api/v1/devices", json=_SWITCH, headers=await api_env.headers(org_a, admin_a)
        )
    ).json()

    async with AsyncSession(api_env.admin_engine) as session:
        owner = (
            await session.execute(
                select(Device.org_id).where(Device.id == UUID(created["id"]))
            )
        ).scalar_one()
    assert owner == org_a
    assert owner != org_b


# ---------------------------------------------------------------------------
# The DNS name (`devices.fqdn`) for a machine that has never run the agent.
#
# Windows push is Kerberos only, and Kerberos issues a ticket for a NAME
# (`HTTP/<fqdn>`), not an address (design 2026-09-28 §2.3). Until now the column
# was written only by an enrolled agent's inventory -- so a machine a push is
# FOR could never carry one, and every Windows push was refused. The operator
# types it; it is never derived from reverse DNS (plan W10: that would let
# whoever answers PTR queries choose which SPN a credential is presented for).
# ---------------------------------------------------------------------------


async def test_an_operator_can_record_a_dns_name_and_it_is_stored_lower_cased(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    resp = await api_env.client.post(
        "/api/v1/devices",
        json={"hostname": "PC1", "primary_ip": "10.20.4.31", "fqdn": "PC1.Corp.Example.com"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["fqdn"] == "pc1.corp.example.com"

    edited = await api_env.client.patch(
        f"/api/v1/devices/{resp.json()['id']}",
        json={"fqdn": "PC2.corp.example.com"},
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["fqdn"] == "pc2.corp.example.com"

    async with AsyncSession(api_env.admin_engine) as session:
        stored = (
            await session.execute(select(Device.fqdn).where(Device.id == UUID(resp.json()["id"])))
        ).scalar_one()
    assert stored == "pc2.corp.example.com"


async def test_a_dns_name_that_is_not_a_hostname_is_refused(api_env: ApiEnv) -> None:
    """Each of these would reach Kerberos as a service name it cannot issue a
    ticket for -- or, for an IP literal, as a name nobody vouched for."""
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    bad = [
        "pc1",  # no dot: not a full name
        "10.20.4.31",  # an IP literal
        "::1",  # an IPv6 literal
        "10.20.4",  # all-numeric last label: reads as an address to many resolvers
        "-pc1.corp.example.com",  # label starts with a hyphen
        "pc1-.corp.example.com",  # label ends with a hyphen
        "pc_1.corp.example.com",  # underscore
        "pc1..corp.example.com",  # empty label
        "pc 1.corp.example.com",  # space
        "pc1.corp.example.com/x",  # path
        ("a" * 64) + ".example.com",  # label over 63
        ".".join(["abcdefghi"] * 26),  # over 253 characters
    ]
    for value in bad:
        resp = await api_env.client.post(
            "/api/v1/devices", json={"description": "x", "fqdn": value}, headers=headers
        )
        assert resp.status_code == 422, (value, resp.text)


async def test_a_dns_name_can_be_cleared(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    created = (
        await api_env.client.post(
            "/api/v1/devices",
            json={"description": "x", "fqdn": "pc1.corp.example.com"},
            headers=headers,
        )
    ).json()
    for cleared in (None, ""):
        resp = await api_env.client.patch(
            f"/api/v1/devices/{created['id']}", json={"fqdn": cleared}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["fqdn"] is None


async def test_setting_a_dns_name_is_audited_by_field_name(api_env: ApiEnv) -> None:
    """Invariant 4. A DNS name decides which Kerberos service a push presents a
    domain credential to, so changing it is recorded -- by field name, like
    every other asset edit, and its value is not needed to reconstruct who
    changed it."""
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    created = (
        await api_env.client.post("/api/v1/devices", json=_SWITCH, headers=headers)
    ).json()
    resp = await api_env.client.patch(
        f"/api/v1/devices/{created['id']}",
        json={"fqdn": "core-sw-01.corp.example.com"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        events = list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.action == "asset.updated")
                    .where(AuditEvent.target_id == created["id"])
                )
            ).scalars()
        )
    assert len(events) == 1
    assert events[0].meta["fields"] == ["fqdn"]
    assert events[0].actor_type == "user"
    assert events[0].actor_id is not None
    # Field NAMES -- except the DNS name, whose old and new values are evidence
    # of which Kerberos service a push would present a domain account to
    # (review I1, 2026-09-29; a DNS name is not personal data).
    assert events[0].meta["fqdn"] == {"old": None, "new": "core-sw-01.corp.example.com"}


async def test_an_operator_can_declare_the_architecture_a_push_installs_for(
    api_env: ApiEnv,
) -> None:
    """Push picks the artefact from the asset's declared OS and architecture
    (`agent_push/dispatch.py`, spec §7.2) and refuses when either is unset --
    and until now only inventory could write `architecture`, so no machine a
    push was for could ever carry one. The agent verifies the declaration on
    the target before installing, so a wrong one is a clean failure."""
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    created = await api_env.client.post(
        "/api/v1/devices",
        json={"hostname": "PC1", "os_family": "windows", "architecture": "amd64"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["architecture"] == "amd64"

    refused = await api_env.client.post(
        "/api/v1/devices", json={"description": "x", "architecture": "sparc"}, headers=headers
    )
    assert refused.status_code == 422, refused.text

    cleared = await api_env.client.patch(
        f"/api/v1/devices/{created.json()['id']}", json={"architecture": None}, headers=headers
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["architecture"] == ""


async def test_a_managed_devices_architecture_is_the_machines(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    device_id = await _enrolled_device(api_env, org_id)
    resp = await api_env.client.patch(
        f"/api/v1/devices/{device_id}",
        json={"architecture": "arm64"},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 409, resp.text


async def test_a_managed_devices_dns_name_is_the_machines(api_env: ApiEnv) -> None:
    """The agent's inventory writes `fqdn` on every run (`inventory/ingest.py`),
    so an edit on a managed device would be reverted in silence -- refused,
    like the hostname."""
    org_id = await api_env.create_org("assets")
    admin = await api_env.create_user(org_id, "org-admin")
    device_id = await _enrolled_device(api_env, org_id)

    resp = await api_env.client.patch(
        f"/api/v1/devices/{device_id}",
        json={"fqdn": "managed-01.corp.example.com"},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 409, resp.text
    assert "fqdn" in resp.json()["detail"]



async def test_a_dns_name_change_is_audited_with_both_values(api_env) -> None:
    """Review I1: the DNS name decides which Kerberos service a domain
    credential is presented to, so its history is evidence. Not secret."""
    from sqlalchemy import select as _select
    from sqlalchemy.ext.asyncio import AsyncSession as _Session

    from app.models import AuditEvent as _AuditEvent

    org_id = await api_env.create_org("fqdn-audit")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    created = await api_env.client.post(
        "/api/v1/devices",
        json={"hostname": "pc1", "primary_ip": "10.9.0.5", "asset_type": "computer",
              "fqdn": "pc1.corp.example.com"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    device_id = created.json()["id"]
    updated = await api_env.client.patch(
        f"/api/v1/devices/{device_id}",
        json={"fqdn": "PC1.Branch.Corp.Example.com"},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    async with _Session(api_env.admin_engine) as session:
        event = (
            await session.execute(
                _select(_AuditEvent).where(
                    _AuditEvent.org_id == org_id, _AuditEvent.action == "asset.updated"
                )
            )
        ).scalar_one()
    assert event.meta["fields"] == ["fqdn"]
    assert event.meta["fqdn"] == {
        "old": "pc1.corp.example.com", "new": "pc1.branch.corp.example.com",
    }
