//go:build linux

package uninstall

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"time"

	"github.com/toeshee/it-suite/agent/internal/config"
)

// Supported: this build advertises agent.uninstall.
const Supported = true

// helperUnit is a transient unit OUTSIDE its-agent.service's cgroup, which
// KillMode=control-group would otherwise kill the moment the service stops --
// internal/update's self-update helper is launched the same way.
const helperUnit = "its-agent-uninstall"

// LaunchHelper starts `<binary> --uninstall-helper` as a transient unit.
func LaunchHelper(ctx context.Context, binary, dataDir string) error {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	args := []string{
		"--unit=" + helperUnit, "--collect", "--no-block", "--quiet",
		"-p", "Type=exec",
		"--setenv=" + config.EnvDataDir + "=" + dataDir,
		"--setenv=" + EnvBinary + "=" + binary,
		"--", binary, HelperArg,
	}
	if out, err := exec.CommandContext(ctx, "systemd-run", args...).CombinedOutput(); err != nil {
		return fmt.Errorf("systemd-run: %w: %s", err, out)
	}
	return nil
}

// hardenProcess: nothing to do. The Windows DLL search path has no equivalent
// here -- the binary is static, and systemd-run starts it with a clean
// environment.
func hardenProcess() error { return nil }

type platformSystem struct{}

func systemctl(ctx context.Context, args ...string) error {
	ctx, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()
	if out, err := exec.CommandContext(ctx, "systemctl", args...).CombinedOutput(); err != nil {
		return fmt.Errorf("systemctl %v: %w: %s", args, err, out)
	}
	return nil
}

func (platformSystem) StopService(ctx context.Context, name string) error {
	return systemctl(ctx, "stop", name+".service")
}

func (platformSystem) DeleteService(ctx context.Context, name string) error {
	return systemctl(ctx, "disable", name+".service")
}

// PurgePackage: a .deb-installed agent (agent/packaging/linux/build-deb.sh)
// is removed by dpkg, which owns /usr/bin/its-agent and the packaged unit.
func (platformSystem) PurgePackage(ctx context.Context) (bool, error) {
	if _, err := os.Stat("/var/lib/dpkg/info/its-agent.list"); err != nil {
		return false, nil
	}
	if out, err := exec.CommandContext(ctx, "dpkg", "--purge", "its-agent").CombinedOutput(); err != nil {
		return true, fmt.Errorf("dpkg --purge its-agent: %w: %s", err, out)
	}
	return true, nil
}

func (platformSystem) ScheduleDelete(string) error { return nil }

func (platformSystem) HelperExiting(ctx context.Context) {
	_ = systemctl(ctx, "daemon-reload")
	_ = systemctl(ctx, "reset-failed", "its-agent.service")
}
