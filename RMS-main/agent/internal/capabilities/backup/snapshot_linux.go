//go:build linux

package backup

import "context"

// PlatformSnapshot takes an LVM snapshot on Linux for `lvm`, and for `auto`
// selects btrfs, ZFS, LVM or a flagged live read per root (snapshot_auto.go).
//
// The job id used in the tag is this process's pid: the tag exists so the
// orphan sweep can recognise snapshots THIS AGENT created, and a pid is enough
// for that. Threading the real job id would change Options.OpenSnapshot's
// signature for no gain.
func PlatformSnapshot(ctx context.Context, kind string, roots []string) (Snapshot, error) {
	if kind == snapshotAuto {
		return openAuto(ctx, execSnapRunner, pidTag(), roots, snapOptionsFrom(ctx).allowLive)
	}
	if err := requireKind(kind, snapshotLVM); err != nil {
		return nil, err
	}
	return openLVM(ctx, execSnapRunner, pidTag(), roots)
}
