//go:build windows

package secdir

import (
	"errors"
	"fmt"
	"unsafe"

	"golang.org/x/sys/windows"
)

// scratchSDDL is the DACL applied to a job's scratch directory.
//
//	D:PAI            a PROTECTED, auto-inherited DACL. `P` is the important
//	                 letter: it stops the parent directory's ACEs being
//	                 inherited, so a %TEMP% that some other product widened
//	                 does not widen this.
//	(A;OICI;FA;;;SY) full access for NT AUTHORITY\SYSTEM, inherited by the
//	                 objects and containers inside (the script body, the
//	                 staged installer).
//	(A;OICI;FA;;;BA) full access for BUILTIN\Administrators, so an
//	                 administrator can inspect a failed job.
//
// Nobody else is named, and because the DACL is protected, nobody else is
// inherited in either. In particular there is no ACE for Users, Authenticated
// Users, or the interactive user — which is the whole point: a local
// non-admin must not be able to read a script body or replace a
// hash-verified installer between staging and execution.
const scratchSDDL = "D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"

// Restrict replaces the directory's DACL with scratchSDDL.
//
// NEVER EXECUTED IN CI. This file compiles under GOOS=windows and is vetted on
// every build, but the container this was written in is Linux, so the ACL it
// produces has not been observed on a real Windows 11 endpoint. That is stated
// here rather than left for someone to discover: the falsifying observation is
// `icacls <scratch dir>` on a deployed agent showing any principal other than
// SYSTEM and Administrators, or showing inherited ACEs.
//
// PROTECTED_DACL_SECURITY_INFORMATION is passed alongside
// DACL_SECURITY_INFORMATION so the protection bit itself is applied; setting a
// protected SDDL without it leaves inheritance on, which is the failure mode
// that looks correct in the source and is wrong on the box.
func Restrict(dir string) error {
	sd, err := windows.SecurityDescriptorFromString(scratchSDDL)
	if err != nil {
		return fmt.Errorf("scratch DACL is not a valid security descriptor: %w", err)
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		return fmt.Errorf("scratch DACL could not be read back: %w", err)
	}
	if err := windows.SetNamedSecurityInfo(
		dir,
		windows.SE_FILE_OBJECT,
		windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION,
		nil, nil, dacl, nil,
	); err != nil {
		return fmt.Errorf("scratch DACL could not be applied: %w", err)
	}
	return nil
}

// ownedSDDL is scratchSDDL with an owner: BUILTIN\Administrators.
const ownedSDDL = "O:BA" + scratchSDDL

// MkdirOwned creates dir -- which must not exist -- with ownedSDDL as its
// security descriptor, in ONE CreateDirectory call. There is no moment at
// which the directory exists with the ACL it would have inherited, which is
// what a staging directory for an image that will run as SYSTEM needs (the
// uninstall helper's copy, review M1). The caller must be elevated or SYSTEM.
func MkdirOwned(dir string) error {
	sd, err := windows.SecurityDescriptorFromString(ownedSDDL)
	if err != nil {
		return fmt.Errorf("owned DACL is not a valid security descriptor: %w", err)
	}
	path, err := windows.UTF16PtrFromString(dir)
	if err != nil {
		return err
	}
	sa := &windows.SecurityAttributes{SecurityDescriptor: sd}
	sa.Length = uint32(unsafe.Sizeof(*sa))
	if err := windows.CreateDirectory(path, sa); err != nil {
		return fmt.Errorf("create %s: %w", dir, err)
	}
	return nil
}

// RestrictOwned is Restrict plus the OWNER: BUILTIN\Administrators.
//
// The DACL alone does not protect a directory a local user could have
// PRE-CREATED -- `%ProgramData%\ITSuite\agent` before the first install, or a
// staging directory under `%ProgramData%` -- because that user is its owner,
// and an owner is always granted READ_CONTROL and WRITE_DAC whatever the DACL
// says. Taking ownership for Administrators removes the one principal the
// DACL cannot. The caller must be elevated (an administrator's token carries
// BUILTIN\Administrators as an assignable owner; LocalSystem's does too).
//
// Executed on windows-latest by owned_windows_test.go.
//
// It sets the descriptor BY NAME, so it follows a junction and does not
// inspect the directory first. The self-installer used it on the data
// directory until 2026-09-29 and now uses SecureOwned on the data directory
// AND its parent; so should anything whose parent a non-administrator might
// control.
func RestrictOwned(dir string) error {
	owner, dacl, err := ownedParts()
	if err != nil {
		return err
	}
	if err := windows.SetNamedSecurityInfo(
		dir,
		windows.SE_FILE_OBJECT,
		windows.OWNER_SECURITY_INFORMATION|windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION,
		owner, nil, dacl, nil,
	); err != nil {
		return fmt.Errorf("owner and DACL could not be applied: %w", err)
	}
	return nil
}

// ownedParts is ownedSDDL's owner and DACL.
func ownedParts() (*windows.SID, *windows.ACL, error) {
	sd, err := windows.SecurityDescriptorFromString(ownedSDDL)
	if err != nil {
		return nil, nil, fmt.Errorf("owned DACL is not a valid security descriptor: %w", err)
	}
	owner, _, err := sd.Owner()
	if err != nil {
		return nil, nil, fmt.Errorf("owned DACL has no owner: %w", err)
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		return nil, nil, fmt.Errorf("owned DACL could not be read back: %w", err)
	}
	return owner, dacl, nil
}

