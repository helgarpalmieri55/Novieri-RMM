"""Reading backup manifests of either schema version (spec-server-backup.md §5.2).

A v1 manifest lists every entry and its chunk ids inline. A v2 manifest lists
only PAGES: chunks holding NDJSON entries, and an entry whose chunk list is
long names further id pages (`chunk_pages`, one id per line). Pages are
ordinary chunks in the repository, so everything that decides whether a chunk
is still needed (GC, verification, replication, off-site GC) must count page
ids as referenced, or GC deletes the pages and the point is unreadable.

`referenced_chunk_ids` is the one place that walk happens. It fails closed: an
unknown schema version, a page that does not hash to its id or does not parse,
raises, and every caller treats a raise as "delete nothing".
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from blake3 import blake3
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.modules.backup.schema import (
    BackupManifest,
    BackupManifestV2,
    FiletreeStream,
    ManifestEntry,
    ManifestEntryV2,
)

Fetch = Callable[[str], Awaitable[bytes]]
_NONCE = 12
_ID_LINE = 65  # 64 hex + "\n"


class ManifestError(ValueError):
    """A manifest or page that cannot be trusted; the message quotes no data."""


def _open(repo_key: bytes, blob: bytes) -> bytes:
    return AESGCM(repo_key).decrypt(blob[:_NONCE], blob[_NONCE:], None)


def open_manifest_document(repo_key: bytes, blob: bytes) -> dict[str, Any]:
    doc = json.loads(_open(repo_key, blob))
    if not isinstance(doc, dict):
        raise ManifestError(  # i18n-exempt: fails closed; deletes stop; the manifest GET 500s
            "the manifest is not an object"
        )
    return doc


async def _page(repo_key: bytes, fetch: Fetch, page_id: str) -> bytes:
    plain = _open(repo_key, await fetch(page_id))
    if blake3(plain).hexdigest() != page_id:
        raise ManifestError(  # i18n-exempt: fails closed; deletes stop; the manifest GET 500s
            "a manifest page does not hash to its id"
        )
    return plain


def _entries(page: bytes) -> list[ManifestEntryV2]:
    out: list[ManifestEntryV2] = []
    for line in page.split(b"\n"):
        if not line:
            continue
        try:
            out.append(ManifestEntryV2.model_validate_json(line))
        except ValueError as exc:
            raise ManifestError(  # i18n-exempt: fails closed; deletes stop; the manifest GET 500s
                "a manifest page did not decode to entries"
            ) from exc
    return out


def _ids(page: bytes) -> list[str]:
    if len(page) % _ID_LINE != 0:
        raise ManifestError(  # i18n-exempt: fails closed; deletes stop; the manifest GET 500s
            "a chunk id page is malformed"
        )
    ids = [page[i : i + 64].decode() for i in range(0, len(page), _ID_LINE)]
    if any(page[i + 64 : i + 65] != b"\n" for i in range(0, len(page), _ID_LINE)):
        raise ManifestError(  # i18n-exempt: fails closed; deletes stop; the manifest GET 500s
            "a chunk id page is malformed"
        )
    return ids


async def _v2_entries(
    repo_key: bytes, manifest: BackupManifestV2, fetch: Fetch, referenced: set[str]
) -> list[tuple[ManifestEntryV2, list[str]]]:
    """Every entry with its full chunk list; adds every page id to `referenced`."""
    out: list[tuple[ManifestEntryV2, list[str]]] = []
    for stream in manifest.streams:
        for page_id in stream.pages:
            referenced.add(page_id)
            for entry in _entries(await _page(repo_key, fetch, page_id)):
                chunks = list(entry.chunk_ids or [])
                for id_page in entry.chunk_pages or []:
                    referenced.add(id_page)
                    chunks.extend(_ids(await _page(repo_key, fetch, id_page)))
                referenced.update(chunks)
                out.append((entry, chunks))
    return out


async def referenced_chunk_ids(repo_key: bytes, blob: bytes, fetch: Fetch) -> set[str]:
    """Every chunk object a sealed manifest keeps alive: data chunks, and for
    v2 its entry pages and chunk id pages."""
    doc = open_manifest_document(repo_key, blob)
    version = doc.get("schema_version")
    referenced: set[str] = set()
    if version == 1:
        for stream in BackupManifest.model_validate(doc).streams:
            for entry in stream.entries:
                referenced.update(entry.chunk_ids)
        return referenced
    if version == 2:
        await _v2_entries(repo_key, BackupManifestV2.model_validate(doc), fetch, referenced)
        return referenced
    raise ManifestError(  # i18n-exempt: fails closed; deletes stop; the manifest GET 500s
        f"manifest schema_version {version!r} is not one this server reads"
    )


async def browse_entries(repo_key: bytes, blob: bytes, fetch: Fetch) -> BackupManifest:
    """The manifest as the v1 browse shape: paths, sizes and chunk ids, never
    file contents. A v2 manifest's pages are read and flattened."""
    doc = open_manifest_document(repo_key, blob)
    if doc.get("schema_version") == 1:
        return BackupManifest.model_validate(doc)
    if doc.get("schema_version") != 2:
        raise ManifestError(  # i18n-exempt: fails closed; deletes stop; the manifest GET 500s
            "unknown manifest schema_version"
        )
    pairs = await _v2_entries(repo_key, BackupManifestV2.model_validate(doc), fetch, set())
    entries = [
        ManifestEntry(
            path=e.path,
            kind=e.kind if e.kind in ("file", "dir", "symlink") else "file",
            mode=e.mode,
            size=e.size,
            mtime=e.mtime,
            chunk_ids=chunks,
            target=e.target,
        )
        for e, chunks in pairs
    ]
    return BackupManifest(schema_version=2, streams=[FiletreeStream(type="filetree",
                                                                    entries=entries)])
