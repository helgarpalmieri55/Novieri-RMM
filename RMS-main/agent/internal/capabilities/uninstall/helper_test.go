package uninstall

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

type fakeSystem struct {
	calls   []string
	purged  bool
	stopErr error
}

func (f *fakeSystem) StopService(_ context.Context, name string) error {
	f.calls = append(f.calls, "stop "+name)
	return f.stopErr
}
func (f *fakeSystem) DeleteService(_ context.Context, name string) error {
	f.calls = append(f.calls, "delete "+name)
	return nil
}
func (f *fakeSystem) PurgePackage(context.Context) (bool, error) {
	f.calls = append(f.calls, "purge")
	return f.purged, nil
}
func (f *fakeSystem) ScheduleDelete(path string) error {
	f.calls = append(f.calls, "schedule "+filepath.Base(path))
	return nil
}
func (f *fakeSystem) HelperExiting(context.Context) { f.calls = append(f.calls, "exiting") }

// layout builds an installed agent in a temp dir: a binary, a data directory
// holding every credential plus one other file, and one extra path to remove.
func layout(t *testing.T) Targets {
	t.Helper()
	root := t.TempDir()
	data := filepath.Join(root, "data")
	if err := os.MkdirAll(filepath.Join(data, "update"), 0o700); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"enroll-token", "agent.key", "agent.key.pending", "previous.json", "identity.json"} {
		if err := os.WriteFile(filepath.Join(data, name), []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	bin := filepath.Join(root, "bin", "its-agent")
	extra := filepath.Join(root, "etc-its-agent")
	for _, dir := range []string{filepath.Dir(bin), extra} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(bin, []byte("elf"), 0o755); err != nil {
		t.Fatal(err)
	}
	return Targets{
		ServiceName:   "its-agent",
		Binary:        bin,
		DataDir:       data,
		Credentials:   []string{"enroll-token", "agent.key", "agent.key.pending", "previous.json"},
		Remove:        []string{extra},
		RemoveIfEmpty: []string{filepath.Dir(bin)},
	}
}

func gone(t *testing.T, paths ...string) {
	t.Helper()
	for _, p := range paths {
		if _, err := os.Stat(p); !os.IsNotExist(err) {
			t.Errorf("%s survived the uninstall (%v)", p, err)
		}
	}
}

func TestTheHelperWaitsThenStopsThenRemovesEverything(t *testing.T) {
	tg := layout(t)
	sys := &fakeSystem{}
	var waited []time.Duration
	err := RunHelper(context.Background(), sys, tg, tg.Binary, func(d time.Duration) { waited = append(waited, d) })
	if err != nil {
		t.Fatalf("RunHelper: %v", err)
	}
	if len(waited) == 0 || waited[0] != HelperDelay {
		t.Fatalf("the first wait was %v, want %v: the job result must reach the gateway "+
			"before the service stops", waited, HelperDelay)
	}
	if want := []string{"stop its-agent", "purge", "delete its-agent", "exiting"}; !reflect.DeepEqual(sys.calls, want) {
		t.Fatalf("calls = %v, want %v", sys.calls, want)
	}
	gone(t, tg.DataDir, tg.Binary, tg.Remove[0], tg.RemoveIfEmpty[0])
}

func TestAPackageManagedAgentIsPurgedNotDeletedFileByFile(t *testing.T) {
	// dpkg owns /usr/bin/its-agent and the unit: deleting them by hand leaves
	// a package database that says they exist.
	tg := layout(t)
	sys := &fakeSystem{purged: true}
	if err := RunHelper(context.Background(), sys, tg, tg.Binary, func(time.Duration) {}); err != nil {
		t.Fatal(err)
	}
	for _, c := range sys.calls {
		if strings.HasPrefix(c, "delete") {
			t.Fatalf("a purged package's service was deleted again: %v", sys.calls)
		}
	}
	// The data directory is not the package's, and still goes.
	gone(t, tg.DataDir)
}

func TestAStopFailureDoesNotLeaveTheCredentialsBehind(t *testing.T) {
	tg := layout(t)
	sys := &fakeSystem{stopErr: errors.New("service did not stop")}
	err := RunHelper(context.Background(), sys, tg, tg.Binary, func(time.Duration) {})
	if err == nil || !strings.Contains(err.Error(), "service did not stop") {
		t.Fatalf("err = %v; the stop failure must be reported", err)
	}
	for _, name := range tg.Credentials {
		gone(t, filepath.Join(tg.DataDir, name))
	}
}

func TestAHelperRunningFromACopyAsksForItsOwnDeletionAtBoot(t *testing.T) {
	// Windows: the helper runs from a copy in the temp directory, because a
	// process cannot delete its own image. The copy goes at the next boot.
	tg := layout(t)
	sys := &fakeSystem{}
	self := filepath.Join(t.TempDir(), "its-agent-uninstall-1.exe")
	if err := RunHelper(context.Background(), sys, tg, self, func(time.Duration) {}); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(sys.calls[len(sys.calls)-2:], []string{"schedule its-agent-uninstall-1.exe", "exiting"}) {
		t.Fatalf("calls = %v", sys.calls)
	}
}

func TestTheHelperRefusesToRunOnTargetsThatAreNotAnAgent(t *testing.T) {
	sys := &fakeSystem{}
	err := RunHelper(context.Background(), sys, Targets{DataDir: "/", Binary: "/bin/sh"}, "/bin/sh", func(time.Duration) {})
	// Nothing is touched -- but the helper still releases itself (review M2):
	// on macOS it is a `launchctl submit` job, which launchd restarts every
	// time it exits until it is removed, so an early refusal that skipped
	// HelperExiting would be a helper respawning forever.
	if err == nil || !reflect.DeepEqual(sys.calls, []string{"exiting"}) {
		t.Fatalf("err = %v, calls = %v; nothing may be touched, and the helper must still release itself", err, sys.calls)
	}
}

// Review M3. The helper deletes RECURSIVELY, and the data directory comes from
// the service's environment. If that directory is not an agent's -- no
// identity.json in it -- the recursive remove would take whatever someone
// pointed ITS_AGENT_DATA_DIR at. Only the named credentials go, and the
// helper says why it stopped there.
func TestAMisconfiguredDataDirectoryLosesOnlyItsCredentials(t *testing.T) {
	tg := layout(t)
	if err := os.Remove(filepath.Join(tg.DataDir, "identity.json")); err != nil {
		t.Fatal(err)
	}
	unrelated := filepath.Join(tg.DataDir, "quarterly-report.xlsx")
	if err := os.WriteFile(unrelated, []byte("someone's work"), 0o600); err != nil {
		t.Fatal(err)
	}
	sys := &fakeSystem{}
	err := RunHelper(context.Background(), sys, tg, tg.Binary, func(time.Duration) {})
	if err == nil || !strings.Contains(err.Error(), "identity.json") {
		t.Fatalf("err = %v; the helper must say it left the directory because it holds no identity.json", err)
	}
	if _, err := os.Stat(unrelated); err != nil {
		t.Fatalf("a file in a data directory that is not an agent's was removed: %v", err)
	}
	for _, name := range tg.Credentials {
		gone(t, filepath.Join(tg.DataDir, name))
	}
	if sys.calls[len(sys.calls)-1] != "exiting" {
		t.Fatalf("calls = %v", sys.calls)
	}
}
