//go:build !windows

package backup

// Built on every OS but Windows, whose provider is VSS and whose paths are not
// POSIX mount paths. It only shells out through the injected snapRunner, so
// every decision it makes -- device resolution, LV detection, per-volume
// grouping, refusals, the sweep filter, and path translation -- is unit-tested
// on any Unix machine, including one with no LVM at all
// (snapshot_lvm_test.go; those tests pass when run locally, and CI's agent job
// is currently red for reasons outside this package -- see
// snapshot_darwin.go's header). Only the dispatch that SELECTS this provider
// (snapshot_linux.go) needs `//go:build linux`; putting the logic itself
// behind a linux tag would make it untestable on macOS, which is part of the
// value of this file.
//
// What the seam CANNOT fake is a real device: no fake proves `lvcreate -s`
// takes a usable snapshot or that mounting one presents the origin's data.
// snapshot_lvm_integration_test.go is the only thing that does, and it skips
// loudly wherever `lvcreate` is absent or the test is not root -- which is
// everywhere in this project so far.

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"path/filepath"
	"strconv"
	"strings"
	"sync"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// resolvedRoot is one root after resolution: the volume it lives on, and its
// offset inside that volume's filesystem.
type resolvedRoot struct {
	root, rel      string
	device, target string
	vg, name       string
}

// volumeGroup is the set of roots that share one snapshot, keyed by device.
type volumeGroup struct {
	vg, lv  string
	members []resolvedRoot
}

// groupRootsPhrase names EVERY root the group's snapshot covers, by index.
//
// One snapshot serves the whole group, so a failure to create, mount, or make
// a mountpoint for it stopped every member. Naming only members[0] reports
// "index 0" for a failure that also stopped index 3, and index 3's owner never
// learns their backup did not run.
//
// Positional, never by path: this phrase is embedded in an operational failure
// whose text reaches `backup_runs.error` -- see the convention in snapshot.go.
// The indices are into the same sorted, de-duplicated list every other message
// in this package counts against.
func groupRootsPhrase(roots []string, members []resolvedRoot) string {
	idx := make([]string, 0, len(members))
	for _, m := range members {
		idx = append(idx, strconv.Itoa(indexOf(roots, m.root)))
	}
	if len(idx) == 1 {
		return "the source root at index " + idx[0]
	}
	return "the source roots at indices " + strings.Join(idx, ", ")
}

