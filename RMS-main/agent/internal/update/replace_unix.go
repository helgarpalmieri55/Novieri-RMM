//go:build !windows

package update

import "os"

// replaceFile is rename(2): atomic, and the running process keeps its inode.
func replaceFile(src, dst string) error { return os.Rename(src, dst) }
