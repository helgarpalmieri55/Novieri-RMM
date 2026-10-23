//go:build !windows

package backup

// Roots passed to openLVM in these tests must be REAL directories, not
// fictional path strings. The fake snapRunner only fakes the command seam
// (findmnt, lvs, lvcreate, mount, ...); it does not and cannot fake
// verifyPresents' filesystem check, which deliberately calls the real
// os.Open on the job's live root -- that real read is the only way it can
// catch a snapshot presenting no data. A root that only exists as a string
// in the fake runner's replies will fail that real check with "cannot read
// the root", not because the LVM logic is wrong. Use t.TempDir() (empty is
// fine -- it short-circuits verifyPresents' liveN == 0 branch) for any root
// that reaches a successful, non-refusing openLVM call. Task 4's APFS tests
// hit the same seam and need the same care.

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// findmntTarget reads the value of findmnt's --target flag out of a fake
// call's args, BY FLAG rather than by position.
//
// The fakes used to read args[len(args)-1], which broke the moment openLVM
// started passing --json after the target. A fake that depends on argument
// ORDER fails for a reason that has nothing to do with what it is testing.
func findmntTarget(args []string) string {
	for i, a := range args {
		if a == "--target" && i+1 < len(args) {
			return args[i+1]
		}
	}
	return ""
}

type fakeRun struct {
	calls [][]string
	reply func(name string, args []string) snapResult
}

func (f *fakeRun) run(_ context.Context, _ []string, name string, args ...string) snapResult {
	f.calls = append(f.calls, append([]string{name}, args...))
	if f.reply != nil {
		return f.reply(name, args)
	}
	return snapResult{}
}

// TestTheShellOutProvidersRefuseZeroRootsBeforeCreatingAnything is Minor 5's
// provider half (the validateRoots half is in snapshot_test.go). The assertion
// is not merely "an error": it is that NOTHING was created, so there is
// nothing left to leak.
func TestTheShellOutProvidersRefuseZeroRootsBeforeCreatingAnything(t *testing.T) {
	for _, tc := range []struct {
		name string
		open func(context.Context, snapRunner, string, []string) (Snapshot, error)
	}{
		{"apfs", openAPFS},
		{"lvm", openLVM},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := &fakeRun{}
			_, err := tc.open(context.Background(), f.run, "job1", nil)
			var refusal *jobs.Refusal
			if !errors.As(err, &refusal) {
				t.Fatalf("open(no roots) = %v (%T), want a *jobs.Refusal", err, err)
			}
			if len(f.calls) != 0 {
				t.Fatalf("open(no roots) ran %v; it must refuse before creating or mounting anything, "+
					"because with zero roots there is no volSnapshot to carry a teardown and the mount "+
					"leaks until the next run's sweep", f.calls)
			}
		})
	}
}

func TestOpenLVMRefusesARootThatIsNotOnALogicalVolume(t *testing.T) {
	f := &fakeRun{reply: func(name string, args []string) snapResult {
		switch name {
		case "findmnt":
			return snapResult{Stdout: "/dev/sda1 /\n"}
		case "lvs":
			if len(args) > 0 && args[len(args)-1] == "/dev/sda1" {
				return snapResult{ExitCode: 5, Err: errors.New("not an LV")}
			}
		}
		return snapResult{}
	}}

	_, err := openLVM(context.Background(), f.run, "job1", []string{"/data"})

	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Fatalf("err is %T, want *jobs.Refusal", err)
	}
	// The root and the device are named POSITIONALLY, not literally: this
	// reason lands in the server's hash-chained audit log. See the convention
	// in snapshot.go.
	if !strings.Contains(refusal.Reason, "index 0") {
		t.Errorf("reason %q does not identify the rejected root by index", refusal.Reason)
	}
	for _, banned := range []string{"/data", "/dev/sda1"} {
		if strings.Contains(refusal.Reason, banned) {
			t.Errorf("reason %q carries %q into the audit log", refusal.Reason, banned)
		}
	}
	for _, c := range f.calls {
		if c[0] == "lvcreate" {
			t.Fatal("lvcreate was called for a non-LV root; it must refuse before creating anything")
		}
	}
}

func TestOpenLVMNamesTheSnapshotWithTheAgentTag(t *testing.T) {
	// A real, empty directory: this test is about the lvcreate -n name
	// carrying the tag, not about verifyPresents (Task 1 has its own
	// dedicated tests for that). Empty means verifyPresents' liveN == 0
	// branch short-circuits before it ever looks at the translated path.
	root := t.TempDir()

	f := &fakeRun{reply: func(name string, args []string) snapResult {
		switch name {
		case "findmnt":
			return snapResult{Stdout: "/dev/vg0/root " + root + "\n"}
		case "lvs":
			if len(args) > 0 && args[len(args)-1] == "/dev/vg0/root" {
				return snapResult{Stdout: "  vg0 root\n"}
			}
		}
		return snapResult{}
	}}

	if _, err := openLVM(context.Background(), f.run, "job42", []string{root}); err != nil {
		t.Fatalf("openLVM = %v, want nil", err)
	}

	var created string
	for _, c := range f.calls {
		if c[0] == "lvcreate" {
			for i, a := range c {
				if a == "-n" && i+1 < len(c) {
					created = c[i+1]
				}
			}
		}
	}
	if !strings.HasPrefix(created, snapTagPrefix) || !strings.Contains(created, "job42") {
		t.Fatalf("lvcreate -n %q, want a name prefixed %q and carrying the job id", created, snapTagPrefix)
	}
}

