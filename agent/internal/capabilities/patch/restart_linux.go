//go:build linux

package patch

import (
	"syscall"
	"time"
)

// Linux machines in this fleet are servers: they restart inside their window
// and never wait for a user.
func osRestarter() restarter {
	return unixRestarter{
		paths:   []string{"/usr/sbin/shutdown", "/sbin/shutdown"},
		session: func() session { return session{} },
	}
}

// bootTime asks the kernel for its uptime: no file, no allocation. Whole
// seconds, so two scans may differ by one; the control plane only compares it
// with a record minutes old.
func bootTime(now time.Time) (time.Time, bool) {
	var info syscall.Sysinfo_t
	if err := syscall.Sysinfo(&info); err != nil || info.Uptime <= 0 {
		return time.Time{}, false
	}
	return now.Add(-time.Duration(info.Uptime) * time.Second).Truncate(time.Second), true
}
