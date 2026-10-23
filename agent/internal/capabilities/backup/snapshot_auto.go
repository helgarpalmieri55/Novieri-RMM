//go:build !windows

package backup

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"strings"
	"sync"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// `snapshot: "auto"` on Linux (spec-server-backup.md §4.4): per root, a volume
// snapshot where the filesystem can take one, otherwise a live read the result
// flags as not crash-consistent.
//
// Order: btrfs and ZFS are asked of the filesystem first, then LVM, then live.
// The spec lists LVM first; the one place that matters is btrfs on an LV, and
// there a block-level snapshot is the wrong tool: mounting it presents a second
// copy of a mounted btrfs filesystem's UUID, which btrfs does not tolerate.
//
// No untagged file here runs a command itself: everything goes through the
// snapRunner, so the selection is tested on a machine with none of the tools.

// snapNamePrefix marks btrfs and ZFS snapshots this agent creates. The orphan
// sweeps remove only names carrying it.
const snapNamePrefix = "itms-"

// btrfsSubvolRootIno is BTRFS_FIRST_FREE_OBJECTID: the inode number of every
// subvolume's root directory.
const btrfsSubvolRootIno = 256

// inodeOf is a seam so btrfs subvolume discovery is testable without btrfs.
var inodeOf = platformInodeOf

type mountInfo struct {
	source, target, fstype, fsroot string
}

type findmntFullJSON struct {
	Filesystems []struct {
		Source string `json:"source"`
		Target string `json:"target"`
		Fstype string `json:"fstype"`
		Fsroot string `json:"fsroot"`
	} `json:"filesystems"`
}

func findMount(ctx context.Context, run snapRunner, root string) (mountInfo, bool) {
	res := run(ctx, nil, "findmnt", "-n", "-o", "SOURCE,TARGET,FSTYPE,FSROOT", "--target", root, "--json")
	if !res.ok() {
		return mountInfo{}, false
	}
	var doc findmntFullJSON
	if err := json.Unmarshal([]byte(strings.TrimSpace(res.Stdout)), &doc); err != nil || len(doc.Filesystems) == 0 {
		return mountInfo{}, false
	}
	last := doc.Filesystems[len(doc.Filesystems)-1]
	if last.Source == "" || last.Target == "" {
		return mountInfo{}, false
	}
	return mountInfo{source: last.Source, target: last.Target, fstype: last.Fstype, fsroot: last.Fsroot}, true
}

// openAuto is the Linux `auto` provider.
func openAuto(ctx context.Context, run snapRunner, tag string, roots []string, allowLive bool) (Snapshot, error) {
	if err := validateRoots(roots); err != nil {
		return nil, err
	}
	mounts := make([]mountInfo, len(roots))
	var lvmIdx, btrfsIdx, zfsIdx, liveIdx []int
	for i, root := range roots {
		mi, ok := findMount(ctx, run, root)
		if !ok {
			return nil, snapFailuref("the filesystem holding the source root at index %d could not be "+
				"resolved", i)
		}
		if _, ok := rootRelative(root, mi.target); !ok {
			return nil, snapFailuref("the source root at index %d is not under the mount point findmnt "+
				"reports for it", i)
		}
		mounts[i] = mi
		switch {
		case mi.fstype == "btrfs":
			btrfsIdx = append(btrfsIdx, i)
		case mi.fstype == "zfs":
			zfsIdx = append(zfsIdx, i)
		case onLogicalVolume(ctx, run, mi.source):
			lvmIdx = append(lvmIdx, i)
		default:
			liveIdx = append(liveIdx, i)
		}
	}
	if len(liveIdx) > 0 && !allowLive {
		return nil, jobs.Refuse("backup.run: the source root at index %d is on a filesystem with no "+
			"snapshot mechanism (not LVM, btrfs or ZFS), and this workload does not allow a live read",
			liveIdx[0])
	}

	var vols []volSnapshot
	fail := func(err error) (Snapshot, error) {
		releaseAll(vols)
		return nil, err
	}
	if len(lvmIdx) > 0 {
		v, err := lvmVolumes(ctx, run, tag, roots, lvmIdx)
		if err != nil {
			return fail(err)
		}
		vols = append(vols, v...)
	}
	if len(btrfsIdx) > 0 {
		v, err := btrfsVolumes(ctx, run, tag, roots, btrfsIdx, mounts)
		vols = append(vols, v...)
		if err != nil {
			return fail(err)
		}
	}
	if len(zfsIdx) > 0 {
		v, err := zfsVolumes(ctx, run, tag, roots, zfsIdx, mounts)
		vols = append(vols, v...)
		if err != nil {
			return fail(err)
		}
	}
	for _, i := range liveIdx {
		vols = append(vols, volSnapshot{origin: roots[i], translated: roots[i], mech: mechNone})
	}
	s := newMultiSnapshot(vols)
	if err := verifyPresents(roots, s); err != nil {
		_ = s.Release()
		return nil, err
	}
	return s, nil
}