// SecureOwned leaves dir a directory owned by BUILTIN\Administrators with
// ownedSDDL's protected SYSTEM+Administrators DACL, and refuses a junction or
// symbolic link.
//
// It exists for the PARENT of a protected directory, which is where that
// protection actually lives: whoever owns `%ProgramData%\ITSuite` holds
// FILE_DELETE_CHILD on it, and can rename the protected `agent` directory
// away and put their own in its place. On a default Windows install
// BUILTIN\Users may create folders in C:\ProgramData and CREATOR OWNER is
// granted Full Control on them, so that parent can be pre-created by anyone.
//
//   - Absent: created by MkdirOwned, with its descriptor, in one call.
//     Nothing can be inside it that an administrator did not put there, so
//     `accept` is not consulted.
//   - Present: opened WITHOUT following a reparse point, and refused if it is
//     one or is not a directory. `accept`, when not nil, is shown the current
//     owner and may refuse -- before anything is changed. Then the owner and
//     DACL are set THROUGH THAT HANDLE, so what is changed is exactly what was
//     inspected. The handle does not share delete: nobody can rename the
//     directory away while it is held, and once this returns nobody but
//     SYSTEM and Administrators can.
//
// What taking ownership cannot undo, and why a fresh install passes `accept`:
// anything the previous owner planted inside survives the new ACL, and a
// handle they opened earlier (with WRITE_DAC, say) keeps the access it was
// granted. A directory a non-administrator owned is only safe if refused.
//
// The caller must be elevated or SYSTEM. Executed on windows-latest by
// secure_windows_test.go; the install-link e2e reads ITSuite back.
func SecureOwned(dir string, accept func(owner *windows.SID) error) error {
	err := MkdirOwned(dir)
	if err == nil || !errors.Is(err, windows.ERROR_ALREADY_EXISTS) {
		return err
	}
	h, err := openDirNoFollow(dir, windows.READ_CONTROL|windows.WRITE_DAC|windows.WRITE_OWNER)
	if err != nil {
		return err
	}
	defer windows.CloseHandle(h)
	if accept != nil {
		owner, err := ownerOf(dir, h)
		if err != nil {
			return err
		}
		if err := accept(owner); err != nil {
			return err
		}
	}
	owner, dacl, err := ownedParts()
	if err != nil {
		return err
	}
	if err := windows.SetSecurityInfo(h, windows.SE_FILE_OBJECT,
		windows.OWNER_SECURITY_INFORMATION|windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION,
		owner, nil, dacl, nil); err != nil {
		return fmt.Errorf("%s: owner and DACL could not be applied: %w", dir, err)
	}
	return nil
}

// CheckOwned inspects dir without changing it: absent is fine; a junction, a
// symbolic link or a file is refused; otherwise `accept`, when not nil, is
// shown the owner. For a directory whose inherited ACL is already right (the
// install directory under %ProgramFiles%, where BUILTIN\Users cannot create
// folders) but whose provenance still needs checking.
func CheckOwned(dir string, accept func(owner *windows.SID) error) error {
	h, err := openDirNoFollow(dir, windows.READ_CONTROL)
	if errors.Is(err, windows.ERROR_FILE_NOT_FOUND) || errors.Is(err, windows.ERROR_PATH_NOT_FOUND) {
		return nil
	}
	if err != nil {
		return err
	}
	defer windows.CloseHandle(h)
	if accept == nil {
		return nil
	}
	owner, err := ownerOf(dir, h)
	if err != nil {
		return err
	}
	return accept(owner)
}

// openDirNoFollow opens dir itself -- a reparse point is opened, not followed
// -- and refuses anything that is not a plain directory. No FILE_SHARE_DELETE.
func openDirNoFollow(dir string, access uint32) (windows.Handle, error) {
	p, err := windows.UTF16PtrFromString(dir)
	if err != nil {
		return 0, err
	}
	h, err := windows.CreateFile(p, access, windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE, nil,
		windows.OPEN_EXISTING, windows.FILE_FLAG_BACKUP_SEMANTICS|windows.FILE_FLAG_OPEN_REPARSE_POINT, 0)
	if err != nil {
		return 0, fmt.Errorf("open %s: %w", dir, err)
	}
	var fi windows.ByHandleFileInformation
	if err := windows.GetFileInformationByHandle(h, &fi); err != nil {
		windows.CloseHandle(h)
		return 0, fmt.Errorf("read the attributes of %s: %w", dir, err)
	}
	switch {
	case fi.FileAttributes&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0:
		windows.CloseHandle(h)
		return 0, fmt.Errorf("%s is a junction or symbolic link, not a directory; "+
			"an administrator must remove it", dir)
	case fi.FileAttributes&windows.FILE_ATTRIBUTE_DIRECTORY == 0:
		windows.CloseHandle(h)
		return 0, fmt.Errorf("%s is not a directory", dir)
	}
	return h, nil
}

// ownerOf reads the owner through the handle openDirNoFollow returned.
func ownerOf(dir string, h windows.Handle) (*windows.SID, error) {
	sd, err := windows.GetSecurityInfo(h, windows.SE_FILE_OBJECT, windows.OWNER_SECURITY_INFORMATION)
	if err != nil {
		return nil, fmt.Errorf("read the owner of %s: %w", dir, err)
	}
	owner, _, err := sd.Owner()
	if err != nil || owner == nil {
		return nil, fmt.Errorf("%s has no readable owner", dir)
	}
	return owner, nil
}
