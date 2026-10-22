//go:build linux || darwin

package backup

import (
	"errors"
	"io/fs"
	"os"
	"os/user"
	"strconv"
	"syscall"
)

func statOf(fi fs.FileInfo) *syscall.Stat_t {
	if fi == nil {
		return nil
	}
	st, _ := fi.Sys().(*syscall.Stat_t)
	return st
}

// modeBits is the unix mode, set-id and sticky bits included, which
// fs.FileMode.Perm() drops.
func modeBits(fi fs.FileInfo) uint32 {
	if st := statOf(fi); st != nil {
		return uint32(st.Mode) & 0o7777
	}
	m := uint32(fi.Mode().Perm())
	if fi.Mode()&os.ModeSetuid != 0 {
		m |= 0o4000
	}
	if fi.Mode()&os.ModeSetgid != 0 {
		m |= 0o2000
	}
	if fi.Mode()&os.ModeSticky != 0 {
		m |= 0o1000
	}
	return m
}

func deviceOf(fi fs.FileInfo) (uint64, bool) {
	if st := statOf(fi); st != nil {
		return uint64(st.Dev), true //nolint:unconvert // int32 on darwin
	}
	return 0, false
}

func platformInodeOf(p string) (uint64, error) {
	fi, err := os.Lstat(p)
	if err != nil {
		return 0, err
	}
	st := statOf(fi)
	if st == nil {
		return 0, errors.New("no inode for this file")
	}
	return uint64(st.Ino), nil //nolint:unconvert
}

// sameFile is the live read's change test: size, mtime, ctime and inode.
func sameFile(a, b fs.FileInfo) bool {
	if a.Size() != b.Size() || !a.ModTime().Equal(b.ModTime()) {
		return false
	}
	sa, sb := statOf(a), statOf(b)
	if sa == nil || sb == nil {
		return true
	}
	return sa.Ino == sb.Ino && ctimeOf(sa) == ctimeOf(sb)
}

func platformCapture(m *metaCapture, p string, fi fs.FileInfo, e *entryV2) {
	st := statOf(fi)
	if st == nil {
		return
	}
	uid, gid := st.Uid, st.Gid
	e.UID, e.GID = &uid, &gid
	e.User = m.userName(uid)
	e.Group = m.groupName(gid)
	if err := captureXattrs(p, e); err != nil {
		m.unreadable++
	}
}

func (m *metaCapture) userName(uid uint32) string {
	if n, ok := m.users[uid]; ok {
		return n
	}
	n := ""
	if u, err := user.LookupId(strconv.FormatUint(uint64(uid), 10)); err == nil {
		n = u.Username
	}
	m.users[uid] = n
	return n
}

func (m *metaCapture) groupName(gid uint32) string {
	if n, ok := m.groups[gid]; ok {
		return n
	}
	n := ""
	if g, err := user.LookupGroupId(strconv.FormatUint(uint64(gid), 10)); err == nil {
		n = g.Name
	}
	m.groups[gid] = n
	return n
}

// applyMeta sets ownership, then mode (chown clears the set-id bits), then
// xattrs; owners by name first and number second (§4.4). A symlink has no
// mode of its own.
func applyMeta(a *metaApply, p string, e *entryV2) {
	if e.UID != nil || e.GID != nil {
		uid, gid := -1, -1
		if e.UID != nil {
			uid = int(a.resolveUser(e.User, *e.UID))
		}
		if e.GID != nil {
			gid = int(a.resolveGroup(e.Group, *e.GID))
		}
		switch {
		case os.Geteuid() == 0:
			if err := os.Lchown(p, uid, gid); err != nil {
				a.notApplied++
			}
		case uid != -1 && uid != os.Geteuid():
			// Only root may give a file away; the bytes are restored anyway.
			a.ownerSkipped++
		}
	}
	if e.Kind != kindSymlink {
		if err := os.Chmod(p, goMode(e.Mode)); err != nil {
			a.notApplied++
		}
	}
	if err := applyXattrs(p, e); err != nil {
		a.notApplied++
	}
}

func (a *metaApply) resolveUser(name string, uid uint32) uint32 {
	if name == "" {
		return uid
	}
	if v, ok := a.uids[name]; ok {
		return v
	}
	v := uid
	if u, err := user.Lookup(name); err == nil {
		if n, err := strconv.ParseUint(u.Uid, 10, 32); err == nil {
			v = uint32(n)
		}
	}
	a.uids[name] = v
	return v
}

func (a *metaApply) resolveGroup(name string, gid uint32) uint32 {
	if name == "" {
		return gid
	}
	if v, ok := a.gids[name]; ok {
		return v
	}
	v := gid
	if g, err := user.LookupGroup(name); err == nil {
		if n, err := strconv.ParseUint(g.Gid, 10, 32); err == nil {
			v = uint32(n)
		}
	}
	a.gids[name] = v
	return v
}
