// Package uninstall implements `agent.uninstall`: the agent removes itself
// (docs/superpowers/specs/2026-09-28-install-link-and-windows-push-design.md
// §1.5 -- the second half of rejecting a machine that joined through the
// install link).
//
// # A SIGNED JOB, and nothing else reaches it
//
// Reached only through internal/jobs, after the signature over the canonical
// bytes, the target device, freshness, the nonce and the type have all been
// checked. The params carry NOTHING: what to remove is this process's own
// knowledge -- its binary, its data directory, its service -- so there is no
// field a control plane could point at a path.
//
// # Scheduled, not done
//
// A process cannot stop and delete its own running service and then report.
// `Run` starts a DETACHED HELPER (this same binary with HelperArg, outside the
// service's process tree) and answers `{"scheduled": true}`. The helper waits
// HelperDelay so that answer reaches the gateway, then stops and deletes the
// service and removes the credentials, the data directory, the service
// definition and the binary (plan D7). The control plane reads `succeeded` as
// the agent's confirmation and revokes the certificate; a result lost with the
// data directory is covered by its 24-hour completion.
//
// # Dormancy
//
// Registration is a map insert and Bind stores options. No goroutine, no file
// and no process until a verified job arrives.
package uninstall

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// JobType matches `app/jobs/registry.py`'s registration.
const JobType = "agent.uninstall"

// HelperArg is the first argument main.go routes to HelperMain.
const HelperArg = "--uninstall-helper"

// EnvBinary tells the helper which installed binary to remove. The helper may
// be running from a copy (Windows), so it cannot use its own path.
const EnvBinary = "ITS_AGENT_UNINSTALL_BINARY"

// HelperDelay is how long the helper waits before stopping the service, so the
// job's `scheduled` answer is on the wire first.
const HelperDelay = 15 * time.Second

// params implements AgentUninstallParams.schema.json: no members. Unknown
// members are ignored (encoding/json's default) -- the inbound half of the
// wire-protocol rule.
type params struct{}

// result implements AgentUninstallResult.schema.json.
type result struct {
	Scheduled bool `json:"scheduled"`
}

type (
	// ParamsShape is `params`, exported for schema_test.go.
	ParamsShape = params
	// ResultShape is `result`, exported for schema_test.go.
	ResultShape = result
)

// Launcher starts the detached helper and returns once it is running. It
// never waits for the helper to finish.
type Launcher func(ctx context.Context, binary, dataDir string) error

// Options wires the capability. main.go binds LaunchHelper, os.Executable and
// the configured data directory.
type Options struct {
	Launch  Launcher
	Binary  func() (string, error)
	DataDir string
	Logger  *slog.Logger
}

// Service is the process-wide holder. The zero value refuses every job.
type Service struct {
	mu   sync.RWMutex
	opts Options
	runs atomic.Int64
}

// Process is the Service the default registry registers.
var Process = &Service{}

// New returns a Service with explicit wiring. Tests use it.
func New(o Options) *Service { return &Service{opts: o} }

// Bind stores the wiring. It starts nothing.
func (s *Service) Bind(o Options) {
	s.mu.Lock()
	s.opts = o
	s.mu.Unlock()
}

// Runs counts helpers this Service has started.
func (s *Service) Runs() int64 { return s.runs.Load() }

// Run is the capability.
func (s *Service) Run(ctx context.Context, job jobs.VerifiedJob) (any, error) {
	var p params
	if err := json.Unmarshal(job.Params(), &p); err != nil {
		return nil, jobs.Refuse("agent.uninstall params are not a JSON object")
	}
	s.mu.RLock()
	o := s.opts
	s.mu.RUnlock()
	if o.Launch == nil || o.Binary == nil || o.DataDir == "" {
		return nil, jobs.Refuse("agent.uninstall is registered but not bound in this process")
	}
	binary, err := o.Binary()
	if err != nil {
		return nil, fmt.Errorf("agent.uninstall: cannot locate this agent's binary: %w", err)
	}
	if err := o.Launch(ctx, binary, o.DataDir); err != nil {
		return nil, fmt.Errorf("agent.uninstall: the removal helper did not start: %w", err)
	}
	s.runs.Add(1)
	log := o.Logger
	if log == nil {
		log = slog.Default()
	}
	log.Warn("agent.uninstall: this agent removes itself in 15 seconds", "job_id", job.ID())
	return result{Scheduled: true}, nil
}
