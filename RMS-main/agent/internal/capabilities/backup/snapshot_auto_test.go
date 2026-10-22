//go:build !windows

package backup

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// `snapshot: "auto"` (spec-server-backup.md §4.4): per root, btrfs, ZFS, LVM,
// or a flagged live read, driven through a fake runner. The fake mimics just
// enough of btrfs (a snapshot is a directory that appears, a delete removes
// it) for verifyPresents to mean something.

type fsFacts struct {
	source, fstype, fsroot string
}

type autoRunner struct {
	fakeRun
	mounts map[string]fsFacts // root -> what findmnt says
	lvs    map[string]bool    // device -> is an LV
	failOn string             // a command prefix that fails
	btrfsL map[string]string  // subvolume -> `btrfs subvolume list -o` output
	zfsSn  string             // `zfs list -t snapshot` output
	zfsFs  string             // `zfs list -t filesystem` output
}

func (a *autoRunner) reply(name string, args []string) snapResult {
	line := name + " " + strings.Join(args, " ")
	if a.failOn != "" && strings.HasPrefix(line, a.failOn) {
		return snapResult{ExitCode: 1, Err: errors.New("exit status 1")}
	}
	switch name {
	case "findmnt":
		root := findmntTarget(args)
		f, ok := a.mounts[root]
		if !ok {
			return snapResult{ExitCode: 1, Err: errors.New("not found")}
		}
		return snapResult{Stdout: fmt.Sprintf(`{"filesystems":[{"source":%q,"target":%q,"fstype":%q,"fsroot":%q}]}`,
			f.source, root, f.fstype, f.fsroot)}
	case "lvs":
		if len(args) > 0 && a.lvs[args[len(args)-1]] {
			return snapResult{Stdout: "  vg0 data\n"}
		}
		if len(args) == 3 { // the orphan sweep's listing
			return snapResult{Stdout: ""}
		}
		return snapResult{ExitCode: 5, Err: errors.New("not an LV")}
	case "btrfs":
		switch {
		case len(args) >= 4 && args[0] == "subvolume" && args[1] == "list":
			return snapResult{Stdout: a.btrfsL[args[3]]}
		case len(args) == 5 && args[0] == "subvolume" && args[1] == "snapshot":
			dest := args[4]
			_ = os.MkdirAll(dest, 0o755)
			_ = os.WriteFile(filepath.Join(dest, "present"), nil, 0o644)
		case len(args) == 3 && args[0] == "subvolume" && args[1] == "delete":
			_ = os.RemoveAll(args[2])
		}
	case "zfs":
		if len(args) > 0 && args[0] == "list" {
			if strings.Contains(line, "-t snapshot") {
				return snapResult{Stdout: a.zfsSn}
			}
			return snapResult{Stdout: a.zfsFs}
		}
	}
	return snapResult{}
}

func newAutoRunner() *autoRunner {
	a := &autoRunner{mounts: map[string]fsFacts{}, lvs: map[string]bool{}, btrfsL: map[string]string{}}
	a.fakeRun.reply = a.reply
	return a
}

func (a *autoRunner) called(prefix string) bool {
	for _, c := range a.calls {
		if strings.HasPrefix(strings.Join(c, " "), prefix) {
			return true
		}
	}
	return false
}

func withInodes(t *testing.T, subvols ...string) {
	t.Helper()
	set := map[string]bool{}
	for _, s := range subvols {
		set[filepath.Clean(s)] = true
	}
	prev := inodeOf
	inodeOf = func(p string) (uint64, error) {
		if set[filepath.Clean(p)] {
			return btrfsSubvolRootIno, nil
		}
		return 1000, nil
	}
	t.Cleanup(func() { inodeOf = prev })
}