// lvmReply is the happy-path fake: findmnt reports SOURCE and TARGET, lvs
// resolves that device to a logical volume, and everything else succeeds.
//
// The TARGET matters and is the point of C1: a job's root is usually a strict
// SUBDIRECTORY of the filesystem it lives on, and the snapshot presents that
// filesystem's root.
func lvmReply(device, target string) func(string, []string) snapResult {
	return func(name string, args []string) snapResult {
		switch name {
		case "findmnt":
			return snapResult{Stdout: device + " " + target + "\n"}
		case "lvs":
			// The sweep calls lvs with no device argument; the resolution
			// calls it with one.
			if len(args) > 0 && args[len(args)-1] == device {
				return snapResult{Stdout: "  vg0 home\n"}
			}
			return snapResult{}
		}
		return snapResult{}
	}
}

// TestOpenLVMTranslatesARootToItsOwnOffsetInsideTheSnapshot is C1.
//
// A root of <mount>/alice/docs on a filesystem mounted at <mount> appears
// inside the snapshot at <snapmount>/alice/docs. Translating it to
// <snapmount> -- the snapshot's own root -- backs up the WHOLE snapshotted
// filesystem under the name of one subdirectory and reports success:
// verifyPresents sees a non-empty directory and passes, because every guard in
// this package checks existence and non-emptiness, never correspondence.
func TestOpenLVMTranslatesARootToItsOwnOffsetInsideTheSnapshot(t *testing.T) {
	mount := t.TempDir()
	root := filepath.Join(mount, "alice", "docs")
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal(err)
	}
	f := &fakeRun{reply: lvmReply("/dev/vg0/home", mount)}

	snap, err := openLVM(context.Background(), f.run, "job1", []string{root})
	if err != nil {
		t.Fatalf("openLVM = %v, want nil", err)
	}

	snapMount := snapMountPath("job1", 0)
	wantRoot := filepath.Join(snapMount, "alice", "docs")
	if got := snap.Translate(root); got != wantRoot {
		t.Fatalf("Translate(root) = %q, want %q. Translating to the snapshot's mount ROOT (%q) backs "+
			"up the whole snapshotted filesystem under the name of one subdirectory, and every "+
			"existence check in this package still passes", got, wantRoot, snapMount)
	}
	canary := filepath.Join(root, "canary.txt")
	if got, want := snap.Translate(canary), filepath.Join(wantRoot, "canary.txt"); got != want {
		t.Fatalf("Translate(%q) = %q, want %q", canary, got, want)
	}
}

// TestOpenLVMTakesOneSnapshotPerLogicalVolumeNotPerRoot is I3. Two roots on
// one filesystem taken at two different instants are two different
// point-in-time views of one volume -- the inconsistency the snapshot exists
// to prevent -- and burn copy-on-write space twice for one volume.
func TestOpenLVMTakesOneSnapshotPerLogicalVolumeNotPerRoot(t *testing.T) {
	mount := t.TempDir()
	a := filepath.Join(mount, "alice")
	b := filepath.Join(mount, "bob", "mail")
	for _, d := range []string{a, b} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	f := &fakeRun{reply: lvmReply("/dev/vg0/home", mount)}

	snap, err := openLVM(context.Background(), f.run, "job9", []string{a, b})
	if err != nil {
		t.Fatalf("openLVM = %v, want nil", err)
	}

	var creates, mounts int
	for _, c := range f.calls {
		switch c[0] {
		case "lvcreate":
			creates++
		case "mount":
			mounts++
		}
	}
	if creates != 1 || mounts != 1 {
		t.Fatalf("lvcreate ran %d time(s) and mount %d time(s) for two roots on ONE logical volume; "+
			"want 1 and 1. Two lvcreate calls are two different point-in-time views of one volume",
			creates, mounts)
	}

	snapMount := snapMountPath("job9", 0)
	for root, want := range map[string]string{
		a: filepath.Join(snapMount, "alice"),
		b: filepath.Join(snapMount, "bob", "mail"),
	} {
		if got := snap.Translate(root); got != want {
			t.Errorf("Translate(%q) = %q, want %q: every root on one LV reads out of that LV's single "+
				"mount at its own offset", root, got, want)
		}
	}
}

// TestOpenLVMTakesOneSnapshotPerDistinctLogicalVolume is I3's other half: the
// grouping must not over-collapse. Two roots on two different volumes still
// need two snapshots.
func TestOpenLVMTakesOneSnapshotPerDistinctLogicalVolume(t *testing.T) {
	home, data := t.TempDir(), t.TempDir()
	f := &fakeRun{reply: func(name string, args []string) snapResult {
		switch name {
		case "findmnt":
			switch findmntTarget(args) {
			case home:
				return snapResult{Stdout: "/dev/vg0/home " + home + "\n"}
			case data:
				return snapResult{Stdout: "/dev/vg0/data " + data + "\n"}
			}
			return snapResult{ExitCode: 1, Err: errors.New("no such target")}
		case "lvs":
			if len(args) == 0 {
				return snapResult{}
			}
			switch args[len(args)-1] {
			case "/dev/vg0/home":
				return snapResult{Stdout: "  vg0 home\n"}
			case "/dev/vg0/data":
				return snapResult{Stdout: "  vg0 data\n"}
			}
			return snapResult{}
		}
		return snapResult{}
	}}

	if _, err := openLVM(context.Background(), f.run, "job10", []string{home, data}); err != nil {
		t.Fatalf("openLVM = %v, want nil", err)
	}

	var creates int
	for _, c := range f.calls {
		if c[0] == "lvcreate" {
			creates++
		}
	}
	if creates != 2 {
		t.Fatalf("lvcreate ran %d time(s) for two roots on two DIFFERENT logical volumes, want 2", creates)
	}
}

