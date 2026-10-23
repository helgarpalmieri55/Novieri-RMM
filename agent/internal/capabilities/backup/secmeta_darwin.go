//go:build darwin

package backup

import "syscall"

func ctimeOf(st *syscall.Stat_t) int64 { return st.Ctimespec.Nano() }

// xattrs are Linux-only in §4.4; macOS servers are out of scope for v1.
func captureXattrs(string, *entryV2) error { return nil }

func applyXattrs(string, *entryV2) error { return nil }
