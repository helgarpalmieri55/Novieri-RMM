"""Stored-document routes (`docs/modules/documentation/spec-attachments.md`
§10). Reads need `docs.read`; mutations `docs.manage`; purge its own code.

**ROUTE ORDERING IS CHECKED HERE, NOT ASSUMED.** Every path in this file sits
under the literal prefix `/docs/documents`, so none shadows the shipped
`/docs/articles`, `/docs/records` or `/docs/search`, and none shadows another
-- a `{...}` segment is always preceded by a literal. This is stated because
`service_desk/api.py` had to fix a real shadowing bug of exactly this kind.

**NO RESPONSE MODEL IN THIS FILE HAS AN `object_key` FIELD.** That is one of
the four mechanisms standing in for the row-level security an object store
cannot have (§3.3): the only way to learn a key is through a row loaded under
the tenant policy, and no key ever leaves the control plane.
`tests/test_doc_documents_api.py` asserts the absence over EVERY route's body
rather than spot-checking (F1b).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.api.deps import AuthContext, require_permission
from app.core.attachments import sanitize_attachment_filename
from app.core.http import read_bounded_body, reject_oversized_content_length
from app.core.i18n import tr
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.modules.documentation import documents as svc
from app.modules.documentation.filetypes import UnsupportedFileType

router = APIRouter(prefix="/docs", tags=["docs"])

_LINK_TARGET_PATTERN = "^(" + "|".join(svc.LINK_TARGET_TYPES) + ")$"


def _object_store(request: Request) -> ObjectStore:
    """Lazily build (and cache) the object store on first use -- the same
    shape `budget`, `service_desk`, `backup`, `deploy` and `remote` all use.
    Construction head-buckets, so it stays out of app startup and out of every
    metadata route: §0.9 makes object storage a SOFT dependency, and the
    documents list must keep answering from PostgreSQL when it is down."""
    store = getattr(request.app.state, "object_store", None)
    if store is None:
        store = ObjectStore(request.app.state.settings)
        request.app.state.object_store = store
    return store


# --------------------------------------------------------------------------- #
# Schemas -- no `object_key`, anywhere, in any of them.
# --------------------------------------------------------------------------- #


class DocumentFields(BaseModel):
    """Document metadata. The file is a separate request (§4.1): there is no
    multipart parser in this project, so metadata and bytes cannot share
    one."""

    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    category: str = Field(default="General", max_length=100)


class DocumentCreate(DocumentFields):
    """A document's client is set when it is created and never changes: the
    edit body (`DocumentFields`) has no client (MSP clients plan 2026-10-26
    Task 14)."""

    client_id: UUID | None = Field(
        default=None,
        description=(
            "The client this document belongs to. Omitted: it is every client's "
            "(global), which needs access to every client."
        ),
    )


class DocumentLinkCreate(BaseModel):
    """A new link from a document to a core entity."""

    # Kept in lockstep with `documents.LINK_TARGET_TYPES`, which is itself read
    # off the SHIPPED `service._LINK_TARGET_MODELS` rather than copied -- so
    # the regex and the resolver cannot drift into the 404/422 mismatch
    # `service.py` already carries a comment about.
    target_type: str = Field(pattern=_LINK_TARGET_PATTERN)
    target_id: UUID


class DocumentLinkOut(BaseModel):
    """One relationship between a document and a core entity. `target_type` is
    the shipped `doc_links` vocabulary; a `credential` target names metadata
    only and never a secret."""

    id: UUID
    target_type: str
    target_id: UUID


class VersionOut(BaseModel):
    """One uploaded file. `content_type` and `viewer` are SERVER-sniffed from
    the leading bytes -- the client never declares a type -- and `viewer` is
    the single key the console's render map uses. `purged_at` non-null means
    the bytes were destroyed and only this tombstone remains. No object key
    appears here or anywhere else."""

    version: int
    filename: str
    byte_size: int
    content_type: str
    viewer: str
    sha256: str
    created_at: datetime
    uploaded_by: UUID | None
    purged_at: datetime | None


class DocumentListItem(BaseModel):
    """A document as it appears in the library list. `current_version` is 0
    for a document that has no file uploaded yet, which is a legal state.
    `client_id` None: global, every client's."""

    id: UUID
    client_id: UUID | None
    title: str
    description: str
    category: str
    archived: bool
    current_version: int
    updated_at: datetime
    link_count: int


