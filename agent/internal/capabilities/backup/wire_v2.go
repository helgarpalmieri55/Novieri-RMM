package backup

import (
	"encoding/json"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// Format 2 (docs/modules/backup/spec-server-backup.md §5).
//
// Every member here is OPTIONAL on the wire and lives in its own struct, not in
// runParams/runResult: those are pinned field-for-field to today's exported
// schemas by schema_test.go, and the server release that first sends
// `format: 2` is the one that adds these members to its models. An old server
// never sends them; an old agent ignores them. The result members are emitted
// only when the job said `format: 2`, because BackupRunResult is
// `extra="forbid"` and an old server would fail the whole job on them.

const (
	formatV1 = 1
	formatV2 = 2

	manifestSchemaV2 = 2

	snapshotAuto = "auto"

	workloadFiles = "files"

	// consistency.level
	levelSnapshot = "snapshot"
	levelLive     = "live"

	// consistency.snapshot: the mechanism that produced the view.
	mechVSS   = "vss"
	mechLVM   = "lvm"
	mechBtrfs = "btrfs"
	mechZFS   = "zfs"
	mechAPFS  = "apfs"
	mechNone  = "none"

	vssTypeCopy = "copy"
	vssTypeFull = "full"

	// entry flags
	flagChangedDuringRead = "changed_during_read"

	// conflict modes (target.conflict)
	conflictOverwrite = "overwrite"
	conflictRename    = "rename"
	conflictSkip      = "skip"
)

// Result warning codes. A fixed vocabulary so the console can translate them;
// never a path or a customer name.
const (
	warnNotSnapshotConsistent = "not_snapshot_consistent"
	warnChangedDuringRead     = "files_changed_during_read"
	warnWriterPostBackup      = "writer_post_backup_failed"
	warnSACLNotCaptured       = "sacl_not_captured"
	warnSecurityUnreadable    = "security_metadata_unreadable"
	warnSecurityNotApplied    = "security_metadata_not_applied"
	warnOwnershipNotApplied   = "ownership_not_applied"
	warnPrivilegeNotHeld      = "backup_privilege_not_held"
	warnReleaseFailed         = "snapshot_release_failed"
)

// runParamsV2 is the format-2 half of BackupRunParams (§5.1).
type runParamsV2 struct {
	Format        int           `json:"format"`
	Workload      *workloadSpec `json:"workload"`
	VSS           *vssSpec      `json:"vss"`
	NotAfterStart *string       `json:"not_after_start"`
}

type workloadSpec struct {
	ID     string          `json:"id"`
	Kind   string          `json:"kind"`
	Config json.RawMessage `json:"config"`
}

// filesConfig is the `files` workload's config (§4.1, §4.4). Unknown members
// are ignored here; the server validates with extra="forbid".
type filesConfig struct {
	Paths         []string `json:"paths"`
	Exclude       []string `json:"exclude"`
	OneFilesystem bool     `json:"one_filesystem"`
	// Nil means the operator default (2026-09-23): live read allowed.
	AllowLive *bool `json:"allow_live"`
}

type vssSpec struct {
	BackupType   string `json:"backup_type"`
	FailOnWriter *bool  `json:"fail_on_writer"`
}

// runJob is one validated backup.run: the pinned params plus the format-2
// reading of them.
type runJob struct {
	runParams
	format        int
	workloadID    *string
	oneFilesystem bool
	allowLive     bool
	vssType       string
	failOnWriter  bool
}

// runResultV2 is BackupRunResult plus §5.3's optional members.
type runResultV2 struct {
	runResult
	WorkloadID  *string         `json:"workload_id"`
	Consistency *consistencyDoc `json:"consistency"`
	Warnings    []string        `json:"warnings"`
}

// consistencyDoc is §5.3's `consistency`. Databases and non-transactional
// tables belong to phase 2 and are always empty here, but present, so the
// document has one shape.
type consistencyDoc struct {
	Level                  string        `json:"level"`
	CrashConsistent        bool          `json:"crash_consistent"`
	Snapshot               string        `json:"snapshot"`
	Writers                []writerDoc   `json:"writers"`
	FilesChangedDuringRead int64         `json:"files_changed_during_read"`
	NonTransactionalTables int64         `json:"non_transactional_tables"`
	Databases              []databaseDoc `json:"databases"`
}

// writerDoc is one VSS writer at one check. Writer names are Microsoft
// component names, not customer data (§4.1).
type writerDoc struct {
	Name  string `json:"name"`
	State string `json:"state"`
	HR    string `json:"hr"`
	At    string `json:"at"`
}

type databaseDoc struct {
	ID    string `json:"id"`
	State string `json:"state"`
	Bytes int64  `json:"bytes"`
}

// restoreParamsV2 is the format-2 half of BackupRestoreParams (§5.1).
type restoreParamsV2 struct {
	Format      int    `json:"format"`
	RestoreKind string `json:"restore_kind"`
}

type restoreJob struct {
	restoreParams
	format   int
	conflict string
}

// restoreResultV2 carries warnings for metadata that could not be applied
// (a SID or user that does not exist here, §7.1). Emitted only under format 2.
type restoreResultV2 struct {
	restoreResult
	Warnings []string `json:"warnings"`
}

// ---------------------------------------------------------------------------
// Manifest v2 (§5.2)
// ---------------------------------------------------------------------------

type manifestV2 struct {
	SchemaVersion int              `json:"schema_version"`
	Header        manifestHeaderV2 `json:"header"`
	Streams       []streamV2       `json:"streams"`
}

type manifestHeaderV2 struct {
	WorkloadKind string          `json:"workload_kind"`
	Consistency  *consistencyDoc `json:"consistency"`
	HostOS       string          `json:"host_os"`
	PageIndex    []pageIndexDoc  `json:"page_index"`
}

// pageIndexDoc lets a browse fetch only the pages under one directory: entries
// are in walk order, so a directory's subtree is one contiguous run of pages.
type pageIndexDoc struct {
	Stream  int    `json:"stream"`
	Page    int    `json:"page"`
	First   string `json:"first"`
	Last    string `json:"last"`
	Entries int    `json:"entries"`
}

// streamV2 covers both stream types. A reader skips a type it does not
// implement, as v1 does.
type streamV2 struct {
	Type       string          `json:"type"`
	Name       string          `json:"name"`
	EntryCount int64           `json:"entry_count"`
	Pages      []string        `json:"pages"`
	Format     string          `json:"format,omitempty"`
	Meta       json.RawMessage `json:"meta,omitempty"`
	Size       int64           `json:"size,omitempty"`
}

// entryV2 is a v2 filetree entry: a ManifestEntry plus optional security
// metadata. One NDJSON line of a page.
type entryV2 struct {
	manifestEntry
	// ChunkPages replaces ChunkIDs for a file whose id list would not fit in a
	// page: each names a page of newline-separated ids.
	ChunkPages []string          `json:"chunk_pages,omitempty"`
	SD         *string           `json:"sd,omitempty"`
	UID        *uint32           `json:"uid,omitempty"`
	GID        *uint32           `json:"gid,omitempty"`
	User       string            `json:"user,omitempty"`
	Group      string            `json:"group,omitempty"`
	Xattrs     map[string]string `json:"xattrs,omitempty"`
	Flags      []string          `json:"flags,omitempty"`
}

func (e *entryV2) hasFlag(f string) bool {
	for _, x := range e.Flags {
		if x == f {
			return true
		}
	}
	return false
}

func (e *entryV2) addFlag(f string) {
	if !e.hasFlag(f) {
		e.Flags = append(e.Flags, f)
	}
}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

func parseRunJob(raw json.RawMessage, now time.Time) (runJob, error) {
	p, err := parseRunParams(raw)
	if err != nil {
		return runJob{}, err
	}
	var v2 runParamsV2
	if err := json.Unmarshal(raw, &v2); err != nil {
		return runJob{}, jobs.Refuse("backup.run: params are not a BackupRunParams")
	}
	j := runJob{runParams: p, format: v2.Format, allowLive: true, vssType: vssTypeCopy, failOnWriter: true}
	switch j.format {
	case 0:
		j.format = formatV1
	case formatV1, formatV2:
	default:
		return runJob{}, jobs.Refuse("backup.run: params format %d is not one this agent implements "+
			"(1, 2)", v2.Format)
	}
	if v2.NotAfterStart != nil && *v2.NotAfterStart != "" {
		deadline, err := time.Parse(time.RFC3339, *v2.NotAfterStart)
		if err != nil {
			return runJob{}, jobs.Refuse("backup.run: not_after_start is not an RFC 3339 time")
		}
		if now.After(deadline) {
			return runJob{}, jobs.Refuse("backup.run: the job's start deadline (not_after_start) has " +
				"passed; the run was not started")
		}
	}
	if v2.Workload != nil {
		if v2.Workload.Kind != workloadFiles {
			return runJob{}, jobs.Refuse("backup.run: workload kind %q is not implemented by this "+
				"agent", clean(v2.Workload.Kind))
		}
		if v2.Workload.ID != "" {
			id := v2.Workload.ID
			j.workloadID = &id
		}
		var cfg filesConfig
		if len(v2.Workload.Config) > 0 && string(v2.Workload.Config) != "null" {
			if err := json.Unmarshal(v2.Workload.Config, &cfg); err != nil {
				return runJob{}, jobs.Refuse("backup.run: the files workload config is malformed")
			}
		}
		if len(cfg.Paths) > 0 {
			j.Sources.Paths = cfg.Paths
		}
		j.Sources.Exclude = append(j.Sources.Exclude, cfg.Exclude...)
		j.oneFilesystem = cfg.OneFilesystem
		if cfg.AllowLive != nil {
			j.allowLive = *cfg.AllowLive
		}
		if _, err := j.Sources.roots(); err != nil {
			return runJob{}, err
		}
	}
	if v2.VSS != nil && v2.VSS.FailOnWriter != nil {
		j.failOnWriter = *v2.VSS.FailOnWriter
	}
	if v2.VSS != nil && v2.VSS.BackupType != "" {
		switch v2.VSS.BackupType {
		case vssTypeCopy, vssTypeFull:
			j.vssType = v2.VSS.BackupType
		default:
			return runJob{}, jobs.Refuse("backup.run: vss.backup_type %q is not one of copy, full",
				clean(v2.VSS.BackupType))
		}
	}
	return j, nil
}

func parseRestoreJob(raw json.RawMessage) (restoreJob, error) {
	p, err := parseRestoreParams(raw)
	if err != nil {
		return restoreJob{}, err
	}
	var v2 restoreParamsV2
	if err := json.Unmarshal(raw, &v2); err != nil {
		return restoreJob{}, jobs.Refuse("backup.restore: params are not a BackupRestoreParams")
	}
	j := restoreJob{restoreParams: p, format: v2.Format}
	switch j.format {
	case 0:
		j.format = formatV1
	case formatV1, formatV2:
	default:
		return restoreJob{}, jobs.Refuse("backup.restore: params format %d is not one this agent "+
			"implements (1, 2)", v2.Format)
	}
	switch v2.RestoreKind {
	case "", workloadFiles:
	default:
		return restoreJob{}, jobs.Refuse("backup.restore: restore_kind %q is not implemented by "+
			"this agent", clean(v2.RestoreKind))
	}
	// Absent means overwrite: that is what a format-1 restore has always done,
	// and it is now safe because nothing is replaced until the new copy is
	// complete (restore.go).
	switch p.Target.Conflict {
	case "", conflictOverwrite:
		j.conflict = conflictOverwrite
	case conflictRename, conflictSkip:
		j.conflict = p.Target.Conflict
	default:
		return restoreJob{}, jobs.Refuse("backup.restore: target.conflict %q is not one of rename, "+
			"skip, overwrite", clean(p.Target.Conflict))
	}
	if p.Mode == modeVerify && j.conflict != conflictOverwrite {
		return restoreJob{}, jobs.Refuse("backup.restore: a verify writes into its own scratch " +
			"directory and takes no conflict mode")
	}
	return j, nil
}
