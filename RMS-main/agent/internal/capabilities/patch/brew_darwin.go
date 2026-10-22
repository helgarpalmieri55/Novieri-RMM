//go:build darwin

package patch

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strconv"
	"syscall"
	"time"
)

// brewPaths are where Homebrew installs itself: Apple Silicon, then Intel.
var brewPaths = []string{"/opt/homebrew/bin/brew", "/usr/local/bin/brew"}

// homebrewSlot finds brew and the account that owns it, or says why not.
func homebrewSlot() appSlot {
	slot := appSlot{source: sourceHomebrew}
	for _, path := range brewPaths {
		info, err := os.Stat(path)
		if err != nil || !info.Mode().IsRegular() {
			continue
		}
		st, ok := info.Sys().(*syscall.Stat_t)
		if !ok {
			continue
		}
		if st.Uid == 0 {
			slot.unsupported = "Homebrew is owned by root, and brew will not run as root"
			return slot
		}
		owner, err := user.LookupId(strconv.FormatUint(uint64(st.Uid), 10))
		if err != nil || owner.HomeDir == "" {
			slot.unsupported = "the account that owns Homebrew could not be found"
			return slot
		}
		slot.eng = &brewEngine{run: brewAs(path, st.Uid, st.Gid, owner)}
		return slot
	}
	slot.unsupported = "Homebrew is not installed, so app updates cannot be listed on this Mac"
	return slot
}

// brewAs runs brew as the prefix owner with a fixed environment: none of the
// agent's own environment reaches it.
func brewAs(path string, uid, gid uint32, owner *user.User) brewRunner {
	prefix := filepath.Dir(filepath.Dir(path))
	env := []string{
		"HOME=" + owner.HomeDir,
		"USER=" + owner.Username,
		"LOGNAME=" + owner.Username,
		"PATH=" + prefix + "/bin:/usr/bin:/bin:/usr/sbin:/sbin",
		"HOMEBREW_NO_AUTO_UPDATE=1",
		"HOMEBREW_NO_ANALYTICS=1",
		"HOMEBREW_NO_ENV_HINTS=1",
		"HOMEBREW_NO_INSTALL_CLEANUP=1",
	}
	return func(ctx context.Context, args ...string) commandResult {
		cmd := exec.CommandContext(ctx, path, args...)
		cmd.Env = env
		cmd.Dir = owner.HomeDir
		cmd.SysProcAttr = &syscall.SysProcAttr{Credential: &syscall.Credential{Uid: uid, Gid: gid}}
		cmd.WaitDelay = 10 * time.Second
		var stdout, stderr boundedBuffer
		stdout.limit, stderr.limit = maxCommandOutput, maxCommandOutput
		cmd.Stdout, cmd.Stderr = &stdout, &stderr
		err := cmd.Run()
		res := commandResult{Stdout: stdout.String(), Stderr: stderr.String(), Err: err}
		var exit *exec.ExitError
		switch {
		case err == nil:
		case errors.As(err, &exit):
			res.ExitCode = exit.ExitCode()
		default:
			res.ExitCode = -1
		}
		return res
	}
}
