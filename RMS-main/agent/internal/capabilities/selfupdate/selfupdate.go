// Package selfupdate is the `agent.update` capability
// (docs/modules/agent-distribution/spec-self-update.md §4). Every check runs
// before anything is fetched; the swap itself belongs to the helper
// (internal/update), so the handler stages, hands off and returns
// jobs.ErrDeferred.
package selfupdate

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"

	"github.com/toeshee/it-suite/agent/internal/config"
	"github.com/toeshee/it-suite/agent/internal/fetch"
	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/secdir"
	"github.com/toeshee/it-suite/agent/internal/update"
)

// JobType is the signed job this capability answers.
const JobType = jobs.DeferrableJobType

// MaxArtifactBytes bounds the download (spec §3.1).
const MaxArtifactBytes = 256 << 20

const (
	defaultHealthWindowS = 600
	minHealthWindowS     = 120
	maxHealthWindowS     = 3600
	diskFactor           = 3
)

// Params mirrors AgentUpdateParams.schema.json. Unknown members are ignored.
type Params struct {
	TargetVersion    *string `json:"target_version"`
	OS               *string `json:"os"`
	Arch             *string `json:"arch"`
	ArtifactURL      *string `json:"artifact_url"`
	ArtifactSHA256   *string `json:"artifact_sha256"`
	ArtifactSize     *int64  `json:"artifact_size"`
	Rollback         bool    `json:"rollback"`
	HealthWindowS    *int    `json:"health_window_s"`
	RolloutID        *string `json:"rollout_id"`
	ReleaseSignature *string `json:"release_signature"`
}

// Runner is the part of jobs.Runner this capability needs.
type Runner interface{ Executing(excludeID string) int }

// Options binds the capability. Zero fields take production defaults.
type Options struct {
	Version    string
	Runner     Runner
	DataDir    func() (string, error)
	Services   update.ServiceManager
	Fetch      func(ctx context.Context, url string) (io.ReadCloser, error)
	FreeBytes  func(path string) (uint64, error)
	Executable func() (string, error)
	ReleaseKey func() (ed25519.PublicKey, error)
	CheckPrev  func(ctx context.Context, binary, version string) error
	Logger     *slog.Logger
}

// Service is the capability. Unbound it refuses every job.
type Service struct {
	mu    sync.Mutex
	opts  Options
	bound bool
}

// Process is this binary's instance, bound by main.
var Process = &Service{}

// Bind wires the capability. Nothing runs until a job arrives.
func (s *Service) Bind(o Options) {
	if o.DataDir == nil {
		o.DataDir = func() (string, error) {
			c, err := config.Load()
			return c.DataDir, err
		}
	}
	if o.Services == nil {
		o.Services = update.SystemServices{}
	}
	if o.Fetch == nil {
		o.Fetch = fetch.HTTPS
	}
	if o.FreeBytes == nil {
		o.FreeBytes = update.FreeBytes
	}
	if o.Executable == nil {
		o.Executable = os.Executable
	}
	if o.ReleaseKey == nil {
		o.ReleaseKey = update.ReleaseKey
	}
	if o.CheckPrev == nil {
		o.CheckPrev = update.CheckRunsAndReports
	}
	if o.Logger == nil {
		o.Logger = slog.Default()
	}
	s.mu.Lock()
	s.opts, s.bound = o, true
	s.mu.Unlock()
}

// Update is the registered handler.
func (s *Service) Update(ctx context.Context, job jobs.VerifiedJob) (any, error) {
	return s.update(ctx, job.ID(), job.Params())
}

func refuse(token string) error { return jobs.Refuse("%s", token) }

// failed is a `failed` result whose error is exactly the token.
func failed(token string) error { return errors.New(token) }