func onLogicalVolume(ctx context.Context, run snapRunner, device string) bool {
	if !strings.HasPrefix(device, "/dev/") {
		return false
	}
	res := run(ctx, nil, "lvs", "--noheadings", "-o", "vg_name,lv_name", device)
	if !res.ok() {
		return false
	}
	_, _, ok := parseLVSLine(res.Stdout)
	return ok
}

// onceTeardown runs fn at most once and remembers its answer, so a teardown
// shared by several volumes reports one failure, not several.
func onceTeardown(fn func() error) func() error {
	var once sync.Once
	var err error
	return func() error {
		once.Do(func() { err = fn() })
		return err
	}
}

// ---------------------------------------------------------------------------
// btrfs
// ---------------------------------------------------------------------------

// subvolumeOf returns the root of the subvolume containing p: the nearest
// ancestor-or-self whose inode is 256, never above the mount point (which is
// always a subvolume root).
func subvolumeOf(p, mountTarget string) (string, error) {
	cur := filepath.Clean(p)
	top := filepath.Clean(mountTarget)
	for {
		ino, err := inodeOf(cur)
		if err != nil {
			return "", err
		}
		if ino == btrfsSubvolRootIno || cur == top {
			return cur, nil
		}
		parent := filepath.Dir(cur)
		if parent == cur {
			return top, nil
		}
		cur = parent
	}
}

// btrfsMountPath maps a subvolume path as `btrfs subvolume list` prints it
// (relative to the filesystem's top level) to where it appears under this
// mount, or ok=false when the mount does not expose it.
func btrfsMountPath(listed string, mi mountInfo) (string, bool) {
	rel := "/" + strings.TrimPrefix(listed, "<FS_TREE>/")
	rel = path.Clean(rel)
	fsroot := path.Clean("/" + strings.TrimPrefix(mi.fsroot, "/"))
	if fsroot != "/" {
		if rel != fsroot && !strings.HasPrefix(rel, fsroot+"/") {
			return "", false
		}
		rel = strings.TrimPrefix(rel, fsroot)
	}
	return filepath.Join(mi.target, filepath.FromSlash(rel)), true
}

// btrfsChildren lists the subvolumes directly below subvol, as mount paths.
func btrfsChildren(ctx context.Context, run snapRunner, subvol string, mi mountInfo) ([]string, error) {
	res := run(ctx, nil, "btrfs", "subvolume", "list", "-o", subvol)
	if !res.ok() {
		return nil, fmt.Errorf("btrfs subvolume list: exit %d", res.ExitCode)
	}
	var out []string
	for _, line := range strings.Split(res.Stdout, "\n") {
		i := strings.Index(line, " path ")
		if i < 0 {
			continue
		}
		listed := strings.TrimSpace(line[i+len(" path "):])
		if strings.HasPrefix(path.Base(listed), btrfsSnapDir) {
			continue
		}
		if p, ok := btrfsMountPath(listed, mi); ok {
			out = append(out, p)
		}
	}
	return out, nil
}

// sweepBtrfs deletes this agent's leftover snapshots directly inside subvol.
func sweepBtrfs(ctx context.Context, run snapRunner, subvol string) error {
	ents, err := os.ReadDir(subvol)
	if err != nil {
		return nil // an unreadable subvolume fails later, at the snapshot
	}
	for _, e := range ents {
		if !strings.HasPrefix(e.Name(), btrfsSnapDir) {
			continue
		}
		if res := run(ctx, nil, "btrfs", "subvolume", "delete", filepath.Join(subvol, e.Name())); !res.ok() {
			return snapFailuref("an orphaned btrfs snapshot could not be reclaimed (exit %d)", res.ExitCode)
		}
	}
	return nil
}

