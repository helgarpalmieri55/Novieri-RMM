package backup

import (
	"context"
	"fmt"
	"strings"
)

// The VSS requester sequence (spec-server-backup.md §4.1), separated from the
// COM calls so the ORDER and the writer-check DECISIONS are unit-tested on any
// OS through a fake backend. snapshot_windows.go implements vssBackend over
// IVssBackupComponents; the failure branches cannot be provoked on a real
// runner, which is why they are proven here.
//
// CoInitializeEx(MTA) -> CreateVssBackupComponents -> InitializeForBackup ->
// SetContext(VSS_CTX_BACKUP) -> GatherWriterMetadata -> SetBackupState(false,
// false, VSS_BT_COPY, false) -> StartSnapshotSet -> AddToSnapshotSet per volume
// -> PrepareForBackup -> writer check 1 -> DoSnapshotSet -> writer check 2 ->
// read -> BackupComplete -> writer check 3 -> DeleteSnapshots(this set) ->
// release.

const (
	vssCtxBackup int32 = 0x00000000

	// VSS_BACKUP_TYPE. COPY does not touch any writer's own backup history:
	// SQL Server records it copy-only and keeps its differential base (D5).
	vssBTFull int32 = 1
	vssBTCopy int32 = 5
)

// Writer check points, as recorded in consistency.writers[].at.
const (
	writerAtPrepare  = "prepare_backup"
	writerAtSnapshot = "do_snapshot_set"
	writerAtComplete = "backup_complete"
)

// warnWriterFailed is a failed writer that fail_on_writer=false let through.
const warnWriterFailed = "writer_failed"

type vssGUID [16]byte

// writerStatus is one writer's answer to IVssBackupComponents::GetWriterStatus.
type writerStatus struct {
	name  string
	state int32
	hr    int32
}

// vssBackend is IVssBackupComponents as the sequence uses it.
type vssBackend interface {
	initializeForBackup() error
	setContext(ctx int32) error
	gatherWriterMetadata() error
	freeWriterMetadata()
	setBackupState(backupType int32) error
	startSnapshotSet() (vssGUID, error)
	addToSnapshotSet(volume string) (vssGUID, error)
	prepareForBackup() error
	doSnapshotSet() error
	// writerStatus is GatherWriterStatus (waited), GetWriterStatusCount,
	// GetWriterStatus per writer, SysFreeString per name, FreeWriterStatus.
	writerStatus() ([]writerStatus, error)
	snapshotDevicePath(id vssGUID) (string, error)
	backupComplete() error
	abortBackup()
	deleteSnapshotSet(id vssGUID) error
	release()
}

// VSS_WRITER_STATE names, indexed by value.
var vssWriterStateNames = [...]string{
	"VSS_WS_UNKNOWN",
	"VSS_WS_STABLE",
	"VSS_WS_WAITING_FOR_FREEZE",
	"VSS_WS_WAITING_FOR_THAW",
	"VSS_WS_WAITING_FOR_POST_SNAPSHOT",
	"VSS_WS_WAITING_FOR_BACKUP_COMPLETE",
	"VSS_WS_FAILED_AT_IDENTIFY",
	"VSS_WS_FAILED_AT_PREPARE_BACKUP",
	"VSS_WS_FAILED_AT_PREPARE_SNAPSHOT",
	"VSS_WS_FAILED_AT_FREEZE",
	"VSS_WS_FAILED_AT_THAW",
	"VSS_WS_FAILED_AT_POST_SNAPSHOT",
	"VSS_WS_FAILED_AT_BACKUP_COMPLETE",
	"VSS_WS_FAILED_AT_PRE_RESTORE",
	"VSS_WS_FAILED_AT_POST_RESTORE",
	"VSS_WS_FAILED_AT_BACKUPSHUTDOWN",
}

func writerStateName(s int32) string {
	if s >= 0 && int(s) < len(vssWriterStateNames) {
		return vssWriterStateNames[s]
	}
	return fmt.Sprintf("VSS_WS_%d", s)
}

// writerFailed is §4.1's rule: any VSS_WS_FAILED_AT_* state, or a failure
// HRESULT other than S_OK.
func writerFailed(w writerStatus) bool {
	return (w.state >= 6 && w.state <= 15) || w.hr != 0
}

func writerDocs(at string, ws []writerStatus) []writerDoc {
	out := make([]writerDoc, 0, len(ws))
	for _, w := range ws {
		out = append(out, writerDoc{
			Name: clean(w.name), State: writerStateName(w.state),
			HR: fmt.Sprintf("0x%08x", uint32(w.hr)), At: at,
		})
	}
	return out
}

// writerFailure fails a run at check 1 or 2. Its writers go into the failed
// result's consistency document.
type writerFailure struct {
	at      string
	failed  []writerStatus
	writers []writerDoc
}

func (e *writerFailure) Error() string {
	parts := make([]string, 0, len(e.failed))
	for _, w := range e.failed {
		parts = append(parts, fmt.Sprintf("%s is %s (0x%08x)", clean(w.name), writerStateName(w.state),
			uint32(w.hr)))
	}
	return fmt.Sprintf("VSS writer failure at %s, so the snapshot is not writer-consistent: %s",
		e.at, strings.Join(parts, "; "))
}

type vssStage int

