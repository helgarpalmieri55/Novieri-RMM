//go:build darwin

package update

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"time"
)

// SystemServices drives launchd.
type SystemServices struct{}

func launchctl(ctx context.Context, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "launchctl", args...).CombinedOutput()
	if err != nil {
		return string(out), fmt.Errorf("launchctl %v: %w", args, err)
	}
	return string(out), nil
}

func (SystemServices) Identify(ctx context.Context) (string, error) {
	out, err := launchctl(ctx, "print", "system/"+launchdLabel)
	if err != nil {
		return "", fail(ReasonNotRunningAsService, err)
	}
	pid, path, err := parseLaunchctlPrint(out)
	if err != nil {
		return "", fail(ReasonNotRunningAsService, err)
	}
	return checkIdentity(pid, path, false)
}

// LaunchHelper bootstraps a one-shot job from a plist in the data directory,
// so a reboot does not relaunch it. `submit` is the fallback if bootstrap
// refuses a plist outside the standard directories.
func (SystemServices) LaunchHelper(ctx context.Context, prev, phase string, l Layout, _ string) error {
	plist := filepath.Join(l.Dir, launchdHelperLabel+".plist")
	_ = os.Remove(plist)
	if err := os.WriteFile(plist, helperPlist(prev, phase, l), 0o644); err != nil {
		return err
	}
	_ = os.Chmod(plist, 0o644)
	_, _ = launchctl(ctx, "bootout", "system/"+launchdHelperLabel)
	if _, err := launchctl(ctx, "bootstrap", "system", plist); err == nil {
		return nil
	}
	_, err := launchctl(ctx, "submit", "-l", launchdHelperLabel,
		"-e", filepath.Join(l.Dir, "helper.log"),
		"--", prev, "--update-helper="+phase, "--state", l.StatePath())
	return err
}

func (SystemServices) StopForSwap() bool { return false }

func (SystemServices) Stop(ctx context.Context) error {
	_, err := launchctl(ctx, "kill", "SIGTERM", "system/"+launchdLabel)
	return err
}

func (SystemServices) Start(ctx context.Context) error {
	_, err := launchctl(ctx, "kickstart", "system/"+launchdLabel)
	return err
}

func (SystemServices) Restart(ctx context.Context) error {
	_, err := launchctl(ctx, "kickstart", "-k", "system/"+launchdLabel)
	return err
}

// HelperExiting unloads the helper's own job as its last act. Not waited
// for: bootout signals this very process.
func (SystemServices) HelperExiting(context.Context) {
	_ = exec.Command("launchctl", "bootout", "system/"+launchdHelperLabel).Start()
	_ = exec.Command("launchctl", "remove", launchdHelperLabel).Start()
}
