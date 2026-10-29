"""Documentation service: versioning, audit, and asset links (spec §5).

Every mutation runs in the tenant session, bumps current_version, snapshots
into doc_versions, and writes an audit event. No-op updates do none of that.
Link targets are resolved inside the tenant session, so RLS makes foreign-org
targets indistinguishable from nonexistent ones.

PER CLIENT, OR GLOBAL ON REQUEST (MSP clients plan 2026-10-26 Task 14). An
article, record or document names its client at creation (`client_for_new_rows`
in the caller's session: 404 for a client the request cannot see, 409 for an
archived one, 422 for Unassigned); without one it is global -- every client's
knowledge base, written only under the every-client scope
(`app.core.global_rows`). Its versions and links take its client by trigger
(migration 0231), and a client's row links only to that client's targets or to
global ones. Every event is filed under the row's client.
"""

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import N_, tr, tr_marked
from app.core.wire_words import say
from app.models import (
    Credential,
    Device,
    DeviceGroup,
    DocArticle,
    DocLink,
    DocRecord,
    DocVersion,
    IpamAddress,
    IpamSubnet,
)
from app.modules.documentation.schemas import validate_record_fields
from app.modules.documentation.words import LINK_TARGET_WORDS
from app.services.client_sites import ClientRefusesNewRow, named_client_for_new_rows


class LinkTargetNotFound(Exception):
    pass


class LinkTargetOfAnotherClient(Exception):
    """A client's article or document naming another client's target: a 422
    whose message is the sentence."""


class DocumentationClientError(Exception):
    """The client a new article, record or document names cannot take it:
    `status_code` 404 (not visible), 409 (archived) or 422 (Unassigned), and
    `detail` the sentence -- the routes answer with both as they are."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def client_for_documentation(
    session: AsyncSession, *, org_id: UUID, client_id: UUID | None
) -> UUID | None:
    """The client a new documentation row is filed under, or None (global).

    Loaded with `named_client_for_new_rows` in the caller's own session, so it is
    read FOR SHARE as the create's FIRST lock, before its audit event
    (`app.services.client_sites`). Global needs no check here: the row is
    then written under the every-client scope or refused by
    `app.core.global_rows` before any SQL is sent."""
    if client_id is None:
        return None
    try:
        client = await named_client_for_new_rows(
            session,
            org_id=org_id,
            client_id=client_id,
            unassigned=N_("documentation cannot be filed under Unassigned"),
        )
    except ClientRefusesNewRow as exc:
        raise DocumentationClientError(exc.status_code, exc.detail) from None
    return client.id


async def check_link_target(
    session: AsyncSession,
    *,
    org_id: UUID,
    owner_client: UUID | None,
    target_type: str,
    target_id: UUID,
) -> None:
    """`LinkTargetNotFound` for an unknown type or a target this session
    cannot see in the organisation; `LinkTargetOfAnotherClient` when the
    article or document is a client's (`owner_client`) and the target is
    another client's. A global target -- a credential with no group, a
    global record -- serves every client; a global article or document may
    name any target the caller can see, as it always could.

    Shared by article and document links: one resolver (`_LINK_TARGET_MODELS`)
    and one rule. Every target model carries `org_id` and `client_id`; the
    `org_id` predicate is the belt, RLS the braces."""
    model = _LINK_TARGET_MODELS.get(target_type)
    if model is None:
        raise LinkTargetNotFound(
            tr("unknown target type {target_type}", target_type=repr(target_type))
        )
    found = (
        await session.execute(
            select(model.client_id).where(model.id == target_id, model.org_id == org_id)
        )
    ).one_or_none()
    if found is None:
        raise LinkTargetNotFound(
            tr(
                "no {target_type} {target_id} in this organization",
                target_type=say(LINK_TARGET_WORDS, target_type),
                target_id=target_id,
            )
        )
    target_client = found[0]
    if owner_client is not None and target_client not in (None, owner_client):
        raise LinkTargetOfAnotherClient(
            tr(
                "{target_type} {target_id} belongs to another client",
                target_type=say(LINK_TARGET_WORDS, target_type),
                target_id=target_id,
            )
        )


_LINK_TARGET_MODELS: dict[str, Any] = {
    "device": Device,
    "device_group": DeviceGroup,
    "credential": Credential,  # metadata reference only — never the secret
    "record": DocRecord,
    # IPAM (docs/modules/ipam/spec.md §14.4): an article links to the REAL
    # entity instead of to a jsonb copy of a prefix. Adding either half alone is
    # a half-change — this dict without `api.py:LinkCreate`'s pattern gives a
    # 422, the pattern without this dict gives a `LinkTargetNotFound` 404.
    "ipam_subnet": IpamSubnet,
    "ipam_address": IpamAddress,
}

#: docs/modules/ipam/spec.md §0.14/§0.18: `network` records are SUPERSEDED by
#: `ipam_subnet`, and two places to write the same fact is how a plan of record
#: rots. `network` stays a legal stored `record_type` — migrations are
#: forward-only and existing rows must keep reading, updating and archiving — so
#: the refusal lives here, in `create_record`, and NOT in `RecordCreate`'s regex.
#: Unconditional: there is no per-module install state in this codebase (§0.18
#: records the search that established it), so "once IPAM is installed" would
#: name no condition a caller could evaluate.
_SUPERSEDED_RECORD_TYPES = {
    "network": (
        N_(
            "record_type 'network' is superseded by the IPAM address plan: create the "
            "subnet with POST /api/v1/ipam/subnets instead. Existing network records "
            "remain readable, editable and archivable, and "
            "POST /api/v1/ipam/import/doc-records converts them"
        )
    )
}


async def _audit(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    action: str,
    target_id: UUID,
    metadata: dict[str, Any] | None = None,
    *,
    client_id: UUID | None,
) -> None:
    """`client_id` is the article's or record's (None: global), and has NO
    default: every event decides."""
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action=action,
        target_type="doc",
        target_id=str(target_id),
        metadata=metadata,
        client_id=client_id,
    )


def _article_snapshot(article: DocArticle) -> dict[str, Any]:
    return {"title": article.title, "body": article.body, "category": article.category}


def _record_snapshot(record: DocRecord) -> dict[str, Any]:
    return {"name": record.name, "record_type": record.record_type, "fields": record.fields}


async def _snapshot(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID | None,
    version: int,
    snapshot: dict[str, Any],
    article_id: UUID | None = None,
    record_id: UUID | None = None,
) -> None:
    session.add(
        DocVersion(
            org_id=org_id,
            article_id=article_id,
            record_id=record_id,
            version=version,
            snapshot=snapshot,
            changed_by=actor_id,
            ts=datetime.now(UTC),
        )
    )


async def create_article(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID | None,
    title: str,
    body: str,
    category: str,
    client_id: UUID | None,
) -> DocArticle:
    """`client_id` is what the request named (None: global); checked here
    with `client_for_documentation`, the create's first lock."""
    client_id = await client_for_documentation(session, org_id=org_id, client_id=client_id)
    article = DocArticle(
        org_id=org_id,
        client_id=client_id,
        title=title,
        body=body,
        category=category,
        created_by=actor_id,
        updated_by=actor_id,
    )
    session.add(article)
    await session.flush()
    await _snapshot(
        session,
        org_id=org_id,
        actor_id=actor_id,
        version=1,
        snapshot=_article_snapshot(article),
        article_id=article.id,
    )
    await _audit(
        session, org_id, actor_id, "docs.article_created", article.id, client_id=client_id
    )
    await session.flush()
    return article


