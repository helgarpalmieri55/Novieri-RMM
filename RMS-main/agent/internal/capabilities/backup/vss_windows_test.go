//go:build windows

package backup

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"golang.org/x/sys/windows"
)

// Real Windows (spec-server-backup.md §12 W1, W3, W4): a real VSS snapshot of a
// temporary folder, the real writers' status at all three checks, a file whose
// DACL denies this process read through backup semantics, and its security
// descriptor restored. The first run of these on windows-latest is the first
// execution of snapshot_windows.go anywhere (R1).

func requireElevated(t *testing.T) {
	t.Helper()
	if runtime.GOARCH != "amd64" {
		t.Skipf("the VSS provider refuses windows/%s by design", runtime.GOARCH)
	}
	if !windows.GetCurrentProcessToken().IsElevated() {
		t.Skip("VSS and SeBackupPrivilege need an elevated (administrator) process, and this one " +
			"is not. GitHub's windows-latest runner is elevated, so CI runs this")
	}
}

// denyAll gives p a protected DACL granting access only to a SID nobody has,
// so an ordinary open of it fails even for an administrator.
func denyAll(t *testing.T, p string) {
	t.Helper()
	sd, err := windows.SecurityDescriptorFromString("D:P(A;;FA;;;S-1-5-21-1-2-3-4)")
	if err != nil {
		t.Fatal(err)
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		t.Fatal(err)
	}
	if err := windows.SetNamedSecurityInfo(p, windows.SE_FILE_OBJECT,
		windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION,
		nil, nil, dacl, nil); err != nil {
		t.Fatal(err)
	}
}

// ordinaryRead opens p for reading the way an ordinary program does: WITHOUT
// FILE_FLAG_BACKUP_SEMANTICS. os.ReadFile is not that -- Go adds the flag to
// every read-only open so directories can be opened -- and with the flag, any
// process whose token has SeBackupPrivilege enabled (an elevated runner, or
// this package while a job holds it) reads straight past the DACL, which is
// how this check passed a file it should have failed.
func ordinaryRead(p string) error {
	u, err := windows.UTF16PtrFromString(p)
	if err != nil {
		return err
	}
	h, err := windows.CreateFile(u, windows.GENERIC_READ, windows.FILE_SHARE_READ, nil,
		windows.OPEN_EXISTING, windows.FILE_ATTRIBUTE_NORMAL, 0)
	if err != nil {
		return err
	}
	return windows.CloseHandle(h)
}

func sddlPart(sddl, tag string) string {
	i := strings.Index(sddl, tag)
	if i < 0 {
		return ""
	}
	rest := sddl[i:]
	for _, next := range []string{"O:", "G:", "D:", "S:"} {
		if next == tag {
			continue
		}
		if j := strings.Index(rest, next); j > 0 {
			rest = rest[:j]
		}
	}
	return rest
}

