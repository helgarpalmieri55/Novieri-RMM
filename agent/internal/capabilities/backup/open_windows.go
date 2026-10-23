//go:build windows

package backup

import (
	"os"
	"sync"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Reading with backup semantics (spec-server-backup.md §4.1, D10).
//
// FILE_FLAG_BACKUP_SEMANTICS bypasses a file's DACL only while the caller's
// token has SeBackupPrivilege ENABLED, which it is not by default even for
// SYSTEM. So a run enables it for its own duration, and every exit path
// disables it again: a service holding it enabled at idle could read any file
// through any code path, not only this one.

const (
	privBackup   = "SeBackupPrivilege"
	privRestore  = "SeRestorePrivilege"
	privSecurity = "SeSecurityPrivilege"
	privTakeOwn  = "SeTakeOwnershipPrivilege"
)

func openForBackup(p string) (*os.File, error) {
	u, err := windows.UTF16PtrFromString(p)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: p, Err: err}
	}
	h, err := windows.CreateFile(u, windows.GENERIC_READ,
		windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE|windows.FILE_SHARE_DELETE, nil,
		windows.OPEN_EXISTING, windows.FILE_FLAG_BACKUP_SEMANTICS|windows.FILE_FLAG_SEQUENTIAL_SCAN, 0)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: p, Err: err}
	}
	return os.NewFile(uintptr(h), p), nil
}

// openDirForBackup uses os.Open: Go already opens a read-only handle with
// FILE_FLAG_BACKUP_SEMANTICS (a directory handle needs it), and the enabled
// privilege is what makes that bypass the DACL.
func openDirForBackup(p string) (*os.File, error) { return os.Open(p) }

func acquireBackupPrivileges() (func(), []string) {
	release, missing := enablePrivileges(privBackup, privSecurity)
	var warn []string
	for _, m := range missing {
		if m == privBackup {
			warn = append(warn, warnPrivilegeNotHeld)
		}
	}
	return release, warn
}

func acquireRestorePrivileges() (func(), []string) {
	release, missing := enablePrivileges(privBackup, privRestore, privSecurity, privTakeOwn)
	var warn []string
	for _, m := range missing {
		if m == privRestore {
			warn = append(warn, warnPrivilegeNotHeld)
		}
	}
	return release, warn
}

var (
	modadvapi32                    = windows.NewLazySystemDLL("advapi32.dll")
	procAdjustTokenPrivilegesCheck = modadvapi32.NewProc("AdjustTokenPrivileges")

	// privMu and privRefs count concurrent jobs holding a privilege, so one
	// job's release does not disable it under another. Nil until first use.
	privMu   sync.Mutex
	privRefs map[string]int
)

// enablePrivileges enables each named privilege on the process token and
// returns the release that disables the ones it enabled, plus the names the
// token does not hold at all.
func enablePrivileges(names ...string) (func(), []string) {
	privMu.Lock()
	defer privMu.Unlock()
	if privRefs == nil {
		privRefs = make(map[string]int)
	}
	var enabled, missing []string
	for _, n := range names {
		if privRefs[n] > 0 {
			privRefs[n]++
			enabled = append(enabled, n)
			continue
		}
		wasOn, err := setPrivilege(n, true)
		if err != nil {
			missing = append(missing, n)
			continue
		}
		if wasOn {
			// Enabled before this job; not ours to disable.
			continue
		}
		privRefs[n] = 1
		enabled = append(enabled, n)
	}
	var once sync.Once
	return func() {
		once.Do(func() {
			privMu.Lock()
			defer privMu.Unlock()
			for _, n := range enabled {
				privRefs[n]--
				if privRefs[n] <= 0 {
					delete(privRefs, n)
					_, _ = setPrivilege(n, false)
				}
			}
		})
	}, missing
}

// setPrivilege enables or disables one privilege and reports whether it was
// enabled before. A privilege the token does not hold is an error:
// AdjustTokenPrivileges reports that only through ERROR_NOT_ALL_ASSIGNED with
// a success return, which the generated wrapper does not surface.
func setPrivilege(name string, enable bool) (bool, error) {
	var tok windows.Token
	if err := windows.OpenProcessToken(windows.CurrentProcess(),
		windows.TOKEN_ADJUST_PRIVILEGES|windows.TOKEN_QUERY, &tok); err != nil {
		return false, err
	}
	defer tok.Close() //nolint:errcheck
	n, err := windows.UTF16PtrFromString(name)
	if err != nil {
		return false, err
	}
	var luid windows.LUID
	if err := windows.LookupPrivilegeValue(nil, n, &luid); err != nil {
		return false, err
	}
	var attrs uint32
	if enable {
		attrs = windows.SE_PRIVILEGE_ENABLED
	}
	tp := windows.Tokenprivileges{PrivilegeCount: 1}
	tp.Privileges[0] = windows.LUIDAndAttributes{Luid: luid, Attributes: attrs}
	var prev windows.Tokenprivileges
	var retLen uint32
	r1, _, callErr := procAdjustTokenPrivilegesCheck.Call(uintptr(tok), 0,
		uintptr(unsafe.Pointer(&tp)), unsafe.Sizeof(prev), uintptr(unsafe.Pointer(&prev)),
		uintptr(unsafe.Pointer(&retLen)))
	if r1 == 0 {
		return false, callErr
	}
	if callErr == windows.ERROR_NOT_ALL_ASSIGNED {
		return false, callErr
	}
	wasOn := prev.PrivilegeCount == 0 && enable // unchanged: it already had this state
	if prev.PrivilegeCount > 0 {
		wasOn = prev.Privileges[0].Attributes&windows.SE_PRIVILEGE_ENABLED != 0
	}
	return wasOn, nil
}
