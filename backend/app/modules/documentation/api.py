"""Documentation API (spec §6). Reads need docs.read; mutations docs.manage."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_permission
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.models import DocArticle, DocLink, DocRecord
from app.modules.documentation import documents as documents_svc
from app.modules.documentation import service as svc
from app.modules.documentation.schemas import RECORD_TYPES

router = APIRouter(prefix="/docs", tags=["docs"])


#: The create bodies' client (MSP clients plan 2026-10-26 Task 14).
CLIENT_FIELD_DESCRIPTION = (
    "The client this belongs to. Omitted: it is every client's (global), which "
    "needs access to every client."
)


class ArticleFields(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = ""
    category: str = Field(default="General", max_length=100)


class ArticleCreate(ArticleFields):
    """An article's client is set when it is created and never changes: the
    edit body (`ArticleFields`) has no client."""

    client_id: UUID | None = Field(default=None, description=CLIENT_FIELD_DESCRIPTION)


class LinkOut(BaseModel):
    id: UUID
    target_type: str
    target_id: UUID


class ArticleListItem(BaseModel):
    id: UUID
    #: None: global, every client's.
    client_id: UUID | None
    title: str
    category: str
    archived: bool
    portal_visible: bool
    updated_at: datetime
    link_count: int


class ArticleListPage(BaseModel):
    items: list[ArticleListItem]
    total: int


class ArticleOut(BaseModel):
    id: UUID
    #: None: global, every client's.
    client_id: UUID | None
    title: str
    body: str
    category: str
    archived: bool
    portal_visible: bool
    current_version: int
    updated_at: datetime
    links: list[LinkOut]


class PortalVisibility(BaseModel):
    portal_visible: bool


class LinkCreate(BaseModel):
    # Kept in lockstep with `service._LINK_TARGET_MODELS` -- adding either alone
    # is a half-change (docs/modules/ipam/spec.md §14.4): the pattern without the
    # dict gives a 404, the dict without the pattern gives a 422.
    target_type: str = Field(
        pattern="^(device|device_group|credential|record|ipam_subnet|ipam_address)$"
    )
    target_id: UUID


class RecordCreate(BaseModel):
    record_type: str = Field(pattern=f"^({'|'.join(RECORD_TYPES)})$")
    name: str = Field(min_length=1, max_length=300)
    fields: dict[str, Any] = Field(default_factory=dict)
    client_id: UUID | None = Field(default=None, description=CLIENT_FIELD_DESCRIPTION)


class RecordUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    fields: dict[str, Any] = Field(default_factory=dict)


class RecordOut(BaseModel):
    id: UUID
    #: None: global, every client's.
    client_id: UUID | None
    record_type: str
    name: str
    fields: dict[str, Any]
    archived: bool
    current_version: int


class RecordListPage(BaseModel):
    items: list[RecordOut]
    total: int


class VersionOut(BaseModel):
    version: int
    snapshot: dict[str, Any]
    changed_by: UUID | None
    ts: datetime


class SearchHit(BaseModel):
    # article | record | document. `document` is spec-attachments.md §8: the
    # SAME route, the same permission and the same response shape, so the
    # console's existing `groupHits` helper needs one more group and no new
    # query. What a document search covers is title, description, category and
    # the CURRENT version's filename -- not file contents (§0.8), which the
    # console states next to the search box (§11.6) rather than leaving people
    # to discover that the library looks empty when it is not.
    kind: str
    id: UUID
    title: str
    category: str
    rank: float


async def _get_article(session: AsyncSession, article_id: UUID) -> DocArticle:
    article: DocArticle | None = (
        await session.execute(select(DocArticle).where(DocArticle.id == article_id))
    ).scalar_one_or_none()
    if article is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such article"))
    return article


def _record_out(record: DocRecord) -> RecordOut:
    return RecordOut(
        id=record.id,
        client_id=record.client_id,
        record_type=record.record_type,
        name=record.name,
        fields=record.fields,
        archived=record.archived,
        current_version=record.current_version,
    )


@router.post("/articles", status_code=status.HTTP_201_CREATED, response_model=ArticleOut)
async def create_article(
    body: ArticleCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.manage")),
) -> ArticleOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            article = await svc.create_article(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                title=body.title,
                body=body.body,
                category=body.category,
                client_id=body.client_id,
            )
        except svc.DocumentationClientError as exc:
            raise HTTPException(exc.status_code, exc.detail) from None
        await session.refresh(article)
        return ArticleOut(
            id=article.id,
            client_id=article.client_id,
            title=article.title,
            body=article.body,
            category=article.category,
            archived=article.archived,
            portal_visible=article.portal_visible,
            current_version=article.current_version,
            updated_at=article.updated_at,
            links=[],
        )


@router.get("/articles", response_model=ArticleListPage)
async def list_articles(
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.read")),
    category: str | None = Query(default=None),
    target_type: str | None = Query(default=None),
    target_id: UUID | None = Query(default=None),
    include_archived: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ArticleListPage:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        query: Select[Any] = select(
            DocArticle, func.count(DocLink.id).label("link_count")
        ).join(DocLink, DocLink.article_id == DocArticle.id, isouter=True)
        if not include_archived:
            query = query.where(DocArticle.archived.is_(False))
        if category:
            query = query.where(DocArticle.category == category)
        if target_type and target_id:
            query = query.where(
                DocArticle.id.in_(
                    select(DocLink.article_id).where(
                        DocLink.target_type == target_type, DocLink.target_id == target_id
                    )
                )
            )
        query = query.group_by(DocArticle.id)
        total = (
            await session.execute(
                select(func.count()).select_from(query.subquery())
            )
        ).scalar_one()
        rows = (
            await session.execute(
                query.order_by(DocArticle.category, DocArticle.title).limit(limit).offset(offset)
            )
        ).all()
        return ArticleListPage(
            items=[
                ArticleListItem(
                    id=a.id,
                    client_id=a.client_id,
                    title=a.title,
                    category=a.category,
                    archived=a.archived,
                    portal_visible=a.portal_visible,
                    updated_at=a.updated_at,
                    link_count=link_count,
                )
                for a, link_count in rows
            ],
            total=total,
        )


@router.get("/articles/{article_id}", response_model=ArticleOut)
async def get_article(
    article_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.read")),
) -> ArticleOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        article = await _get_article(session, article_id)
        links = (
            (
                await session.execute(
                    select(DocLink).where(DocLink.article_id == article_id).order_by(DocLink.id)
                )
            )
            .scalars()
            .all()
        )
        return ArticleOut(
            id=article.id,
            client_id=article.client_id,
            title=article.title,
            body=article.body,
            category=article.category,
            archived=article.archived,
            portal_visible=article.portal_visible,
            current_version=article.current_version,
            updated_at=article.updated_at,
            links=[
                LinkOut(id=link.id, target_type=link.target_type, target_id=link.target_id)
                for link in links
            ],
        )


@router.put("/articles/{article_id}", response_model=ArticleOut)
async def update_article(
    article_id: UUID,
    body: ArticleFields,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.manage")),
) -> ArticleOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        article = await _get_article(session, article_id)
        await svc.update_article(
            session,
            article,
            actor_id=ctx.user_id,
            title=body.title,
            body=body.body,
            category=body.category,
        )
        await session.refresh(article)
        return ArticleOut(
            id=article.id,
            client_id=article.client_id,
            title=article.title,
            body=article.body,
            category=article.category,
            archived=article.archived,
            portal_visible=article.portal_visible,
            current_version=article.current_version,
            updated_at=article.updated_at,
            links=[],
        )


@router.put("/articles/{article_id}/portal-visibility", response_model=PortalVisibility)
async def set_portal_visibility(
    article_id: UUID,
    body: PortalVisibility,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.manage")),
) -> PortalVisibility:
    """Show or hide an article in the end-user portal (requesters read it
    through `/portal/help`, never through these routes)."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        article = await _get_article(session, article_id)
        await svc.set_portal_visibility(
            session, article, actor_id=ctx.user_id, visible=body.portal_visible
        )
        return PortalVisibility(portal_visible=article.portal_visible)


