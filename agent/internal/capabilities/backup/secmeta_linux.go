//go:build linux

package backup

import (
	"encoding/base64"
	"errors"
	"strings"
	"syscall"

	"golang.org/x/sys/unix"
)

func ctimeOf(st *syscall.Stat_t) int64 { return st.Ctim.Nano() }

// maxXattrBytes bounds what one entry may carry in xattrs; a file with more is
// captured without them and counted.
const maxXattrBytes = 64 << 10

// xattrKept is §4.4's list: capabilities, SELinux labels, POSIX ACLs and
// trusted.* (overlay and similar). user.* is application data, not security.
func xattrKept(name string) bool {
	return strings.HasPrefix(name, "security.") ||
		strings.HasPrefix(name, "trusted.") ||
		name == "system.posix_acl_access" || name == "system.posix_acl_default"
}

func captureXattrs(p string, e *entryV2) error {
	sz, err := unix.Llistxattr(p, nil)
	if err != nil {
		if errors.Is(err, unix.ENOTSUP) || errors.Is(err, unix.ENODATA) {
			return nil
		}
		return err
	}
	if sz == 0 {
		return nil
	}
	if sz > maxXattrBytes {
		return errors.New("xattr names exceed the bound")
	}
	names := make([]byte, sz)
	if sz, err = unix.Llistxattr(p, names); err != nil {
		return err
	}
	total := 0
	for _, n := range strings.Split(string(names[:sz]), "\x00") {
		if n == "" || !xattrKept(n) {
			continue
		}
		vsz, err := unix.Lgetxattr(p, n, nil)
		if err != nil {
			continue
		}
		total += vsz
		if total > maxXattrBytes {
			return errors.New("xattr values exceed the bound")
		}
		val := make([]byte, vsz)
		if vsz, err = unix.Lgetxattr(p, n, val); err != nil {
			continue
		}
		if e.Xattrs == nil {
			e.Xattrs = map[string]string{}
		}
		e.Xattrs[n] = base64.StdEncoding.EncodeToString(val[:vsz])
	}
	return nil
}

func applyXattrs(p string, e *entryV2) error {
	var first error
	for n, v := range e.Xattrs {
		if !xattrKept(n) {
			continue // a manifest names only what this agent would capture
		}
		val, err := base64.StdEncoding.DecodeString(v)
		if err != nil {
			if first == nil {
				first = err
			}
			continue
		}
		if err := unix.Lsetxattr(p, n, val, 0); err != nil && first == nil {
			first = err
		}
	}
	return first
}
