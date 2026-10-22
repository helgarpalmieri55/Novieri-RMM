//go:build windows

package update

import "golang.org/x/sys/windows"

// replaceFile renames over dst with write-through, so the rename is on disk
// before the service is started again. The service must be stopped first.
func replaceFile(src, dst string) error {
	from, err := windows.UTF16PtrFromString(src)
	if err != nil {
		return err
	}
	to, err := windows.UTF16PtrFromString(dst)
	if err != nil {
		return err
	}
	return windows.MoveFileEx(from, to, windows.MOVEFILE_REPLACE_EXISTING|windows.MOVEFILE_WRITE_THROUGH)
}