class DocumentListPage(BaseModel):
    """A page of documents plus the org-wide total matching the filters."""

    items: list[DocumentListItem]
    total: int


class DocumentOut(BaseModel):
    """A document with its current version (or null, when nothing has been
    uploaded yet) and its links. `client_id` None: global, every client's."""

    id: UUID
    client_id: UUID | None
    title: str
    description: str
    category: str
    archived: bool
    current_version: int
    updated_at: datetime
    current: VersionOut | None
    links: list[DocumentLinkOut]


def _version_out(row: Any) -> VersionOut:
    return VersionOut(
        version=row.version,
        filename=row.filename,
        byte_size=row.byte_size,
        content_type=row.content_type,
        viewer=row.viewer,
        sha256=row.sha256,
        created_at=row.created_at,
        uploaded_by=row.uploaded_by,
        purged_at=row.purged_at,
    )


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


async def _document_out(session: Any, org_id: UUID, document: Any) -> DocumentOut:
    current = await svc.current_version_row(session, org_id, document)
    links = await svc.list_document_links(session, org_id, document.id)
    return DocumentOut(
        id=document.id,
        client_id=document.client_id,
        title=document.title,
        description=document.description,
        category=document.category,
        archived=document.archived,
        current_version=document.current_version,
        updated_at=document.updated_at,
        current=_version_out(current) if current is not None else None,
        links=[
            DocumentLinkOut(id=link.id, target_type=link.target_type, target_id=link.target_id)
            for link in links
        ],
    )


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


@router.post("/documents", status_code=status.HTTP_201_CREATED, response_model=DocumentOut)
async def create_document(
    body: DocumentCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.manage")),
) -> DocumentOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            document = await svc.create_document(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                title=body.title,
                description=body.description,
                category=body.category,
                client_id=body.client_id,
            )
        except svc.DocumentationClientError as exc:
            raise HTTPException(exc.status_code, exc.detail) from None
        await session.refresh(document)
        return await _document_out(session, ctx.org_id, document)