// openLVM snapshots the logical volumes the roots live on: resolve each root's
// backing device AND the mount point it lives under, confirm the device is a
// logical volume, take one copy-on-write snapshot per distinct volume, and
// mount each snapshot read-only so the backup reads a frozen view instead of
// the live, changing filesystem.
//
// # One snapshot per VOLUME, not per root
//
// Two roots on one filesystem must share one snapshot. Snapshotting per root
// would take two copy-on-write snapshots of the same origin at two different
// instants, so a single job would see two different point-in-time views of one
// volume -- exactly the inconsistency a snapshot exists to prevent -- and burn
// the copy-on-write space twice for it.
//
// The grouping key is the resolved DEVICE ALONE. Two roots sharing a device
// share a filesystem root by construction, because `findmnt`'s SOURCE column
// already encodes a bind's subpath: a bind of a SUBTREE reports
// `device[/subpath]`, a different string from the plain `device` a whole
// filesystem reports, so device-alone grouping cannot merge a subtree bind with
// the filesystem it came from. Observed with real findmnt (util-linux 2.39.3)
// on this project's Linux container, as root, not reasoned about:
//
//	$ mount --bind bindsrc/alice bindmnt
//	$ findmnt -n -o SOURCE,TARGET --target "$PWD/bindmnt"
//	/dev/vda[/…/bindsrc/alice]  /…/bindmnt
//
// Such a root is REPORTED not to reach this grouping at all: `lvs` is asked
// about the bracketed string and does not resolve it, so the root is refused
// above as "not on a logical volume" before any group is formed. That last
// step is UNVERIFIED HERE -- this container has no `lvs` to run it against,
// only the findmnt half above was checked -- so it is written as the reason
// refusing is safe rather than as a fact this file relies on. The SOURCE
// encoding is what makes the device a sound key either way, which is why it is
// stated: not because a subtree bind is expected to arrive here.
//
// A WHOLE-filesystem bind is the other direction, and it is why the key must
// not be the (device, mount point) pair: it reports the IDENTICAL SOURCE at two
// different TARGETs.
//
//	$ mount --bind / rootbind
//	$ findmnt -n -o SOURCE,TARGET --target "$PWD/rootbind/home"
//	/dev/vda  /…/rootbind
//	$ findmnt -n -o SOURCE,TARGET --target /home
//	/dev/vda  /
//
// Keying on the pair splits those into two groups and takes two snapshots of
// one origin LV at two instants -- the exact inconsistency this section exists
// to prevent. One snapshot is also CORRECT for them, not just cheaper: both
// mount points expose the same filesystem root, so a root under `/` and a root
// under the bind each resolve at their own offset inside the single snapshot.
// TestOpenLVMGroupsAWholeFilesystemBindUnderOneSnapshot is that case.
//
// # Where a root appears inside the snapshot
//
// NOT at the snapshot's mount root. A snapshot presents the FILESYSTEM, so a
// root of /home/alice/docs on a filesystem mounted at /home appears at
// <snapmount>/alice/docs. This is why findmnt is asked for TARGET as well as
// SOURCE: rootRelative(root, TARGET) is the offset, and dropping it would back
// up the whole snapshotted filesystem under the name of one subdirectory while
// every guard in this package still passed. snapshot_windows.go's
// volumeRelative/shadowPath is the same model.
//
// # Refused versus failed
//
// A root that does not resolve to a logical volume is REFUSED before anything
// is created: that is a standing property of how the machine is provisioned
// (§9). Everything else here -- lvcreate, mkdir, mount, the sweep -- is an
// operational failure and returns a plain error, so run.go reports a
// `state:"failed"` result document rather than a refusal that would leave the
// `backup_runs` row stuck on `running`.
//
// Refusing before creating anything also matters mechanically: creating
// snapshots for the roots that ARE on LVM and only then discovering one root
// is not would leave orphans behind that only the next backup's sweep reclaims.
func openLVM(ctx context.Context, run snapRunner, tag string, roots []string) (Snapshot, error) {
	if err := validateRoots(roots); err != nil {
		return nil, err
	}
	all := make([]int, len(roots))
	for i := range roots {
		all[i] = i
	}
	vols, err := lvmVolumes(ctx, run, tag, roots, all)
	if err != nil {
		return nil, err
	}
	s := newMultiSnapshot(vols)
	if err := verifyPresents(roots, s); err != nil {
		// A rejection here must not leak every volume just created.
		_ = s.Release()
		return nil, err
	}
	return s, nil
}

