from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device


async def _device(api_env, org_id: UUID) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, hostname="docs-dev", os_family="linux")
        session.add(device)
        await session.commit()
        return device.id


async def _create_article(api_env, headers, title="Backup runbook", body="restic restore steps"):
    return await api_env.client.post(
        "/api/v1/docs/articles",
        json={"title": title, "body": body, "category": "Runbooks"},
        headers=headers,
    )


async def test_rbac_read_only_cannot_mutate(api_env) -> None:
    org_id = await api_env.create_org("docs")
    reader = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, reader)
    assert (await api_env.client.get("/api/v1/docs/articles", headers=headers)).status_code == 200
    assert (await _create_article(api_env, headers)).status_code == 403


async def test_article_crud_versions_and_asset_filter(api_env) -> None:
    org_id = await api_env.create_org("docs")
    device_id = await _device(api_env, org_id)
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    created = await _create_article(api_env, headers)
    assert created.status_code == 201, created.text
    article_id = created.json()["id"]

    updated = await api_env.client.put(
        f"/api/v1/docs/articles/{article_id}",
        json={"title": "Backup runbook", "body": "restic restore steps v2",
              "category": "Runbooks"},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["current_version"] == 2

    versions = await api_env.client.get(
        f"/api/v1/docs/articles/{article_id}/versions", headers=headers
    )
    assert [v["version"] for v in versions.json()] == [2, 1]

    # link to a device, then find via the asset filter
    link = await api_env.client.post(
        f"/api/v1/docs/articles/{article_id}/links",
        json={"target_type": "device", "target_id": str(device_id)},
        headers=headers,
    )
    assert link.status_code == 201
    linked = await api_env.client.get(
        f"/api/v1/docs/articles?target_type=device&target_id={device_id}", headers=headers
    )
    assert [a["id"] for a in linked.json()["items"]] == [article_id]

    # archive hides from default list
    assert (
        await api_env.client.post(
            f"/api/v1/docs/articles/{article_id}/archive", headers=headers
        )
    ).status_code == 200
    default_list = await api_env.client.get("/api/v1/docs/articles", headers=headers)
    assert default_list.json()["items"] == []
    with_archived = await api_env.client.get(
        "/api/v1/docs/articles?include_archived=true", headers=headers
    )
    assert len(with_archived.json()["items"]) == 1


async def test_link_to_foreign_target_404(api_env) -> None:
    org_a = await api_env.create_org("docs")
    org_b = await api_env.create_org("docs-b")
    foreign_device = await _device(api_env, org_b)
    tech = await api_env.create_user(org_a, "technician")
    headers = await api_env.headers(org_a, tech)
    article_id = (await _create_article(api_env, headers)).json()["id"]
    response = await api_env.client.post(
        f"/api/v1/docs/articles/{article_id}/links",
        json={"target_type": "device", "target_id": str(foreign_device)},
        headers=headers,
    )
    assert response.status_code == 404


async def test_records_crud(api_env) -> None:
    """`service` rather than `network`: `record_type='network'` is now refused
    outright (docs/modules/ipam/spec.md §0.14/§0.18 -- the address plan owns
    prefixes, and two places to write one fact is how a plan of record rots).
    The refusal and its three "existing rows keep working" guarantees have their
    own tests below."""
    org_id = await api_env.create_org("docs")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    created = await api_env.client.post(
        "/api/v1/docs/records",
        json={"record_type": "service", "name": "HQ payroll",
              "fields": {"url": "https://payroll.internal", "owner": "finance"}},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    record_id = created.json()["id"]
    bad = await api_env.client.post(
        "/api/v1/docs/records",
        json={"record_type": "vendor", "name": "Bad", "fields": {"contract_end": "nope"}},
        headers=headers,
    )
    assert bad.status_code == 422
    listing = await api_env.client.get(
        "/api/v1/docs/records?record_type=service", headers=headers
    )
    assert [r["id"] for r in listing.json()["items"]] == [record_id]


async def test_search_ranking_phrases_and_records(api_env) -> None:
    org_id = await api_env.create_org("docs")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    await _create_article(api_env, headers, "Printer troubleshooting", "reset the spooler queue")
    await _create_article(api_env, headers, "Network overview", "printer VLAN lives on floor 2")
    archived = (
        await _create_article(api_env, headers, "Old printer doc", "printer spooler")
    ).json()
    await api_env.client.post(
        f"/api/v1/docs/articles/{archived['id']}/archive", headers=headers
    )
    await api_env.client.post(
        "/api/v1/docs/records",
        json={"record_type": "service", "name": "Printer server", "fields": {}},
        headers=headers,
    )

    hits = (await api_env.client.get("/api/v1/docs/search?q=printer", headers=headers)).json()
    kinds = {h["kind"] for h in hits}
    assert kinds == {"article", "record"}
    articles = [h for h in hits if h["kind"] == "article"]
    assert articles[0]["title"] == "Printer troubleshooting"  # title match ranks first
    assert all(h["title"] != "Old printer doc" for h in hits)  # archived excluded

    phrase = (
        await api_env.client.get('/api/v1/docs/search?q="spooler queue"', headers=headers)
    ).json()
    assert [h["title"] for h in phrase if h["kind"] == "article"] == ["Printer troubleshooting"]

    empty = await api_env.client.get("/api/v1/docs/search?q=", headers=headers)
    assert empty.status_code == 422


# ---------------------------------------------------------------------------
# IPAM absorbs `network` records (docs/modules/ipam/spec.md §0.14, §0.18, §8.3,
# §14.4). THIS IS A DELIBERATE BEHAVIOUR CHANGE TO A SHIPPED MODULE: a create
# call that succeeded yesterday now returns an error naming the endpoint to use
# instead. The three guarantees that make it safe -- existing rows still read,
# still update, still archive -- are asserted here rather than assumed, because
# migrations are forward-only and those rows exist in whatever deployments
# created them.
# ---------------------------------------------------------------------------


async def test_creating_a_network_record_is_refused_and_names_the_ipam_endpoint(
    api_env,
) -> None:
    """§0.18: the refusal is UNCONDITIONAL and lives in the SERVICE.

    There is no per-module install state in this codebase, so "once IPAM is
    installed" would name no condition a caller could evaluate. And it is not in
    `RecordCreate`'s regex: `network` must stay in `RECORD_TYPES` so existing
    rows keep validating, reading, updating and archiving.

    The error names `POST /api/v1/ipam/subnets`, because an error that only says
    "no" leaves the operator to guess where the fact now lives.
    """
    org_id = await api_env.create_org("docs-net")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))
    refused = await api_env.client.post(
        "/api/v1/docs/records",
        json={"record_type": "network", "name": "HQ LAN",
              "fields": {"cidr": "10.1.0.0/16"}},
        headers=headers,
    )
    assert refused.status_code == 422, refused.text
    assert "/api/v1/ipam/subnets" in refused.text

    from app.modules.documentation.schemas import RECORD_TYPES

    assert "network" in RECORD_TYPES, (
        "`network` must stay a legal stored record_type: migrations are "
        "forward-only and existing rows must keep validating"
    )