async def update_article(
    session: AsyncSession,
    article: DocArticle,
    *,
    actor_id: UUID | None,
    title: str,
    body: str,
    category: str,
) -> bool:
    if (article.title, article.body, article.category) == (title, body, category):
        return False
    article.title, article.body, article.category = title, body, category
    article.current_version += 1
    article.updated_by = actor_id
    session.add(article)
    await _snapshot(
        session,
        org_id=article.org_id,
        actor_id=actor_id,
        version=article.current_version,
        snapshot=_article_snapshot(article),
        article_id=article.id,
    )
    await _audit(
        session,
        article.org_id,
        actor_id,
        "docs.article_updated",
        article.id,
        client_id=article.client_id,
    )
    await session.flush()
    return True


async def archive_article(
    session: AsyncSession, article: DocArticle, *, actor_id: UUID | None
) -> None:
    article.archived = True
    article.updated_by = actor_id
    session.add(article)
    await _audit(
        session,
        article.org_id,
        actor_id,
        "docs.article_archived",
        article.id,
        client_id=article.client_id,
    )
    await session.flush()


async def set_portal_visibility(
    session: AsyncSession, article: DocArticle, *, actor_id: UUID | None, visible: bool
) -> bool:
    """Show or hide an article in the end-user portal.

    Not a content edit, so no version bump; audited because it decides what
    requesters can read.
    """
    if article.portal_visible == visible:
        return False
    article.portal_visible = visible
    article.updated_by = actor_id
    session.add(article)
    await _audit(
        session,
        article.org_id,
        actor_id,
        "docs.article_portal_visibility_changed",
        article.id,
        {"portal_visible": visible},
        client_id=article.client_id,
    )
    await session.flush()
    return True


async def create_record(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID | None,
    record_type: str,
    name: str,
    fields: dict[str, Any],
    client_id: UUID | None,
) -> DocRecord:
    """`client_id` as in `create_article`. The fields are validated first --
    a pure check that reads nothing -- so the client is still the first
    lock."""
    superseded = _SUPERSEDED_RECORD_TYPES.get(record_type)
    if superseded is not None:
        raise ValueError(tr_marked(superseded))
    validated = validate_record_fields(record_type, fields)
    client_id = await client_for_documentation(session, org_id=org_id, client_id=client_id)
    record = DocRecord(
        org_id=org_id,
        client_id=client_id,
        record_type=record_type,
        name=name,
        fields=validated,
        created_by=actor_id,
        updated_by=actor_id,
    )
    session.add(record)
    await session.flush()
    await _snapshot(
        session,
        org_id=org_id,
        actor_id=actor_id,
        version=1,
        snapshot=_record_snapshot(record),
        record_id=record.id,
    )
    await _audit(
        session, org_id, actor_id, "docs.record_created", record.id, client_id=client_id
    )
    await session.flush()
    return record


