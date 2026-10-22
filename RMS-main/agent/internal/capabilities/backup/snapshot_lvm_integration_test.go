//go:build !windows

package backup

// Built everywhere but Windows; the test itself skips at runtime when the
// tooling or privilege it needs is absent, per the note atop
// snapshot_lvm_test.go about verifyPresents doing a real os.Open outside the
// injected command seam. The fixture here is a real mounted directory, not a
// fictional path string, so that trap does not apply.

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// testVGPrefix namespaces every volume group this test creates. The pid
// suffix means two concurrent/interrupted runs can never collide on a name;
// the shared prefix is what lets a later run recognise and reclaim a
// leftover from an interrupted earlier one -- same discipline as
// sweepLVMOrphans in snapshot_lvm.go, which matches snapTagPrefix by
// strings.HasPrefix precisely so it can never remove a volume it did not
// create.
const testVGPrefix = "toeshee-test-vg-"

// TestARealLVMSnapshotIsTakenAndReleased builds a loopback volume group, takes
// a real snapshot through openLVM, and verifies it presents THE TREE THE ROOT
// NAMES -- not merely that it presents something.
//
// # Why the root is a strict subdirectory of the mount point
//
// This is C1's closure. The earlier version of this test backed up the mount
// point itself, where the snapshot's mount root and the root's translation are
// the same path, so it passed identically whether the offset between the root
// and its filesystem was honoured or discarded. Every guard and fixture in
// this cycle checked EXISTENCE and NON-EMPTINESS; none checked CORRESPONDENCE,
// and a snapshot that presents the whole filesystem under the name of one
// subdirectory is real, readable and populated.
//
// So: back up <mnt>/alice/docs, put a canary at a known path RELATIVE to it,
// read that canary back at the same relative path through Translate, and
// assert that <mnt>/bob -- a sibling outside the root, on the same snapshotted
// filesystem -- does not appear anywhere in the translated tree.
//
// It SKIPS LOUDLY when the tooling is absent -- which it is in the standard
// development container. A skip that reads as a pass is how a gap survives.
func TestARealLVMSnapshotIsTakenAndReleased(t *testing.T) {
	for _, tool := range []string{
		"lvcreate", "lvs", "lvremove", "pvcreate", "vgcreate", "vgs",
		"losetup", "mount", "umount", "findmnt", "mkfs.ext4",
	} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skipf("SKIPPING the only end-to-end LVM proof: %q is not installed. "+
				"Nothing in this run exercised a real %s.", tool, tool)
		}
	}
	if os.Geteuid() != 0 {
		t.Skip("SKIPPING the only end-to-end LVM proof: not root, so no loopback volume group can be built.")
	}
	if err := dmCommunicable(); err != nil {
		t.Skipf("SKIPPING the only end-to-end LVM proof: device-mapper is not usable in this kernel (%v). "+
			"lvm2 is installed, but nothing in this run exercised a real device-mapper snapshot.", err)
	}
	dir := t.TempDir()
	img := filepath.Join(dir, "pv.img")
	mustRun(t, "truncate", "-s", "256M", img)

	loop := strings.TrimSpace(mustOut(t, "losetup", "--find", "--show", img))
	t.Cleanup(func() { _ = exec.Command("losetup", "-d", loop).Run() })

	// A prior run that was interrupted before its own vgremove cleanup ran
	// would otherwise leave a same-named VG behind and turn this run's
	// vgcreate into a confusing name collision instead of a clean pass or a
	// clean skip. Sweep any leftover carrying the shared prefix -- and only
	// the shared prefix -- before creating this run's own.
	sweepStaleTestVGs(t)

	vg := testVGPrefix + strconv.Itoa(os.Getpid())
	mustRun(t, "pvcreate", "-f", loop)
	mustRun(t, "vgcreate", vg, loop)
	t.Cleanup(func() { _ = exec.Command("vgremove", "-f", vg).Run() })

	mustRun(t, "lvcreate", "-n", "origin", "-L", "64M", vg)
	origin := "/dev/" + vg + "/origin"
	mustRun(t, "mkfs.ext4", "-q", origin)

	mnt := filepath.Join(dir, "mnt")
	if err := os.MkdirAll(mnt, 0o755); err != nil {
		t.Fatal(err)
	}
	mustRun(t, "mount", origin, mnt)
	t.Cleanup(func() { _ = exec.Command("umount", mnt).Run() })

	// The job's root: a STRICT SUBDIRECTORY of the mount point.
	root := filepath.Join(mnt, "alice", "docs")
	// The canary's path RELATIVE to that root, one level down so a translation
	// that lands on the wrong tree cannot coincidentally satisfy it.
	const canaryRel = "reports/canary.txt"
	canary := filepath.Join(root, filepath.FromSlash(canaryRel))
	if err := os.MkdirAll(filepath.Dir(canary), 0o755); err != nil {
		t.Fatal(err)
	}
	want := []byte("payload")
	if err := os.WriteFile(canary, want, 0o600); err != nil {
		t.Fatal(err)
	}

	// A sibling directory OUTSIDE the root but on the same snapshotted
	// filesystem. It is the negative half: if the root translates to the
	// snapshot's mount root, this appears in the walk and the job backs up
	// data no policy named.
	const siblingName = "bob"
	if err := os.MkdirAll(filepath.Join(mnt, siblingName), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(mnt, siblingName, "not-in-this-backup.txt"),
		[]byte("outside"), 0o600); err != nil {
		t.Fatal(err)
	}

	snap, err := openLVM(context.Background(), execSnapRunner, "itest", []string{root})
	if err != nil {
		t.Fatalf("openLVM against a real volume group = %v, want nil", err)
	}
	t.Cleanup(func() { _ = snap.Release() })

	translated := snap.Translate(root)
	if base := filepath.Base(translated); base != "docs" {
		t.Errorf("Translate(root) = %q; the last element is %q, want \"docs\". A root that is not itself "+
			"a mount point must keep its offset inside the snapshot", translated, base)
	}

	got, err := os.ReadFile(snap.Translate(canary))
	if err != nil {
		t.Fatalf("reading the canary at %q through the snapshot: %v", canaryRel, err)
	}
	if string(got) != string(want) {
		t.Fatalf("through the snapshot = %q, want %q", got, want)
	}
	// The canary must be found at THE SAME RELATIVE PATH, which is the
	// correspondence assertion: translated + canaryRel, not merely "somewhere".
	if snap.Translate(canary) != filepath.Join(translated, filepath.FromSlash(canaryRel)) {
		t.Fatalf("Translate(canary) = %q, want %q -- the canary must appear at its own relative path "+
			"inside the snapshot", snap.Translate(canary), filepath.Join(translated, filepath.FromSlash(canaryRel)))
	}

	// The negative: nothing outside the root may appear in the walk.
	err = filepath.WalkDir(translated, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.Name() == siblingName {
			rel, _ := filepath.Rel(translated, p)
			return fmt.Errorf("the sibling directory %q, which is outside the backed-up root, appears "+
				"inside the translated tree at %q: the snapshot is presenting the whole filesystem "+
				"under the name of one subdirectory", siblingName, rel)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

// sweepStaleTestVGs removes any volume group left behind by a prior run of
// this test that was interrupted before its own t.Cleanup ran. It matches
// ONLY the exact testVGPrefix -- never a substring, never everything -- the
// same discipline sweepLVMOrphans applies to snapTagPrefix in
// snapshot_lvm.go, so this can never remove a volume group it did not
// create itself.
//
// Best-effort, and the consequence of that is a LEAK, not a loud failure.
// A leftover found here already failed to clean up after itself once; if
// removing it fails too -- for instance because a crash left its origin LV
// still mounted -- the stale volume group, and any loopback device still
// backing it, are silently orphaned. Nothing reports them.
//
// It does NOT cause the vgcreate below to collide. That name carries THIS
// process's pid, and a leftover carries the pid of the run that crashed, so
// the two names differ and vgcreate succeeds. A genuine collision needs the
// OS to have recycled that exact pid between runs, which is possible but not
// the ordinary case.
//
// So a developer whose run is killed part-way loses a volume group until they
// remove it by hand. That is the accepted cost of not running a destructive
// vgremove against a name this test did not create.
func sweepStaleTestVGs(t *testing.T) {
	t.Helper()
	out := mustOut(t, "vgs", "--noheadings", "-o", "vg_name")
	for _, line := range strings.Split(out, "\n") {
		name := strings.TrimSpace(line)
		if name == "" || !strings.HasPrefix(name, testVGPrefix) {
			continue
		}
		_ = exec.Command("vgremove", "-f", name).Run()
	}
}

// dmCommunicable reports whether the kernel's device-mapper driver is
// actually reachable, by attempting the same interaction lvcreate itself
// performs: opening /dev/mapper/control. lvm2 can be fully installed -- the
// binaries the caller already checked for -- while the container's kernel
// has no device-mapper support at all, in which case that open fails with
// ENODEV ("no such device"), not a missing-file or permission error. Checking
// this directly, instead of pattern-matching lvcreate's stderr text, is the
// honest signal: the file existing (it does, even here) says nothing about
// whether the kernel behind it can do the work.
func dmCommunicable() error {
	f, err := os.OpenFile("/dev/mapper/control", os.O_RDONLY, 0)
	if err != nil {
		return err
	}
	return f.Close()
}

// mustRun runs name with args and fails the test, including combined output,
// on a non-zero exit.
func mustRun(t *testing.T, name string, args ...string) {
	t.Helper()
	cmd := exec.Command(name, args...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("%s %s: %v\n%s", name, strings.Join(args, " "), err, out)
	}
}

// mustOut runs name with args and returns stdout, failing the test with the
// combined output on a non-zero exit.
func mustOut(t *testing.T, name string, args ...string) string {
	t.Helper()
	cmd := exec.Command(name, args...)
	var out, errOut strings.Builder
	cmd.Stdout = &out
	cmd.Stderr = &errOut
	if err := cmd.Run(); err != nil {
		t.Fatalf("%s %s: %v\n%s%s", name, strings.Join(args, " "), err, out.String(), errOut.String())
	}
	return out.String()
}