func btrfsVolumes(ctx context.Context, run snapRunner, tag string, roots []string, indices []int,
	mounts []mountInfo) ([]volSnapshot, error) {
	type plan struct {
		subvol string
		mi     mountInfo
	}
	var order []plan
	planned := map[string]bool{}
	add := func(sv string, mi mountInfo) {
		if !planned[sv] {
			planned[sv] = true
			order = append(order, plan{sv, mi})
		}
	}
	var rootVols []volSnapshot
	for _, i := range indices {
		root, mi := roots[i], mounts[i]
		sv, err := subvolumeOf(root, mi.target)
		if err != nil {
			return nil, snapFailuref("the btrfs subvolume holding the source root at index %d could not "+
				"be found", i)
		}
		add(sv, mi)
		rel, _ := rootRelative(root, sv)
		rootVols = append(rootVols, volSnapshot{
			origin:     root,
			translated: filepath.Join(sv, btrfsSnapDir+tag, rel),
			mech:       mechBtrfs,
		})
		// Snapshots are not recursive: every subvolume nested under the root
		// needs its own, or it reads as an empty directory.
		queue := []string{sv}
		for len(queue) > 0 {
			cur := queue[0]
			queue = queue[1:]
			kids, err := btrfsChildren(ctx, run, cur, mi)
			if err != nil {
				return nil, snapFailuref("the btrfs subvolumes under the source root at index %d could "+
					"not be listed", i)
			}
			for _, k := range kids {
				// sv is the nearest subvolume holding root, so a child is
				// either under root or beside it.
				if rel, ok := rootRelative(k, root); ok && rel != "" {
					queue = append(queue, k)
					add(k, mi)
				}
			}
		}
	}

	var vols []volSnapshot
	for n, p := range order {
		if err := sweepBtrfs(ctx, run, p.subvol); err != nil {
			return vols, err
		}
		snap := filepath.Join(p.subvol, btrfsSnapDir+tag)
		if res := run(ctx, nil, "btrfs", "subvolume", "snapshot", "-r", p.subvol, snap); !res.ok() {
			return vols, snapFailuref("a btrfs snapshot could not be created for subvolume %d of this "+
				"job (exit %d)", n, res.ExitCode)
		}
		td := onceTeardown(func() error {
			if res := run(context.Background(), nil, "btrfs", "subvolume", "delete", snap); !res.ok() {
				return fmt.Errorf("btrfs subvolume delete: exit %d", res.ExitCode)
			}
			return nil
		})
		vols = append(vols, volSnapshot{origin: p.subvol, translated: snap, teardown: td, mech: mechBtrfs})
	}
	// Root views ride on their subvolume's snapshot; its teardown is enough.
	vols = append(vols, rootVols...)
	return vols, nil
}

// ---------------------------------------------------------------------------
// ZFS
// ---------------------------------------------------------------------------

func zfsVolumes(ctx context.Context, run snapRunner, tag string, roots []string, indices []int,
	mounts []mountInfo) ([]volSnapshot, error) {
	snapName := snapNamePrefix + tag
	var vols []volSnapshot
	done := map[string]bool{}
	for _, i := range indices {
		root, mi := roots[i], mounts[i]
		ds := mi.source
		rel, _ := rootRelative(root, mi.target)
		vols = append(vols, volSnapshot{
			origin:     root,
			translated: filepath.Join(mi.target, ".zfs", "snapshot", snapName, rel),
			mech:       mechZFS,
		})
		if done[ds] {
			continue
		}
		done[ds] = true
		if strings.HasPrefix(ds, "-") || strings.ContainsAny(ds, "@ \t\n") {
			return vols, jobs.Refuse("backup.run: the ZFS dataset holding the source root at index %d "+
				"has a name this agent will not pass to zfs", i)
		}
		if err := sweepZFS(ctx, run, ds); err != nil {
			return vols, err
		}
		// -r is atomic across the dataset and its children.
		if res := run(ctx, nil, "zfs", "snapshot", "-r", ds+"@"+snapName); !res.ok() {
			return vols, snapFailuref("a ZFS snapshot could not be created for the dataset holding the "+
				"source root at index %d (exit %d)", i, res.ExitCode)
		}
		full := ds + "@" + snapName
		td := onceTeardown(func() error {
			if res := run(context.Background(), nil, "zfs", "destroy", "-r", full); !res.ok() {
				return fmt.Errorf("zfs destroy: exit %d", res.ExitCode)
			}
			return nil
		})
		vols = append(vols, volSnapshot{origin: mi.target, translated: filepath.Join(mi.target, ".zfs",
			"snapshot", snapName), teardown: td, mech: mechZFS})
		// Child datasets are mounted separately; each has its own .zfs view.
		res := run(ctx, nil, "zfs", "list", "-H", "-o", "name,mountpoint", "-t", "filesystem", "-r", ds)
		if !res.ok() {
			return vols, snapFailuref("the ZFS datasets under the source root at index %d could not be "+
				"listed", i)
		}
		for _, line := range strings.Split(res.Stdout, "\n") {
			f := strings.Split(strings.TrimRight(line, "\r"), "\t")
			if len(f) != 2 || f[0] == ds || !filepath.IsAbs(f[1]) {
				continue
			}
			vols = append(vols, volSnapshot{origin: filepath.Clean(f[1]),
				translated: filepath.Join(f[1], ".zfs", "snapshot", snapName), mech: mechZFS})
		}
	}
	return vols, nil
}

func sweepZFS(ctx context.Context, run snapRunner, ds string) error {
	res := run(ctx, nil, "zfs", "list", "-H", "-o", "name", "-t", "snapshot", "-r", ds)
	if !res.ok() {
		return snapFailuref("ZFS snapshots could not be listed to reclaim orphans (exit %d)", res.ExitCode)
	}
	for _, name := range strings.Split(res.Stdout, "\n") {
		name = strings.TrimSpace(name)
		at := strings.IndexByte(name, '@')
		if at < 0 || !strings.HasPrefix(name[at+1:], snapNamePrefix) {
			continue
		}
		if r := run(ctx, nil, "zfs", "destroy", name); !r.ok() {
			return snapFailuref("an orphaned ZFS snapshot could not be reclaimed (exit %d)", r.ExitCode)
		}
	}
	return nil
}