// TestOpenLVMGroupsAWholeFilesystemBindUnderOneSnapshot is the case the
// (device, mount point) PAIR key split and should not have.
//
// A whole-filesystem bind mount presents ONE filesystem at TWO mount points,
// and `findmnt` reports the IDENTICAL SOURCE for both. Observed on this
// project's Linux container with real findmnt (util-linux 2.39.3) as root:
//
//	$ mount --bind / rootbind
//	$ findmnt -n -o SOURCE,TARGET --target "$PWD/rootbind/home"
//	/dev/vda  /…/rootbind
//	$ findmnt -n -o SOURCE,TARGET --target /home
//	/dev/vda  /
//
// Keying the group on the pair makes two groups out of that, so one origin LV
// is snapshotted TWICE at two different instants -- one job seeing two
// point-in-time views of one volume, which is the whole inconsistency I3
// exists to prevent, plus double copy-on-write space.
//
// One snapshot is not merely cheaper here, it is CORRECT: both mount points
// expose the same filesystem root, so each root's offset measured from its own
// TARGET resolves to the right tree inside that single snapshot. This test
// asserts both halves -- one lvcreate, and each root still landing at its own
// offset.
func TestOpenLVMGroupsAWholeFilesystemBindUnderOneSnapshot(t *testing.T) {
	// fsMount stands in for `/`; bindMount for a whole-filesystem bind of it.
	// Both are real, empty directories: empty short-circuits verifyPresents'
	// liveN == 0 branch (see the note atop this file).
	fsMount, bindMount := t.TempDir(), t.TempDir()
	alice := filepath.Join(fsMount, "home", "alice")
	bob := filepath.Join(bindMount, "home", "bob")
	for _, d := range []string{alice, bob} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			t.Fatal(err)
		}
	}

	const device = "/dev/vg0/home"
	f := &fakeRun{reply: func(name string, args []string) snapResult {
		switch name {
		case "findmnt":
			// The SAME device at TWO different mount points -- exactly what
			// real findmnt reports for a whole-filesystem bind.
			target := findmntTarget(args)
			switch {
			case strings.HasPrefix(target, bindMount):
				return snapResult{Stdout: device + " " + bindMount + "\n"}
			case strings.HasPrefix(target, fsMount):
				return snapResult{Stdout: device + " " + fsMount + "\n"}
			}
			return snapResult{ExitCode: 1, Err: errors.New("no such target")}
		case "lvs":
			if len(args) > 0 && args[len(args)-1] == device {
				return snapResult{Stdout: "  vg0 home\n"}
			}
		}
		return snapResult{}
	}}

	snap, err := openLVM(context.Background(), f.run, "job11", []string{alice, bob})
	if err != nil {
		t.Fatalf("openLVM = %v, want nil", err)
	}

	var creates, mounts int
	for _, c := range f.calls {
		switch c[0] {
		case "lvcreate":
			creates++
		case "mount":
			mounts++
		}
	}
	if creates != 1 || mounts != 1 {
		t.Fatalf("lvcreate ran %d time(s) and mount %d time(s) for two roots reaching ONE filesystem "+
			"through two mount points; want 1 and 1. Two lvcreate calls against one origin LV are two "+
			"different point-in-time views of one volume, and twice the copy-on-write space",
			creates, mounts)
	}

	snapMount := snapMountPath("job11", 0)
	for root, want := range map[string]string{
		alice: filepath.Join(snapMount, "home", "alice"),
		bob:   filepath.Join(snapMount, "home", "bob"),
	} {
		if got := snap.Translate(root); got != want {
			t.Errorf("Translate(%q) = %q, want %q: a whole-filesystem bind exposes the same filesystem "+
				"root, so each root's offset from its OWN mount point resolves inside the one snapshot",
				root, got, want)
		}
	}
}

