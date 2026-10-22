//go:build darwin

package uninstall

import (
	"context"
	"fmt"
	"os/exec"
	"time"

	"github.com/toeshee/it-suite/agent/internal/config"
)

// Supported: this build advertises agent.uninstall.
const Supported = true

// helperLabel is a launchd job of its own: launchd kills a job's children
// when it boots the job out, so the helper cannot be the service's child.
const helperLabel = "com.toeshee.itsagent.uninstall"

// LaunchHelper submits `<binary> --uninstall-helper` as its own launchd job.
// `/usr/bin/env` carries the two variables; `launchctl submit` takes no
// environment of its own.
func LaunchHelper(ctx context.Context, binary, dataDir string) error {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	_ = exec.CommandContext(ctx, "launchctl", "remove", helperLabel).Run()
	out, err := exec.CommandContext(ctx, "launchctl", "submit", "-l", helperLabel, "--",
		"/usr/bin/env", config.EnvDataDir+"="+dataDir, EnvBinary+"="+binary, binary, HelperArg,
	).CombinedOutput()
	if err != nil {
		return fmt.Errorf("launchctl submit: %w: %s", err, out)
	}
	return nil
}

// hardenProcess: nothing to do. The Windows DLL search path has no equivalent
// for a static, cgo-free binary.
func hardenProcess() error { return nil }

type platformSystem struct{}

func (platformSystem) StopService(ctx context.Context, name string) error {
	if out, err := exec.CommandContext(ctx, "launchctl", "bootout", "system/"+name).CombinedOutput(); err != nil {
		return fmt.Errorf("launchctl bootout system/%s: %w: %s", name, err, out)
	}
	return nil
}

// DeleteService: the plist is in Targets.Remove; bootout already unloaded it.
func (platformSystem) DeleteService(context.Context, string) error { return nil }

func (platformSystem) PurgePackage(context.Context) (bool, error) { return false, nil }

func (platformSystem) ScheduleDelete(string) error { return nil }

// HelperExiting removes the helper's own submitted job, which launchd would
// otherwise restart. Not waited for: it signals this very process.
func (platformSystem) HelperExiting(context.Context) {
	_ = exec.Command("launchctl", "remove", helperLabel).Start()
}
