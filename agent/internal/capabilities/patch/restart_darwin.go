//go:build darwin

package patch

import (
	"os"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

// A Mac is a workstation. Someone is signed in at the screen when
// /dev/console belongs to a user rather than root.
func osRestarter() restarter {
	return unixRestarter{paths: []string{"/sbin/shutdown"}, session: func() session {
		s := session{Workstation: true}
		if fi, err := os.Stat("/dev/console"); err == nil {
			if st, ok := fi.Sys().(*syscall.Stat_t); ok && st.Uid != 0 {
				s.UserPresent = true
			}
		}
		return s
	}}
}

func bootTime(time.Time) (time.Time, bool) {
	tv, err := unix.SysctlTimeval("kern.boottime")
	if err != nil {
		return time.Time{}, false
	}
	return time.Unix(tv.Unix()), true
}