func (s *Service) update(ctx context.Context, jobID string, raw json.RawMessage) (any, error) {
	s.mu.Lock()
	o, bound := s.opts, s.bound
	s.mu.Unlock()
	if !bound || o.Runner == nil {
		return nil, refuse(update.ReasonUnbound)
	}
	log := o.Logger.With("job_id", jobID)

	// 1. Params.
	p, window, want, err := parse(raw)
	if err != nil {
		log.Warn("agent.update params refused", "err", err)
		return nil, refuse(update.ReasonBadParams)
	}
	// 2. Platform.
	if *p.OS != runtime.GOOS || *p.Arch != runtime.GOARCH {
		return nil, refuse(update.ReasonPlatformMismatch)
	}
	// 3. Both versions orderable; a dev or git-SHA build never self-updates.
	current, err1 := update.ParseVersion(o.Version)
	target, err2 := update.ParseVersion(*p.TargetVersion)
	if err1 != nil || err2 != nil {
		return nil, refuse(update.ReasonVersionNotOrderable)
	}
	// 3a. The offline release signature, over version, platform and digest.
	sigStatus, token := releaseSignature(o, target.String(), want, p.ReleaseSignature)
	if token != "" {
		return nil, refuse(token)
	}
	// 4. Ordering.
	self, err := o.Executable()
	if err != nil {
		return nil, refuse(update.ReasonUnsupportedLayout)
	}
	switch cmp := update.Compare(target, current); {
	case cmp < 0 && !p.Rollback:
		return nil, refuse(update.ReasonDowngradeNotPermitted)
	case cmp == 0:
		have, err := update.FileSHA256(self)
		if err != nil {
			return nil, refuse(update.ReasonUnsupportedLayout)
		}
		if !strings.EqualFold(have, want) {
			return nil, refuse(update.ReasonVersionConflict)
		}
		return map[string]any{
			"outcome": update.OutcomeAlreadyCurrent, "from_version": current.String(),
			"to_version": target.String(), "sha256": have, "signature": string(sigStatus), "starts": 0,
		}, nil
	}
	// 5. No update already in flight.
	dataDir, err := o.DataDir()
	if err != nil {
		return nil, refuse(update.ReasonUnsupportedLayout)
	}
	l := update.NewLayout(dataDir)
	if st, err := update.ReadState(l.StatePath()); err == nil && !st.Phase.Terminal() {
		return nil, refuse(update.ReasonUpdateInProgress)
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, refuse(update.ReasonUpdateInProgress)
	}
	// 6. Nothing else running: a swap restarts the process.
	if o.Runner.Executing(jobID) > 0 {
		return nil, refuse(update.ReasonBusy)
	}
	// 7. Running as the service, from the file the service names.
	targetPath, err := o.Services.Identify(ctx)
	if err != nil {
		log.Warn("agent.update: service identity check failed", "err", err)
		if t := update.TokenOf(err); t != "" {
			return nil, refuse(t)
		}
		return nil, refuse(update.ReasonNotRunningAsService)
	}
	// 8. Artifact URL and size.
	if !strings.HasPrefix(*p.ArtifactURL, "https://") {
		return nil, refuse(update.ReasonBadArtifactURL)
	}
	if *p.ArtifactSize > MaxArtifactBytes {
		return nil, refuse(update.ReasonArtifactTooLarge)
	}
	// 9. Disk.
	need := uint64(*p.ArtifactSize) * diskFactor
	for _, dir := range []string{dataDir, filepath.Dir(targetPath)} {
		if free, err := o.FreeBytes(dir); err != nil || free < need {
			return nil, refuse(update.ReasonInsufficientDisk)
		}
	}

	// 10. Fetch, stage, hand off.
	st := &update.State{
		Phase: update.PhaseStaged, JobID: jobID, FromVersion: current.String(),
		ToVersion: target.String(), SHA256: want, TargetPath: targetPath,
		HealthWindowS: window, Signature: sigStatus,
	}
	if p.RolloutID != nil {
		st.RolloutID = *p.RolloutID
	}
	if token, err := s.stage(ctx, o, l, p, st, self); token != "" {
		log.Warn("agent.update failed while staging", "reason", token, "err", err)
		_ = os.RemoveAll(l.StagedDir())
		return nil, failed(token)
	}
	log.Info("agent.update staged; the helper takes it from here",
		"from", st.FromVersion, "to", st.ToVersion)
	return nil, fmt.Errorf("agent.update handed to the helper: %w", jobs.ErrDeferred)
}

