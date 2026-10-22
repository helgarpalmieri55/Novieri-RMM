"""The document service's OWN `org_id` predicate, observed where Postgres is
not also filtering (F1, `spec-attachments.md` §12.1).

**WHY THIS FILE EXISTS SEPARATELY, AND WHY IT USES `bypassrls_session`.**
Everything a request touches runs as `app_user`, which is NOBYPASSRLS, and
migration 0075 puts a forced `tenant_isolation` policy on all three document
tables. Inside `org_session` a SELECT carrying no tenant predicate at all
therefore returns exactly the rows a correct one would -- so deleting a
`WHERE org_id` from `documents.py` turns no route test in
`test_doc_documents_api.py` red, not one. The audit
`docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md` counted 219 tests
sitting in that position, several of them named as though they pinned the
filter.

On the BYPASSRLS admin session Postgres stops filtering and the application's
predicate is the only thing scoping anything. A test written here goes RED when
the predicate is deleted; a test written on `org_session` does not. That is the
entire distinction, and it is why these assertions are not folded in with the
route tests -- one name should not cover two things that fail so differently.

**COUNTS AND TOTALS ARE ASSERTED AS WELL AS PAGE CONTENTS.** An org-wide
`count()` is where a leak hides behind a correctly-filtered page.
"""

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Device,
    DocDocument,
    DocDocumentLink,
    DocDocumentVersion,
    Organization,
)
from app.modules.documentation import documents as svc


class _Seeded:
    def __init__(self, org_id: UUID, document_id: UUID, device_id: UUID) -> None:
        self.org_id = org_id
        self.document_id = document_id
        self.device_id = device_id


