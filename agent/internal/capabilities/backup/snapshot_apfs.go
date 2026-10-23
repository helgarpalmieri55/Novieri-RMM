//go:build !windows

package backup

// Built on every OS but Windows, whose provider is VSS and whose paths are not
// POSIX mount paths. It only shells out through the injected snapRunner, so
// every decision it makes -- snapshot creation, the mount invocation, the
// sweep filter, and path translation -- is unit-tested on any Unix machine,
// including one with no APFS at all (snapshot_apfs_test.go; those
// tests pass when run locally, and CI's agent job is currently red for reasons
// outside this package -- see snapshot_darwin.go's header). Only the dispatch
// that SELECTS this provider (snapshot_darwin.go) needs `//go:build darwin`;
// putting the logic itself behind a darwin tag would make it untestable on Linux,
// which is most of the value of this file.
//
// TESTED IS NOT VERIFIED HERE, and the distinction is larger than it is for
// LVM. What the tests exercise is this file's DECISIONS against a fake macOS;
// what no test in this project touches is whether the fake is right. tmutil's
// stdout format, mount_apfs's flags, `mount`'s output shape, and the claim
// that /System/Volumes/Data is what a root's offset should be measured from
// are all UNVERIFIED assumptions, each marked as such at the point it is made.
// There is no Mac in this project and LVM's loopback equivalent does not exist
// for APFS.

import (
	"context"
	"fmt"
	"log/slog"
	"path/filepath"
	"regexp"
	"strings"
)

// apfsMountRoot is snapMountRoot, indirected through a var.
//
// Production code never touches this: the zero-value initializer here IS
// snapMountRoot, so openAPFS mounts in exactly the place snapMountPath names
// everywhere else in this package -- nothing about a real agent's behavior
// changes.
//
// The indirection exists purely so
// TestOpenAPFSRefusesWhenTheSnapshotPresentsNoData can point it at a
// t.TempDir(). That test needs verifyPresents to perform a REAL, successful
// os.Open on an EMPTY directory at the exact path openAPFS mounts to -- that
// real read is the whole of the TCC simulation (see snapshot.go's
// verifyPresents doc comment) -- and the real snapMountRoot
// (/var/lib/toeshee-agent/snap) is not writable by the unprivileged account CI
// runs `go test` as. Without this seam that one test would need either root
// (unavailable in CI) or a fake past a REAL os.Open call, which is exactly the
// trap the note atop snapshot_lvm_test.go warns about. Every other test in
// this file leaves its root empty instead, so verifyPresents' liveN == 0
// branch short-circuits and never looks at the translated path at all -- the
// same technique the LVM tests already rely on.
var apfsMountRoot = snapMountRoot

// apfsSnapMountPoint is snapMountPath's naming scheme, re-rooted through
// apfsMountRoot. It calls the real snapMountPath so the NAME -- the part every
// snapshot provider and the orphan sweep agree on -- never drifts from it;
// only the base directory is swappable, and only in tests.
func apfsSnapMountPoint(tag string, n int) string {
	return filepath.Join(apfsMountRoot, filepath.Base(snapMountPath(tag, n)))
}

// tmutilDateRE matches the date `tmutil localsnapshot` prints after creating
// a snapshot, e.g. "Created local snapshot with date: 2024-01-15-142530".
var tmutilDateRE = regexp.MustCompile(`date:\s*(\S+)`)

// parseTMUtilLocalSnapshotName turns `tmutil localsnapshot`'s stdout into the
// snapshot name mount_apfs's -s flag expects.
//
// UNVERIFIED: no macOS machine exists in this project (see
// snapshot_darwin.go's header). This follows Apple's documented naming for a
// tmutil-created local snapshot -- "com.apple.TimeMachine.<date>.local", the
// same form `tmutil listlocalsnapshots` prints -- but nothing here has run
// against real tmutil output. If the format is wrong, mount_apfs simply fails
// to find a snapshot by that name and openAPFS refuses at the mount step
// below; it does not silently mount nothing and report success -- that
// failure mode is exactly what verifyPresents exists to catch regardless.
func parseTMUtilLocalSnapshotName(stdout string) (string, error) {
	m := tmutilDateRE.FindStringSubmatch(stdout)
	if m == nil {
		return "", fmt.Errorf("tmutil localsnapshot: no snapshot date found in output %q", stdout)
	}
	return "com.apple.TimeMachine." + m[1] + ".local", nil
}