// TestParseFindmntOutputPrefersJSONAndFallsBackToFields covers both halves of
// the findmnt parse.
//
// The JSON shapes below are copied from real findmnt (util-linux 2.39.3) run
// on this project's Linux container, as root, including the two bind-mount
// cases and the one with a space in the mount point:
//
//	$ findmnt -n -o SOURCE,TARGET --target /home --json
//	{"filesystems": [{"source": "/dev/vda", "target": "/"}]}
func TestParseFindmntOutputPrefersJSONAndFallsBackToFields(t *testing.T) {
	for _, tc := range []struct {
		name             string
		out              string
		wantDev, wantTgt string
		wantOK           bool
	}{
		{
			name:    "json, ordinary mount",
			out:     `{"filesystems":[{"source":"/dev/vda","target":"/"}]}`,
			wantDev: "/dev/vda", wantTgt: "/", wantOK: true,
		},
		{
			// The whole point of the change: a mount point containing a space
			// is UNPARSEABLE as two whitespace fields, so before this the root
			// lost its backup entirely. JSON delimits it correctly.
			name:    "json, mount point containing a space",
			out:     `{"filesystems":[{"source":"/dev/vda","target":"/mnt/space dir"}]}`,
			wantDev: "/dev/vda", wantTgt: "/mnt/space dir", wantOK: true,
		},
		{
			// A subtree bind's SOURCE carries its subpath in brackets. That
			// string must survive verbatim -- it is the grouping key.
			name:    "json, subtree bind carries its subpath in SOURCE",
			out:     `{"filesystems":[{"source":"/dev/vda[/srv/alice]","target":"/mnt/bind"}]}`,
			wantDev: "/dev/vda[/srv/alice]", wantTgt: "/mnt/bind", wantOK: true,
		},
		{
			name:    "not json at all: falls back to the two-field form",
			out:     "/dev/vg0/home /home\n",
			wantDev: "/dev/vg0/home", wantTgt: "/home", wantOK: true,
		},
		{
			// An over-mounted target: two binds stacked on one mountpoint.
			// `--target` reports BOTH, in mountinfo order (oldest first), but
			// the kernel resolves the mountpoint to the LAST -- the most
			// recent bind shadows the ones under it. Captured from real
			// findmnt (util-linux 2.39.3) on this project's Linux container,
			// as root, with two `mount --bind` calls onto the same target
			// (paths shortened, shape unchanged):
			//
			//	$ mount --bind ovtest/a ovtest/mp
			//	$ mount --bind ovtest/b ovtest/mp
			//	$ findmnt -n -o SOURCE,TARGET --target ovtest/mp --json
			//	{"filesystems":[
			//	   {"source":"/dev/vda[/…/ovtest/a]","target":"/…/ovtest/mp"},
			//	   {"source":"/dev/vda[/…/ovtest/b]","target":"/…/ovtest/mp"}
			//	]}
			//	$ cat ovtest/mp/marker
			//	BBB   <- the kernel resolves to /b, the LAST entry, not /a
			name: "json, over-mounted target: the LAST entry is the visible mount",
			out: `{"filesystems":[` +
				`{"source":"/dev/vda[/srv/ovtest/a]","target":"/srv/ovtest/mp"},` +
				`{"source":"/dev/vda[/srv/ovtest/b]","target":"/srv/ovtest/mp"}` +
				`]}`,
			wantDev: "/dev/vda[/srv/ovtest/b]", wantTgt: "/srv/ovtest/mp", wantOK: true,
		},
		{
			// Same over-mount case, whitespace fallback. findmnt emits one
			// mount per line in the same mountinfo (oldest-first) order as
			// the JSON form above, so the fallback's "which line wins" rule
			// must agree with the JSON rule: the LAST line, not the first.
			name:    "not json at all, over-mounted target: the LAST line is the visible mount",
			out:     "/dev/vg0/old /srv/data\n/dev/vg0/new /srv/data\n",
			wantDev: "/dev/vg0/new", wantTgt: "/srv/data", wantOK: true,
		},
		{
			// Valid JSON, unexpected shape. Falling back finds no two fields
			// either, so this fails loudly rather than guessing.
			name: "json with no filesystems: no fallback match, refuses to guess",
			out:  `{"filesystems":[]}`,
		},
		{
			name: "json missing target: refuses to guess",
			out:  `{"filesystems":[{"source":"/dev/vda"}]}`,
		},
		{
			name: "empty output",
			out:  "",
		},
		{
			// The pre-existing rule this change does NOT relax: three
			// whitespace fields and no JSON to disambiguate them still fails,
			// because guessing where the path ends is C1 with a new first cause.
			name: "three fields, no json: still refuses to guess",
			out:  "/dev/vda /mnt/space dir\n",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dev, tgt, ok := parseFindmntOutput(tc.out)
			if ok != tc.wantOK {
				t.Fatalf("parseFindmntOutput(%q) ok = %v, want %v", tc.out, ok, tc.wantOK)
			}
			if !tc.wantOK {
				return
			}
			if dev != tc.wantDev || tgt != tc.wantTgt {
				t.Fatalf("parseFindmntOutput(%q) = (%q, %q), want (%q, %q)",
					tc.out, dev, tgt, tc.wantDev, tc.wantTgt)
			}
		})
	}
}

// TestOpenLVMBacksUpARootWhoseMountPointContainsASpace is the failure class
// --json removes. A mount point with a space in it is not two whitespace
// fields, so the two-field parse rejected it and the root LOST ITS BACKUP
// ENTIRELY -- silently, from the policy author's point of view, since nothing
// about the path looks unusual.
//
// It also asserts openLVM actually asks findmnt for --json; parsing JSON that
// is never requested would leave every real run on the fallback.
func TestOpenLVMBacksUpARootWhoseMountPointContainsASpace(t *testing.T) {
	mount := filepath.Join(t.TempDir(), "space dir")
	root := filepath.Join(mount, "alice")
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal(err)
	}

	f := &fakeRun{reply: func(name string, args []string) snapResult {
		switch name {
		case "findmnt":
			// Exactly the shape real findmnt --json produced for a bind mount
			// whose target contains a space (verified on this container).
			b, err := json.Marshal(map[string]any{
				"filesystems": []map[string]string{{"source": "/dev/vg0/home", "target": mount}},
			})
			if err != nil {
				t.Fatal(err)
			}
			return snapResult{Stdout: string(b)}
		case "lvs":
			if len(args) > 0 && args[len(args)-1] == "/dev/vg0/home" {
				return snapResult{Stdout: "  vg0 home\n"}
			}
		}
		return snapResult{}
	}}

	snap, err := openLVM(context.Background(), f.run, "job13", []string{root})
	if err != nil {
		t.Fatalf("openLVM = %v, want nil. A mount point containing a space must not cost the root "+
			"its backup", err)
	}

	var askedJSON bool
	for _, c := range f.calls {
		if c[0] != "findmnt" {
			continue
		}
		for _, a := range c {
			if a == "--json" {
				askedJSON = true
			}
		}
	}
	if !askedJSON {
		t.Error("openLVM never passed --json to findmnt, so every real run stays on the two-field " +
			"fallback and a mount point with a space still loses its backup")
	}

	want := filepath.Join(snapMountPath("job13", 0), "alice")
	if got := snap.Translate(root); got != want {
		t.Fatalf("Translate(root) = %q, want %q", got, want)
	}
}