// lvmVolumes snapshots the roots at the given indices. Messages index into the
// whole of roots, so a caller handling a subset (auto selection) still names
// the operator's root.
func lvmVolumes(ctx context.Context, run snapRunner, tag string, roots []string, indices []int) ([]volSnapshot, error) {
	resolved := make([]resolvedRoot, 0, len(indices))
	for _, i := range indices {
		root := roots[i]
		// --json is requested, and parseFindmntOutput prefers it: the
		// whitespace form cannot represent a mount point containing a space,
		// and a root on one would lose its backup entirely. A findmnt too old
		// to know the flag fails here and the run reports a failure rather
		// than a wrong tree; a findmnt that accepts it but emits an
		// unexpected shape falls back to the two-field parse.
		res := run(ctx, nil, "findmnt", "-n", "-o", "SOURCE,TARGET", "--target", root, "--json")
		if !res.ok() {
			// Not a refusal: the usual cause is a root the policy names and
			// this machine does not have, which §9 classes as a failed run
			// ("Configuration, changes day to day").
			return nil, snapFailuref("the device and mount point backing the source root at index %d "+
				"could not be resolved", i)
		}
		device, target, ok := parseFindmntOutput(res.Stdout)
		if !ok {
			return nil, snapFailuref("findmnt did not report a source and a mount point for the "+
				"source root at index %d", i)
		}
		rel, ok := rootRelative(root, target)
		if !ok {
			return nil, snapFailuref("the source root at index %d is not under the mount point "+
				"findmnt reports for it, so it cannot be located inside a snapshot of that filesystem", i)
		}

		lvRes := run(ctx, nil, "lvs", "--noheadings", "-o", "vg_name,lv_name", device)
		vg, name, parsed := "", "", false
		if lvRes.ok() {
			vg, name, parsed = parseLVSLine(lvRes.Stdout)
		}
		if !parsed {
			return nil, jobs.Refuse("backup.run: the source root at index %d is not on a logical "+
				"volume, so LVM cannot snapshot it", i)
		}
		resolved = append(resolved, resolvedRoot{
			root: root, rel: rel, device: device, target: target, vg: vg, name: name,
		})
	}

	// Group by the resolved DEVICE, preserving first-seen order so the snapshot
	// names a job produces are stable for a given source list. See the header
	// for why the device alone is the right key and the (device, mount point)
	// pair is not.
	var groups []*volumeGroup
	byKey := make(map[string]*volumeGroup, len(resolved))
	for _, r := range resolved {
		g, ok := byKey[r.device]
		if !ok {
			g = &volumeGroup{vg: r.vg, lv: r.name}
			byKey[r.device] = g
			groups = append(groups, g)
		}
		g.members = append(g.members, r)
	}

	// Every root resolved to a logical volume. Reclaim any orphan from a prior
	// crashed run before creating anything new, so orphans never accumulate --
	// and so this reclaim, which itself calls umount and lvremove, never runs
	// after we have created snapshots of our own that a bug here could
	// mistakenly sweep.
	if err := sweepLVMOrphans(ctx, run); err != nil {
		return nil, err
	}

	var vols []volSnapshot
	for n, g := range groups {
		snapName := fmt.Sprintf("%s%s-%d", snapTagPrefix, tag, n)

		// -l 10%ORIGIN sizes the snapshot's copy-on-write space at 10% of the
		// origin LV. This is the spec's REASONED default (spec §3.3), not a
		// measured one: it is not tuned against any observed write-churn
		// workload, only chosen as a starting point unlikely to fill during a
		// single backup window on a typical mostly-read-only root. A
		// snapshot that fills its CoW space is silently invalidated by LVM,
		// which is worse than refusing up front, so this number should be
		// revisited once real churn data exists.
		// MEASURE THE ROOM FIRST, so the failure can name the shortfall instead
		// of guessing at it. The requirement is a tenth of the ORIGIN volume in
		// UNALLOCATED extents -- independent of how small a source root the job
		// names, and independent of free space inside the filesystem. Those are
		// different quantities, and conflating them is the trap: an operator
		// told the volume group "may be out of space" checks `df`, sees 164G
		// free, and concludes the message is wrong. It is not; the space
		// lvcreate needs is outside any filesystem, and `df` cannot see it.
		//
		// Deliberately advisory: if the numbers cannot be read the attempt goes
		// ahead and lvcreate decides. A diagnostic that fails closed on an
		// LVM report format change would refuse to back up healthy hosts.
		if free, need, known := lvmSnapshotRoom(ctx, run, g.vg, g.lv); known && free < need {
			releaseAll(vols)
			return nil, snapFailuref("the volume group holding %s has %s of unallocated space but this "+
				"snapshot needs %s, a tenth of the origin volume. That is space outside any filesystem, "+
				"so df does not show it; extend the volume group or use a host with unallocated extents",
				groupRootsPhrase(roots, g.members), humanBytes(free), humanBytes(need))
		}

		createRes := run(ctx, nil, "lvcreate", "-s", "-n", snapName, "-l", "10%ORIGIN",
			fmt.Sprintf("/dev/%s/%s", g.vg, g.lv))
		if !createRes.ok() {
			releaseAll(vols)
			return nil, snapFailuref("an LVM snapshot could not be created for the volume holding %s "+
				"(the volume group may have no unallocated extents for the copy-on-write area -- space "+
				"outside any filesystem, which df does not show)", groupRootsPhrase(roots, g.members))
		}

		mountPoint := snapMountPath(tag, n)
		if mkRes := run(ctx, nil, "mkdir", "-p", mountPoint); !mkRes.ok() {
			// The snapshot LV was already created; tear it down before
			// failing so this does not orphan it.
			run(ctx, nil, "lvremove", "-f", fmt.Sprintf("%s/%s", g.vg, snapName))
			releaseAll(vols)
			return nil, snapFailuref("the mountpoint for the snapshot covering %s could not be created",
				groupRootsPhrase(roots, g.members))
		}

		snapDevice := fmt.Sprintf("/dev/%s/%s", g.vg, snapName)
		fsType := run(ctx, nil, "blkid", "-o", "value", "-s", "TYPE", snapDevice)
		opts := snapshotMountOptions(strings.TrimSpace(fsType.Stdout))
		if mountRes := run(ctx, nil, "mount", "-o", opts, snapDevice, mountPoint); !mountRes.ok() {
			run(ctx, nil, "lvremove", "-f", fmt.Sprintf("%s/%s", g.vg, snapName))
			releaseAll(vols)
			return nil, snapFailuref("the snapshot covering %s could not be mounted",
				groupRootsPhrase(roots, g.members))
		}

		// ONE teardown for the group, shared by every member's volSnapshot.
		// multiSnapshot.Release walks volumes, so without the sync.Once inside
		// lvTeardown a two-root group would umount once and then report the
		// second umount's failure as a teardown error.
		teardown := lvTeardown(run, mountPoint, g.vg, snapName)
		for _, m := range g.members {
			vols = append(vols, volSnapshot{
				origin:     m.root,
				translated: filepath.Join(mountPoint, m.rel),
				teardown:   teardown,
				mech:       mechLVM,
			})
		}
	}
	return vols, nil
}