// apfsDataVolume is what `mount_apfs` is pointed at, and therefore what
// appears at the mountpoint: the whole APFS DATA VOLUME, not a subtree.
const apfsDataVolume = "/System/Volumes/Data"

// apfsRelative returns a root's offset inside the mounted snapshot, with no
// leading separator.
//
// UNVERIFIED, like everything else in this file that concerns a real macOS
// system: no Mac exists in this project (see snapshot_darwin.go's header).
// This is the REASONING, stated rather than assumed, and it is deliberately
// NOT the same computation as LVM's:
//
//   - LVM measures a root's offset from the mount point `findmnt --target`
//     reports for that root, because the snapshot is of THAT filesystem.
//   - APFS has nothing to ask. `mount_apfs` is given `/System/Volumes/Data`
//     unconditionally, so the tree that appears at the mountpoint is the data
//     volume's own root, and the offset must be measured from the data volume
//     rather than from whatever mount point a root happens to sit under.
//   - On a modern macOS the data volume's contents are FIRMLINKED into `/`:
//     `/Users` is physically `/System/Volumes/Data/Users`, and a policy will
//     ordinarily spell it the short way. So a root is first tried against the
//     data volume's own mount point (a policy that spells the long way), and
//     otherwise against `/` (the firmlinked spelling). Both yield the same
//     offset for the same directory.
//
// What this gets WRONG if the reasoning is wrong: a root on the read-only
// SYSTEM volume (`/System/Library`, say) is not in the data volume at all, so
// no offset inside this snapshot corresponds to it. That does not silently
// back up the wrong tree -- verifyPresents finds the translated path
// unreadable or empty and the run fails with a stated reason -- which is the
// failure mode this file's UNVERIFIED assumptions are all shaped to have.
//
// The `/` fallback is what makes that failure LOOK LIKE something else, and
// verifyPresents' reasons are worded for it. A root on a separate or external
// volume (`/Volumes/ExternalDisk/photos`) is under `/` but not under the data
// volume, so the fallback returns `Volumes/ExternalDisk/photos` and the run
// looks inside the snapshot at a path that is an empty mount-point directory
// there. It fails loudly, correctly -- but it is indistinguishable at that
// point from a missing Full Disk Access grant, so verifyPresents names BOTH
// causes rather than asserting the grant. UNVERIFIED: no Mac exists here to
// confirm which of the two a given fleet would actually hit.
func apfsRelative(root string) (string, bool) {
	if rel, ok := rootRelative(root, apfsDataVolume); ok {
		return rel, true
	}
	return rootRelative(root, string(filepath.Separator))
}

