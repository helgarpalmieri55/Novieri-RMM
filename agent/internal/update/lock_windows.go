//go:build windows

package update

import (
	"os"

	"golang.org/x/sys/windows"
)

// AcquireHelperLock takes an OS lock the helper holds for its lifetime. The
// kernel drops it when the process dies, including at reboot.
func AcquireHelperLock(path string) (func(), error) {
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, err
	}
	h := windows.Handle(f.Fd())
	ol := new(windows.Overlapped)
	if err := windows.LockFileEx(h, windows.LOCKFILE_EXCLUSIVE_LOCK|windows.LOCKFILE_FAIL_IMMEDIATELY,
		0, 1, 0, ol); err != nil {
		f.Close()
		return nil, err
	}
	return func() {
		_ = windows.UnlockFileEx(h, 0, 1, 0, ol)
		f.Close()
	}, nil
}

// HelperAlive reports whether some process holds the helper lock.
func HelperAlive(path string) bool {
	if _, err := os.Stat(path); err != nil {
		return false
	}
	release, err := AcquireHelperLock(path)
	if err != nil {
		return true
	}
	release()
	return false
}