const (
	// Nothing to undo but releasing the components object.
	vssStageCreated vssStage = iota
	// StartSnapshotSet succeeded; the set must be aborted and deleted.
	vssStageSetStarted
	// DoSnapshotSet succeeded; BackupComplete, check 3 and delete follow.
	vssStageSnapshotted
)

// vssSet is one snapshot set and what it knows about its writers.
type vssSet struct {
	b            vssBackend
	setID        vssGUID
	stage        vssStage
	devices      map[string]string
	failOnWriter bool
	writers      []writerDoc
	warnings     []string
}

// check runs one writer check. Before the read (1, 2) a failed writer fails
// the job; after it (3) the data was captured while every writer was stable,
// so the point stands with a warning.
func (s *vssSet) check(at string, beforeRead bool) error {
	ws, err := s.b.writerStatus()
	if err != nil {
		if beforeRead {
			return fmt.Errorf("VSS writer status could not be read at %s: %w", at, err)
		}
		s.warnings = appendWarn(s.warnings, warnWriterPostBackup)
		return nil
	}
	s.writers = append(s.writers, writerDocs(at, ws)...)
	var failed []writerStatus
	for _, w := range ws {
		if writerFailed(w) {
			failed = append(failed, w)
		}
	}
	if len(failed) == 0 {
		return nil
	}
	if !beforeRead {
		s.warnings = appendWarn(s.warnings, warnWriterPostBackup)
		return nil
	}
	if !s.failOnWriter {
		s.warnings = appendWarn(s.warnings, warnWriterFailed)
		return nil
	}
	return &writerFailure{at: at, failed: failed, writers: append([]writerDoc(nil), s.writers...)}
}

// finish tears the set down according to how far it got, and reports the
// first failure without stopping at it.
func (s *vssSet) finish() error {
	var first error
	note := func(err error) {
		if err != nil && first == nil {
			first = err
		}
	}
	switch s.stage {
	case vssStageSnapshotted:
		note(s.b.backupComplete())
		note(s.check(writerAtComplete, false))
		note(s.b.deleteSnapshotSet(s.setID))
	case vssStageSetStarted:
		s.b.abortBackup()
		if s.setID != (vssGUID{}) {
			_ = s.b.deleteSnapshotSet(s.setID)
		}
	case vssStageCreated:
	}
	s.b.release()
	return first
}

// takeSnapshotSet drives the sequence up to a readable shadow copy per volume.
// On any failure everything created so far is torn down before it returns.
func takeSnapshotSet(ctx context.Context, b vssBackend, volumes []string, backupType int32,
	failOnWriter bool) (*vssSet, error) {
	s := &vssSet{b: b, stage: vssStageCreated, devices: make(map[string]string, len(volumes)),
		failOnWriter: failOnWriter}
	fail := func(err error) (*vssSet, error) {
		if ferr := s.finish(); ferr != nil {
			return nil, fmt.Errorf("%w (and the teardown that followed also failed: %v)", err, ferr)
		}
		return nil, err
	}
	phase := func(name string, fn func() error) error {
		if err := ctx.Err(); err != nil {
			return fmt.Errorf("the job's deadline passed before %s: %w", name, err)
		}
		return fn()
	}

	if err := phase("InitializeForBackup", b.initializeForBackup); err != nil {
		return fail(err)
	}
	if err := phase("SetContext", func() error { return b.setContext(vssCtxBackup) }); err != nil {
		return fail(err)
	}
	if err := phase("GatherWriterMetadata", b.gatherWriterMetadata); err != nil {
		return fail(err)
	}
	b.freeWriterMetadata()
	// Before StartSnapshotSet, in MSDN's requester order: the backup type is
	// part of what writers are told at PrepareForBackup.
	if err := phase("SetBackupState", func() error { return b.setBackupState(backupType) }); err != nil {
		return fail(err)
	}
	setID, err := b.startSnapshotSet()
	if err != nil {
		return fail(err)
	}
	s.setID, s.stage = setID, vssStageSetStarted

	snapIDs := make(map[string]vssGUID, len(volumes))
	for _, volume := range volumes {
		if err := ctx.Err(); err != nil {
			return fail(fmt.Errorf("the job's deadline passed while adding volumes to the "+
				"snapshot set: %w", err))
		}
		snapID, err := b.addToSnapshotSet(volume)
		if err != nil {
			return fail(err)
		}
		snapIDs[strings.ToLower(volume)] = snapID
	}
	if err := phase("PrepareForBackup", b.prepareForBackup); err != nil {
		return fail(err)
	}
	if err := s.check(writerAtPrepare, true); err != nil {
		return fail(err)
	}
	if err := phase("DoSnapshotSet", b.doSnapshotSet); err != nil {
		return fail(err)
	}
	s.stage = vssStageSnapshotted
	if err := s.check(writerAtSnapshot, true); err != nil {
		// Abort, never BackupComplete: nothing was backed up.
		s.stage = vssStageSetStarted
		return fail(err)
	}
	for volume, snapID := range snapIDs {
		device, err := b.snapshotDevicePath(snapID)
		if err != nil {
			return fail(err)
		}
		s.devices[volume] = device
	}
	return s, nil
}

func vssBackupType(t string) int32 {
	if t == vssTypeFull {
		return vssBTFull
	}
	return vssBTCopy
}