// openAPFS snapshots every root via a single whole-container APFS local
// snapshot: sweep any mountpoint this agent left behind from a prior crashed
// run, take one local snapshot with `tmutil localsnapshot`, mount that
// snapshot read-only ONCE at a mountpoint under apfsMountRoot, and give every
// root its own offset inside it.
//
// One snapshot serves every root -- unlike LVM, which snapshots one logical
// volume per distinct volume -- because `tmutil localsnapshot` snapshots the
// whole APFS volume group in one call; there is no per-directory equivalent to
// ask for. ONE MOUNT serves every root too: with apfsRelative computing each
// root's offset, a second mount of the identical snapshot at a second
// mountpoint would present exactly the same bytes, and every extra mount is
// one more thing to fail and one more thing to leak permanently when a umount
// sticks (see sweepAPFSMountpoints).
//
// # Refused versus failed
//
// Nothing here is a refusal. A root this provider will not map is refused by
// validateRoots in the shared core; everything below is something this agent
// tried and could not finish -- §9's `state:"failed"` result document.
func openAPFS(ctx context.Context, run snapRunner, tag string, roots []string) (Snapshot, error) {
	if err := validateRoots(roots); err != nil {
		return nil, err
	}
	if err := sweepAPFSMountpoints(ctx, run); err != nil {
		return nil, err
	}

	snapRes := run(ctx, nil, "tmutil", "localsnapshot")
	if !snapRes.ok() {
		// Neither the stderr nor any root is quoted: tmutil echoes the paths
		// it was given, and this text reaches `backup_runs.error`.
		return nil, snapFailuref("a local APFS snapshot could not be created (exit %d)", snapRes.ExitCode)
	}
	name, err := parseTMUtilLocalSnapshotName(snapRes.Stdout)
	if err != nil {
		return nil, snapFailuref("tmutil created a local snapshot but its name could not be " +
			"determined from the output")
	}

	// Index 0: there is exactly one mount, so the per-root numbering LVM needs
	// has nothing to distinguish here.
	mountPoint := apfsSnapMountPoint(tag, 0)
	if mkRes := run(ctx, nil, "mkdir", "-p", mountPoint); !mkRes.ok() {
		return nil, snapFailuref("the mountpoint for the local APFS snapshot could not be created "+
			"(exit %d)", mkRes.ExitCode)
	}
	if mountRes := run(ctx, nil, "mount_apfs", "-o", "ro", "-s", name,
		apfsDataVolume, mountPoint); !mountRes.ok() {
		return nil, snapFailuref("the local APFS snapshot could not be mounted (exit %d)", mountRes.ExitCode)
	}

	// umount is the ENTIRE teardown. Unlike LVM's snapshot LV, there is no
	// separate device this agent created to remove: tmutil owns the underlying
	// local snapshot's lifecycle, and this agent must never touch that with
	// `tmutil deletelocalsnapshots` -- see sweepAPFSMountpoints. One mount
	// means one teardown, so nothing here needs LVM's sync.Once guard: it is
	// attached to the first volume only.
	teardown := func() error {
		if res := run(context.Background(), nil, "umount", mountPoint); !res.ok() {
			return fmt.Errorf("umount %s: exit %d", mountPoint, res.ExitCode)
		}
		return nil
	}

	vols := make([]volSnapshot, 0, len(roots))
	for i, root := range roots {
		rel, ok := apfsRelative(root)
		if !ok {
			// Cannot happen: apfsRelative falls back to "/", which every
			// absolute path is under, and validateRoots has already refused a
			// root that is not absolute. Handled rather than asserted because
			// the alternative -- an empty rel -- is C1 again.
			_ = teardown()
			return nil, snapFailuref("the source root at index %d could not be located inside the "+
				"APFS data volume", i)
		}
		v := volSnapshot{origin: root, translated: filepath.Join(mountPoint, rel), mech: mechAPFS}
		if i == 0 {
			// The mount's ONLY teardown hangs off this one volume, so a run
			// reaching here with zero roots would mount and never unmount --
			// an empty `vols` makes Release a no-op. validateRoots refuses an
			// empty root list above, in the shared core, so this loop always
			// runs at least once and the teardown is always attached.
			v.teardown = teardown
		}
		vols = append(vols, v)
	}

	s := newMultiSnapshot(vols)
	if err := verifyPresents(roots, s); err != nil {
		// A rejection here must not leak the mount just made.
		_ = s.Release()
		return nil, err
	}
	return s, nil
}

