"""Stored documents: the service layer
(`docs/modules/documentation/spec-attachments.md` §4, §6, §7, §8, §12).

**EVERY QUERY IN THIS FILE CARRIES AN EXPLICIT `org_id` PREDICATE, AND THAT IS
NOT DECORATION.** `org_session` runs as `app_user`, which is NOBYPASSRLS, so
inside it a SELECT with no tenant predicate at all returns exactly the rows a
correct one would -- deleting a `WHERE org_id` here turns no route test red.
`docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md` counted 219 tests
sitting in that position, several named as though they pinned the filter. The
belt is written here; the braces are the RLS policy; and
`tests/test_doc_documents_tenancy.py` observes the belt independently by
running these same functions on the BYPASSRLS admin session, where Postgres
stops filtering and this file's predicate is the only thing scoping anything.

**WHAT REPLACES RLS ON THE BYTES.** The object store has no row-level security
(§3.3). Four mechanisms stand in for it and all four are required: the key is
entirely server-derived (`ObjectStore._doc_document_key`), the only way to
learn a key is through a row this file loads under the tenant policy, both
storage directions assert the `{org_id}/docs/` prefix, and no presigned URL is
ever minted. Nothing here returns `object_key` to a caller, and no response
model in `api.py` has a field for it.
"""

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.storage import ObjectStore
from app.core.wire_words import say
from app.models import DocDocument, DocDocumentLink, DocDocumentVersion
from app.modules.documentation.filetypes import UnsupportedFileType, sniff
from app.modules.documentation.service import _LINK_TARGET_MODELS, LinkTargetNotFound
from app.modules.documentation.words import LINK_TARGET_WORDS

#: Per file (§4.2). Enforced by BOTH `app.core.http` guards in the route, in
#: that order, plus this module's own check on a body already read.
#:
#: WHY 25 MiB AND NOT 100. §0.3 forbids a client-to-bucket upload, so every
#: byte of a document passes through the API process and `read_bounded_body`
#: buffers the whole body. Peak memory is therefore
#: `concurrent_uploads x MAX_DOCUMENT_BYTES`; 25 MiB holds a scanned contract
#: or a diagram export and keeps ten simultaneous uploads inside a quarter of a
#: gigabyte. Raising it raises that PRODUCT linearly, which is the thing to
#: argue about -- not the file size. (And that peak is a DESIGN TARGET, not a
#: measured number: nothing in this repo measures API-process memory under
#: concurrent uploads. Per CLAUDE.md it stays described as a target until
#: something does. §13 F5 says so too.)
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024

#: Per organisation (§0.10, §4.2). A module constant with no UI in v1.
#:
#: THE BUCKET IS SHARED WITH BACKUP REPOSITORIES. Agent binaries, deployment
#: packages, OS images, job artefacts, session recordings and backup chunks all
#: live in it under their own prefixes, so an unbounded document upload path is
#: not a documentation problem: a `docs.manage` holder -- technician tier --
#: filling the volume stops BACKUPS from writing. That is why a quota exists at
#: all rather than being deferred with the rest of retention (§15.9).
ORG_DOCUMENT_QUOTA_BYTES = 5 * 1024 * 1024 * 1024

#: `target_type` values a document link accepts. Read off the SHIPPED
#: dictionary rather than copied: §0.7 refuses a second vocabulary, and
#: `service.py` already carries the comment about what happens when the
#: resolver and the route's regex drift (a 404 on one side, a 422 on the
#: other).
LINK_TARGET_TYPES: tuple[str, ...] = tuple(sorted(_LINK_TARGET_MODELS))


class DocumentNotFound(Exception):
    """The document, version or link is not in this org. A cross-org id and a
    nonexistent id are indistinguishable here, and deliberately so."""


class DocumentArchived(Exception):
    """An archived document takes no new versions -- the same freeze Service
    Desk applies to terminal tickets (409, not 400)."""


class VersionPurged(Exception):
    """The bytes were deliberately destroyed. 410 Gone, never 404: the version
    existed, and the audit log says who destroyed it. Conflating that with
    "never existed" loses the only fact worth keeping."""


class QuotaExceeded(Exception):
    """The organisation is at or over `ORG_DOCUMENT_QUOTA_BYTES` (507)."""


async def _audit(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    action: str,
    document_id: UUID,
    metadata: dict[str, Any] | None = None,
) -> None:
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action=action,
        target_type="doc_document",
        target_id=str(document_id),
        metadata=metadata or {},
    )


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