@router.post("/articles/{article_id}/archive")
async def archive_article(
    article_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.manage")),
) -> dict[str, bool]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        article = await _get_article(session, article_id)
        await svc.archive_article(session, article, actor_id=ctx.user_id)
    return {"archived": True}


@router.get("/articles/{article_id}/versions", response_model=list[VersionOut])
async def article_versions(
    article_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.read")),
) -> list[VersionOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        await _get_article(session, article_id)
        versions = await svc.list_versions(session, article_id=article_id)
        return [
            VersionOut(
                version=v.version, snapshot=v.snapshot, changed_by=v.changed_by, ts=v.ts
            )
            for v in versions
        ]


@router.post(
    "/articles/{article_id}/links", status_code=status.HTTP_201_CREATED, response_model=LinkOut
)
async def add_link(
    article_id: UUID,
    body: LinkCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.manage")),
) -> LinkOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        article = await _get_article(session, article_id)
        try:
            link = await svc.add_link(
                session,
                article,
                actor_id=ctx.user_id,
                target_type=body.target_type,
                target_id=body.target_id,
            )
        except svc.LinkTargetNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
        except svc.LinkTargetOfAnotherClient as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
        except IntegrityError:
            raise HTTPException(status.HTTP_409_CONFLICT, tr("already linked")) from None
        return LinkOut(id=link.id, target_type=link.target_type, target_id=link.target_id)


@router.delete("/articles/{article_id}/links/{link_id}")
async def remove_link(
    article_id: UUID,
    link_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.manage")),
) -> dict[str, bool]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        article = await _get_article(session, article_id)
        removed = await svc.remove_link(session, article, link_id, actor_id=ctx.user_id)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such link"))
    return {"removed": True}


@router.post("/records", status_code=status.HTTP_201_CREATED, response_model=RecordOut)
async def create_record(
    body: RecordCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.manage")),
) -> RecordOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            record = await svc.create_record(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                record_type=body.record_type,
                name=body.name,
                fields=body.fields,
                client_id=body.client_id,
            )
        except svc.DocumentationClientError as exc:
            raise HTTPException(exc.status_code, exc.detail) from None
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
        return _record_out(record)


@router.get("/records", response_model=RecordListPage)
async def list_records(
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.read")),
    record_type: str | None = Query(default=None),
    include_archived: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> RecordListPage:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        query = select(DocRecord)
        if not include_archived:
            query = query.where(DocRecord.archived.is_(False))
        if record_type:
            query = query.where(DocRecord.record_type == record_type)
        total = (
            await session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar_one()
        rows = (
            (await session.execute(query.order_by(DocRecord.name).limit(limit).offset(offset)))
            .scalars()
            .all()
        )
        return RecordListPage(items=[_record_out(r) for r in rows], total=total)


@router.get("/records/{record_id}", response_model=RecordOut)
async def get_record(
    record_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.read")),
) -> RecordOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        record = (
            await session.execute(select(DocRecord).where(DocRecord.id == record_id))
        ).scalar_one_or_none()
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such record"))
    return _record_out(record)


@router.put("/records/{record_id}", response_model=RecordOut)
async def update_record(
    record_id: UUID,
    body: RecordUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.manage")),
) -> RecordOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        record = (
            await session.execute(select(DocRecord).where(DocRecord.id == record_id))
        ).scalar_one_or_none()
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such record"))
        try:
            await svc.update_record(
                session, record, actor_id=ctx.user_id, name=body.name, fields=body.fields
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
        return _record_out(record)


@router.post("/records/{record_id}/archive")
async def archive_record(
    record_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.manage")),
) -> dict[str, bool]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        record = (
            await session.execute(select(DocRecord).where(DocRecord.id == record_id))
        ).scalar_one_or_none()
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such record"))
        await svc.archive_record(session, record, actor_id=ctx.user_id)
    return {"archived": True}


@router.get("/records/{record_id}/versions", response_model=list[VersionOut])
async def record_versions(
    record_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.read")),
) -> list[VersionOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        versions = await svc.list_versions(session, record_id=record_id)
        return [
            VersionOut(
                version=v.version, snapshot=v.snapshot, changed_by=v.changed_by, ts=v.ts
            )
            for v in versions
        ]


class ArticleSuggestion(BaseModel):
    """An article that may answer a ticket."""

    id: UUID
    title: str
    category: str


@router.get("/suggest", response_model=list[ArticleSuggestion])
async def suggest(
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.read")),
    q: str = Query(min_length=1, max_length=300),
    limit: int = Query(default=5, ge=1, le=20),
) -> list[ArticleSuggestion]:
    """Articles sharing any meaningful word with `q` (a ticket subject), best
    match first. Used beside a ticket reply; `/search` is the search box."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await svc.suggest_articles(session, ctx.org_id, q, limit=limit)
        return [ArticleSuggestion(id=r.id, title=r.title, category=r.category) for r in rows]


@router.get("/search", response_model=list[SearchHit])
async def search(
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.read")),
    q: str = Query(min_length=1),
) -> list[SearchHit]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        document = func.to_tsvector(
            "english", DocArticle.title + " " + DocArticle.body
        )
        ts_query = func.websearch_to_tsquery("english", q)
        rank = func.ts_rank(document, ts_query).label("rank")
        article_rows = (
            await session.execute(
                select(DocArticle.id, DocArticle.title, DocArticle.category, rank)
                .where(DocArticle.archived.is_(False), document.op("@@")(ts_query))
                .order_by(rank.desc())
                .limit(50)
            )
        ).all()
        record_rows = (
            (
                await session.execute(
                    select(DocRecord)
                    .where(
                        DocRecord.archived.is_(False),
                        DocRecord.name.ilike(f"%{q.strip('\"')}%"),
                    )
                    .order_by(DocRecord.name)
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        document_rows = await documents_svc.search_documents(session, ctx.org_id, q)
    hits = [
        SearchHit(kind="article", id=r.id, title=r.title, category=r.category, rank=float(r.rank))
        for r in article_rows
    ]
    hits += [
        SearchHit(kind="record", id=r.id, title=r.name, category=r.record_type, rank=0.0)
        for r in record_rows
    ]
    hits += [
        SearchHit(
            kind="document", id=d.id, title=d.title, category=d.category, rank=rank_value
        )
        for d, rank_value in document_rows
    ]
    return hits