// TestOpenLVMFailsARootWhoseSymlinkCrossesAMountBoundary is Minor 7: a NEW
// loud failure for a policy that used to "succeed".
//
// `findmnt --target` resolves symlinks; rootRelative compares lexically. So a
// policy naming /var/www where /var/www -> /mnt/data/www gets TARGET
// /mnt/data, which /var/www is not lexically under, and the run fails.
//
// The resolution half was checked with real findmnt (util-linux 2.39.3) on
// this container, with a symlink pointing across a real bind mount:
//
//	$ ln -s "$WD/bindmnt" "$WD/lnk2"
//	$ findmnt -n -o SOURCE,TARGET --target "$WD/lnk2"
//	/dev/vda[/…/bindsrc/alice]  /…/bindmnt      # the LINK's target, not its own dir
//
// This is the RIGHT direction -- before C1's fix such a root backed up the
// wrong tree and reported success -- but it is a behaviour change for existing
// policies, so it is recorded in agent-engine.md §9 as well as pinned here.
//
// The root need not exist on disk: this fails during resolution, before
// verifyPresents' real filesystem read.
func TestOpenLVMFailsARootWhoseSymlinkCrossesAMountBoundary(t *testing.T) {
	const root = "/var/www" // symlinked to /mnt/data/www
	f := &fakeRun{reply: func(name string, args []string) snapResult {
		if name == "findmnt" {
			// findmnt followed the symlink and reports the mount the RESOLVED
			// path lives on.
			return snapResult{Stdout: `{"filesystems":[{"source":"/dev/vg0/data","target":"/mnt/data"}]}`}
		}
		return snapResult{}
	}}

	_, err := openLVM(context.Background(), f.run, "job14", []string{root})
	if err == nil {
		t.Fatal("openLVM = nil for a root whose symlink crosses a mount boundary, want a failure. " +
			"Silently computing an offset here backs up a tree that is not the one the root names")
	}
	// An OPERATIONAL failure, not a refusal: §9 keeps `refused` for a standing
	// incapacity of the device, and a symlink is configuration.
	var refusal *jobs.Refusal
	if errors.As(err, &refusal) {
		t.Errorf("err = %q is a *jobs.Refusal; a refusal leaves backup_runs stuck on `running`", refusal.Reason)
	}
	if !strings.Contains(err.Error(), "index 0") {
		t.Errorf("error %q does not identify the root positionally", err)
	}
	for _, banned := range []string{root, "/mnt/data", "/dev/vg0/data"} {
		if strings.Contains(err.Error(), banned) {
			t.Errorf("error %q carries %q into backup_runs.error", err, banned)
		}
	}
	// It must fail BEFORE creating anything.
	for _, c := range f.calls {
		if c[0] == "lvcreate" || c[0] == "mount" {
			t.Fatalf("openLVM ran %v before failing; nothing may be created for a root it cannot map", c)
		}
	}
}

// TestOpenLVMGroupFailureNamesEveryMemberIndex: when one snapshot covers
// several roots, a failure to create, mount, or make a mountpoint for it
// stopped ALL of them, and the operator is owed every index -- not just the
// group's first member.
//
// Roots 0 and 3 share a volume here and roots 1 and 2 sit on another, so
// naming only members[0] would report "index 0" for a failure that also
// stopped index 3, and the operator would never look at it.
func TestOpenLVMGroupFailureNamesEveryMemberIndex(t *testing.T) {
	for _, failing := range []string{"lvcreate", "mkdir", "mount"} {
		t.Run(failing, func(t *testing.T) {
			// Four real, empty directories. Empty short-circuits
			// verifyPresents (see the note atop this file) -- though these
			// cases never reach it anyway.
			shared0, other1, other2, shared3 := t.TempDir(), t.TempDir(), t.TempDir(), t.TempDir()
			roots := []string{shared0, other1, other2, shared3}

			const sharedDev, otherDev = "/dev/vg0/home", "/dev/vg0/data"
			f := &fakeRun{reply: func(name string, args []string) snapResult {
				switch name {
				case "findmnt":
					target := findmntTarget(args)
					switch target {
					case shared0, shared3:
						// One filesystem, reached at each root's own mount
						// point: the whole-filesystem-bind shape.
						return snapResult{Stdout: sharedDev + " " + target + "\n"}
					case other1, other2:
						return snapResult{Stdout: otherDev + " " + target + "\n"}
					}
					return snapResult{ExitCode: 1, Err: errors.New("no such target")}
				case "lvs":
					if len(args) > 0 {
						switch args[len(args)-1] {
						case sharedDev:
							return snapResult{Stdout: "  vg0 home\n"}
						case otherDev:
							return snapResult{Stdout: "  vg0 data\n"}
						}
					}
				case failing:
					// The shared group is formed first (first-seen order), so
					// the first failure of this command is that group's.
					return snapResult{ExitCode: 1, Err: errors.New("boom")}
				}
				return snapResult{}
			}}

			_, err := openLVM(context.Background(), f.run, "job12", roots)
			if err == nil {
				t.Fatal("openLVM = nil, want an error")
			}
			for _, want := range []string{"0", "3"} {
				if !strings.Contains(err.Error(), want) {
					t.Errorf("error %q does not name index %s. This one snapshot covered roots 0 AND 3, "+
						"so its failure stopped both; naming only the group's first member sends the "+
						"operator to investigate a root that was never the problem", err, want)
				}
			}
			// And it must still carry no path, per the convention in snapshot.go.
			for _, banned := range roots {
				if strings.Contains(err.Error(), banned) {
					t.Errorf("error %q carries a source path into backup_runs.error", err)
				}
			}
		})
	}
}