async def create_document(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID | None,
    title: str,
    description: str,
    category: str,
) -> DocDocument:
    document = DocDocument(
        org_id=org_id,
        title=title,
        description=description,
        category=category,
        created_by=actor_id,
        updated_by=actor_id,
    )
    session.add(document)
    await session.flush()
    await _audit(
        session,
        org_id,
        actor_id,
        "docs.document_created",
        document.id,
        {"title": title, "category": category},
    )
    await session.flush()
    return document


async def get_document(session: AsyncSession, org_id: UUID, document_id: UUID) -> DocDocument:
    document = (
        await session.execute(
            select(DocDocument).where(
                DocDocument.id == document_id, DocDocument.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if document is None:
        raise DocumentNotFound(
            tr("no document {document_id} in this organization", document_id=document_id)
        )
    return document


def _document_query(
    org_id: UUID,
    *,
    category: str | None,
    target_type: str | None,
    target_id: UUID | None,
    include_archived: bool,
) -> Select[Any]:
    query: Select[Any] = (
        select(DocDocument, func.count(DocDocumentLink.id).label("link_count"))
        .join(
            DocDocumentLink,
            DocDocumentLink.document_id == DocDocument.id,
            isouter=True,
        )
        .where(DocDocument.org_id == org_id)
    )
    if not include_archived:
        query = query.where(DocDocument.archived.is_(False))
    if category:
        query = query.where(DocDocument.category == category)
    if target_type and target_id:
        query = query.where(
            DocDocument.id.in_(
                select(DocDocumentLink.document_id).where(
                    DocDocumentLink.org_id == org_id,
                    DocDocumentLink.target_type == target_type,
                    DocDocumentLink.target_id == target_id,
                )
            )
        )
    return query.group_by(DocDocument.id)


async def list_documents(
    session: AsyncSession,
    org_id: UUID,
    *,
    category: str | None = None,
    target_type: str | None = None,
    target_id: UUID | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[tuple[DocDocument, int]], int]:
    query = _document_query(
        org_id,
        category=category,
        target_type=target_type,
        target_id=target_id,
        include_archived=include_archived,
    )
    total = (
        await session.execute(select(func.count()).select_from(query.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(
            query.order_by(DocDocument.category, DocDocument.title).limit(limit).offset(offset)
        )
    ).all()
    return [(row[0], row[1]) for row in rows], int(total)


async def update_document(
    session: AsyncSession,
    document: DocDocument,
    *,
    actor_id: UUID | None,
    title: str,
    description: str,
    category: str,
) -> bool:
    """Returns whether anything changed. A NO-OP WRITES NOTHING -- no audit
    event, no `updated_by`, no timestamp bump -- matching `update_article`. An
    audit log that records edits that did not happen is a log nobody can use to
    answer "who changed this"."""
    if (document.title, document.description, document.category) == (
        title,
        description,
        category,
    ):
        return False
    changed = [
        field
        for field, before, after in (
            ("title", document.title, title),
            ("description", document.description, description),
            ("category", document.category, category),
        )
        if before != after
    ]
    document.title, document.description, document.category = title, description, category
    document.updated_by = actor_id
    session.add(document)
    await _audit(
        session,
        document.org_id,
        actor_id,
        "docs.document_updated",
        document.id,
        {"changed": changed},
    )
    await session.flush()
    return True


async def archive_document(
    session: AsyncSession, document: DocDocument, *, actor_id: UUID | None
) -> None:
    document.archived = True
    document.updated_by = actor_id
    session.add(document)
    await _audit(session, document.org_id, actor_id, "docs.document_archived", document.id)
    await session.flush()


# --------------------------------------------------------------------------- #
# Quota (§4.2) -- two passes, and neither is redundant.
# --------------------------------------------------------------------------- #


async def org_usage_bytes(session: AsyncSession, org_id: UUID) -> int:
    """Total live bytes for the org. Purged versions do not count: their
    tombstone keeps the size for the audit record, but the storage is gone and
    charging an org for bytes it no longer occupies would make the quota a
    ratchet nobody could unwind."""
    total = (
        await session.execute(
            select(func.coalesce(func.sum(DocDocumentVersion.byte_size), 0)).where(
                DocDocumentVersion.org_id == org_id,
                DocDocumentVersion.purged_at.is_(None),
            )
        )
    ).scalar_one()
    return int(total)


def _quota_message(usage: int) -> str:
    return tr(
        "organization document storage quota reached ({usage} of {quota} bytes used). "
        "Purge or archive documents to free space.",
        usage=usage,
        quota=ORG_DOCUMENT_QUOTA_BYTES,
    )


async def assert_not_already_full(session: AsyncSession, org_id: UUID) -> None:
    """QUOTA PASS ONE (§4.4 step 5), which runs BEFORE the body is read.

    It asks one question -- "is this organisation already at the cap?" -- and
    it CANNOT ask the other one, because nothing has been read yet and the
    incoming size does not exist. What it buys is that an org which is already
    full is told so without first buffering 25 MiB of its upload in API-process
    memory. That is the whole of its job, and it is not the guard that enforces
    the limit.

    Note `>=`, not `>`: an org sitting exactly ON the cap is full, and the next
    byte it sends would exceed it.
    """
    usage = await org_usage_bytes(session, org_id)
    if usage >= ORG_DOCUMENT_QUOTA_BYTES:
        raise QuotaExceeded(_quota_message(usage))


async def assert_room_for(session: AsyncSession, org_id: UUID, *, incoming: int) -> None:
    """QUOTA PASS TWO (§4.4 step 8), which runs after the body is read and
    before the object write. THIS is the pass that enforces the limit.

    A DIFFERENT PREDICATE FROM PASS ONE, not the same one with a different
    argument, and that is why the two cannot be collapsed into a single call.
    Pass one asks "already full?" (`usage >= cap`); this asks "would this write
    exceed the cap?" (`usage + incoming > cap`). Neither implies the other: at
    `usage == cap` pass two would let a zero-byte write through, and below the
    cap pass one lets everything through however large. An upload that lands
    exactly ON the cap is ALLOWED here and refused by pass one next time, which
    is the behaviour an operator would predict from the number.

    KNOWN AND ACCEPTED: two concurrent uploads can both pass this. The
    overshoot is bounded by `concurrency x MAX_DOCUMENT_BYTES` and is not worth
    an advisory lock on a soft, operator-visible limit. Stated rather than
    fixed (§4.2).
    """
    usage = await org_usage_bytes(session, org_id)
    if usage + incoming > ORG_DOCUMENT_QUOTA_BYTES:
        raise QuotaExceeded(_quota_message(usage))


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #


async def add_version(
    session: AsyncSession,
    document: DocDocument,
    *,
    actor_id: UUID | None,
    filename: str,
    data: bytes,
    store: ObjectStore,
) -> DocDocumentVersion:
    """Steps 4, 7, 8 and 9 of §4.4. The caller has already done 1-3 and 5-6,
    which need the `Request` the service must not see.

    ORDER IS THE POINT. The archived check and the quota's second pass both run
    BEFORE the object write, so a refused upload leaves no orphan. The
    `len(data)` check is defence in depth on a body `read_bounded_body` has
    already bounded, not a substitute for it.

    KNOWN LIMITATION, CARRIED NOT FIXED: a failure between the object write
    succeeding and the transaction committing leaves an object with no row
    pointing at it. This is the identical, already-accepted limitation in
    `service_desk.threads.add_attachment`. It leaks storage and nothing else --
    an unreferenced key is unreachable by every path in §3.3.
    """
    if document.archived:
        raise DocumentArchived(
            tr(
                "document {document_id} is archived and accepts no new versions",
                document_id=document.id,
            )
        )
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError(
            tr(
                "document exceeds the {max_document_bytes}-byte limit",
                max_document_bytes=MAX_DOCUMENT_BYTES,
            )
        )

    content_type, viewer = sniff(data, filename)  # raises UnsupportedFileType -> 415
    await assert_room_for(session, document.org_id, incoming=len(data))

    version_id = uuid4()
    digest = hashlib.sha256(data).hexdigest()
    ref = await store.put_doc_document(document.org_id, document.id, version_id, data)

    version = DocDocumentVersion(
        id=version_id,
        org_id=document.org_id,
        document_id=document.id,
        version=document.current_version + 1,
        object_key=ref,
        filename=filename,
        byte_size=len(data),
        content_type=content_type,
        viewer=viewer,
        sha256=digest,
        uploaded_by=actor_id,
        created_at=datetime.now(UTC),
    )
    session.add(version)
    document.current_version += 1
    document.updated_by = actor_id
    session.add(document)
    await _audit(
        session,
        document.org_id,
        actor_id,
        "docs.document_version_uploaded",
        document.id,
        {
            "version": version.version,
            "filename": filename,
            "byte_size": len(data),
            "content_type": content_type,
            "sha256": digest,
        },
    )
    await session.flush()
    return version


async def list_document_versions(
    session: AsyncSession,
    org_id: UUID,
    document_id: UUID,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[DocDocumentVersion]:
    return list(
        (
            await session.execute(
                select(DocDocumentVersion)
                .where(
                    DocDocumentVersion.org_id == org_id,
                    DocDocumentVersion.document_id == document_id,
                )
                .order_by(DocDocumentVersion.version.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )


async def get_version(
    session: AsyncSession, org_id: UUID, document_id: UUID, version: int
) -> DocDocumentVersion:
    row = (
        await session.execute(
            select(DocDocumentVersion).where(
                DocDocumentVersion.org_id == org_id,
                DocDocumentVersion.document_id == document_id,
                DocDocumentVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise DocumentNotFound(
            tr(
                "no version {version} of document {document_id}",
                version=version,
                document_id=document_id,
            )
        )
    return row


async def current_version_row(
    session: AsyncSession, org_id: UUID, document: DocDocument
) -> DocDocumentVersion | None:
    if document.current_version == 0:
        return None
    return (
        await session.execute(
            select(DocDocumentVersion).where(
                DocDocumentVersion.org_id == org_id,
                DocDocumentVersion.document_id == document.id,
                DocDocumentVersion.version == document.current_version,
            )
        )
    ).scalar_one_or_none()


async def record_download(
    session: AsyncSession,
    version: DocDocumentVersion,
    *,
    actor_id: UUID | None,
) -> None:
    """EVERY VIEW IS AN AUDITED DOWNLOAD (§12.3, D10), and that is correct
    rather than lazy. The server cannot distinguish "viewed" from "saved" --
    the bytes left the control plane either way, and the client could always
    keep them -- so an audit log that claimed the distinction would be lying in
    the one place lying is most expensive. The consequence, stated so it is not
    a surprise: audit volume on this module grows with READING, not writing.

    No deduplication, either. Two opens are two events; a "we already logged
    that one" rule is how a log stops answering "how many times".
    """
    await _audit(
        session,
        version.org_id,
        actor_id,
        "docs.document_downloaded",
        version.document_id,
        {"version": version.version, "filename": version.filename},
    )
    await session.flush()


async def purge_version(
    session: AsyncSession,
    version: DocDocumentVersion,
    *,
    actor_id: UUID | None,
    store: ObjectStore,
) -> None:
    """The ONLY irreversible act in this module (§6.3).

    Archiving is soft and keeps the bytes, but a file uploaded by mistake may
    contain a password or personal data and "we archived it" is not an answer
    to that. So the object is deleted and the row survives as a TOMBSTONE:
    filename, size, sha256, uploader and timestamps all remain, and the audit
    event records them too. A destroyed file leaves a mark; that is the point.

    `object_key` is cleared to `""` so no code path can even attempt a fetch --
    a 410 is then a property of the data, not of a branch someone might delete.

    `current_version` is NOT decremented. The document's current version is
    then a tombstone and the console says so; renumbering would make the
    version history lie about how many uploads there were.
    """
    if version.purged_at is not None:
        return
    if version.object_key:
        await store.delete_doc_document(version.org_id, version.object_key)
    version.object_key = ""
    version.purged_at = datetime.now(UTC)
    version.purged_by = actor_id
    session.add(version)
    await _audit(
        session,
        version.org_id,
        actor_id,
        "docs.document_version_purged",
        version.document_id,
        {
            "version": version.version,
            "filename": version.filename,
            "byte_size": version.byte_size,
            "sha256": version.sha256,
        },
    )
    await session.flush()


# --------------------------------------------------------------------------- #
# Links (§7) -- the shipped vocabulary and the shipped resolver.
# --------------------------------------------------------------------------- #


async def add_document_link(
    session: AsyncSession,
    document: DocDocument,
    *,
    actor_id: UUID | None,
    target_type: str,
    target_id: UUID,
) -> DocDocumentLink:
    """The target is resolved INSIDE the tenant session, so a foreign-org
    target is indistinguishable from a nonexistent one -- 404 either way. That
    is the shipped module's own reasoning and it is not restated differently
    here. The explicit `org_id` predicate is on the models that carry one; a
    handful of link targets are org-scoped tables where the RLS policy is the
    braces and this predicate is the belt."""
    model = _LINK_TARGET_MODELS.get(target_type)
    if model is None:
        raise LinkTargetNotFound(
            tr("unknown target type {target_type}", target_type=repr(target_type))
        )
    exists = (
        await session.execute(
            select(model.id).where(model.id == target_id, model.org_id == document.org_id)
        )
    ).scalar_one_or_none()
    if exists is None:
        raise LinkTargetNotFound(
            tr(
                "no {target_type} {target_id} in this organization",
                target_type=say(LINK_TARGET_WORDS, target_type),
                target_id=target_id,
            )
        )
    link = DocDocumentLink(
        org_id=document.org_id,
        document_id=document.id,
        target_type=target_type,
        target_id=target_id,
        created_at=datetime.now(UTC),
    )
    session.add(link)
    await _audit(
        session,
        document.org_id,
        actor_id,
        "docs.document_link_added",
        document.id,
        {"target_type": target_type, "target_id": str(target_id)},
    )
    await session.flush()
    return link


async def list_document_links(
    session: AsyncSession, org_id: UUID, document_id: UUID
) -> list[DocDocumentLink]:
    return list(
        (
            await session.execute(
                select(DocDocumentLink)
                .where(
                    DocDocumentLink.org_id == org_id,
                    DocDocumentLink.document_id == document_id,
                )
                .order_by(DocDocumentLink.id)
            )
        )
        .scalars()
        .all()
    )


async def remove_document_link(
    session: AsyncSession, document: DocDocument, link_id: UUID, *, actor_id: UUID | None
) -> bool:
    link = (
        await session.execute(
            select(DocDocumentLink).where(
                DocDocumentLink.id == link_id,
                DocDocumentLink.document_id == document.id,
                DocDocumentLink.org_id == document.org_id,
            )
        )
    ).scalar_one_or_none()
    if link is None:
        return False
    target_type, target_id = link.target_type, link.target_id
    await session.delete(link)
    await _audit(
        session,
        document.org_id,
        actor_id,
        "docs.document_link_removed",
        document.id,
        {"target_type": target_type, "target_id": str(target_id)},
    )
    await session.flush()
    return True


# --------------------------------------------------------------------------- #
# Search (§8) -- metadata only, and the console says so on screen (§11.6).
# --------------------------------------------------------------------------- #


async def search_documents(
    session: AsyncSession, org_id: UUID, q: str, *, limit: int = 50
) -> list[tuple[DocDocument, float]]:
    """Title, description and category through the FTS index; filename through
    an `ilike` against the CURRENT version only.

    TWO MECHANISMS BECAUSE THE DATA IS ON TWO ROWS, which is the same split the
    shipped search already has (FTS for articles, `ilike` for record names).
    `filename` lives on `doc_document_versions`, so it cannot be in an
    expression index on `doc_documents`. Older versions' filenames are not
    searched, and §11.6 puts that on the screen rather than leaving people to
    discover it.

    Document CONTENTS are not searched at all in v1 -- §0.8 names the three
    dependencies, the licence reviews, the worker and the re-index pass that
    would cost, and states the additive forward path.
    """
    vector = func.to_tsvector(
        "english",
        DocDocument.title + " " + DocDocument.description + " " + DocDocument.category,
    )
    ts_query = func.websearch_to_tsquery("english", q)
    rank = func.ts_rank(vector, ts_query).label("rank")
    fts_rows = (
        await session.execute(
            select(DocDocument, rank)
            .where(
                DocDocument.org_id == org_id,
                DocDocument.archived.is_(False),
                vector.op("@@")(ts_query),
            )
            .order_by(rank.desc())
            .limit(limit)
        )
    ).all()

    needle = q.strip('"').strip()
    filename_rows: list[tuple[DocDocument, float]] = []
    if needle:
        filename_rows = [
            (row[0], 0.0)
            for row in (
                await session.execute(
                    select(DocDocument)
                    .join(
                        DocDocumentVersion,
                        DocDocumentVersion.document_id == DocDocument.id,
                    )
                    .where(
                        DocDocument.org_id == org_id,
                        DocDocument.archived.is_(False),
                        DocDocumentVersion.org_id == org_id,
                        DocDocumentVersion.version == DocDocument.current_version,
                        DocDocumentVersion.filename.ilike(f"%{needle}%"),
                    )
                    .order_by(DocDocument.title)
                    .limit(limit)
                )
            ).all()
        ]

    seen: set[UUID] = set()
    hits: list[tuple[DocDocument, float]] = []
    for document, score in [(r[0], float(r[1])) for r in fts_rows] + filename_rows:
        if document.id in seen:
            continue
        seen.add(document.id)
        hits.append((document, score))
    return hits


__all__ = [
    "LINK_TARGET_TYPES",
    "MAX_DOCUMENT_BYTES",
    "ORG_DOCUMENT_QUOTA_BYTES",
    "DocumentArchived",
    "DocumentNotFound",
    "LinkTargetNotFound",
    "QuotaExceeded",
    "UnsupportedFileType",
    "VersionPurged",
    "add_document_link",
    "add_version",
    "archive_document",
    "assert_not_already_full",
    "assert_room_for",
    "create_document",
    "current_version_row",
    "get_document",
    "get_version",
    "list_document_links",
    "list_document_versions",
    "list_documents",
    "org_usage_bytes",
    "purge_version",
    "record_download",
    "remove_document_link",
    "search_documents",
    "update_document",
]
