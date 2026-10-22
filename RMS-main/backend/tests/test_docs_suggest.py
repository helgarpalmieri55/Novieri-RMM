"""`GET /docs/suggest`: articles that may answer a ticket, from its subject.

Unlike `/docs/search` (every word must match), a suggestion matches ANY
meaningful word and ranks by how well, so a sentence-long ticket subject
still finds the runbook that mentions two of its words.
"""

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

SUGGEST = "/api/v1/docs/suggest"


async def _article(api_env, headers, title: str, body: str = "") -> str:
    resp = await api_env.client.post(
        "/api/v1/docs/articles",
        json={"title": title, "body": body, "category": "Runbooks"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    article_id: str = resp.json()["id"]
    return article_id


async def _tech(api_env, slug: str) -> tuple[UUID, dict[str, str]]:
    org_id = await api_env.create_org(slug)
    return org_id, await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))


async def test_a_subject_finds_articles_sharing_any_of_its_words(api_env) -> None:
    _org, headers = await _tech(api_env, "docs-sug")
    dock = await _article(
        api_env, headers, "Docking station troubleshooting", "Reseat the dock; update firmware."
    )
    monitor = await _article(api_env, headers, "Second monitor setup", "Display settings.")
    await _article(api_env, headers, "VPN client install", "Download the installer.")

    resp = await api_env.client.get(
        SUGGEST,
        params={"q": "Docking station not detecting second screen"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    ids = [hit["id"] for hit in resp.json()]
    # Two words match the docking article, one the monitor article.
    assert ids == [dock, monitor]
    assert set(resp.json()[0]) == {"id", "title", "category"}


async def test_punctuation_and_operators_in_the_subject_are_harmless(api_env) -> None:
    _org, headers = await _tech(api_env, "docs-sug-p")
    printer = await _article(api_env, headers, "Printer jams", "Open tray 2.")
    resp = await api_env.client.get(
        SUGGEST, params={"q": "printer!!! & | ( ) :* 'jam' <-> \\"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert [hit["id"] for hit in resp.json()] == [printer]

    nothing = await api_env.client.get(SUGGEST, params={"q": "&& ||"}, headers=headers)
    assert nothing.status_code == 200
    assert nothing.json() == []


async def test_archived_articles_are_not_suggested_and_limit_is_respected(api_env) -> None:
    _org, headers = await _tech(api_env, "docs-sug-a")
    archived = await _article(api_env, headers, "Outlook crash", "Old fix")
    await api_env.client.post(f"/api/v1/docs/articles/{archived}/archive", headers=headers)
    for n in range(4):
        await _article(api_env, headers, f"Outlook profile {n}", "Rebuild the profile.")

    resp = await api_env.client.get(
        SUGGEST, params={"q": "Outlook crash", "limit": 3}, headers=headers
    )
    ids = [hit["id"] for hit in resp.json()]
    assert archived not in ids
    assert len(ids) == 3


async def test_suggestions_need_docs_read_and_stay_in_the_org(api_env) -> None:
    org_id, headers = await _tech(api_env, "docs-sug-r")
    _other_org, other_headers = await _tech(api_env, "docs-sug-o")
    await _article(api_env, other_headers, "Wifi password rotation")

    mine = await api_env.client.get(SUGGEST, params={"q": "wifi"}, headers=headers)
    assert mine.status_code == 200
    assert mine.json() == []

    requester = await api_env.headers(org_id, await api_env.create_user(org_id, "requester"))
    denied = await api_env.client.get(SUGGEST, params={"q": "wifi"}, headers=requester)
    assert denied.status_code == 403


async def test_the_suggester_is_org_scoped_where_rls_is_not_the_thing_rejecting(
    admin_engine: AsyncEngine,
) -> None:
    from app.models import DocArticle, Organization
    from app.modules.documentation.service import suggest_articles

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        orgs = []
        for key in ("mine", "theirs"):
            org = Organization(name=key, slug=f"docs-sug-rls-{uuid4().hex[:8]}")
            session.add(org)
            await session.flush()
            orgs.append(org.id)
        session.add(DocArticle(org_id=orgs[1], title="Laptop battery", body="Calibrate"))
        await session.flush()
        assert await suggest_articles(session, orgs[0], "laptop battery") == []
        assert len(await suggest_articles(session, orgs[1], "laptop battery")) == 1
        await session.rollback()