// TestOpenLVMRefusalsAndFailuresNameNoPathOrDevice is I1. Every reason and
// every error text from this provider reaches either the server's
// hash-chained audit log (a refusal) or `backup_runs.error` (an operational
// failure). Neither may learn a customer's directory names, and every
// pre-existing jobs.Refuse in this package is already path-free.
func TestOpenLVMRefusalsAndFailuresNameNoPathOrDevice(t *testing.T) {
	const rootMarker = "ZZmarkerrootZZ"
	const devMarker = "ZZmarkerdevZZ"

	for _, tc := range []struct {
		name  string
		reply func(string, []string) snapResult
	}{
		{"findmnt fails", func(name string, args []string) snapResult {
			if name == "findmnt" {
				return snapResult{ExitCode: 1, Err: errors.New("nope")}
			}
			return snapResult{}
		}},
		{"not a logical volume", func(name string, args []string) snapResult {
			switch name {
			case "findmnt":
				return snapResult{Stdout: "/dev/" + devMarker + " /\n"}
			case "lvs":
				if len(args) > 0 && strings.Contains(args[len(args)-1], devMarker) {
					return snapResult{ExitCode: 5, Err: errors.New("not an LV")}
				}
			}
			return snapResult{}
		}},
		{"lvcreate fails", func(name string, args []string) snapResult {
			switch name {
			case "findmnt":
				return snapResult{Stdout: "/dev/" + devMarker + " /\n"}
			case "lvs":
				if len(args) > 0 && strings.Contains(args[len(args)-1], devMarker) {
					return snapResult{Stdout: "  vg0 home\n"}
				}
			case "lvcreate":
				return snapResult{ExitCode: 5, Err: errors.New("insufficient free space")}
			}
			return snapResult{}
		}},
		{"mount fails", func(name string, args []string) snapResult {
			switch name {
			case "findmnt":
				return snapResult{Stdout: "/dev/" + devMarker + " /\n"}
			case "lvs":
				if len(args) > 0 && strings.Contains(args[len(args)-1], devMarker) {
					return snapResult{Stdout: "  vg0 home\n"}
				}
			case "mount":
				return snapResult{ExitCode: 32, Err: errors.New("mount failed")}
			}
			return snapResult{}
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := filepath.Join(t.TempDir(), rootMarker)
			if err := os.MkdirAll(root, 0o700); err != nil {
				t.Fatal(err)
			}
			f := &fakeRun{reply: tc.reply}
			_, err := openLVM(context.Background(), f.run, "job1", []string{root})
			if err == nil {
				t.Fatal("openLVM = nil, want an error")
			}
			for _, marker := range []string{rootMarker, devMarker} {
				if strings.Contains(err.Error(), marker) {
					t.Errorf("error %q carries %q; a refusal reason lands in the hash-chained audit log "+
						"and a failure's text lands in backup_runs.error", err, marker)
				}
			}
			if !strings.Contains(err.Error(), "index 0") {
				t.Errorf("error %q does not identify the root positionally, so an operator cannot tell "+
					"which root was rejected", err)
			}
		})
	}
}

// TestOpenLVMSeparatesStandingIncapacityFromTransientFailure is I2.
// agent-engine.md §9: `refused` is a standing incapacity of the device, and a
// Go error for a TRANSIENT condition produces a failed job,
// `ingest_backup_result` never runs, and the `backup_runs` row is stuck on
// `running` forever with no error text anywhere an operator looks.
func TestOpenLVMSeparatesStandingIncapacityFromTransientFailure(t *testing.T) {
	dev := "/dev/sda1"
	standing := func(name string, args []string) snapResult {
		switch name {
		case "findmnt":
			return snapResult{Stdout: dev + " /\n"}
		case "lvs":
			if len(args) > 0 && args[len(args)-1] == dev {
				return snapResult{ExitCode: 5, Err: errors.New("not an LV")}
			}
		}
		return snapResult{}
	}
	transient := func(fail string) func(string, []string) snapResult {
		return func(name string, args []string) snapResult {
			switch name {
			case "findmnt":
				return snapResult{Stdout: "/dev/vg0/home /\n"}
			case "lvs":
				if len(args) > 0 && args[len(args)-1] == "/dev/vg0/home" {
					return snapResult{Stdout: "  vg0 home\n"}
				}
			}
			if name == fail {
				return snapResult{ExitCode: 1, Err: errors.New("transient")}
			}
			return snapResult{}
		}
	}

	for _, tc := range []struct {
		name      string
		reply     func(string, []string) snapResult
		wantRefus bool
	}{
		{"root is not on a logical volume", standing, true},
		{"lvcreate failed (volume group out of space)", transient("lvcreate"), false},
		{"the mountpoint could not be created", transient("mkdir"), false},
		{"mount failed", transient("mount"), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			f := &fakeRun{reply: tc.reply}
			_, err := openLVM(context.Background(), f.run, "job1", []string{root})
			if err == nil {
				t.Fatal("openLVM = nil, want an error")
			}
			var refusal *jobs.Refusal
			gotRefusal := errors.As(err, &refusal)
			if gotRefusal != tc.wantRefus {
				if tc.wantRefus {
					t.Fatalf("err = %v (%T), want a *jobs.Refusal: this is a standing property of how "+
						"the machine is provisioned", err, err)
				}
				t.Fatalf("err = %v, want a plain error: a refusal for a transient condition leaves the "+
					"backup_runs row stuck on `running` with no error text anywhere an operator looks",
					refusal.Reason)
			}
		})
	}
}

func TestSweepLVMOrphansRemovesOnlyTaggedVolumes(t *testing.T) {
	f := &fakeRun{reply: func(name string, args []string) snapResult {
		if name == "lvs" {
			return snapResult{Stdout: "  vg0 " + snapTagPrefix + "old\n  vg0 home\n  vg0 " + snapTagPrefix + "older\n"}
		}
		return snapResult{}
	}}

	if err := sweepLVMOrphans(context.Background(), f.run); err != nil {
		t.Fatalf("sweepLVMOrphans = %v, want nil", err)
	}

	var removed []string
	for _, c := range f.calls {
		if c[0] == "lvremove" {
			removed = append(removed, c[len(c)-1])
		}
	}
	sort.Strings(removed)
	want := []string{"vg0/" + snapTagPrefix + "old", "vg0/" + snapTagPrefix + "older"}
	if !reflect.DeepEqual(removed, want) {
		t.Fatalf("removed %v, want %v: the sweep must never touch a volume the agent did not create", removed, want)
	}
}

