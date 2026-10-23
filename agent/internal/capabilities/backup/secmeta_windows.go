//go:build windows

package backup

import (
	"errors"
	"io/fs"

	"golang.org/x/sys/windows"
)

func modeBits(fi fs.FileInfo) uint32 { return uint32(fi.Mode().Perm()) }

// No live read on Windows (VSS always), and no one-filesystem rule.
func deviceOf(fs.FileInfo) (uint64, bool) { return 0, false }

func platformInodeOf(string) (uint64, error) { return 0, errors.New("no inode on this platform") }

func sameFile(a, b fs.FileInfo) bool {
	return a.Size() == b.Size() && a.ModTime().Equal(b.ModTime())
}

const secInfoBase = windows.OWNER_SECURITY_INFORMATION | windows.GROUP_SECURITY_INFORMATION |
	windows.DACL_SECURITY_INFORMATION

// platformCapture stores the owner, group and DACL as SDDL, and the SACL when
// SeSecurityPrivilege is held (its absence is warned, not fatal: §4.1).
func platformCapture(m *metaCapture, p string, fi fs.FileInfo, e *entryV2) {
	link := fi.Mode()&fs.ModeSymlink != 0
	sddl, err := readSDDL(p, link, !m.saclMissing)
	if err != nil && !m.saclMissing &&
		(errors.Is(err, windows.ERROR_PRIVILEGE_NOT_HELD) || errors.Is(err, windows.ERROR_ACCESS_DENIED)) {
		if sddl, err = readSDDL(p, link, false); err == nil {
			m.saclMissing = true
		}
	}
	if err != nil {
		m.unreadable++
		return
	}
	e.SD = &sddl
}

func readSDDL(p string, link, sacl bool) (string, error) {
	u, err := windows.UTF16PtrFromString(p)
	if err != nil {
		return "", err
	}
	access := uint32(windows.READ_CONTROL)
	info := windows.SECURITY_INFORMATION(secInfoBase)
	if sacl {
		access |= windows.ACCESS_SYSTEM_SECURITY
		info |= windows.SACL_SECURITY_INFORMATION
	}
	flags := uint32(windows.FILE_FLAG_BACKUP_SEMANTICS)
	if link {
		flags |= windows.FILE_FLAG_OPEN_REPARSE_POINT
	}
	h, err := windows.CreateFile(u, access,
		windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE|windows.FILE_SHARE_DELETE, nil,
		windows.OPEN_EXISTING, flags, 0)
	if err != nil {
		return "", err
	}
	defer windows.CloseHandle(h) //nolint:errcheck
	sd, err := windows.GetSecurityInfo(h, windows.SE_FILE_OBJECT, info)
	if err != nil {
		return "", err
	}
	return sd.String(), nil
}

// applyMeta applies the SDDL with SeRestorePrivilege held, so an owner other
// than the restoring account can be set (§7.1). A SID unknown here is applied
// raw; a failure is counted and the bytes stay restored.
func applyMeta(a *metaApply, p string, e *entryV2) {
	if e.SD == nil || *e.SD == "" {
		return
	}
	if err := applySDDL(p, *e.SD, true); err != nil {
		if err2 := applySDDL(p, *e.SD, false); err2 != nil {
			a.notApplied++
		}
	}
}

func applySDDL(p, sddl string, withSACL bool) error {
	sd, err := windows.SecurityDescriptorFromString(sddl)
	if err != nil {
		return err
	}
	control, _, err := sd.Control()
	if err != nil {
		return err
	}
	var info windows.SECURITY_INFORMATION
	owner, _, _ := sd.Owner()
	if owner != nil {
		info |= windows.OWNER_SECURITY_INFORMATION
	}
	group, _, _ := sd.Group()
	if group != nil {
		info |= windows.GROUP_SECURITY_INFORMATION
	}
	var dacl, sacl *windows.ACL
	if control&windows.SE_DACL_PRESENT != 0 {
		dacl, _, _ = sd.DACL()
		info |= windows.DACL_SECURITY_INFORMATION
		if control&windows.SE_DACL_PROTECTED != 0 {
			info |= windows.PROTECTED_DACL_SECURITY_INFORMATION
		} else {
			info |= windows.UNPROTECTED_DACL_SECURITY_INFORMATION
		}
	}
	if withSACL && control&windows.SE_SACL_PRESENT != 0 {
		sacl, _, _ = sd.SACL()
		info |= windows.SACL_SECURITY_INFORMATION
		if control&windows.SE_SACL_PROTECTED != 0 {
			info |= windows.PROTECTED_SACL_SECURITY_INFORMATION
		} else {
			info |= windows.UNPROTECTED_SACL_SECURITY_INFORMATION
		}
	}
	if info == 0 {
		return nil
	}
	return windows.SetNamedSecurityInfo(p, windows.SE_FILE_OBJECT, info, owner, group, dacl, sacl)
}