async def _seed(session: AsyncSession, slug: str, *, title: str, filename: str) -> _Seeded:
    """One org with one document, one version and one device link, written
    through the BYPASSRLS session so both orgs' rows genuinely coexist and the
    database is not doing the filtering the assertions are about."""
    org = Organization(name=slug, slug=f"{slug}-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()

    device = Device(org_id=org.id, hostname=f"{slug}-dev", os_family="linux")
    session.add(device)
    await session.flush()

    document = DocDocument(
        org_id=org.id,
        title=title,
        description=f"{slug} description",
        category="Vendors",
        current_version=1,
    )
    session.add(document)
    await session.flush()

    session.add(
        DocDocumentVersion(
            org_id=org.id,
            document_id=document.id,
            version=1,
            object_key=f"s3://bucket/{org.id}/docs/documents/{document.id}/{uuid4()}",
            filename=filename,
            byte_size=1024,
            content_type="application/pdf",
            viewer="pdf",
            sha256=hashlib.sha256(slug.encode()).hexdigest(),
            created_at=datetime.now(UTC),
        )
    )
    session.add(
        DocDocumentLink(
            org_id=org.id,
            document_id=document.id,
            target_type="device",
            target_id=device.id,
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()
    return _Seeded(org.id, document.id, device.id)


@pytest.fixture()
async def two_orgs(bypassrls_session: AsyncSession):
    a = await _seed(
        bypassrls_session, "tenancy-a", title="Alpha vendor contract", filename="alpha.pdf"
    )
    b = await _seed(
        bypassrls_session, "tenancy-b", title="Bravo vendor contract", filename="bravo.pdf"
    )
    return a, b


async def test_get_document_refuses_the_other_orgs_id(
    bypassrls_session: AsyncSession, two_orgs
) -> None:
    a, b = two_orgs
    assert (await svc.get_document(bypassrls_session, a.org_id, a.document_id)).id == a.document_id
    with pytest.raises(svc.DocumentNotFound):
        await svc.get_document(bypassrls_session, a.org_id, b.document_id)


async def test_list_documents_pages_and_totals_are_both_scoped(
    bypassrls_session: AsyncSession, two_orgs
) -> None:
    """Both halves. A `total` computed without the predicate is a disclosure
    even when every row on the page is correct -- it tells org A how many
    documents org B has."""
    a, b = two_orgs
    rows, total = await svc.list_documents(bypassrls_session, a.org_id)
    assert [d.id for d, _ in rows] == [a.document_id]
    assert total == 1
    assert b.document_id not in {d.id for d, _ in rows}


async def test_the_link_filter_does_not_reach_across_orgs(
    bypassrls_session: AsyncSession, two_orgs
) -> None:
    """`?target_type=device&target_id=` is the device-detail tab's query, and
    the subselect it builds is a second place the predicate has to appear."""
    a, b = two_orgs
    rows, total = await svc.list_documents(
        bypassrls_session, a.org_id, target_type="device", target_id=b.device_id
    )
    assert rows == []
    assert total == 0


async def test_versions_and_the_current_version_are_scoped(
    bypassrls_session: AsyncSession, two_orgs
) -> None:
    """The versions row is the one that holds the object key, so it is the row
    that stands between one tenant's token and another tenant's bytes."""
    a, b = two_orgs
    assert await svc.list_document_versions(bypassrls_session, a.org_id, b.document_id) == []
    with pytest.raises(svc.DocumentNotFound):
        await svc.get_version(bypassrls_session, a.org_id, b.document_id, 1)

    foreign = await svc.get_document(bypassrls_session, b.org_id, b.document_id)
    assert await svc.current_version_row(bypassrls_session, a.org_id, foreign) is None


async def test_links_are_scoped(bypassrls_session: AsyncSession, two_orgs) -> None:
    a, b = two_orgs
    assert await svc.list_document_links(bypassrls_session, a.org_id, b.document_id) == []


async def test_org_usage_counts_only_this_orgs_bytes(
    bypassrls_session: AsyncSession, two_orgs
) -> None:
    """The quota's `SUM`. Without the predicate org A would be charged for org
    B's storage -- and would be refused uploads because of it, which is a
    cross-tenant denial of service rather than a disclosure."""
    a, _ = two_orgs
    assert await svc.org_usage_bytes(bypassrls_session, a.org_id) == 1024


async def test_search_does_not_reach_across_orgs(
    bypassrls_session: AsyncSession, two_orgs
) -> None:
    """Both mechanisms: the FTS match on title/description/category, and the
    `ilike` on the current version's filename."""
    a, b = two_orgs
    by_title = await svc.search_documents(bypassrls_session, a.org_id, "Bravo")
    assert by_title == []
    by_filename = await svc.search_documents(bypassrls_session, a.org_id, "bravo.pdf")
    assert by_filename == []

    own = await svc.search_documents(bypassrls_session, a.org_id, "Alpha")
    assert [d.id for d, _ in own] == [a.document_id]


async def test_a_link_target_in_another_org_does_not_resolve(
    bypassrls_session: AsyncSession, two_orgs
) -> None:
    """§7: the target is resolved with the document's own `org_id`, so a
    foreign-org target is indistinguishable from a nonexistent one. Without the
    predicate this would happily link org A's document to org B's device -- and
    the console would then render org B's hostname on org A's page."""
    a, b = two_orgs
    document = await svc.get_document(bypassrls_session, a.org_id, a.document_id)
    with pytest.raises(svc.LinkTargetNotFound):
        await svc.add_document_link(
            bypassrls_session,
            document,
            actor_id=None,
            target_type="device",
            target_id=b.device_id,
        )


async def test_removing_a_link_cannot_reach_another_orgs_row(
    bypassrls_session: AsyncSession, two_orgs
) -> None:
    a, b = two_orgs
    document = await svc.get_document(bypassrls_session, a.org_id, a.document_id)
    foreign_link = (await svc.list_document_links(bypassrls_session, b.org_id, b.document_id))[0]
    assert await svc.remove_document_link(
        bypassrls_session, document, foreign_link.id, actor_id=None
    ) is False
    # And it is still there.
    assert len(await svc.list_document_links(bypassrls_session, b.org_id, b.document_id)) == 1
