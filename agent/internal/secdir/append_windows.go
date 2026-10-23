//go:build windows

package secdir

import (
	"errors"
	"fmt"
	"os"

	"golang.org/x/sys/windows"
)

// AppendLine appends line, then CRLF, to the file `name` inside dir: the Group
// Policy MSI's diagnostics (`%ProgramData%\ITSuite\msi.log`, written by
// `its-agent --msi-install` and `--msi-uninstall`).
//
// THE PARENT IS SECURED BEFORE THE FILE IS TOUCHED. dir goes through
// SecureOwned -- created owned and protected when absent, otherwise opened
// without following a reparse point and given the owned descriptor -- and an
// existing dir owned by anyone but BUILTIN\Administrators or SYSTEM is
// refused unchanged: a directory someone else owns is an administrator's to
// look at, and taking it over here would make the next install accept it.
// With create false, an absent dir is left absent (a removal that took
// %ProgramData%\ITSuite away must not bring it back) and os.ErrNotExist is
// returned.
//
// The file is opened for appending only, without following a reparse point,
// and refused if it is one, is not a regular file, or has another hard link:
// nothing planted before the directory was secured can redirect the write.
// It inherits dir's protected SYSTEM+Administrators DACL.
//
// The caller must be elevated or SYSTEM. Executed on windows-latest by
// append_windows_test.go.
func AppendLine(dir, name, line string, create bool) error {
	if !create {
		h, err := openDirNoFollow(dir, windows.READ_CONTROL)
		if errors.Is(err, windows.ERROR_FILE_NOT_FOUND) || errors.Is(err, windows.ERROR_PATH_NOT_FOUND) {
			return fmt.Errorf("%s: %w", dir, os.ErrNotExist)
		}
		if err != nil {
			return err
		}
		windows.CloseHandle(h)
	}
	if err := SecureOwned(dir, administratorsOrSystem(dir)); err != nil {
		return err
	}
	path, err := windows.UTF16PtrFromString(dir + `\` + name)
	if err != nil {
		return err
	}
	h, err := windows.CreateFile(path, windows.FILE_APPEND_DATA|windows.FILE_READ_ATTRIBUTES,
		windows.FILE_SHARE_READ, nil, windows.OPEN_ALWAYS,
		windows.FILE_ATTRIBUTE_NORMAL|windows.FILE_FLAG_OPEN_REPARSE_POINT, 0)
	if err != nil {
		return fmt.Errorf("open %s\\%s: %w", dir, name, err)
	}
	defer windows.CloseHandle(h)
	var fi windows.ByHandleFileInformation
	if err := windows.GetFileInformationByHandle(h, &fi); err != nil {
		return fmt.Errorf("read the attributes of %s\\%s: %w", dir, name, err)
	}
	switch {
	case fi.FileAttributes&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0:
		return fmt.Errorf("%s\\%s is a junction or symbolic link, not a file", dir, name)
	case fi.FileAttributes&windows.FILE_ATTRIBUTE_DIRECTORY != 0:
		return fmt.Errorf("%s\\%s is a directory", dir, name)
	case fi.NumberOfLinks != 1:
		return fmt.Errorf("%s\\%s has another hard link", dir, name)
	}
	var written uint32
	if err := windows.WriteFile(h, []byte(line+"\r\n"), &written, nil); err != nil {
		return fmt.Errorf("write %s\\%s: %w", dir, name, err)
	}
	return nil
}

// administratorsOrSystem accepts an existing directory owned by
// BUILTIN\Administrators or NT AUTHORITY\SYSTEM, and nothing else.
func administratorsOrSystem(dir string) func(owner *windows.SID) error {
	return func(owner *windows.SID) error {
		for _, known := range []windows.WELL_KNOWN_SID_TYPE{windows.WinBuiltinAdministratorsSid, windows.WinLocalSystemSid} {
			if sid, err := windows.CreateWellKnownSid(known); err == nil && owner != nil && owner.Equals(sid) {
				return nil
			}
		}
		return fmt.Errorf("%s is not owned by Administrators or SYSTEM; an administrator must look at it", dir)
	}
}