// indexOf returns root's position in the job's source list, or -1. It exists
// so a message can identify a root positionally without carrying its path --
// see the convention in snapshot.go.
func indexOf(roots []string, root string) int {
	for i, r := range roots {
		if r == root {
			return i
		}
	}
	return -1
}

// lvTeardown unmounts and removes one snapshot LV, AT MOST ONCE.
//
// Every root in a volume group shares this closure, and multiSnapshot.Release
// calls each volume's teardown, so without the sync.Once a group of two roots
// would run the whole teardown twice and report the second umount's inevitable
// failure as the release error.
//
// The messages name the agent's OWN mountpoint and the volume group, never a
// source path, and never the command's stderr -- umount and mount echo the
// path they were given, and this error reaches a log line and
// `backup_runs.error`.
func lvTeardown(run snapRunner, mountPoint, vg, snapName string) func() error {
	var once sync.Once
	var err error
	return func() error {
		once.Do(func() {
			// Unmount BEFORE lvremove: removing a mounted LV fails, and a
			// failed teardown leaves copy-on-write space consuming until the
			// snapshot is silently invalidated.
			if res := run(context.Background(), nil, "umount", mountPoint); !res.ok() {
				err = fmt.Errorf("umount %s: exit %d", mountPoint, res.ExitCode)
				return
			}
			if res := run(context.Background(), nil, "lvremove", "-f",
				fmt.Sprintf("%s/%s", vg, snapName)); !res.ok() {
				err = fmt.Errorf("lvremove %s/%s: exit %d", vg, snapName, res.ExitCode)
			}
		})
		return err
	}
}

// findmntJSON is the shape `findmnt --json` emits for `-o SOURCE,TARGET`.
//
// Verified against real findmnt (util-linux 2.39.3) on this project's Linux
// container, as root:
//
//	$ findmnt -n -o SOURCE,TARGET --target /home --json
//	{
//	   "filesystems": [
//	      {
//	         "source": "/dev/vda",
//	         "target": "/"
//	      }
//	   ]
//	}
//
// `--target` can report MORE than one element: a mountpoint that is
// over-mounted (a second `mount` stacked on the same target without
// unmounting the first) shows every stacked mount, and the kernel resolves
// the target to the LAST one, not the first -- the earlier mounts are merely
// shadowed, still present underneath. Observed on real findmnt (util-linux
// 2.39.3) on this project's Linux container, as root: two `mount --bind`
// calls onto one target reported both, oldest first, and reading the
// mountpoint's own file content resolved to the SECOND (most recent) bind's
// data, not the first. A third stacked bind confirmed the same rule: the
// visible mount was always the last entry in mountinfo order, never the
// first. `filesystems[len-1]` is therefore taken, matching the whitespace
// parser's last-line rule below -- this is the ordering findmnt emitted on
// this run, not a guarantee this code verifies against the running kernel.
type findmntJSON struct {
	Filesystems []struct {
		Source string `json:"source"`
		Target string `json:"target"`
	} `json:"filesystems"`
}