@router.get("/documents", response_model=DocumentListPage)
async def list_documents(
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.read")),
    category: str | None = Query(default=None),
    target_type: str | None = Query(default=None),
    target_id: UUID | None = Query(default=None),
    include_archived: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> DocumentListPage:
    """§0.9/F8a: this route never touches the object store, so the library
    still lists when MinIO is down. A metadata list rendered as an empty list
    would tell an operator their documents are gone."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows, total = await svc.list_documents(
            session,
            ctx.org_id,
            category=category,
            target_type=target_type,
            target_id=target_id,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )
        return DocumentListPage(
            items=[
                DocumentListItem(
                    id=d.id,
                    client_id=d.client_id,
                    title=d.title,
                    description=d.description,
                    category=d.category,
                    archived=d.archived,
                    current_version=d.current_version,
                    updated_at=d.updated_at,
                    link_count=link_count,
                )
                for d, link_count in rows
            ],
            total=total,
        )


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.read")),
) -> DocumentOut:
    """One document, its current version's metadata and its links. A cross-org
    id is a 404, indistinguishable from a nonexistent one."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            document = await svc.get_document(session, ctx.org_id, document_id)
        except svc.DocumentNotFound as exc:
            raise _not_found(exc) from None
        return await _document_out(session, ctx.org_id, document)


@router.put("/documents/{document_id}", response_model=DocumentOut)
async def update_document(
    document_id: UUID,
    body: DocumentFields,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.manage")),
) -> DocumentOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            document = await svc.get_document(session, ctx.org_id, document_id)
        except svc.DocumentNotFound as exc:
            raise _not_found(exc) from None
        await svc.update_document(
            session,
            document,
            actor_id=ctx.user_id,
            title=body.title,
            description=body.description,
            category=body.category,
        )
        await session.refresh(document)
        return await _document_out(session, ctx.org_id, document)


@router.post("/documents/{document_id}/archive")
async def archive_document(
    document_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.manage")),
) -> dict[str, bool]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            document = await svc.get_document(session, ctx.org_id, document_id)
        except svc.DocumentNotFound as exc:
            raise _not_found(exc) from None
        await svc.archive_document(session, document, actor_id=ctx.user_id)
    return {"archived": True}


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #


@router.get("/documents/{document_id}/versions", response_model=list[VersionOut])
async def list_versions(
    document_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.read")),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[VersionOut]:
    """Every version of one document, newest first. Purged versions appear as
    tombstones -- filename, size and digest survive, `purged_at` is set.

    BOUNDED even though a document's history is naturally short: versions are
    append-only and nothing prunes them in v1 (§15.9), so "naturally short" is
    a claim about how people behave rather than about the data. `limit` is the
    same shape the documents list uses.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await svc.get_document(session, ctx.org_id, document_id)
        except svc.DocumentNotFound as exc:
            raise _not_found(exc) from None
        rows = await svc.list_document_versions(
            session, ctx.org_id, document_id, limit=limit, offset=offset
        )
        return [_version_out(row) for row in rows]


@router.post(
    "/documents/{document_id}/versions",
    status_code=status.HTTP_201_CREATED,
    response_model=VersionOut,
)
async def upload_version(
    document_id: UUID,
    request: Request,
    filename: str = Query(min_length=1, max_length=255),
    ctx: AuthContext = Depends(require_permission("docs.manage")),
) -> VersionOut:
    """§4.4, cheapest first, and the order is load-bearing rather than tidy.

    1. `reject_oversized_content_length` -- no DB, no wire read.
    2. `sanitize_attachment_filename` -- no DB, no wire read. A rejected
       filename is a 400 before a byte of body exists.
    3. Load the document under `org_session`: 404 for missing or cross-org
       BEFORE the body is read. Reading first would let any `docs.manage`
       holder buffer 25 MiB against a nonexistent id.
    4. Archived -> 409, the same freeze Service Desk applies to terminal
       tickets.
    5. Quota, first pass. It cannot see the incoming size -- nothing has been
       read -- which is precisely why there is a second one.
    6. `read_bounded_body`: the guard a chunked or lying client cannot route
       around, because it counts real bytes rather than a declared header.
    7. `sniff` -> 415 on refusal.
    8. Quota, second pass, WITH the real length. This is the one that
       enforces the limit.
    9. sha256, object write, row, bump, audit.

    Steps 7-9 and the second quota pass live in the service; the rest need the
    `Request` the service must never see.
    """
    reject_oversized_content_length(request, svc.MAX_DOCUMENT_BYTES)
    try:
        clean_filename = sanitize_attachment_filename(filename)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            document = await svc.get_document(session, ctx.org_id, document_id)
        except svc.DocumentNotFound as exc:
            raise _not_found(exc) from None
        if document.archived:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                tr(
                    "document {document_id} is archived and accepts no new versions",
                    document_id=document_id,
                ),
            )
        try:
            await svc.assert_not_already_full(
                session, ctx.org_id, client_id=document.client_id
            )
        except svc.QuotaExceeded as exc:
            raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(exc)) from None

        data = await read_bounded_body(request, svc.MAX_DOCUMENT_BYTES)

        store = _object_store(request)
        try:
            version = await svc.add_version(
                session,
                document,
                actor_id=ctx.user_id,
                filename=clean_filename,
                data=data,
                store=store,
            )
        except UnsupportedFileType as exc:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)
            ) from None
        except svc.DocumentArchived as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
        except svc.QuotaExceeded as exc:
            raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc)) from None
        return _version_out(version)


@router.get("/documents/{document_id}/versions/{version}/content")
async def download_version(
    document_id: UUID,
    version: int,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.read")),
) -> Response:
    """The ONE byte-serving route, and it is always a download (§5.1, wall 1).

    `application/octet-stream` REGARDLESS of the sniffed type, including for
    PDFs and images: the stored `content_type` is metadata for the console's
    render map and never a response header. Direct navigation to this URL
    downloads a file; it never renders a page, whatever the bytes are.

    `Cache-Control: no-store` is set here explicitly rather than by widening
    `security_headers.py`'s `/checkout` suffix rule -- a suffix match that
    grows is how one of those eventually catches a route nobody meant. A
    documentation library holds signed contracts and those should not sit in a
    shared workstation's disk cache.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await svc.get_document(session, ctx.org_id, document_id)
            row = await svc.get_version(session, ctx.org_id, document_id, version)
        except svc.DocumentNotFound as exc:
            raise _not_found(exc) from None
        if row.purged_at is not None:
            # 410, not 404 (§5.1): the version existed and was deliberately
            # destroyed, and the audit log says by whom. Conflating that with
            # "never existed" loses the only fact worth keeping.
            raise HTTPException(
                status.HTTP_410_GONE,
                tr(
                    "version {version} was purged on {purged_at}",
                    version=version,
                    purged_at=row.purged_at.isoformat(),
                ),
            )
        ref, filename = row.object_key, row.filename
        # Audited HERE, once per response, BEFORE the bytes are fetched. Every
        # view is a download and the log says so (§12.3, D10): the server
        # cannot tell viewing from saving, and a log that claimed the
        # distinction would be lying in the one place lying is most expensive.
        await svc.record_download(session, row, actor_id=ctx.user_id)

    try:
        data = await _object_store(request).get_doc_document(ref, ctx.org_id)
    except Exception as exc:  # noqa: BLE001 -- see below
        # §0.9/F8b: object storage is a SOFT dependency (probe_objectstore
        # never gates readiness), so it being down must fail loudly and
        # SPECIFICALLY on the byte path rather than as an anonymous 500 or, far
        # worse, an empty 200. Broad by design: botocore raises a generated
        # exception hierarchy and MinIO, a proxy and a DNS failure all arrive
        # differently. The metadata above is already committed and intact,
        # which is what the message tells the operator.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            tr(
                "object storage is unavailable, so this document's bytes could not be read; "
                "its metadata is intact ({error_class})",
                error_class=type(exc).__name__,
            ),
        ) from None

    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/documents/{document_id}/versions/{version}/purge")
