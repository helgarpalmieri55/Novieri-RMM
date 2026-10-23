"""Help articles in the end-user portal.

An article reaches a requester only when staff flag it "visible in the
portal" and it is not archived. The database enforces that (migration 0160's
restrictive `portal_isolation` policy and column-level grant), so the tests
below check the policy directly as well as through the routes.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

SUGGEST = "/api/v1/portal/help/suggest"
ARTICLE = "/api/v1/portal/help/articles"


async def _article(api_env, headers, title: str, body: str = "", *, visible: bool) -> str:
    resp = await api_env.client.post(
        "/api/v1/docs/articles",
        json={"title": title, "body": body, "category": "Help"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    article_id: str = resp.json()["id"]
    if visible:
        flagged = await api_env.client.put(
            f"/api/v1/docs/articles/{article_id}/portal-visibility",
            json={"portal_visible": True},
            headers=headers,
        )
        assert flagged.status_code == 200, flagged.text
    return article_id


async def _org(api_env, slug: str) -> tuple[UUID, dict[str, str], dict[str, str]]:
    org_id = await api_env.create_org(slug)
    tech = await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))
    requester = await api_env.headers(org_id, await api_env.create_user(org_id, "requester"))
    return org_id, tech, requester


# --------------------------------------------------------------------------- #
# The flag: default off, set by whoever can edit articles, audited.
# --------------------------------------------------------------------------- #


async def test_articles_are_internal_by_default(api_env) -> None:
    _org_id, tech, _req = await _org(api_env, "help-default")
    article_id = await _article(api_env, tech, "Reset your password", visible=False)
    got = await api_env.client.get(f"/api/v1/docs/articles/{article_id}", headers=tech)
    assert got.json()["portal_visible"] is False
    listed = await api_env.client.get("/api/v1/docs/articles", headers=tech)
    assert [a["portal_visible"] for a in listed.json()["items"]] == [False]


async def test_the_flag_needs_docs_manage_and_is_audited(api_env) -> None:
    from app.models import AuditEvent

    org_id, tech, requester = await _org(api_env, "help-flag")
    article_id = await _article(api_env, tech, "Printer setup", visible=False)
    url = f"/api/v1/docs/articles/{article_id}/portal-visibility"

    reader = await api_env.headers(org_id, await api_env.create_user(org_id, "read-only"))
    for headers in (reader, requester):
        denied = await api_env.client.put(url, json={"portal_visible": True}, headers=headers)
        assert denied.status_code == 403

    on = await api_env.client.put(url, json={"portal_visible": True}, headers=tech)
    assert on.status_code == 200, on.text
    assert on.json() == {"portal_visible": True}
    got = await api_env.client.get(f"/api/v1/docs/articles/{article_id}", headers=tech)
    assert got.json()["portal_visible"] is True
    # Visibility is not an edit of the content, so no new version.
    assert got.json()["current_version"] == 1

    # Setting the same value again changes nothing and writes nothing.
    again = await api_env.client.put(url, json={"portal_visible": True}, headers=tech)
    assert again.status_code == 200

    off = await api_env.client.put(url, json={"portal_visible": False}, headers=tech)
    assert off.json() == {"portal_visible": False}

    async with AsyncSession(api_env.admin_engine) as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent)
                    .where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "docs.article_portal_visibility_changed",
                    )
                    .order_by(AuditEvent.ts)
                )
            )
            .scalars()
            .all()
        )
    assert [e.target_id for e in events] == [article_id, article_id]
    assert [e.meta["portal_visible"] for e in events] == [True, False]


async def test_the_flag_on_another_orgs_article_is_404(api_env) -> None:
    _org_a, tech_a, _ = await _org(api_env, "help-flag-a")
    _org_b, tech_b, _ = await _org(api_env, "help-flag-b")
    theirs = await _article(api_env, tech_b, "Theirs", visible=False)
    got = await api_env.client.put(
        f"/api/v1/docs/articles/{theirs}/portal-visibility",
        json={"portal_visible": True},
        headers=tech_a,
    )
    assert got.status_code == 404


# --------------------------------------------------------------------------- #
# Portal suggestions.
# --------------------------------------------------------------------------- #


async def test_suggestions_offer_only_published_portal_articles(api_env) -> None:
    _org_id, tech, requester = await _org(api_env, "help-sug")
    visible = await _article(
        api_env, tech, "Connect to the office wifi", "Pick the Staff network.", visible=True
    )
    internal = await _article(
        api_env, tech, "Wifi controller admin", "Controller password is in the vault.",
        visible=False,
    )
    archived = await _article(api_env, tech, "Old wifi guide", visible=True)
    await api_env.client.post(f"/api/v1/docs/articles/{archived}/archive", headers=tech)

    resp = await api_env.client.get(
        SUGGEST, params={"q": "wifi keeps dropping"}, headers=requester
    )
    assert resp.status_code == 200, resp.text
    assert [hit["id"] for hit in resp.json()] == [visible]
    assert set(resp.json()[0]) == {"id", "title"}
    assert internal not in resp.text and archived not in resp.text


async def test_suggestions_use_the_same_safe_word_matching(api_env) -> None:
    _org_id, tech, requester = await _org(api_env, "help-sug-safe")
    printer = await _article(api_env, tech, "Printer jams", "Open tray 2.", visible=True)
    resp = await api_env.client.get(
        SUGGEST, params={"q": "printer!!! & | ( ) :* 'jam' <-> \\"}, headers=requester
    )
    assert resp.status_code == 200, resp.text
    assert [hit["id"] for hit in resp.json()] == [printer]
    nothing = await api_env.client.get(SUGGEST, params={"q": "&& ||"}, headers=requester)
    assert nothing.status_code == 200
    assert nothing.json() == []


async def test_suggestions_stay_in_the_requesters_org(api_env) -> None:
    _org_a, _tech_a, requester_a = await _org(api_env, "help-sug-a")
    _org_b, tech_b, _ = await _org(api_env, "help-sug-b")
    await _article(api_env, tech_b, "VPN setup", visible=True)
    resp = await api_env.client.get(SUGGEST, params={"q": "vpn"}, headers=requester_a)
    assert resp.status_code == 200
    assert resp.json() == []


async def test_a_technician_without_portal_access_cannot_use_portal_help(api_env) -> None:
    _org_id, tech, _ = await _org(api_env, "help-sug-tech")
    article_id = await _article(api_env, tech, "Mail setup", visible=True)
    assert (
        await api_env.client.get(SUGGEST, params={"q": "mail"}, headers=tech)
    ).status_code == 403
    assert (await api_env.client.get(f"{ARTICLE}/{article_id}", headers=tech)).status_code == 403


# --------------------------------------------------------------------------- #
# Reading one article.
# --------------------------------------------------------------------------- #


async def test_a_requester_reads_a_portal_article_as_title_and_body_only(api_env) -> None:
    from app.models import Device

    org_id, tech, requester = await _org(api_env, "help-read")
    article_id = await _article(
        api_env, tech, "Map a network drive", "Open File Explorer.", visible=True
    )
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            device = Device(org_id=org_id, hostname="fs-01", os_family="linux")
            session.add(device)
            await session.flush()
            device_id = device.id
    linked = await api_env.client.post(
        f"/api/v1/docs/articles/{article_id}/links",
        json={"target_type": "device", "target_id": str(device_id)},
        headers=tech,
    )
    assert linked.status_code == 201, linked.text

    got = await api_env.client.get(f"{ARTICLE}/{article_id}", headers=requester)
    assert got.status_code == 200, got.text
    assert got.json() == {
        "id": article_id,
        "title": "Map a network drive",
        "body": "Open File Explorer.",
    }
    assert str(device_id) not in got.text


async def test_a_requester_cannot_reach_an_internal_article_even_by_its_id(api_env) -> None:
    """The guessed-id case: every non-visible article is the same 404 as an
    id that does not exist, so the portal confirms nothing."""
    _org_id, tech, requester = await _org(api_env, "help-guess")
    _other, tech_other, _ = await _org(api_env, "help-guess-o")
    internal = await _article(api_env, tech, "Domain admin runbook", "secret steps", visible=False)
    archived = await _article(api_env, tech, "Retired guide", visible=True)
    await api_env.client.post(f"/api/v1/docs/articles/{archived}/archive", headers=tech)
    other_org = await _article(api_env, tech_other, "Other org guide", visible=True)

    for article_id in (internal, archived, other_org, str(uuid4())):
        got = await api_env.client.get(f"{ARTICLE}/{article_id}", headers=requester)
        assert got.status_code == 404, (article_id, got.text)
        assert "secret" not in got.text

    # Flagging it later is what makes it readable -- and unflagging hides it again.
    url = f"/api/v1/docs/articles/{internal}/portal-visibility"
    await api_env.client.put(url, json={"portal_visible": True}, headers=tech)
    assert (await api_env.client.get(f"{ARTICLE}/{internal}", headers=requester)).status_code == 200
    await api_env.client.put(url, json={"portal_visible": False}, headers=tech)
    assert (await api_env.client.get(f"{ARTICLE}/{internal}", headers=requester)).status_code == 404


async def test_a_requester_still_cannot_reach_the_staff_docs_routes(api_env) -> None:
    _org_id, tech, requester = await _org(api_env, "help-staff")
    article_id = await _article(api_env, tech, "Visible guide", visible=True)
    for path in (
        f"/api/v1/docs/articles/{article_id}",
        f"/api/v1/docs/articles/{article_id}/versions",
        "/api/v1/docs/articles",
    ):
        assert (await api_env.client.get(path, headers=requester)).status_code == 403, path


# --------------------------------------------------------------------------- #
# The database is the guard, not the query.
# --------------------------------------------------------------------------- #


@pytest.fixture
async def portal_engine(api_env) -> AsyncEngine:
    engine = create_async_engine(api_env.app.state.settings.database_portal_url)
    yield engine
    await engine.dispose()


async def test_the_portal_role_sees_only_the_published_portal_slice(
    api_env, portal_engine: AsyncEngine
) -> None:
    """An unfiltered SELECT as `portal_user` returns the visible article and
    nothing else: the restrictive policy, not the portal's WHERE clause,
    keeps internal and archived articles out."""
    from app.core.tenancy import portal_session
    from app.models import DocArticle

    org_id, tech, _ = await _org(api_env, "help-rls")
    visible = await _article(api_env, tech, "Visible", visible=True)
    await _article(api_env, tech, "Internal", visible=False)
    archived = await _article(api_env, tech, "Archived", visible=True)
    await api_env.client.post(f"/api/v1/docs/articles/{archived}/archive", headers=tech)

    async with portal_session(org_id, uuid4(), engine=portal_engine) as session:
        ids = (await session.execute(select(DocArticle.id))).scalars().all()
    assert [str(i) for i in ids] == [visible]


@pytest.mark.parametrize("column", ["created_by", "updated_by", "category", "current_version"])
async def test_the_portal_role_cannot_read_other_article_columns(
    api_env, portal_engine: AsyncEngine, column: str
) -> None:
    from app.core.tenancy import portal_session

    org_id, tech, _ = await _org(api_env, f"help-cols-{column[:4]}")
    await _article(api_env, tech, "Visible", visible=True)
    with pytest.raises(DBAPIError, match="permission denied"):
        async with portal_session(org_id, uuid4(), engine=portal_engine) as session:
            await session.execute(text(f"SELECT {column} FROM doc_articles"))


async def test_the_portal_policy_is_restrictive_and_portal_only(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT permissive, roles, qual FROM pg_policies "
                    "WHERE tablename = 'doc_articles' AND policyname = 'portal_isolation'"
                )
            )
        ).one()
        assert row.permissive == "RESTRICTIVE"
        assert row.roles == ["portal_user"]
        assert "portal_visible" in row.qual and "archived" in row.qual
        for table in ("doc_links", "doc_versions", "doc_records"):
            got = await conn.execute(
                text("SELECT has_table_privilege('portal_user', :t, 'SELECT')"), {"t": table}
            )
            assert got.scalar() is False, table
        for priv in ("INSERT", "UPDATE", "DELETE"):
            got = await conn.execute(
                text("SELECT has_table_privilege('portal_user', 'doc_articles', :p)"),
                {"p": priv},
            )
            assert got.scalar() is False, priv