// parseFindmntOutput reads findmnt's SOURCE and TARGET, preferring --json.
//
// JSON is asked for and parsed FIRST because the whitespace form loses a mount
// point containing a space: three whitespace fields is not two, so
// parseFindmntSourceTarget rejects it and the run fails -- meaning the root
// LOSES ITS BACKUP ENTIRELY, silently from the policy author's point of view,
// since nothing about such a path looks unusual. JSON delimits the fields
// explicitly and removes that whole failure class. Observed on this container
// with a real bind mount whose target contained a space:
//
//	"source": "/dev/vda[/…/space dir]",
//	"target": "/…/spacetgt"
//
// The whitespace form stays as the FALLBACK, for a findmnt too old to know
// `--json` or one whose output is not the expected shape. It keeps its own
// strictness: still exactly two fields, still no guessing where a path with a
// space ends, because guessing wrong computes an offset against the wrong
// mount point and backs up a tree that is not the one the root names -- C1
// with a new first cause. So a space-containing mount point on the fallback
// path still fails loudly rather than silently backing up the wrong thing.
//
// `encoding/json` is already in this binary (wire.go, manifest.go); this adds
// no dependency.
func parseFindmntOutput(out string) (device, target string, ok bool) {
	var doc findmntJSON
	if err := json.Unmarshal([]byte(strings.TrimSpace(out)), &doc); err == nil && len(doc.Filesystems) > 0 {
		last := doc.Filesystems[len(doc.Filesystems)-1]
		if last.Source != "" && last.Target != "" {
			return last.Source, last.Target, true
		}
	}
	return parseFindmntSourceTarget(out)
}

// parseFindmntSourceTarget parses one `findmnt -n -o SOURCE,TARGET --target`
// line, e.g. "/dev/vg0/home /home".
//
// This is parseFindmntOutput's FALLBACK, not the primary path; see its doc
// comment for why JSON is preferred.
//
// Exactly two whitespace-separated fields are required. A mount point
// containing whitespace therefore does NOT parse, and the caller fails the run
// rather than guessing which side of the space the path ends on: guessing
// wrong would compute an offset against the wrong mount point and back up a
// tree that is not the one the root names, which is precisely the failure
// class C1 was.
func parseFindmntSourceTarget(out string) (device, target string, ok bool) {
	trimmed := strings.TrimSpace(out)
	if trimmed == "" {
		return "", "", false
	}
	// The LAST line matters, not the first: --target can report several
	// mounts when the target is over-mounted, one per line in mountinfo
	// order, and the kernel resolves the target to the most recent one --
	// see findmntJSON's doc comment for the observed transcript. Both forms
	// must agree on which entry wins.
	lines := strings.Split(trimmed, "\n")
	line := strings.TrimSpace(lines[len(lines)-1])
	fields := strings.Fields(line)
	if len(fields) != 2 {
		return "", "", false
	}
	return fields[0], fields[1], true
}

// releaseAll tears down every volume created so far. Used on a mid-loop
// failure in openLVM, where no Snapshot has been constructed yet to call
// Release on.
func releaseAll(vols []volSnapshot) {
	newMultiSnapshot(vols).Release() //nolint:errcheck
}

// parseLVSLine parses one `lvs --noheadings -o vg_name,lv_name` line, e.g.
// "  vg0 root". Returns ok=false if the output does not have exactly two
// whitespace-separated fields -- including the non-zero-exit-with-empty-
// stdout case, which `lvs` produces for a device that is not an LV.
func parseLVSLine(out string) (vg, name string, ok bool) {
	line := strings.TrimSpace(out)
	if line == "" {
		return "", "", false
	}
	// Only the first line matters; a device resolves to at most one LV.
	if i := strings.IndexByte(line, '\n'); i >= 0 {
		line = line[:i]
	}
	fields := strings.Fields(line)
	if len(fields) != 2 {
		return "", "", false
	}
	return fields[0], fields[1], true
}

