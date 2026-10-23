"""Every organization is born with its two system clients and the internal
client's "Principal" site (spec §1.1, §1.2), and the internal pair carries the
organization's own id (plan Global Constraints: id convention)."""

from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import org_session
from app.models import Client, Organization, Site


async def _new_org(admin_engine) -> Organization:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
        s.add(org)
        await s.commit()
        return org


async def test_an_org_is_born_with_internal_and_unassigned_clients(admin_engine) -> None:
    org = await _new_org(admin_engine)
    async with AsyncSession(admin_engine) as s:
        rows = (
            await s.execute(
                select(Client.kind, Client.id, Client.name).where(Client.org_id == org.id)
            )
        ).all()
    kinds = {k: (i, n) for k, i, n in rows}
    assert set(kinds) == {"internal", "unassigned"}
    assert kinds["internal"][0] == org.id
    assert kinds["internal"][1] == "Acme"
    assert kinds["unassigned"][0] != org.id


async def test_the_internal_client_has_a_principal_site_with_the_org_id(admin_engine) -> None:
    org = await _new_org(admin_engine)
    async with AsyncSession(admin_engine) as s:
        sites = (await s.execute(select(Site).where(Site.org_id == org.id))).scalars().all()
    assert [(x.id, x.client_id, x.name) for x in sites] == [(org.id, org.id, "Principal")]


async def test_only_one_internal_and_one_unassigned_client_per_org(admin_engine) -> None:
    org = await _new_org(admin_engine)
    async with AsyncSession(admin_engine) as s:
        for kind in ("internal", "unassigned"):
            try:
                async with s.begin():
                    await s.execute(
                        text(
                            "INSERT INTO clients (id, org_id, name, kind) "
                            "VALUES (gen_random_uuid(), :o, :n, :k)"
                        ),
                        {"o": org.id, "n": f"dup-{kind}", "k": kind},
                    )
            except Exception as exc:  # noqa: BLE001 -- the assertion is that it raised
                assert "uq_clients_one_" in str(exc)
            else:
                raise AssertionError(f"a second {kind} client was accepted")


async def test_a_contact_must_name_a_client_and_staff_must_not(admin_engine) -> None:
    org = await _new_org(admin_engine)
    async with AsyncSession(admin_engine) as s:
        for kind, client in (("contact", None), ("staff", org.id)):
            try:
                async with s.begin():
                    await s.execute(
                        text(
                            "INSERT INTO users (id, org_id, external_subject, email, "
                            "display_name, status, kind, client_id) "
                            "VALUES (gen_random_uuid(), :o, :sub, 'x@test.local', "
                            "'X', 'active', :k, :c)"
                        ),
                        {"o": org.id, "sub": uuid4().hex, "k": kind, "c": client},
                    )
            except Exception as exc:  # noqa: BLE001
                assert "ck_users_kind_client" in str(exc)
            else:
                raise AssertionError(f"{kind} with client_id={client} was accepted")


async def test_a_site_cannot_belong_to_the_unassigned_client_or_to_another_orgs_client(
    admin_engine,
) -> None:
    org = await _new_org(admin_engine)
    other = await _new_org(admin_engine)
    async with AsyncSession(admin_engine) as s:
        unassigned = (
            await s.execute(
                select(Client.id).where(Client.org_id == org.id, Client.kind == "unassigned")
            )
        ).scalar_one()
    cases = (
        ("the unassigned client cannot have sites", org.id, unassigned),
        ("names a client of another organization", org.id, other.id),
    )
    async with AsyncSession(admin_engine) as s:
        for message, site_org, client in cases:
            try:
                async with s.begin():
                    await s.execute(
                        text(
                            "INSERT INTO sites (id, org_id, client_id, name) "
                            "VALUES (gen_random_uuid(), :o, :c, 'x')"
                        ),
                        {"o": site_org, "c": client},
                    )
            except Exception as exc:  # noqa: BLE001 -- the assertion is that it raised
                assert message in str(exc)
            else:
                raise AssertionError(f"a site was accepted: {message}")


async def test_clients_and_sites_are_tenant_scoped(admin_engine, app_engine) -> None:
    org_a = await _new_org(admin_engine)
    org_b = await _new_org(admin_engine)

    async with org_session(org_a.id, engine=app_engine) as s:
        client_rows = (await s.execute(text("SELECT org_id FROM clients"))).all()
        site_rows = (await s.execute(text("SELECT org_id FROM sites"))).all()
    client_orgs = {r.org_id for r in client_rows}
    site_orgs = {r.org_id for r in site_rows}
    assert client_orgs == {org_a.id}
    assert site_orgs == {org_a.id}

    with pytest.raises(DBAPIError) as refused:
        async with org_session(org_a.id, engine=app_engine) as s:
            await s.execute(
                text(
                    "INSERT INTO clients (id, org_id, name) "
                    "VALUES (gen_random_uuid(), :o, 'smuggled')"
                ),
                {"o": org_b.id},
            )
    # The refusal is the row-level security policy, not some other error.
    assert "row-level security" in str(refused.value)
