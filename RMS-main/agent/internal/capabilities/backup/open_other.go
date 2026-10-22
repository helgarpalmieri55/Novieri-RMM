//go:build !windows

package backup

import "os"

// openForBackup opens a source file for reading. Only Windows needs more than
// os.Open: there, a DACL that excludes SYSTEM must not stop a backup (D10).
func openForBackup(p string) (*os.File, error) { return os.Open(p) }

func openDirForBackup(p string) (*os.File, error) { return os.Open(p) }

// acquireBackupPrivileges and acquireRestorePrivileges are Windows token
// adjustments; nothing to do elsewhere.
func acquireBackupPrivileges() (func(), []string) { return func() {}, nil }

func acquireRestorePrivileges() (func(), []string) { return func() {}, nil }