// sweepLVMOrphans removes every logical volume this agent's naming scheme
// created, in case a previous run crashed or was killed between lvcreate and
// teardown. It runs before the first lvcreate of a new snapshot so orphans
// from earlier failures never accumulate.
//
// An orphan left by a crash AFTER mount succeeded is still MOUNTED.
// Removing a mounted LV fails (see the teardown closure in openLVM), so a
// sweep that only ever ran lvremove would fail on that same LV on every
// single future sweep, forever -- it would never actually be reclaimed
// despite this function's name. This is why the sweep unmounts everything
// tagged FIRST, in its own complete pass, before removing anything.
//
// The mountpoint for a tagged LV is derived from its NAME rather than
// discovered by listing snapMountRoot on disk: openLVM names an LV and
// mounts it at a path whose basename is that same name (compare snapName's
// construction here with snapMountPath), so
// filepath.Join(snapMountRoot, name) is exactly where that LV is mounted, if
// it is mounted at all. This keeps the sweep entirely inside the injected
// snapRunner seam -- a real directory listing would need a real directory
// on disk to be unit-tested, the same trap a prior review found in
// verifyPresents' real filesystem check (see the note atop
// snapshot_lvm_test.go).
//
// A failed umount does NOT, by itself, determine whether this function
// reports an error. An orphan left by a crash BETWEEN lvcreate and mount was
// never mounted at all, so its umount fails harmlessly here -- and there is
// no reliable, cross-distribution way to tell "there was nothing to unmount"
// apart from a genuinely stuck mount by parsing umount's exit code or
// stderr. Treating every umount failure as fatal would refuse the entire
// backup job over an orphan the removal pass below was about to clean up
// successfully anyway. So umount failures are only logged here, never
// recorded in the return value: lvremove is the real test. A mount that is
// genuinely stuck fails to remove, and THAT failure is what gets reported.
//
// The filter is a PREFIX match on snapTagPrefix, and only a prefix match:
// this is a fleet-wide "remove logical volumes" operation, and a substring
// match (or no match at all) would delete an operator's own volume that
// merely happens to contain the tag string, or worse, everything.
func sweepLVMOrphans(ctx context.Context, run snapRunner) error {
	res := run(ctx, nil, "lvs", "--noheadings", "-o", "vg_name,lv_name")
	if !res.ok() {
		return snapFailuref("logical volumes could not be listed to reclaim orphaned snapshots (exit %d)",
			res.ExitCode)
	}

	type taggedLV struct{ vg, name string }
	var tagged []taggedLV
	for _, line := range strings.Split(res.Stdout, "\n") {
		fields := strings.Fields(line)
		if len(fields) != 2 {
			continue
		}
		vg, name := fields[0], fields[1]
		if !strings.HasPrefix(name, snapTagPrefix) {
			continue
		}
		tagged = append(tagged, taggedLV{vg: vg, name: name})
	}

	// Pass 1: unmount every tagged LV's mountpoint, over the WHOLE set,
	// before pass 2 removes anything. One stuck mountpoint must not block
	// attempting the rest, so a failure here never stops the pass -- and,
	// per the doc comment above, it is deliberately NOT recorded as the
	// sweep's error either. It is only logged: this call cannot tell a
	// harmless "nothing was mounted" apart from a real, stuck mount, and
	// pass 2 is what actually tests which one it was.
	//
	// Logged at Warn, via the process default logger, NOT via
	// Options.Logger: sweepLVMOrphans/openLVM take (ctx, run) with no
	// logger parameter, because Options.OpenSnapshot's signature carries
	// none and is shared across main.go and all three snapshot providers --
	// reshaping that seam for one diagnostic line is not worth it. That
	// means this does NOT land in the agent's configured structured log
	// alongside everything else; it goes wherever slog.Default() is
	// pointed, which nothing in this codebase ever changes away from
	// slog's own built-in handler. Warn (not Debug) matters here for that
	// reason: the built-in default handler writes Warn and above to
	// stderr, so on a stock agent this line is actually visible, whereas a
	// Debug record it writes is silently dropped -- and a failed unmount
	// during orphan cleanup, meaning a snapshot this agent created could
	// not be released, is genuinely warn-worthy on its own.
	for _, t := range tagged {
		mountPoint := filepath.Join(snapMountRoot, t.name)
		if umRes := run(ctx, nil, "umount", mountPoint); !umRes.ok() {
			slog.WarnContext(ctx, "sweepLVMOrphans: umount failed; may be harmless if this LV was never mounted",
				"mountpoint", mountPoint, "stderr", umRes.Stderr)
		}
	}

	// Pass 2: remove every tagged LV. This is the real test of whether an
	// orphan was actually reclaimed. An LV never mounted (a crash between
	// lvcreate and mount) removes cleanly here regardless of pass 1's
	// umount failing on it above -- lvremove never even sees that. An LV
	// still genuinely stuck mounted fails to remove, and THIS is the
	// failure that gets reported, not pass 1's.
	var firstErr error
	for _, t := range tagged {
		if rmRes := run(ctx, nil, "lvremove", "-f", fmt.Sprintf("%s/%s", t.vg, t.name)); !rmRes.ok() && firstErr == nil {
			firstErr = fmt.Errorf("lvremove %s/%s: exit %d", t.vg, t.name, rmRes.ExitCode)
		}
	}

	if firstErr != nil {
		// An OPERATIONAL failure, not a refusal (§9): a snapshot this agent
		// left behind and cannot reclaim is a condition of this run, not a
		// standing incapacity of the device, and refusing would leave the
		// `backup_runs` row on `running` with no error text. This only fires
		// on an actual failed removal -- a harmless umount failure on an LV
		// that was never mounted never reaches here, because pass 2's
		// lvremove for it succeeds. The text names only the agent's own
		// snapshot LV, never a source path.
		return snapFailuref("an orphaned LVM snapshot could not be reclaimed: %v", firstErr)
	}
	return nil
}

