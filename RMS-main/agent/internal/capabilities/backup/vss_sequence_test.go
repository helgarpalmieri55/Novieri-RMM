package backup

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
)

// The VSS requester sequence and its writer-check decisions, through a fake
// backend. The real writers on a CI runner are all stable, so every failure
// branch below is reachable only here; vss_windows_test.go proves the statuses
// are really read from real writers.

type fakeVSS struct {
	calls []string
	// statuses[i] answers the i-th writerStatus call.
	statuses  [][]writerStatus
	statusErr map[int]error
	failAt    string
	statusN   int
}

func (f *fakeVSS) do(name string) error {
	f.calls = append(f.calls, name)
	if f.failAt == name {
		return fmt.Errorf("%s failed (0x80042301)", name)
	}
	return nil
}

func (f *fakeVSS) initializeForBackup() error  { return f.do("InitializeForBackup") }
func (f *fakeVSS) setContext(c int32) error    { return f.do(fmt.Sprintf("SetContext(%d)", c)) }
func (f *fakeVSS) gatherWriterMetadata() error { return f.do("GatherWriterMetadata") }
func (f *fakeVSS) freeWriterMetadata()         { _ = f.do("FreeWriterMetadata") }
func (f *fakeVSS) setBackupState(t int32) error {
	return f.do(fmt.Sprintf("SetBackupState(%d)", t))
}
func (f *fakeVSS) startSnapshotSet() (vssGUID, error) {
	return vssGUID{1}, f.do("StartSnapshotSet")
}
func (f *fakeVSS) addToSnapshotSet(v string) (vssGUID, error) {
	return vssGUID{2}, f.do("AddToSnapshotSet(" + v + ")")
}
func (f *fakeVSS) prepareForBackup() error { return f.do("PrepareForBackup") }
func (f *fakeVSS) doSnapshotSet() error    { return f.do("DoSnapshotSet") }
func (f *fakeVSS) writerStatus() ([]writerStatus, error) {
	n := f.statusN
	f.statusN++
	f.calls = append(f.calls, "WriterStatus")
	if err := f.statusErr[n]; err != nil {
		return nil, err
	}
	if n < len(f.statuses) {
		return f.statuses[n], nil
	}
	return []writerStatus{{name: "System Writer", state: 1}}, nil
}
func (f *fakeVSS) snapshotDevicePath(vssGUID) (string, error) {
	return `\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy9`, f.do("GetSnapshotProperties")
}
func (f *fakeVSS) backupComplete() error           { return f.do("BackupComplete") }
func (f *fakeVSS) abortBackup()                    { _ = f.do("AbortBackup") }
func (f *fakeVSS) deleteSnapshotSet(vssGUID) error { return f.do("DeleteSnapshots") }
func (f *fakeVSS) release()                        { _ = f.do("Release") }

func stable(names ...string) []writerStatus {
	out := make([]writerStatus, 0, len(names))
	for _, n := range names {
		out = append(out, writerStatus{name: n, state: 1})
	}
	return out
}

