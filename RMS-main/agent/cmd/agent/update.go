package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"

	"github.com/toeshee/it-suite/agent/internal/capabilities"
	"github.com/toeshee/it-suite/agent/internal/capabilities/selfupdate"
	"github.com/toeshee/it-suite/agent/internal/config"
	"github.com/toeshee/it-suite/agent/internal/enroll"
	"github.com/toeshee/it-suite/agent/internal/update"
)

// selfTest is --update-selftest (spec §5.1 step 3): the init path of a real
// start, minus the network and minus every write. It runs on the candidate
// beside the target before the swap, so a binary that cannot start fails
// here rather than after the service has been restarted onto it.
func selfTest(out io.Writer) error {
	if e2eBadSelfTest {
		return errors.New("built with e2e_badselftest")
	}
	cfg, err := config.Load()
	if err != nil {
		return fmt.Errorf("configuration: %w", err)
	}
	id, err := enroll.Load(cfg)
	if err != nil {
		return fmt.Errorf("identity: %w", err)
	}
	if len(id.JobSigningKey) == 0 {
		return errors.New("no pinned job-signing key")
	}
	if _, ok := capabilities.Default().Lookup(selfupdate.JobType); !ok {
		return errors.New("the capability registry lacks agent.update")
	}
	if _, err := update.ReleaseKey(); err != nil {
		return err
	}
	return json.NewEncoder(out).Encode(update.SelfTestReport{
		Version: version, OS: runtime.GOOS, Arch: runtime.GOARCH,
	})
}

// runHelper is --update-helper (spec §5.2). It takes the phase and the state
// path and nothing else; the path must be <something>/update/state.json.
func runHelper(phase, statePath string) int {
	if phase != update.HelperApply && phase != update.HelperRollback {
		fmt.Fprintln(os.Stderr, "--update-helper must be apply or rollback")
		return 2
	}
	statePath = filepath.Clean(statePath)
	if filepath.Base(statePath) != "state.json" || filepath.Base(filepath.Dir(statePath)) != "update" {
		fmt.Fprintln(os.Stderr, "--state must name <data>/update/state.json")
		return 2
	}
	var sink io.Writer = os.Stderr
	if f, err := os.OpenFile(filepath.Join(filepath.Dir(statePath), "helper.log"),
		os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600); err == nil {
		defer f.Close()
		sink = io.MultiWriter(os.Stderr, f)
	}
	log := slog.New(slog.NewTextHandler(sink, &slog.HandlerOptions{Level: slog.LevelInfo})).
		With("component", "update-helper", "version", version)
	log.Info("update helper starting", "phase", phase)
	code := update.RunHelper(context.Background(), update.HelperOptions{
		StatePath: statePath,
		Phase:     phase,
		Services:  update.SystemServices{},
		Logger:    log,
	})
	log.Info("update helper exiting", "code", code)
	return code
}
