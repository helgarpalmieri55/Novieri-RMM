//go:build linux || darwin

package patch

import (
	"context"
	"fmt"
	"os/exec"
	"strconv"
	"syscall"
	"time"
)

// unixRestarter runs `shutdown -r +<minutes> <message>`. The command returns
// at once: systemd hands the schedule to logind, and BSD shutdown forks a
// background process. That child gets its own session and no pipes, so an agent
// restart does not take the scheduled restart with it and closing a pipe cannot
// kill it.
//
// NEVER EXECUTED on macOS. The Linux path is systemd's `shutdown`.
type unixRestarter struct {
	paths   []string
	session func() session
}

func (u unixRestarter) Session(ctx context.Context) session { return u.session() }

func (u unixRestarter) Schedule(ctx context.Context, grace time.Duration, message string) error {
	path := ""
	for _, p := range u.paths {
		if resolved, err := exec.LookPath(p); err == nil {
			path = resolved
			break
		}
	}
	if path == "" {
		return fmt.Errorf("no shutdown command on this host")
	}
	cmd := exec.CommandContext(ctx, path, "-r", "+"+strconv.Itoa(graceMinutes(grace)), message)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("shutdown could not schedule the restart: %w", err)
	}
	return nil
}