async def test_an_existing_network_record_still_reads_updates_and_archives(
    api_env, admin_engine
) -> None:
    """§0.14's three guarantees, ALL THREE, against a row seeded the way a
    pre-existing deployment's row exists -- directly, since the create path is
    now closed.

    A refusal that also broke reading, editing or archiving would be a data-loss
    change dressed up as a deprecation.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import DocRecord

    org_id = await api_env.create_org("docs-legacy")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        record = DocRecord(
            org_id=org_id, record_type="network", name="Legacy LAN",
            fields={"cidr": "10.1.0.0/16", "vlan": 10},
        )
        session.add(record)
        await session.commit()
        record_id = record.id

    read = await api_env.client.get(f"/api/v1/docs/records/{record_id}", headers=headers)
    assert read.status_code == 200, read.text
    assert read.json()["fields"]["cidr"] == "10.1.0.0/16"

    updated = await api_env.client.put(
        f"/api/v1/docs/records/{record_id}",
        json={"name": "Legacy LAN (renamed)",
              "fields": {"cidr": "10.1.0.0/16", "vlan": 20}},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["fields"]["vlan"] == 20

    archived = await api_env.client.post(
        f"/api/v1/docs/records/{record_id}/archive", headers=headers
    )
    assert archived.status_code in (200, 204), archived.text
    async with AsyncSession(admin_engine) as session:
        after = await session.get(DocRecord, record_id)
    assert after is not None, "archiving must not delete the row"
    assert after.archived is True


async def test_an_article_can_link_to_an_ipam_subnet_and_address(
    api_env, admin_engine
) -> None:
    """§14.4: `ipam_subnet` and `ipam_address` are added in TWO places, and
    adding either alone is a half-change -- the `LinkCreate` pattern without
    `_LINK_TARGET_MODELS` gives a 404, the dict without the pattern gives a 422.

    Both target types are exercised, and a cross-org id is exercised too: link
    resolution runs inside the tenant session, so another org's subnet must be
    indistinguishable from a nonexistent one.
    """
    from tests.ipam_fixtures import seed_address, seed_subnet, seed_vrf

    org_id = await api_env.create_org("docs-ipam")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))
    vrf_id = await seed_vrf(admin_engine, org_id)
    subnet_id = await seed_subnet(
        admin_engine, org_id, vrf_id=vrf_id, prefix="10.70.0.0/24"
    )
    address_id = await seed_address(
        admin_engine, org_id, vrf_id=vrf_id, subnet_id=subnet_id,
        address="10.70.0.42", state="reserved",
    )
    article = await api_env.client.post(
        "/api/v1/docs/articles",
        json={"title": "Manila addressing", "body": "see the plan"},
        headers=headers,
    )
    assert article.status_code == 201, article.text
    article_id = article.json()["id"]

    for target_type, target_id in (
        ("ipam_subnet", subnet_id),
        ("ipam_address", address_id),
    ):
        linked = await api_env.client.post(
            f"/api/v1/docs/articles/{article_id}/links",
            json={"target_type": target_type, "target_id": str(target_id)},
            headers=headers,
        )
        assert linked.status_code in (200, 201), f"{target_type}: {linked.text}"

    other_org = await api_env.create_org("docs-ipam-b")
    other_vrf = await seed_vrf(admin_engine, other_org)
    foreign = await seed_subnet(
        admin_engine, other_org, vrf_id=other_vrf, prefix="10.99.0.0/24"
    )
    refused = await api_env.client.post(
        f"/api/v1/docs/articles/{article_id}/links",
        json={"target_type": "ipam_subnet", "target_id": str(foreign)},
        headers=headers,
    )
    assert refused.status_code == 404, refused.text


def test_the_link_target_pattern_and_the_model_map_agree() -> None:
    """The half-change guard, structurally: the regex in `api.py` and the dict in
    `service.py` are two places naming one set, and §14.4 says adding to either
    alone is a defect. Asserted as set equality so a future target type added to
    one is a named failure rather than a 404 somebody debugs at runtime."""
    import re

    from app.modules.documentation.api import LinkCreate
    from app.modules.documentation.service import _LINK_TARGET_MODELS

    pattern = LinkCreate.model_fields["target_type"].metadata[0].pattern
    named = set(re.fullmatch(r"\^\((.*)\)\$", pattern).group(1).split("|"))
    assert named == set(_LINK_TARGET_MODELS)
