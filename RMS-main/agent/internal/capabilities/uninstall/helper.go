package uninstall

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"github.com/toeshee/it-suite/agent/internal/config"
)

// System is what differs per service manager. platformSystem implements it
// in system_<goos>.go; tests use a fake.
type System interface {
	StopService(ctx context.Context, name string) error
	DeleteService(ctx context.Context, name string) error
	// PurgePackage removes a package-manager-installed agent and reports
	// whether there was one.
	PurgePackage(ctx context.Context) (bool, error)
	// ScheduleDelete removes a file at the next boot (the helper's own copy on
	// Windows, which a running process cannot delete).
	ScheduleDelete(path string) error
	// HelperExiting is the helper's last act (daemon-reload, or unloading its
	// own launchd job).
	HelperExiting(ctx context.Context)
}

// RunHelper removes the agent. Every step runs even if an earlier one failed;
// the failures are joined. `self` is the helper's own image.
//
// HelperExiting runs on EVERY return, the refusals included (review M2): on
// macOS the helper is a `launchctl submit` job, which launchd restarts each
// time it exits until someone removes it.
func RunHelper(ctx context.Context, sys System, t Targets, self string, wait func(time.Duration)) error {
	defer sys.HelperExiting(ctx)
	if err := checkTargets(t); err != nil {
		return err
	}
	wait(HelperDelay)
	var errs []error
	note := func(err error) {
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			errs = append(errs, err)
		}
	}
	purged := removeServiceAndData(ctx, sys, t, note)
	if !purged {
		// Windows keeps an image mapped briefly after its process exits.
		var rmErr error
		for attempt := 0; attempt < 20; attempt++ {
			if rmErr = os.Remove(t.Binary); rmErr == nil || errors.Is(rmErr, os.ErrNotExist) {
				rmErr = nil
				break
			}
			wait(500 * time.Millisecond)
		}
		note(rmErr)
	}
	for _, dir := range t.RemoveIfEmpty {
		_ = os.Remove(dir) // fails, correctly, if anything else is in it
	}
	if self != "" && self != t.Binary {
		note(sys.ScheduleDelete(self))
	}
	return errors.Join(errs...)
}

// removeServiceAndData is what every removal does, in this order: stop the
// service, purge the package or delete the service, remove the credentials BY
// NAME, then the data directory -- recursively only when it is an agent's --
// and the service definitions and config in t.Remove. Every step runs; the
// failures go to note. It reports whether a package manager removed the agent
// (and with it the binary). RunHelper and RunForMSI both call it, so the two
// removals cannot drift apart.
func removeServiceAndData(ctx context.Context, sys System, t Targets, note func(error)) bool {
	// Decided BEFORE anything is removed: whether this data directory is an
	// agent's (review M3). The credentials below are not identity.json, so
	// removing them does not change the answer, but reading it first means
	// nothing this function does can.
	ownsData := ownsDataDir(t)
	note(sys.StopService(ctx, t.ServiceName))
	purged, err := sys.PurgePackage(ctx)
	note(err)
	if !purged {
		note(sys.DeleteService(ctx, t.ServiceName))
	}
	for _, name := range t.Credentials {
		note(os.Remove(filepath.Join(t.DataDir, name)))
	}
	if ownsData {
		note(os.RemoveAll(t.DataDir))
	} else {
		note(fmt.Errorf("uninstall: %s holds no %s, so it may not be an agent's data directory; "+
			"only the credentials in it were removed", t.DataDir, identityFile))
	}
	for _, p := range t.Remove {
		note(os.RemoveAll(p))
	}
	return purged
}

// HelperMain is `its-agent --uninstall-helper`. Its inputs are the environment
// the capability set when it launched this process -- never a job's params.
func HelperMain() int {
	// FIRST, before anything can load a library (review I2): on Windows, DLLs
	// resolve from System32 only, never from the directory this image was
	// started from. Fails closed.
	if err := hardenProcess(); err != nil {
		fmt.Fprintln(os.Stderr, "uninstall helper: cannot restrict the DLL search path:", err)
		return 2
	}
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	binary, dataDir := os.Getenv(EnvBinary), os.Getenv(config.EnvDataDir)
	if binary == "" || dataDir == "" {
		platformSystem{}.HelperExiting(context.Background()) // review M2: never respawned
		log.Error("uninstall helper: started without its environment; nothing removed")
		return 2
	}
	self, _ := os.Executable()
	if err := RunHelper(context.Background(), platformSystem{}, For(runtime.GOOS, binary, dataDir), self, time.Sleep); err != nil {
		log.Error("uninstall helper finished with errors", "err", err)
		return 1
	}
	log.Info("its-agent removed")
	return 0
}