// snapshotOriginPercent is the 10 in `lvcreate -l 10%ORIGIN` above, named here
// so the pre-check and the flag cannot drift apart. Changing one without the
// other makes the pre-check refuse jobs lvcreate would have accepted, or pass
// jobs it rejects.
const snapshotOriginPercent = 10

// lvmSnapshotRoom reports the volume group's unallocated space and how much a
// snapshot of this origin needs, or known=false if it cannot get two numbers it
// trusts.
//
// EVERY UNCERTAIN CASE RETURNS known=false. This exists to improve a failure
// message, so it must never be the reason a job fails: an LVM report format
// change, a missing binary, or an unparseable line all mean "say nothing and
// let lvcreate decide", not "refuse".
func lvmSnapshotRoom(ctx context.Context, run snapRunner, vg, lv string) (free, need int64, known bool) {
	// --units b --nosuffix gives plain integer bytes; without --nosuffix each
	// value carries a "B" and every parse below fails, silently disabling this.
	freeRes := run(ctx, nil, "vgs", "--noheadings", "--nosuffix", "--units", "b", "-o", "vg_free", vg)
	if !freeRes.ok() {
		return 0, 0, false
	}
	free, err := strconv.ParseInt(strings.TrimSpace(freeRes.Stdout), 10, 64)
	if err != nil || free < 0 {
		return 0, 0, false
	}

	sizeRes := run(ctx, nil, "lvs", "--noheadings", "--nosuffix", "--units", "b", "-o", "lv_size",
		vg+"/"+lv)
	if !sizeRes.ok() {
		return 0, 0, false
	}
	origin, err := strconv.ParseInt(strings.TrimSpace(sizeRes.Stdout), 10, 64)
	if err != nil || origin <= 0 {
		return 0, 0, false
	}
	return free, origin / snapshotOriginPercent, true
}

// humanBytes renders a byte count for an operator reading a failure message.
//
// IEC units, because that is what LVM, lsblk and df -h all print: a message
// saying "24.2 GB" against tooling saying "23.6G" invites the reader to think
// they are looking at two different numbers.
func humanBytes(n int64) string {
	const unit = 1024
	if n < unit {
		return fmt.Sprintf("%d B", n)
	}
	units := [...]string{"KiB", "MiB", "GiB", "TiB", "PiB"}
	v, i := float64(n), -1
	for v >= unit && i < len(units)-1 {
		v /= unit
		i++
	}
	return fmt.Sprintf("%.1f %s", v, units[i])
}

// snapshotMountOptions returns the mount options for an LVM snapshot of a
// filesystem of type fsType. An XFS snapshot carries the same UUID as its
// mounted origin, and XFS refuses to mount a duplicate UUID without nouuid,
// so every snapshot of an XFS root (the RHEL default) failed to mount.
func snapshotMountOptions(fsType string) string {
	if fsType == "xfs" {
		return "ro,nouuid"
	}
	return "ro"
}
