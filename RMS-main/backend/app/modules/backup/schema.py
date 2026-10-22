"""Wire contracts for `backup.run` / `backup.restore` (docs/modules/backup/
spec.md), plus the pinned repository-format models that document the
on-disk manifest layout the agent writes to and reads from a backup
repository.

`BackupRunParams`/`BackupRestoreParams` are the params the server sends when
dispatching the corresponding signed jobs; `BackupRunResult`/
`BackupRestoreResult` are the documents the agent sends back as job results.
`ManifestEntry`, `FiletreeStream`, and `BackupManifest` are not job
params/results — they are the pinned repository-format contract (the shape
of the manifest object the agent writes into repository storage) shared
between the server (for restore browsing) and the agent (for run/restore).
All are strict wire contracts: `extra="forbid"` so a malformed or unexpected
field is rejected rather than silently dropped. Exported as JSON Schema to
docs/kernel/wire-protocol/ for `agent/` to build its Go
structs against.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Format 2 members (spec-server-backup.md §5.1, §5.3). Every one is optional
# with a default, so an agent that predates format 2 validates unchanged.


class BackupWorkload(_Wire):
    id: str
    kind: Literal["files"]
    config: dict[str, Any] = {}


class BackupVss(_Wire):
    backup_type: Literal["copy", "full"] = "copy"
    fail_on_writer: bool = True


class BackupRunParams(_Wire):
    repository_id: str
    storage_ref: str
    sources: dict[str, Any] = {}
    dedup_params: dict[str, Any] = {}
    repo_key: str
    snapshot: Literal["vss", "lvm", "apfs", "auto"]
    format: int = 1
    workload: BackupWorkload | None = None
    vss: BackupVss | None = None
    # RFC 3339. A current agent does not start after it; an older one ignores
    # it, and the server then refuses its uploads (spec-server-backup §3.3).
    not_after_start: str | None = None


class BackupWriterStatus(_Wire):
    name: str
    state: str
    hr: str = ""
    at: str = ""


class BackupConsistency(_Wire):
    level: Literal["application", "snapshot", "live", "partial"]
    crash_consistent: bool
    snapshot: str = "none"
    writers: list[BackupWriterStatus] | None = None
    files_changed_during_read: int = 0
    non_transactional_tables: int = 0
    databases: list[dict[str, Any]] | None = None


class BackupRunResult(_Wire):
    state: Literal["success", "failed"]
    manifest_ref: str | None = None
    bytes_protected: int
    bytes_stored: int
    chunk_count: int
    dedup_stats: dict[str, Any] = {}
    error: str | None = None
    workload_id: str | None = None
    warnings: list[str] | None = None
    consistency: BackupConsistency | None = None


class BackupRestoreParams(_Wire):
    repository_id: str
    storage_ref: str
    manifest_ref: str
    selection: list[str]
    target: dict[str, Any]
    mode: Literal["restore", "verify"]
    repo_key: str
    format: int = 1
    restore_kind: Literal["files"] | None = None


class BackupRestoreResult(_Wire):
    state: Literal["success", "failed"]
    restored_bytes: int
    verified: bool
    mismatch: list[str] | None = None
    error: str | None = None
    warnings: list[str] | None = None


class ManifestEntry(_Wire):
    path: str
    kind: Literal["file", "dir", "symlink"]
    mode: int
    size: int
    mtime: float
    chunk_ids: list[str] = []
    target: str | None = None


class FiletreeStream(_Wire):
    type: Literal["filetree"]
    entries: list[ManifestEntry] = []


class BackupManifest(_Wire):
    schema_version: int
    streams: list[FiletreeStream] = []


# Manifest v2 (spec-server-backup.md §5.2). Read-only on the server and not
# exported: the server never writes one, it only has to find every chunk a v2
# point references (GC, verification, replication) and list its entries.
# `extra="ignore"` on purpose: an entry carrying a member this server does not
# know must still be readable, or GC would stop at it.


class _Read(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ManifestPageIndex(_Read):
    stream: int = 0
    page: int = 0
    first: str = ""
    last: str = ""
    entries: int = 0


class ManifestHeaderV2(_Read):
    workload_kind: str = "files"
    consistency: dict[str, Any] | None = None
    host_os: str = ""
    page_index: list[ManifestPageIndex] = []


class StreamV2(_Read):
    type: str
    name: str = ""
    entry_count: int = 0
    pages: list[str] = []


class BackupManifestV2(_Read):
    schema_version: Literal[2]
    header: ManifestHeaderV2 = ManifestHeaderV2()
    streams: list[StreamV2] = []


class ManifestEntryV2(_Read):
    path: str
    kind: str
    mode: int = 0
    size: int = 0
    mtime: float = 0.0
    chunk_ids: list[str] | None = None
    chunk_pages: list[str] | None = None
    target: str | None = None
    flags: list[str] | None = None
