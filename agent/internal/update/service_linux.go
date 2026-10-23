//go:build linux

package update

import (
	"context"
	"fmt"
	"os/exec"
	"time"
)

// SystemServices drives systemd.
type SystemServices struct{}

func systemctl(ctx context.Context, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "systemctl", args...).CombinedOutput()
	if err != nil {
		return string(out), fmt.Errorf("systemctl %v: %w", args, err)
	}
	return string(out), nil
}

func (SystemServices) Identify(ctx context.Context) (string, error) {
	out, err := systemctl(ctx, "show", systemdUnit, "-p", "MainPID", "-p", "ExecStart")
	if err != nil {
		return "", fail(ReasonNotRunningAsService, err)
	}
	pid, path, err := parseSystemdShow(out)
	if err != nil {
		return "", fail(ReasonNotRunningAsService, err)
	}
	return checkIdentity(pid, path, false)
}

// LaunchHelper starts a transient unit outside its-agent.service's cgroup,
// which KillMode=control-group would otherwise kill on restart.
func (SystemServices) LaunchHelper(ctx context.Context, prev, phase string, l Layout, targetDir string) error {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	args := []string{
		"--unit=" + helperUnit, "--collect", "--no-block", "--quiet",
		"-p", "Type=exec",
		"-p", "ProtectSystem=strict",
		"-p", "ReadWritePaths=" + targetDir,
		"-p", "ReadWritePaths=" + l.DataDir(),
		"-p", "ProtectHome=yes",
		"-p", "PrivateTmp=yes",
		"-p", "NoNewPrivileges=yes",
		"-p", "IPAddressDeny=any",
		"--setenv=ITS_AGENT_DATA_DIR=" + l.DataDir(),
		"--", prev, "--update-helper=" + phase, "--state", l.StatePath(),
	}
	if out, err := exec.CommandContext(ctx, "systemd-run", args...).CombinedOutput(); err != nil {
		return fmt.Errorf("systemd-run: %w: %s", err, out)
	}
	return nil
}

func (SystemServices) StopForSwap() bool { return false }

func (SystemServices) Stop(ctx context.Context) error {
	_, err := systemctl(ctx, "stop", systemdUnit)
	return err
}

func (SystemServices) Start(ctx context.Context) error {
	_, _ = systemctl(ctx, "reset-failed", systemdUnit)
	_, err := systemctl(ctx, "start", systemdUnit)
	return err
}

// Restart clears a StartLimitBurst trip first, then restarts.
func (SystemServices) Restart(ctx context.Context) error {
	_, _ = systemctl(ctx, "reset-failed", systemdUnit)
	_, err := systemctl(ctx, "restart", systemdUnit)
	return err
}

func (SystemServices) HelperExiting(context.Context) {}