func TestAutoPicksAMechanismPerRootAndFallsBackToLive(t *testing.T) {
	plain, lv, bt, zf := t.TempDir(), t.TempDir(), t.TempDir(), t.TempDir()
	a := newAutoRunner()
	a.mounts[plain] = fsFacts{source: "/dev/sda1", fstype: "ext4", fsroot: "/"}
	a.mounts[lv] = fsFacts{source: "/dev/vg0/data", fstype: "xfs", fsroot: "/"}
	a.mounts[bt] = fsFacts{source: "/dev/sdb", fstype: "btrfs", fsroot: "/"}
	a.mounts[zf] = fsFacts{source: "tank/data", fstype: "zfs", fsroot: "/"}
	a.lvs["/dev/vg0/data"] = true
	a.zfsFs = "tank/data\t" + zf + "\n"
	withInodes(t, bt)

	roots := []string{plain, lv, bt, zf}
	snap, err := openAuto(context.Background(), a.run, "T", roots, true)
	if err != nil {
		t.Fatal(err)
	}
	d := snap.(snapshotDetail)
	for root, want := range map[string]string{plain: mechNone, lv: mechLVM, bt: mechBtrfs, zf: mechZFS} {
		if got := d.mechanism(root); got != want {
			t.Errorf("mechanism(%s) = %q, want %q", root, got, want)
		}
	}
	if snap.Translate(plain) != plain {
		t.Error("a live root is not read in place")
	}
	if got := snap.Translate(bt); got != filepath.Join(bt, ".itms-snap-T") {
		t.Errorf("btrfs root reads from %s", got)
	}
	if got := snap.Translate(zf); got != filepath.Join(zf, ".zfs", "snapshot", "itms-T") {
		t.Errorf("zfs root reads from %s", got)
	}
	for _, c := range []string{"lvcreate -s", "btrfs subvolume snapshot -r " + bt, "zfs snapshot -r tank/data@itms-T"} {
		if !a.called(c) {
			t.Errorf("%q never ran", c)
		}
	}
	if err := snap.Release(); err != nil {
		t.Fatal(err)
	}
	for _, c := range []string{"lvremove -f vg0/", "btrfs subvolume delete " + filepath.Join(bt, ".itms-snap-T"),
		"zfs destroy -r tank/data@itms-T"} {
		if !a.called(c) {
			t.Errorf("release did not run %q", c)
		}
	}
}

func TestAutoRefusesALiveRootTheWorkloadForbidsBeforeCreatingAnything(t *testing.T) {
	lv, plain := t.TempDir(), t.TempDir()
	a := newAutoRunner()
	a.mounts[lv] = fsFacts{source: "/dev/vg0/data", fstype: "ext4", fsroot: "/"}
	a.mounts[plain] = fsFacts{source: "/dev/sda1", fstype: "ext4", fsroot: "/"}
	a.lvs["/dev/vg0/data"] = true
	roots := []string{lv, plain}
	if roots[0] > roots[1] {
		roots[0], roots[1] = roots[1], roots[0]
	}
	_, err := openAuto(context.Background(), a.run, "T", roots, false)
	var r *jobs.Refusal
	if !errors.As(err, &r) || !strings.Contains(r.Reason, "does not allow a live read") {
		t.Fatalf("err = %v", err)
	}
	if strings.Contains(r.Reason, plain) {
		t.Error("the refusal names a path")
	}
	if a.called("lvcreate") {
		t.Error("an LVM snapshot was created before the refusal")
	}
}

func TestAFailureMidwayReleasesWhatWasAlreadyCreated(t *testing.T) {
	lv, bt := t.TempDir(), t.TempDir()
	a := newAutoRunner()
	a.mounts[lv] = fsFacts{source: "/dev/vg0/data", fstype: "ext4", fsroot: "/"}
	a.mounts[bt] = fsFacts{source: "/dev/sdb", fstype: "btrfs", fsroot: "/"}
	a.lvs["/dev/vg0/data"] = true
	a.failOn = "btrfs subvolume snapshot"
	withInodes(t, bt)
	if _, err := openAuto(context.Background(), a.run, "T", []string{lv, bt}, true); err == nil {
		t.Fatal("no error")
	}
	if !a.called("lvremove -f vg0/") {
		t.Error("the LVM snapshot taken before the btrfs failure was left behind")
	}
}

func TestBtrfsSnapshotsNestedSubvolumesSweepsOrphansAndRedirects(t *testing.T) {
	root := t.TempDir()
	nested := filepath.Join(root, "vm", "images")
	if err := os.MkdirAll(nested, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(root, ".itms-snap-crashed"), 0o755); err != nil {
		t.Fatal(err)
	}
	a := newAutoRunner()
	a.mounts[root] = fsFacts{source: "/dev/sdb", fstype: "btrfs", fsroot: "/@data"}
	a.btrfsL[root] = "ID 258 gen 12 top level 256 path @data/vm/images\n" +
		"ID 259 gen 12 top level 256 path @data/.itms-snap-crashed\n" +
		"ID 260 gen 12 top level 5 path @elsewhere/x\n"
	withInodes(t, root, nested)

	snap, err := openAuto(context.Background(), a.run, "T", []string{root}, true)
	if err != nil {
		t.Fatal(err)
	}
	defer snap.Release() //nolint:errcheck
	if !a.called("btrfs subvolume delete " + filepath.Join(root, ".itms-snap-crashed")) {
		t.Error("an orphan from a crashed run was not reclaimed")
	}
	for _, sv := range []string{root, nested} {
		if !a.called("btrfs subvolume snapshot -r " + sv + " " + filepath.Join(sv, ".itms-snap-T")) {
			t.Errorf("subvolume %s was not snapshotted; it would read as an empty directory", sv)
		}
	}
	r, ok := snap.(snapshotRedirector).redirect(nested)
	if !ok || r != filepath.Join(nested, ".itms-snap-T") {
		t.Errorf("redirect(nested) = %q, %v", r, ok)
	}
}