// TestSweepLVMOrphansUnmountsAMountedOrphanBeforeRemovingIt is the crash-orphan
// case: an LV left behind by a prior run that crashed AFTER mount succeeded is
// still mounted when the next sweep finds it. Removing a mounted LV fails, so
// a sweep that only ran lvremove would fail on this exact LV every single
// time it runs -- the orphan would never actually be reclaimed. This asserts
// the sweep unmounts the orphan's mountpoint BEFORE it tries to remove the LV,
// and that both calls actually happen.
func TestSweepLVMOrphansUnmountsAMountedOrphanBeforeRemovingIt(t *testing.T) {
	const orphan = snapTagPrefix + "crashed"
	f := &fakeRun{reply: func(name string, args []string) snapResult {
		if name == "lvs" {
			return snapResult{Stdout: "  vg0 " + orphan + "\n"}
		}
		return snapResult{}
	}}

	if err := sweepLVMOrphans(context.Background(), f.run); err != nil {
		t.Fatalf("sweepLVMOrphans = %v, want nil", err)
	}

	wantMount := filepath.Join(snapMountRoot, orphan)
	umountIdx, lvremoveIdx := -1, -1
	for i, c := range f.calls {
		switch c[0] {
		case "umount":
			if len(c) < 2 || c[1] != wantMount {
				t.Fatalf("umount call %v, want target %q", c, wantMount)
			}
			umountIdx = i
		case "lvremove":
			want := "vg0/" + orphan
			if c[len(c)-1] != want {
				t.Fatalf("lvremove call %v, want target %q", c, want)
			}
			lvremoveIdx = i
		}
	}
	if umountIdx == -1 {
		t.Fatal("sweepLVMOrphans never unmounted the orphan's mountpoint; a still-mounted LV cannot be " +
			"removed and would accumulate forever, one failed sweep at a time")
	}
	if lvremoveIdx == -1 {
		t.Fatal("sweepLVMOrphans never removed the orphan LV")
	}
	if umountIdx > lvremoveIdx {
		t.Fatalf("umount happened AFTER lvremove (call indices %d, %d): removing a mounted LV fails, so "+
			"unmount must run first", umountIdx, lvremoveIdx)
	}
}

// TestSweepLVMOrphansSucceedsWhenUmountFailsButRemovalSucceeds is the
// crash-BETWEEN-lvcreate-and-mount case: the LV exists but was never
// mounted, so pass 1's umount fails (there is nothing to unmount) while
// pass 2's lvremove succeeds outright. sweepLVMOrphans must report success
// here -- the orphan WAS reclaimed. Treating the harmless umount failure as
// fatal would refuse the entire backup job over an orphan that had already
// been cleaned up, which is exactly the regression a prior review caught:
// the pre-fix code (a bare `lvremove -f` with no unmount at all) handled
// this case correctly, and the unmount-pass fix must not make it worse.
func TestSweepLVMOrphansSucceedsWhenUmountFailsButRemovalSucceeds(t *testing.T) {
	const orphan = snapTagPrefix + "neverMounted"
	f := &fakeRun{reply: func(name string, args []string) snapResult {
		switch name {
		case "lvs":
			return snapResult{Stdout: "  vg0 " + orphan + "\n"}
		case "umount":
			return snapResult{ExitCode: 32, Err: errors.New("not mounted")}
		}
		return snapResult{} // lvremove succeeds
	}}

	if err := sweepLVMOrphans(context.Background(), f.run); err != nil {
		t.Fatalf("sweepLVMOrphans = %v, want nil: lvremove succeeded, so the orphan WAS reclaimed "+
			"despite umount failing on an LV that was never mounted", err)
	}

	var sawRemove bool
	for _, c := range f.calls {
		if c[0] == "lvremove" && c[len(c)-1] == "vg0/"+orphan {
			sawRemove = true
		}
	}
	if !sawRemove {
		t.Fatal("lvremove was never called for the orphan")
	}
}

// lvsSizeQuery reports whether a fake `lvs` call is the ORIGIN SIZE query the
// free-space pre-check makes, rather than the vg/lv resolution query.
//
// By flag, not by position, for the reason findmntTarget above is: a fake that
// depends on argument order fails for reasons unrelated to what it tests.
func lvsSizeQuery(args []string) bool {
	for i, a := range args {
		if a == "-o" && i+1 < len(args) && args[i+1] == "lv_size" {
			return true
		}
	}
	return false
}