func parse(raw json.RawMessage) (Params, int, string, error) {
	var p Params
	if err := json.Unmarshal(raw, &p); err != nil {
		return p, 0, "", err
	}
	if p.TargetVersion == nil || p.OS == nil || p.Arch == nil || p.ArtifactURL == nil ||
		p.ArtifactSHA256 == nil || p.ArtifactSize == nil {
		return p, 0, "", errors.New("a required member is absent")
	}
	want := strings.ToLower(strings.TrimSpace(*p.ArtifactSHA256))
	if len(want) != 64 {
		return p, 0, "", errors.New("artifact_sha256 is not 64 characters")
	}
	if _, err := hex.DecodeString(want); err != nil {
		return p, 0, "", errors.New("artifact_sha256 is not hexadecimal")
	}
	if *p.ArtifactSize < 1 {
		return p, 0, "", errors.New("artifact_size is below 1")
	}
	window := defaultHealthWindowS
	if p.HealthWindowS != nil {
		window = *p.HealthWindowS
	}
	if window < minHealthWindowS || window > maxHealthWindowS {
		return p, 0, "", errors.New("health_window_s is out of range")
	}
	return p, window, want, nil
}

func releaseSignature(o Options, version, sha string, sig *string) (update.SignatureStatus, string) {
	pub, err := o.ReleaseKey()
	if err != nil {
		return "", update.ReasonSignatureInvalid
	}
	s := ""
	if sig != nil {
		s = *sig
	}
	if update.AuthenticodePinned() {
		// The seam exists; verification does not until Windows is signed.
		return "", update.ReasonSignatureRequired
	}
	return update.CheckReleaseSignature(pub, version, runtime.GOOS, runtime.GOARCH, sha, s)
}

// stage implements spec §4.2 and returns a failure token, "" on success.
func (s *Service) stage(ctx context.Context, o Options, l update.Layout, p Params, st *update.State,
	self string) (string, error) {
	if err := os.MkdirAll(l.Dir, 0o700); err != nil {
		return update.ReasonStagingFailed, err
	}
	if err := secdir.Restrict(l.Dir); err != nil {
		return update.ReasonStagingFailed, err
	}
	_ = os.RemoveAll(l.StagedDir())
	_ = os.Remove(l.DecisionLock())
	_ = os.Remove(l.StatePath())
	for _, d := range []string{l.StagedDir(), l.PrevDir()} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			return update.ReasonStagingFailed, err
		}
	}

	if token, err := download(ctx, o, l, p, st.SHA256); token != "" {
		return token, err
	}
	if err := os.Rename(l.Download(), l.StagedBinary()); err != nil {
		return update.ReasonStagingFailed, err
	}
	_ = os.Chmod(l.StagedBinary(), 0o700)

	// The rollback source, and the binary the helper runs.
	_ = os.Remove(l.PrevBinary())
	if err := copyFile(self, l.PrevBinary()); err != nil {
		return update.ReasonStagingFailed, err
	}
	prevSHA, err := update.FileSHA256(l.PrevBinary())
	if err != nil {
		return update.ReasonStagingFailed, err
	}
	st.PrevSHA256 = prevSHA
	if err := o.CheckPrev(ctx, l.PrevBinary(), o.Version); err != nil {
		return update.ReasonDataDirNoexec, err
	}
	if err := update.WriteState(l.StatePath(), st); err != nil {
		return update.ReasonStagingFailed, err
	}
	if err := o.Services.LaunchHelper(ctx, l.PrevBinary(), update.HelperApply, l,
		filepath.Dir(st.TargetPath)); err != nil {
		_ = os.Remove(l.StatePath())
		return update.ReasonHelperLaunchFailed, err
	}
	return "", nil
}

func download(ctx context.Context, o Options, l update.Layout, p Params, want string) (string, error) {
	body, err := o.Fetch(ctx, *p.ArtifactURL)
	if err != nil {
		return update.ReasonDownloadFailed, err
	}
	defer body.Close()
	f, err := os.OpenFile(l.Download(), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return update.ReasonStagingFailed, err
	}
	h := sha256.New()
	n, err := io.Copy(io.MultiWriter(f, h), io.LimitReader(body, *p.ArtifactSize+1))
	serr := f.Sync()
	cerr := f.Close()
	if err != nil {
		_ = os.Remove(l.Download())
		return update.ReasonDownloadFailed, err
	}
	if serr != nil || cerr != nil {
		_ = os.Remove(l.Download())
		return update.ReasonStagingFailed, errors.Join(serr, cerr)
	}
	if n != *p.ArtifactSize {
		_ = os.Remove(l.Download())
		return update.ReasonSizeMismatch, nil
	}
	// Constant-time, and neither digest is echoed.
	if got := hex.EncodeToString(h.Sum(nil)); !update.DigestEqual(got, want) {
		_ = os.Remove(l.Download())
		return update.ReasonHashMismatch, nil
	}
	return "", nil
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o700)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	if err := out.Sync(); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}