async def purge_version(
    document_id: UUID,
    version: int,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.document.purge")),
) -> dict[str, bool]:
    """The only irreversible act in this module, on its own admin-tier code
    (§6.3, §9). A technician holding `docs.manage` can archive a document;
    only an admin can destroy its bytes."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await svc.get_document(session, ctx.org_id, document_id)
            row = await svc.get_version(session, ctx.org_id, document_id, version)
        except svc.DocumentNotFound as exc:
            raise _not_found(exc) from None
        store = _object_store(request)
        try:
            await svc.purge_version(session, row, actor_id=ctx.user_id, store=store)
        except Exception as exc:  # noqa: BLE001 -- as in `download_version`
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                tr(
                    "object storage is unavailable, so these bytes could not be destroyed; nothing "
                    "was changed ({error_class})",
                    error_class=type(exc).__name__,
                ),
            ) from None
    return {"purged": True}


# --------------------------------------------------------------------------- #
# Links (§7)
# --------------------------------------------------------------------------- #


@router.post(
    "/documents/{document_id}/links",
    status_code=status.HTTP_201_CREATED,
    response_model=DocumentLinkOut,
)
async def add_link(
    document_id: UUID,
    body: DocumentLinkCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.manage")),
) -> DocumentLinkOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            document = await svc.get_document(session, ctx.org_id, document_id)
        except svc.DocumentNotFound as exc:
            raise _not_found(exc) from None
        try:
            link = await svc.add_document_link(
                session,
                document,
                actor_id=ctx.user_id,
                target_type=body.target_type,
                target_id=body.target_id,
            )
        except svc.LinkTargetNotFound as exc:
            raise _not_found(exc) from None
        except svc.LinkTargetOfAnotherClient as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
        except IntegrityError:
            raise HTTPException(status.HTTP_409_CONFLICT, tr("already linked")) from None
        return DocumentLinkOut(id=link.id, target_type=link.target_type, target_id=link.target_id)


@router.delete("/documents/{document_id}/links/{link_id}")
async def remove_link(
    document_id: UUID,
    link_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("docs.manage")),
) -> dict[str, bool]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            document = await svc.get_document(session, ctx.org_id, document_id)
        except svc.DocumentNotFound as exc:
            raise _not_found(exc) from None
        removed = await svc.remove_document_link(
            session, document, link_id, actor_id=ctx.user_id
        )
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such link"))
    return {"removed": True}