func readWithBackupSemantics(t *testing.T, p string) []byte {
	t.Helper()
	release, missing := enablePrivileges(privBackup)
	defer release()
	if len(missing) != 0 {
		t.Fatalf("this elevated process does not hold %v", missing)
	}
	f, err := openForBackup(p)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close() //nolint:errcheck
	b, err := io.ReadAll(f)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestARealVSSSnapshotOfATempFolderRoundTripsWithWritersAndACLs(t *testing.T) {
	requireElevated(t)
	src := filepath.Join(t.TempDir(), "vss-src")
	if err := os.MkdirAll(filepath.Join(src, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	big := make([]byte, 300_000)
	fill(big, 11)
	files := map[string][]byte{
		"plain.txt":      []byte("hello from a shadow copy\r\n"),
		"sub/big.bin":    big,
		"sub/locked.txt": []byte("a DACL that excludes this process must not stop a backup"),
	}
	for rel, b := range files {
		if err := os.WriteFile(filepath.Join(src, filepath.FromSlash(rel)), b, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	locked := filepath.Join(src, "sub", "locked.txt")
	denyAll(t, locked)
	if err := ordinaryRead(locked); !errors.Is(err, windows.ERROR_ACCESS_DENIED) {
		t.Fatalf("the fixture's DACL does not deny an ordinary read (%v); W3 would prove nothing", err)
	}
	origSD, err := readSDDL(locked, false, false)
	if err != nil {
		t.Fatal(err)
	}

	store := newFakeStore(testStorageRef)
	svc := newTestService(store, quiet())
	svc.opts.OpenSnapshot = PlatformSnapshot
	var doc map[string]any
	_ = json.Unmarshal(v2RunParams(t, []string{src}, nil), &doc)
	doc["snapshot"] = snapshotVSS
	raw, _ := json.Marshal(doc)
	o, err := svc.execRun(context.Background(), testJobID, raw)
	if err != nil {
		t.Fatalf("backup.run refused: %v", err)
	}
	if o.res.State != stateSuccess {
		t.Fatalf("backup.run failed: %s", deref(o.res.Error))
	}
	if n, _ := o.res.DedupStats["unreadable_files"].(int64); n != 0 {
		t.Fatalf("W3: %d unreadable files; the denied file was not read through backup semantics", n)
	}

	w := o.wire().(*runResultV2)
	c := w.Consistency
	if c.Snapshot != mechVSS || c.Level != levelSnapshot || !c.CrashConsistent {
		t.Fatalf("consistency = %+v", c)
	}
	at := map[string]int{}
	for _, wd := range c.Writers {
		at[wd.At]++
		t.Logf("writer %-45s %-40s %s at %s", wd.Name, wd.State, wd.HR, wd.At)
	}
	if at[writerAtPrepare] == 0 || at[writerAtSnapshot] == 0 || at[writerAtComplete] == 0 {
		t.Fatalf("W1: writers were not read at all three checks: %v", at)
	}
	t.Logf("W1: %d writer statuses over three checks; warnings %v", len(c.Writers), w.Warnings)

	// W4: the security descriptor travels and comes back.
	_, m := readManifestDoc(t, store, *o.res.ManifestRef)
	var sd string
	for _, e := range v2Entries(t, store, m) {
		if strings.HasSuffix(e.Path, "/locked.txt") && e.SD != nil {
			sd = *e.SD
		}
	}
	if sd == "" {
		t.Fatal("W4: the manifest carries no security descriptor for the denied file")
	}

	dest := filepath.Join(t.TempDir(), "vss-restored")
	ro, err := svc.execRestore(context.Background(), testJobID, v2RestoreParams(t,
		store.prefix+*o.res.ManifestRef, []string{src}, dest, modeRestore, nil))
	if err != nil || ro.res.State != stateSuccess || len(ro.res.Mismatch) != 0 {
		t.Fatalf("restore: %v %+v", err, ro.res)
	}
	out := expectedDest(dest, src)
	for rel, want := range files {
		p := filepath.Join(out, filepath.FromSlash(rel))
		if got := readWithBackupSemantics(t, p); !bytes.Equal(got, want) {
			t.Errorf("%s: %d bytes restored, %d backed up", rel, len(got), len(want))
		}
	}
	gotSD, err := readSDDL(filepath.Join(out, "sub", "locked.txt"), false, false)
	if err != nil {
		t.Fatal(err)
	}
	for _, part := range []string{"O:", "D:"} {
		if sddlPart(gotSD, part) != sddlPart(origSD, part) {
			t.Errorf("W4: %s restored as %q, was %q", part, sddlPart(gotSD, part), sddlPart(origSD, part))
		}
	}
}

func TestTheBackupPrivilegeIsEnabledOnlyForTheJob(t *testing.T) {
	requireElevated(t)
	// Start from disabled, as a service token does.
	if _, err := setPrivilege(privBackup, false); err != nil {
		t.Fatal(err)
	}
	release, warn := acquireBackupPrivileges()
	if len(warn) != 0 {
		t.Fatalf("an elevated process could not enable SeBackupPrivilege: %v", warn)
	}
	if on, err := setPrivilege(privBackup, true); err != nil || !on {
		t.Fatalf("SeBackupPrivilege not enabled during the job (on=%v err=%v)", on, err)
	}
	release()
	if on, err := setPrivilege(privBackup, false); err != nil || on {
		t.Fatalf("SeBackupPrivilege still enabled after the job (on=%v err=%v)", on, err)
	}
}