// sweepAPFSMountpoints reclaims mountpoints this agent left behind under
// snapMountRoot, in case a previous run crashed or was killed between mount
// and teardown.
//
// This is deliberately NOT the LVM sweep's shape. LVM names every snapshot it
// creates (snapTagPrefix-prefixed) and sweeps by that name; APFS local
// snapshots are named by tmutil in Apple's own format, which this agent
// cannot tag, and a local snapshot found on the machine may be one Time
// Machine itself relies on. Deleting a user's Time Machine snapshot to tidy
// up would be a worse bug than the leak it fixes. So this sweep never asks
// tmutil about snapshots at all -- it only reclaims the agent's OWN
// mountpoints, identified by where they are (under snapMountRoot), found by
// listing what is currently mounted through the injected snapRunner seam
// rather than a real directory listing, for the same testability reason
// sweepLVMOrphans' doc comment gives.
//
// A failed umount is only logged, never returned as this sweep's error --
// and unlike sweepLVMOrphans, nothing downstream catches it either. There is
// no second pass here: LVM's sweep can fall back to lvremove failing loudly
// on a still-mounted LV, but an APFS mountpoint's only teardown IS umount, so
// once it fails there is nothing left to try. The job's own mount step does
// NOT collide with a stuck orphan and so does NOT refuse on its behalf: the
// mountpoint name is built from pidTag(), this process's pid, and a crashed
// agent restarts with a NEW pid, so the orphan a crash left behind sits at a
// DIFFERENT path than the one this job is about to mount at. The two paths
// only coincide in the unlikely case of a pid getting reused across restarts.
// So a genuinely stuck umount here is a real, permanent leak: that
// mountpoint stays mounted (read-only, harmless to the live filesystem, but
// consuming a mount) with nothing past this WarnContext line to surface it to
// an operator. Documented as a known gap rather than papered over with a
// second pass or an escalated error this project has no macOS machine to
// exercise: an untestable new failure path would be worse than an honestly
// stated one.
func sweepAPFSMountpoints(ctx context.Context, run snapRunner) error {
	res := run(ctx, nil, "mount")
	if !res.ok() {
		return snapFailuref("mounted filesystems could not be listed to reclaim orphaned snapshot "+
			"mounts (exit %d)", res.ExitCode)
	}

	for _, line := range strings.Split(res.Stdout, "\n") {
		mountPoint, ok := parseMountTarget(line)
		if !ok || !underSnapMountRoot(mountPoint) {
			continue
		}
		if umRes := run(ctx, nil, "umount", mountPoint); !umRes.ok() {
			// This line is the ONLY place a stuck orphan is ever reported.
			// Nothing downstream catches it: the mountpoint carries this
			// process's pid, a crashed agent restarts with a new one, so the
			// orphan sits at a different path than the one this job mounts at
			// and the job's own mount step never collides with it. Said this
			// way because the doc comment above says it -- an earlier version
			// of this message claimed the mount step would refuse on the
			// orphan's behalf, which commit fe7f3be withdrew from the doc
			// comment and left standing here.
			slog.WarnContext(ctx, "sweepAPFSMountpoints: umount failed; if this mountpoint is "+
				"genuinely still stuck it is a permanent leak, and this line is the only report of it",
				"mountpoint", mountPoint, "stderr", umRes.Stderr)
		}
	}
	return nil
}

// underSnapMountRoot reports whether p is snapMountRoot itself or a path
// beneath it. This is the ENTIRE filter sweepAPFSMountpoints applies: it must
// never match a mountpoint outside the agent's own directory, no matter what
// else `mount` reports (an ordinary volume, a Time Machine local snapshot
// browsed under /Volumes, a user's own mount).
func underSnapMountRoot(p string) bool {
	return p == snapMountRoot || strings.HasPrefix(p, snapMountRoot+string(filepath.Separator))
}

// parseMountTarget extracts the mountpoint from one line of `mount`'s output,
// e.g. "/dev/disk1s5s1 on /var/lib/toeshee-agent/snap/toeshee-snap-42-0
// (apfs, local, read-only, journaled, nobrowse)" -> the path between " on "
// and the trailing "(...)" options.
//
// UNVERIFIED, like parseTMUtilLocalSnapshotName: no macOS machine exists in
// this project to check a real `mount` line against. It fails safe either
// way -- a line this does not recognise is simply skipped (ok=false), and
// underSnapMountRoot's prefix check is the real safety boundary regardless of
// how many lines parse correctly, so a wrong assumption here costs a missed
// sweep, never a wrong umount target.
func parseMountTarget(line string) (string, bool) {
	const sep = " on "
	idx := strings.Index(line, sep)
	if idx < 0 {
		return "", false
	}
	rest := line[idx+len(sep):]
	paren := strings.LastIndex(rest, " (")
	if paren < 0 {
		return "", false
	}
	return rest[:paren], true
}