async def update_record(
    session: AsyncSession,
    record: DocRecord,
    *,
    actor_id: UUID | None,
    name: str,
    fields: dict[str, Any],
) -> bool:
    validated = validate_record_fields(record.record_type, fields)
    if (record.name, record.fields) == (name, validated):
        return False
    record.name, record.fields = name, validated
    record.current_version += 1
    record.updated_by = actor_id
    session.add(record)
    await _snapshot(
        session,
        org_id=record.org_id,
        actor_id=actor_id,
        version=record.current_version,
        snapshot=_record_snapshot(record),
        record_id=record.id,
    )
    await _audit(
        session,
        record.org_id,
        actor_id,
        "docs.record_updated",
        record.id,
        client_id=record.client_id,
    )
    await session.flush()
    return True


async def archive_record(
    session: AsyncSession, record: DocRecord, *, actor_id: UUID | None
) -> None:
    record.archived = True
    record.updated_by = actor_id
    session.add(record)
    await _audit(
        session,
        record.org_id,
        actor_id,
        "docs.record_archived",
        record.id,
        client_id=record.client_id,
    )
    await session.flush()


async def add_link(
    session: AsyncSession,
    article: DocArticle,
    *,
    actor_id: UUID | None,
    target_type: str,
    target_id: UUID,
) -> DocLink:
    await check_link_target(
        session,
        org_id=article.org_id,
        owner_client=article.client_id,
        target_type=target_type,
        target_id=target_id,
    )
    link = DocLink(
        org_id=article.org_id,
        article_id=article.id,
        target_type=target_type,
        target_id=target_id,
    )
    session.add(link)
    await _audit(
        session,
        article.org_id,
        actor_id,
        "docs.link_added",
        article.id,
        client_id=article.client_id,
    )
    await session.flush()
    return link


async def remove_link(
    session: AsyncSession, article: DocArticle, link_id: UUID, *, actor_id: UUID | None
) -> bool:
    link = (
        await session.execute(
            select(DocLink).where(DocLink.id == link_id, DocLink.article_id == article.id)
        )
    ).scalar_one_or_none()
    if link is None:
        return False
    await session.delete(link)
    await _audit(
        session,
        article.org_id,
        actor_id,
        "docs.link_removed",
        article.id,
        client_id=article.client_id,
    )
    await session.flush()
    return True


async def list_versions(
    session: AsyncSession,
    *,
    article_id: UUID | None = None,
    record_id: UUID | None = None,
) -> list[DocVersion]:
    query = select(DocVersion).order_by(DocVersion.version.desc())
    if article_id is not None:
        query = query.where(DocVersion.article_id == article_id)
    if record_id is not None:
        query = query.where(DocVersion.record_id == record_id)
    return list((await session.execute(query)).scalars().all())


# Words shorter than this are almost always noise in a ticket subject.
_MIN_SUGGEST_WORD = 3
_MAX_SUGGEST_WORDS = 12


def _suggest_terms(text: str) -> list[str]:
    """Alphanumeric words only, so nothing a person typed can be read as
    tsquery syntax."""
    seen: list[str] = []
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if len(word) >= _MIN_SUGGEST_WORD and word not in seen:
            seen.append(word)
    return seen[:_MAX_SUGGEST_WORDS]


def suggest_match(text: str) -> tuple[ColumnElement[bool], ColumnElement[float]] | None:
    """The article filter and rank for suggestions from `text`, or None when
    it has no meaningful word.

    Matches ANY word, since `/docs/search` requires every word and a
    sentence-long ticket subject almost never satisfies that. The document
    expression is the one `ix_doc_articles_fts` indexes, so this stays an
    index scan. Shared with the portal's help suggestions.
    """
    terms = _suggest_terms(text)
    if not terms:
        return None
    document = func.to_tsvector("english", DocArticle.title + " " + DocArticle.body)
    query = func.to_tsquery("english", " | ".join(terms))
    return document.op("@@")(query), func.ts_rank(document, query)


async def suggest_articles(
    session: AsyncSession, org_id: UUID, text: str, *, limit: int = 5
) -> list[DocArticle]:
    """Live articles matching any word of `text`, best match first."""
    match = suggest_match(text)
    if match is None:
        return []
    matches, rank = match
    stmt = (
        select(DocArticle)
        .where(DocArticle.org_id == org_id, DocArticle.archived.is_(False), matches)
        .order_by(rank.desc(), DocArticle.title, DocArticle.id)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
