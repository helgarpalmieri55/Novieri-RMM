"""Wire-contract tests for `backup.run` / `backup.restore` (docs/modules/
backup/spec.md) and the pinned repository-format models.

Mirrors `tests/test_deploy_schema.py`'s style: construct each model from a
dict, assert `extra="forbid"` rejects an unknown key, assert invalid
literals are rejected, and assert the manifest format round-trips.
"""

import pytest
from pydantic import ValidationError

from app.modules.backup.schema import (
    BackupManifest,
    BackupRestoreParams,
    BackupRestoreResult,
    BackupRunParams,
    BackupRunResult,
    FiletreeStream,
    ManifestEntry,
)


class TestBackupRunParams:
    def test_construct(self) -> None:
        params = BackupRunParams(
            repository_id="0198c1f4-0000-7000-8000-000000000000",
            storage_ref="org/backup/repo-1",
            sources={"paths": ["/etc"]},
            dedup_params={"algo": "buzhash"},
            repo_key="c29tZS1iYXNlNjQta2V5",
            snapshot="vss",
        )
        assert params.repository_id == "0198c1f4-0000-7000-8000-000000000000"
        assert params.snapshot == "vss"
        assert isinstance(params.repo_key, str)

    def test_defaults(self) -> None:
        params = BackupRunParams(
            repository_id="repo-1",
            storage_ref="org/backup/repo-1",
            repo_key="key",
            snapshot="lvm",
        )
        assert params.sources == {}
        assert params.dedup_params == {}

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            BackupRunParams(
                repository_id="repo-1",
                storage_ref="ref",
                repo_key="key",
                snapshot="vss",
                bogus="x",
            )

    def test_rejects_invalid_snapshot(self) -> None:
        with pytest.raises(ValidationError):
            BackupRunParams(
                repository_id="repo-1",
                storage_ref="ref",
                repo_key="key",
                snapshot="zfs",
            )


class TestBackupRunResult:
    def test_construct(self) -> None:
        result = BackupRunResult(
            state="success",
            manifest_ref="org/backup/repo-1/manifests/abc",
            bytes_protected=1024,
            bytes_stored=512,
            chunk_count=4,
            dedup_stats={"ratio": 2.0},
        )
        assert result.state == "success"
        assert result.error is None

    def test_failed_state(self) -> None:
        result = BackupRunResult(
            state="failed",
            bytes_protected=0,
            bytes_stored=0,
            chunk_count=0,
            error="disk full",
        )
        assert result.manifest_ref is None
        assert result.error == "disk full"

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            BackupRunResult(
                state="success",
                bytes_protected=0,
                bytes_stored=0,
                chunk_count=0,
                bogus="x",
            )

    def test_rejects_invalid_state(self) -> None:
        with pytest.raises(ValidationError):
            BackupRunResult(
                state="partial",
                bytes_protected=0,
                bytes_stored=0,
                chunk_count=0,
            )


class TestBackupRestoreParams:
    def test_construct(self) -> None:
        params = BackupRestoreParams(
            repository_id="repo-1",
            storage_ref="org/backup/repo-1",
            manifest_ref="org/backup/repo-1/manifests/abc",
            selection=["/etc/hosts"],
            target={"path": "/restore"},
            mode="restore",
            repo_key="key",
        )
        assert params.mode == "restore"
        assert isinstance(params.repo_key, str)

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            BackupRestoreParams(
                repository_id="repo-1",
                storage_ref="ref",
                manifest_ref="ref",
                selection=[],
                target={},
                mode="restore",
                repo_key="key",
                bogus="x",
            )

    def test_rejects_invalid_mode(self) -> None:
        with pytest.raises(ValidationError):
            BackupRestoreParams(
                repository_id="repo-1",
                storage_ref="ref",
                manifest_ref="ref",
                selection=[],
                target={},
                mode="preview",
                repo_key="key",
            )


class TestBackupRestoreResult:
    def test_construct(self) -> None:
        result = BackupRestoreResult(
            state="success",
            restored_bytes=1024,
            verified=True,
        )
        assert result.mismatch is None
        assert result.error is None

    def test_failed_with_mismatch(self) -> None:
        result = BackupRestoreResult(
            state="failed",
            restored_bytes=0,
            verified=False,
            mismatch=["/etc/hosts"],
            error="checksum mismatch",
        )
        assert result.mismatch == ["/etc/hosts"]

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            BackupRestoreResult(
                state="success",
                restored_bytes=0,
                verified=True,
                bogus="x",
            )

    def test_rejects_invalid_state(self) -> None:
        with pytest.raises(ValidationError):
            BackupRestoreResult(
                state="unknown",
                restored_bytes=0,
                verified=True,
            )


class TestManifestFormat:
    def test_manifest_entry_file(self) -> None:
        entry = ManifestEntry(
            path="/etc/hosts",
            kind="file",
            mode=0o644,
            size=128,
            mtime=1732000000.0,
            chunk_ids=["chunk-1", "chunk-2"],
        )
        assert entry.kind == "file"
        assert entry.chunk_ids == ["chunk-1", "chunk-2"]
        assert entry.target is None

    def test_manifest_entry_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            ManifestEntry(
                path="/etc/hosts",
                kind="file",
                mode=0o644,
                size=128,
                mtime=1732000000.0,
                bogus="x",
            )

    def test_manifest_entry_rejects_invalid_kind(self) -> None:
        with pytest.raises(ValidationError):
            ManifestEntry(
                path="/etc/hosts",
                kind="block",
                mode=0o644,
                size=128,
                mtime=1732000000.0,
            )

    def test_backup_manifest_round_trips_filetree(self) -> None:
        manifest = BackupManifest(
            schema_version=1,
            streams=[
                FiletreeStream(
                    type="filetree",
                    entries=[
                        ManifestEntry(
                            path="/etc/hosts",
                            kind="file",
                            mode=0o644,
                            size=128,
                            mtime=1732000000.0,
                            chunk_ids=["chunk-1"],
                        ),
                        ManifestEntry(
                            path="/etc",
                            kind="dir",
                            mode=0o755,
                            size=0,
                            mtime=1732000000.0,
                        ),
                    ],
                )
            ],
        )
        dumped = manifest.model_dump()
        rebuilt = BackupManifest(**dumped)
        assert rebuilt == manifest
        assert len(rebuilt.streams[0].entries) == 2
        assert rebuilt.streams[0].entries[0].chunk_ids == ["chunk-1"]
        assert rebuilt.streams[0].entries[1].kind == "dir"

    def test_filetree_stream_rejects_invalid_type(self) -> None:
        with pytest.raises(ValidationError):
            FiletreeStream(type="dirtree", entries=[])