func TestSubvolumeOfFindsTheNearestSubvolumeRoot(t *testing.T) {
	withInodes(t, filepath.FromSlash("/mnt/pool"), filepath.FromSlash("/mnt/pool/home"))
	for p, want := range map[string]string{
		"/mnt/pool/home/alice/docs": "/mnt/pool/home",
		"/mnt/pool/etc":             "/mnt/pool",
		"/mnt/pool":                 "/mnt/pool",
	} {
		got, err := subvolumeOf(filepath.FromSlash(p), filepath.FromSlash("/mnt/pool"))
		if err != nil || got != filepath.FromSlash(want) {
			t.Errorf("subvolumeOf(%s) = %q, %v; want %q", p, got, err, want)
		}
	}
}

func TestBtrfsListedPathsMapThroughTheMountsFsroot(t *testing.T) {
	for _, c := range []struct {
		listed, fsroot, target, want string
		ok                           bool
	}{
		{"a/b", "/", "/mnt", "/mnt/a/b", true},
		{"@home/alice", "/@home", "/home", "/home/alice", true},
		{"<FS_TREE>/@home/alice", "/@home", "/home", "/home/alice", true},
		{"@other/x", "/@home", "/home", "", false},
		{"@homework/x", "/@home", "/home", "", false},
	} {
		got, ok := btrfsMountPath(c.listed, mountInfo{target: filepath.FromSlash(c.target), fsroot: c.fsroot})
		if ok != c.ok || (ok && got != filepath.FromSlash(c.want)) {
			t.Errorf("btrfsMountPath(%q, %q) = %q, %v", c.listed, c.fsroot, got, ok)
		}
	}
}

func TestZFSSnapshotsRecursivelyDestroysOnlyItsOwnAndMapsChildren(t *testing.T) {
	root := t.TempDir()
	a := newAutoRunner()
	a.mounts[root] = fsFacts{source: "tank/data", fstype: "zfs", fsroot: "/"}
	a.zfsSn = "tank/data@itms-1234\ntank/data@daily-2026-09-22\ntank/data/child@itms-99\n"
	a.zfsFs = "tank/data\t" + root + "\ntank/data/child\t" + filepath.Join(root, "child") +
		"\ntank/data/legacy\tlegacy\n"
	snap, err := openAuto(context.Background(), a.run, "T", []string{root}, true)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range []string{"zfs destroy tank/data@itms-1234", "zfs destroy tank/data/child@itms-99"} {
		if !a.called(c) {
			t.Errorf("orphan not reclaimed: %q", c)
		}
	}
	if a.called("zfs destroy tank/data@daily") {
		t.Fatal("a snapshot this agent did not create was destroyed")
	}
	r, ok := snap.(snapshotRedirector).redirect(filepath.Join(root, "child"))
	if !ok || r != filepath.Join(root, "child", ".zfs", "snapshot", "itms-T") {
		t.Errorf("child dataset redirect = %q, %v", r, ok)
	}
	if err := snap.Release(); err != nil || !a.called("zfs destroy -r tank/data@itms-T") {
		t.Errorf("release: %v", err)
	}
}

func TestAZFSDatasetNameThatLooksLikeAnOptionIsRefused(t *testing.T) {
	root := t.TempDir()
	a := newAutoRunner()
	a.mounts[root] = fsFacts{source: "-rf", fstype: "zfs", fsroot: "/"}
	_, err := openAuto(context.Background(), a.run, "T", []string{root}, true)
	var r *jobs.Refusal
	if !errors.As(err, &r) {
		t.Fatalf("err = %v", err)
	}
	if a.called("zfs snapshot") {
		t.Fatal("zfs ran with an option-looking dataset")
	}
}
