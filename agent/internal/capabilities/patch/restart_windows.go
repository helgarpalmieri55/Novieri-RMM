//go:build windows

package patch

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"path/filepath"
	"strconv"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// NEVER EXECUTED HERE: there is no Windows runner. See agent-capabilities.md §6.

const (
	// errShutdownIsScheduled is ERROR_SHUTDOWN_IS_SCHEDULED: a restart is
	// already pending, which is the outcome this job wanted.
	errShutdownIsScheduled = 1190
	// shutdown.exe's /t ceiling.
	maxShutdownSeconds = 315360000
	verNTWorkstation   = 1
)

type windowsRestarter struct{}

func osRestarter() restarter { return windowsRestarter{} }

// Session: a user is present when any non-zero session is active or
// disconnected (a disconnected session still holds someone's unsaved work).
func (windowsRestarter) Session(ctx context.Context) session {
	s := session{Workstation: windows.RtlGetVersion().ProductType == verNTWorkstation}
	var info *windows.WTS_SESSION_INFO
	var count uint32
	if err := windows.WTSEnumerateSessions(0, 0, 1, &info, &count); err != nil {
		return s
	}
	defer windows.WTSFreeMemory(uintptr(unsafe.Pointer(info)))
	for _, si := range unsafe.Slice(info, count) {
		if si.SessionID == 0 {
			continue
		}
		if si.State == windows.WTSActive || si.State == windows.WTSDisconnected {
			s.UserPresent = true
			break
		}
	}
	return s
}

// Schedule runs `shutdown.exe /r /t <seconds> /d p:2:17 /c <message>` from
// System32 by absolute path. /d p:2:17 records a planned OS hotfix restart in
// the event log.
func (windowsRestarter) Schedule(ctx context.Context, grace time.Duration, message string) error {
	sys, err := windows.GetSystemDirectory()
	if err != nil {
		return fmt.Errorf("cannot locate System32: %w", err)
	}
	seconds := int(grace / time.Second)
	if seconds > maxShutdownSeconds {
		seconds = maxShutdownSeconds
	}
	cmd := exec.CommandContext(ctx, filepath.Join(sys, "shutdown.exe"),
		"/r", "/t", strconv.Itoa(seconds), "/d", "p:2:17", "/c", message)
	err = cmd.Run()
	var exit *exec.ExitError
	if errors.As(err, &exit) && exit.ExitCode() == errShutdownIsScheduled {
		return nil
	}
	if err != nil {
		return fmt.Errorf("shutdown.exe could not schedule the restart: %w", err)
	}
	return nil
}

func bootTime(now time.Time) (time.Time, bool) {
	return now.Add(-windows.DurationSinceBoot()), true
}
