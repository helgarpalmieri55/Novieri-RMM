package backup

import (
	"io/fs"
	"os"
)

// Security metadata (spec-server-backup.md §4.1, §4.4, §7.1; D8).
//
// Captured into format-2 entries: the SDDL security descriptor on Windows;
// uid, gid, user and group names, full mode bits and selected xattrs on Linux.
// Restored onto the temporary file before it is renamed into place, so the
// file never appears with the wrong owner or ACL. A failure to capture or
// apply is counted and warned, never a reason to drop the file's bytes.

// metaCapture holds one job's lookups: user and group names per id are read
// once, not once per file.
type metaCapture struct {
	users       map[uint32]string
	groups      map[uint32]string
	unreadable  int64
	saclMissing bool
}

func newMetaCapture() *metaCapture {
	return &metaCapture{users: map[uint32]string{}, groups: map[uint32]string{}}
}

func (m *metaCapture) capture(p string, fi fs.FileInfo, e *entryV2) { platformCapture(m, p, fi, e) }

func (m *metaCapture) warnings() []string {
	var w []string
	if m.saclMissing {
		w = append(w, warnSACLNotCaptured)
	}
	if m.unreadable > 0 {
		w = append(w, warnSecurityUnreadable)
	}
	return w
}

func (o *osTreeFS) warnings() []string { return o.meta.warnings() }

// metaApply holds one restore's lookups and outcome.
type metaApply struct {
	uids         map[string]uint32
	gids         map[string]uint32
	notApplied   int64
	ownerSkipped int64
}

func newMetaApply() *metaApply {
	return &metaApply{uids: map[string]uint32{}, gids: map[string]uint32{}}
}

func (a *metaApply) warnings() []string {
	var w []string
	if a.notApplied > 0 {
		w = append(w, warnSecurityNotApplied)
	}
	if a.ownerSkipped > 0 {
		w = append(w, warnOwnershipNotApplied)
	}
	return w
}

// goMode turns stored mode bits (unix st_mode & 07777 under format 2) into an
// fs.FileMode os.Chmod understands, special bits included.
func goMode(m uint32) fs.FileMode {
	mode := fs.FileMode(m & 0o777)
	if m&0o4000 != 0 {
		mode |= os.ModeSetuid
	}
	if m&0o2000 != 0 {
		mode |= os.ModeSetgid
	}
	if m&0o1000 != 0 {
		mode |= os.ModeSticky
	}
	return mode
}