func TestTheVSSSequenceIsMSDNsRequesterOrderWithACopyBackup(t *testing.T) {
	// D5: SetBackupState(VSS_BT_COPY) BEFORE StartSnapshotSet, and three writer
	// checks: after PrepareForBackup, after DoSnapshotSet, after BackupComplete.
	f := &fakeVSS{}
	set, err := takeSnapshotSet(context.Background(), f, []string{`C:\`}, vssBackupType(vssTypeCopy), true)
	if err != nil {
		t.Fatal(err)
	}
	if err := set.finish(); err != nil {
		t.Fatal(err)
	}
	want := []string{
		"InitializeForBackup", "SetContext(0)", "GatherWriterMetadata", "FreeWriterMetadata",
		"SetBackupState(5)", "StartSnapshotSet", `AddToSnapshotSet(C:\)`, "PrepareForBackup",
		"WriterStatus", "DoSnapshotSet", "WriterStatus", "GetSnapshotProperties",
		"BackupComplete", "WriterStatus", "DeleteSnapshots", "Release",
	}
	if strings.Join(f.calls, " ") != strings.Join(want, " ") {
		t.Fatalf("calls:\n got %v\nwant %v", f.calls, want)
	}
	ats := map[string]int{}
	for _, w := range set.writers {
		ats[w.At]++
	}
	if ats[writerAtPrepare] != 1 || ats[writerAtSnapshot] != 1 || ats[writerAtComplete] != 1 {
		t.Errorf("writers recorded per check = %v", ats)
	}
	if len(set.warnings) != 0 {
		t.Errorf("warnings = %v", set.warnings)
	}
	if vssBackupType(vssTypeFull) != 1 || vssBackupType(vssTypeCopy) != 5 || vssBackupType("") != 5 {
		t.Error("VSS_BACKUP_TYPE mapping is wrong: FULL=1, COPY=5, default COPY")
	}
}

func TestAFailedWriterBeforeTheReadFailsTheJobAndAbortsTheSet(t *testing.T) {
	failed := writerStatus{name: "SqlServerWriter", state: 7, hr: int32(-2147212300)} // FAILED_AT_PREPARE_BACKUP
	for _, c := range []struct {
		name      string
		statuses  [][]writerStatus
		at        string
		snapshots bool
	}{
		{"check 1", [][]writerStatus{{failed}}, writerAtPrepare, false},
		{"check 2", [][]writerStatus{stable("SqlServerWriter"), {failed}}, writerAtSnapshot, true},
		{"stable state, failure HRESULT", [][]writerStatus{{{name: "Registry Writer", state: 1, hr: 5}}},
			writerAtPrepare, false},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := &fakeVSS{statuses: c.statuses}
			_, err := takeSnapshotSet(context.Background(), f, []string{`D:\`}, vssBTCopy, true)
			var wf *writerFailure
			if !errors.As(err, &wf) {
				t.Fatalf("err = %v (%T), want a *writerFailure", err, err)
			}
			if wf.at != c.at || len(wf.writers) == 0 {
				t.Errorf("failure at %q with %d writers", wf.at, len(wf.writers))
			}
			msg := err.Error()
			if !strings.Contains(msg, wf.failed[0].name) || !strings.Contains(msg, "0x") {
				t.Errorf("the error does not name the writer and its HRESULT: %q", msg)
			}
			calls := strings.Join(f.calls, " ")
			if !strings.Contains(calls, "AbortBackup DeleteSnapshots Release") {
				t.Errorf("the set was not aborted and deleted: %s", calls)
			}
			if strings.Contains(calls, "BackupComplete") {
				t.Errorf("BackupComplete ran for a backup that captured nothing: %s", calls)
			}
			if strings.Contains(calls, "DoSnapshotSet") != c.snapshots {
				t.Errorf("DoSnapshotSet ran = %v, want %v: %s", !c.snapshots, c.snapshots, calls)
			}
		})
	}
}

func TestAFailedWriterAfterTheReadKeepsThePointWithAWarning(t *testing.T) {
	f := &fakeVSS{statuses: [][]writerStatus{
		stable("A"), stable("A"), {{name: "A", state: 12, hr: 1}}, // FAILED_AT_BACKUP_COMPLETE
	}}
	set, err := takeSnapshotSet(context.Background(), f, []string{`C:\`}, vssBTCopy, true)
	if err != nil {
		t.Fatal(err)
	}
	if err := set.finish(); err != nil {
		t.Fatalf("a failure at check 3 failed the teardown: %v", err)
	}
	if len(set.warnings) != 1 || set.warnings[0] != warnWriterPostBackup {
		t.Errorf("warnings = %v", set.warnings)
	}
	last := set.writers[len(set.writers)-1]
	if last.State != "VSS_WS_FAILED_AT_BACKUP_COMPLETE" || last.At != writerAtComplete || last.HR != "0x00000001" {
		t.Errorf("check 3 recorded as %+v", last)
	}
}

func TestFailOnWriterFalseLetsTheRunContinueWithAWarning(t *testing.T) {
	f := &fakeVSS{statuses: [][]writerStatus{{{name: "A", state: 9}}}}
	set, err := takeSnapshotSet(context.Background(), f, []string{`C:\`}, vssBTCopy, false)
	if err != nil {
		t.Fatalf("fail_on_writer=false still failed: %v", err)
	}
	if len(set.warnings) != 1 || set.warnings[0] != warnWriterFailed {
		t.Errorf("warnings = %v", set.warnings)
	}
}

func TestUnreadableWriterStatusBeforeTheReadIsFatal(t *testing.T) {
	f := &fakeVSS{statusErr: map[int]error{0: errors.New("GatherWriterStatus failed (0x80042318)")}}
	_, err := takeSnapshotSet(context.Background(), f, []string{`C:\`}, vssBTCopy, true)
	if err == nil || !strings.Contains(err.Error(), "writer status could not be read") {
		t.Fatalf("err = %v", err)
	}
}

func TestAFailingCOMCallTearsDownWhatExists(t *testing.T) {
	f := &fakeVSS{failAt: "DoSnapshotSet"}
	if _, err := takeSnapshotSet(context.Background(), f, []string{`C:\`}, vssBTCopy, true); err == nil {
		t.Fatal("no error")
	}
	if !strings.HasSuffix(strings.Join(f.calls, " "), "DoSnapshotSet AbortBackup DeleteSnapshots Release") {
		t.Errorf("calls = %v", f.calls)
	}
	f = &fakeVSS{failAt: "GatherWriterMetadata"}
	_, _ = takeSnapshotSet(context.Background(), f, []string{`C:\`}, vssBTCopy, true)
	if strings.Contains(strings.Join(f.calls, " "), "AbortBackup") {
		t.Errorf("aborted a set that was never started: %v", f.calls)
	}
}

func TestWriterStateNamesCoverTheEnum(t *testing.T) {
	if writerStateName(1) != "VSS_WS_STABLE" || writerStateName(15) != "VSS_WS_FAILED_AT_BACKUPSHUTDOWN" {
		t.Error("state names are off by one")
	}
	if writerStateName(99) != "VSS_WS_99" {
		t.Error("an unknown state is not named by number")
	}
	for s := int32(0); s <= 5; s++ {
		if writerFailed(writerStatus{state: s}) {
			t.Errorf("state %d counted as failed", s)
		}
	}
	for s := int32(6); s <= 15; s++ {
		if !writerFailed(writerStatus{state: s}) {
			t.Errorf("state %d not counted as failed", s)
		}
	}
}

func TestAWriterFailureBecomesAFailedResultCarryingTheWriters(t *testing.T) {
	// Not a refusal: a writer timing out is this run's condition, and a
	// refusal would leave backup_runs stuck on running.
	src := t.TempDir()
	store := newFakeStore(testStorageRef)
	svc, _ := newTestServiceWithDedup(store, quiet())
	svc.opts.OpenSnapshot = func(context.Context, string, []string) (Snapshot, error) {
		wf := &writerFailure{at: writerAtPrepare, failed: []writerStatus{{name: "SqlServerWriter", state: 7}},
			writers: writerDocs(writerAtPrepare, []writerStatus{{name: "SqlServerWriter", state: 7}})}
		return nil, snapFailuref("the Volume Shadow Copy Service did not produce a snapshot: %w", wf)
	}
	o, err := svc.execRun(context.Background(), testJobID, v2RunParams(t, []string{src}, nil))
	if err != nil {
		t.Fatalf("a writer failure was returned as an error: %v", err)
	}
	if o.res.State != stateFailed || o.res.Error == nil || !strings.Contains(*o.res.Error, "SqlServerWriter") {
		t.Fatalf("result = %+v", o.res)
	}
	doc := o.wire().(*runResultV2)
	if doc.Consistency == nil || len(doc.Consistency.Writers) != 1 ||
		doc.Consistency.Writers[0].State != "VSS_WS_FAILED_AT_PREPARE_BACKUP" {
		t.Fatalf("consistency = %+v", doc.Consistency)
	}
}