// TestOpenLVMFailsWithMeasuredNumbersWhenTheVolumeGroupCannotFitTheSnapshot
// pins the difference between a guess and a measurement.
//
// `lvcreate -s -l 10%ORIGIN` needs free extents equal to a TENTH OF THE ORIGIN
// VOLUME, whatever the source root's size -- so a 236G root LV needs ~24G free
// in the volume group to back up a single directory. An installer that hands
// the whole disk to one logical volume leaves VFree at 0, which is the state
// the first target host was found in.
//
// Before this, the failure said the volume group "may be out of space". That
// sends an operator to look at `df`, which shows plenty free -- the space is
// inside the filesystem, and lvcreate needs it OUTSIDE, unallocated in the VG.
// The numbers are knowable before the attempt, so they are reported instead of
// speculated about.
func TestOpenLVMFailsWithMeasuredNumbersWhenTheVolumeGroupCannotFitTheSnapshot(t *testing.T) {
	const devMarker = "ZZmarkerdevZZ"
	root := t.TempDir()

	f := &fakeRun{reply: func(name string, args []string) snapResult {
		switch name {
		case "findmnt":
			return snapResult{Stdout: "/dev/" + devMarker + " " + root + "\n"}
		case "lvs":
			if lvsSizeQuery(args) {
				return snapResult{Stdout: "  107374182400\n"} // a 100 GiB origin
			}
			if len(args) > 0 && strings.Contains(args[len(args)-1], devMarker) {
				return snapResult{Stdout: "  vg0 home\n"}
			}
		case "vgs":
			return snapResult{Stdout: "  0\n"} // not one free extent
		case "lvcreate":
			t.Error("lvcreate was attempted although the volume group provably cannot fit the snapshot")
		}
		return snapResult{}
	}}

	_, err := openLVM(context.Background(), f.run, "job1", []string{root})
	if err == nil {
		t.Fatal("openLVM = nil, want a failure")
	}
	// The two numbers an operator needs, and the positional root id every
	// other message in this file carries.
	for _, want := range []string{"0 B", "10.0 GiB", "index 0"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error %q does not contain %q", err, want)
		}
	}
	if strings.Contains(err.Error(), "may be") {
		t.Errorf("error %q still speculates; the numbers were measured", err)
	}
	// Same constraint as every other message here: no device name.
	if strings.Contains(err.Error(), devMarker) {
		t.Errorf("error %q carries the device name", err)
	}
}

// Non-vacuity for the check above: a volume group WITH room is not blocked.
func TestOpenLVMTakesTheSnapshotWhenTheVolumeGroupHasRoom(t *testing.T) {
	root := t.TempDir()
	created := false

	f := &fakeRun{reply: func(name string, args []string) snapResult {
		switch name {
		case "findmnt":
			return snapResult{Stdout: "/dev/vg0/home " + root + "\n"}
		case "lvs":
			if lvsSizeQuery(args) {
				return snapResult{Stdout: "  107374182400\n"} // 100 GiB origin, needs 10
			}
			if len(args) > 0 && args[len(args)-1] == "/dev/vg0/home" {
				return snapResult{Stdout: "  vg0 home\n"}
			}
		case "vgs":
			return snapResult{Stdout: "  53687091200\n"} // 50 GiB free -- ample
		case "lvcreate":
			created = true
		}
		return snapResult{}
	}}

	if _, err := openLVM(context.Background(), f.run, "job1", []string{root}); err != nil {
		t.Fatalf("openLVM = %v, want nil", err)
	}
	if !created {
		t.Error("lvcreate was never attempted, so the pre-check blocked a volume group with ample room")
	}
}

// A DIAGNOSTIC MUST NOT BECOME A NEW FAILURE MODE. The pre-check shells out to
// vgs and lvs, and every LVM release is free to change their output. If it
// cannot get two numbers it trusts, it must stand aside and let lvcreate decide
// -- the alternative is an agent that refuses to back up a perfectly healthy
// volume group because a report format moved.
func TestOpenLVMStillAttemptsTheSnapshotWhenFreeSpaceCannotBeMeasured(t *testing.T) {
	for _, tc := range []struct {
		name         string
		vgs, lvsSize snapResult
	}{
		{"vgs says nothing", snapResult{}, snapResult{Stdout: "  107374182400\n"}},
		{"vgs errors", snapResult{ExitCode: 5, Err: errors.New("no such vg")}, snapResult{Stdout: "  107374182400\n"}},
		{"vgs is not a number", snapResult{Stdout: "  4.00g\n"}, snapResult{Stdout: "  107374182400\n"}},
		{"origin size unknown", snapResult{Stdout: "  0\n"}, snapResult{}},
		{"origin size is zero", snapResult{Stdout: "  0\n"}, snapResult{Stdout: "  0\n"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			created := false
			f := &fakeRun{reply: func(name string, args []string) snapResult {
				switch name {
				case "findmnt":
					return snapResult{Stdout: "/dev/vg0/home " + root + "\n"}
				case "lvs":
					if lvsSizeQuery(args) {
						return tc.lvsSize
					}
					if len(args) > 0 && args[len(args)-1] == "/dev/vg0/home" {
						return snapResult{Stdout: "  vg0 home\n"}
					}
				case "vgs":
					return tc.vgs
				case "lvcreate":
					created = true
				}
				return snapResult{}
			}}

			if _, err := openLVM(context.Background(), f.run, "job1", []string{root}); err != nil {
				t.Fatalf("openLVM = %v, want nil -- an unreadable report must not fail the job", err)
			}
			if !created {
				t.Error("lvcreate was never attempted; the pre-check failed closed on an unreadable report")
			}
		})
	}
}

// TestOpenLVMMountsAnXFSSnapshotWithNoUUID: an XFS snapshot shares its
// origin's UUID, and XFS will not mount it without nouuid.
func TestOpenLVMMountsAnXFSSnapshotWithNoUUID(t *testing.T) {
	for fsType, want := range map[string]string{"xfs": "ro,nouuid", "ext4": "ro", "": "ro"} {
		mount := t.TempDir()
		base := lvmReply("/dev/vg0/home", mount)
		f := &fakeRun{reply: func(name string, args []string) snapResult {
			if name == "blkid" {
				return snapResult{Stdout: fsType + "\n"}
			}
			return base(name, args)
		}}

		if _, err := openLVM(context.Background(), f.run, "job1", []string{mount}); err != nil {
			t.Fatalf("fstype %q: openLVM = %v, want nil", fsType, err)
		}

		var got string
		for _, c := range f.calls {
			if c[0] == "mount" && len(c) > 2 && c[1] == "-o" {
				got = c[2]
			}
		}
		if got != want {
			t.Errorf("fstype %q: mount -o %q, want %q", fsType, got, want)
		}
	}
}
