package selfupdate

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/update"
)

const (
	running = "1.0.0"
	jobID   = "0192a000-0000-7000-8000-0000000000aa"
)

var newBinary = []byte("the new agent binary")

func digest(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

type countingFetcher struct {
	mu    sync.Mutex
	calls int
	body  []byte
	err   error
}

func (f *countingFetcher) fetch(context.Context, string) (io.ReadCloser, error) {
	f.mu.Lock()
	f.calls++
	f.mu.Unlock()
	if f.err != nil {
		return nil, f.err
	}
	return io.NopCloser(bytes.NewReader(f.body)), nil
}

type fakeServices struct {
	target      string
	identifyErr error
	launchErr   error
	launched    []string
}

func (f *fakeServices) Identify(context.Context) (string, error) { return f.target, f.identifyErr }
func (f *fakeServices) LaunchHelper(_ context.Context, prev, phase string, _ update.Layout, _ string) error {
	f.launched = append(f.launched, phase+" "+prev)
	return f.launchErr
}
func (f *fakeServices) StopForSwap() bool             { return false }
func (f *fakeServices) Stop(context.Context) error    { return nil }
func (f *fakeServices) Start(context.Context) error   { return nil }
func (f *fakeServices) Restart(context.Context) error { return nil }
func (f *fakeServices) HelperExiting(context.Context) {}

type rig struct {
	t        *testing.T
	s        *Service
	dataDir  string
	self     string
	fetcher  *countingFetcher
	services *fakeServices
	busy     int
	free     uint64
	key      ed25519.PublicKey
	priv     ed25519.PrivateKey
	prevOK   error
}

func newRig(t *testing.T) *rig {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	r := &rig{
		t:       t,
		dataDir: t.TempDir(),
		fetcher: &countingFetcher{body: newBinary},
		free:    1 << 40,
		key:     pub,
		priv:    priv,
	}
	r.self = filepath.Join(t.TempDir(), update.BinaryName())
	if err := os.WriteFile(r.self, []byte("the running agent binary"), 0o755); err != nil {
		t.Fatal(err)
	}
	r.services = &fakeServices{target: r.self}
	r.s = &Service{}
	r.s.Bind(Options{
		Version:    running,
		Runner:     executingFunc(func(string) int { return r.busy }),
		DataDir:    func() (string, error) { return r.dataDir, nil },
		Services:   r.services,
		Fetch:      r.fetcher.fetch,
		FreeBytes:  func(string) (uint64, error) { return r.free, nil },
		Executable: func() (string, error) { return r.self, nil },
		ReleaseKey: func() (ed25519.PublicKey, error) { return r.key, nil },
		CheckPrev:  func(context.Context, string, string) error { return r.prevOK },
		Logger:     slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	return r
}

type executingFunc func(string) int

func (f executingFunc) Executing(exclude string) int { return f(exclude) }

func (r *rig) sign(version, sha string) string {
	return base64.StdEncoding.EncodeToString(ed25519.Sign(r.priv,
		update.ReleaseMessage(version, runtime.GOOS, runtime.GOARCH, sha)))
}

func (r *rig) params(mut func(p map[string]any)) json.RawMessage {
	sha := digest(newBinary)
	p := map[string]any{
		"target_version":    "1.1.0",
		"os":                runtime.GOOS,
		"arch":              runtime.GOARCH,
		"artifact_url":      "https://objects.example.test/agent-releases/1.1.0/its-agent?X-Amz-Signature=abc",
		"artifact_sha256":   sha,
		"artifact_size":     len(newBinary),
		"rollback":          false,
		"health_window_s":   600,
		"rollout_id":        nil,
		"release_signature": r.sign("1.1.0", sha),
	}
	if mut != nil {
		mut(p)
	}
	b, err := json.Marshal(p)
	if err != nil {
		r.t.Fatal(err)
	}
	return b
}

func (r *rig) run(raw json.RawMessage) (any, error) {
	return r.s.update(r.t.Context(), jobID, raw)
}

func refusalOf(err error) string {
	var ref *jobs.Refusal
	if errors.As(err, &ref) {
		return ref.Reason
	}
	return ""
}

func (r *rig) layout() update.Layout { return update.NewLayout(r.dataDir) }

func (r *rig) assertNothingStaged(why string) {
	r.t.Helper()
	if r.fetcher.calls != 0 {
		r.t.Fatalf("%s: %d downloads", why, r.fetcher.calls)
	}
	r.assertNoTrace(why)
}

func (r *rig) assertNoTrace(why string) {
	r.t.Helper()
	l := r.layout()
	for _, p := range []string{l.StagedBinary(), l.StatePath(), l.Download()} {
		if _, err := os.Stat(p); err == nil {
			r.t.Fatalf("%s: %s exists", why, p)
		}
	}
	if len(r.services.launched) != 0 {
		r.t.Fatalf("%s: a helper was launched", why)
	}
}

// Spec §4.1, one test per refusal, in order; each asserts no network call.
func TestRefusalsHappenBeforeAnyDownload(t *testing.T) {
	cases := []struct {
		name  string
		setup func(r *rig)
		mut   func(p map[string]any)
		want  string
	}{
		{"missing target_version", nil, func(p map[string]any) { delete(p, "target_version") }, update.ReasonBadParams},
		{"short digest", nil, func(p map[string]any) { p["artifact_sha256"] = "abc" }, update.ReasonBadParams},
		{"non-hex digest", nil, func(p map[string]any) { p["artifact_sha256"] = strings.Repeat("z", 64) }, update.ReasonBadParams},
		{"health window too short", nil, func(p map[string]any) { p["health_window_s"] = 60 }, update.ReasonBadParams},
		{"zero size", nil, func(p map[string]any) { p["artifact_size"] = 0 }, update.ReasonBadParams},
		{"wrong os", nil, func(p map[string]any) { p["os"] = otherOS() }, update.ReasonPlatformMismatch},
		{"wrong arch", nil, func(p map[string]any) { p["arch"] = otherArch() }, update.ReasonPlatformMismatch},
		{"target not semver", nil, func(p map[string]any) { p["target_version"] = "abc1234" }, update.ReasonVersionNotOrderable},
		{"running a dev build", func(r *rig) { r.s.opts.Version = "dev" }, nil, update.ReasonVersionNotOrderable},
		{"no signature", nil, func(p map[string]any) { p["release_signature"] = nil }, update.ReasonSignatureRequired},
		{"forged signature", nil, func(p map[string]any) {
			p["release_signature"] = base64.StdEncoding.EncodeToString(make([]byte, 64))
		}, update.ReasonSignatureInvalid},
		{"downgrade", nil, func(p map[string]any) {
			p["target_version"] = "0.9.0"
			p["release_signature"] = nil
		}, update.ReasonSignatureRequired},
		{"update in progress", func(r *rig) {
			l := r.layout()
			_ = os.MkdirAll(l.Dir, 0o700)
			_ = update.WriteState(l.StatePath(), &update.State{Phase: update.PhaseTrial})
		}, nil, update.ReasonUpdateInProgress},
		{"busy", func(r *rig) { r.busy = 1 }, nil, update.ReasonBusy},
		{"not a service", func(r *rig) {
			r.services.identifyErr = &update.Failure{Token: update.ReasonNotRunningAsService}
		}, nil, update.ReasonNotRunningAsService},
		{"layout", func(r *rig) {
			r.services.identifyErr = &update.Failure{Token: update.ReasonUnsupportedLayout}
		}, nil, update.ReasonUnsupportedLayout},
		{"http url", nil, func(p map[string]any) { p["artifact_url"] = "http://objects.example.test/x" }, update.ReasonBadArtifactURL},
		{"too large", nil, func(p map[string]any) { p["artifact_size"] = MaxArtifactBytes + 1 }, update.ReasonArtifactTooLarge},
		{"disk", func(r *rig) { r.free = uint64(3*len(newBinary)) - 1 }, nil, update.ReasonInsufficientDisk},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := newRig(t)
			if c.setup != nil {
				c.setup(r)
			}
			res, err := r.run(r.params(c.mut))
			if got := refusalOf(err); got != c.want {
				t.Fatalf("refusal = %q (err %v, result %v), want %q", got, err, res, c.want)
			}
			if c.name == "update in progress" {
				if r.fetcher.calls != 0 || len(r.services.launched) != 0 {
					t.Fatal("acted on a refused job")
				}
				return
			}
			r.assertNothingStaged(c.name)
		})
	}
}

func TestADowngradeNeedsTheRollbackFlag(t *testing.T) {
	r := newRig(t)
	sha := digest(newBinary)
	raw := r.params(func(p map[string]any) {
		p["target_version"] = "0.9.0"
		p["release_signature"] = r.sign("0.9.0", sha)
	})
	if _, err := r.run(raw); refusalOf(err) != update.ReasonDowngradeNotPermitted {
		t.Fatalf("err = %v", err)
	}
	r.assertNothingStaged("downgrade")

	raw = r.params(func(p map[string]any) {
		p["target_version"] = "0.9.0"
		p["rollback"] = true
		p["release_signature"] = r.sign("0.9.0", sha)
	})
	if _, err := r.run(raw); !errors.Is(err, jobs.ErrDeferred) {
		t.Fatalf("rollback: err = %v", err)
	}
}

func TestSameVersionSameDigestIsAlreadyCurrent(t *testing.T) {
	r := newRig(t)
	self, _ := os.ReadFile(r.self)
	sha := digest(self)
	res, err := r.run(r.params(func(p map[string]any) {
		p["target_version"] = running
		p["artifact_sha256"] = sha
		p["artifact_size"] = len(self)
		p["release_signature"] = r.sign(running, sha)
	}))
	if err != nil {
		t.Fatal(err)
	}
	body, _ := res.(map[string]any)
	if body["outcome"] != update.OutcomeAlreadyCurrent || body["signature"] != "verified" {
		t.Fatalf("result = %v", res)
	}
	r.assertNothingStaged("already current")
}

func TestSameVersionDifferentDigestIsAConflict(t *testing.T) {
	r := newRig(t)
	_, err := r.run(r.params(func(p map[string]any) {
		p["target_version"] = running
		p["release_signature"] = r.sign(running, digest(newBinary))
	}))
	if refusalOf(err) != update.ReasonVersionConflict {
		t.Fatalf("err = %v", err)
	}
	r.assertNothingStaged("conflict")
}

func TestAWrongDigestLeavesNothingBehind(t *testing.T) {
	r := newRig(t)
	// Same length, different bytes.
	r.fetcher.body = bytes.ToUpper(newBinary)
	_, err := r.run(r.params(nil))
	if err == nil || err.Error() != update.ReasonHashMismatch {
		t.Fatalf("err = %v", err)
	}
	r.assertNoTrace("hash mismatch")
}

func TestAWrongSizeLeavesNothingBehind(t *testing.T) {
	r := newRig(t)
	r.fetcher.body = append(append([]byte{}, newBinary...), 'x')
	_, err := r.run(r.params(nil))
	if err == nil || err.Error() != update.ReasonSizeMismatch {
		t.Fatalf("err = %v", err)
	}
	r.assertNoTrace("size mismatch")
}

func TestADownloadFailureIsAFailureNotARefusal(t *testing.T) {
	r := newRig(t)
	r.fetcher.err = errors.New("connection reset")
	_, err := r.run(r.params(nil))
	if refusalOf(err) != "" || err == nil || err.Error() != update.ReasonDownloadFailed {
		t.Fatalf("err = %v", err)
	}
	r.assertNoTrace("download failure")
}

func TestANoexecDataDirIsCaughtBeforeAnythingChanges(t *testing.T) {
	r := newRig(t)
	r.prevOK = errors.New("permission denied")
	_, err := r.run(r.params(nil))
	if err == nil || err.Error() != update.ReasonDataDirNoexec {
		t.Fatalf("err = %v", err)
	}
	r.assertNoTrace("noexec")
}

func TestStagingHandsOffToTheHelper(t *testing.T) {
	r := newRig(t)
	_, err := r.run(r.params(nil))
	if !errors.Is(err, jobs.ErrDeferred) {
		t.Fatalf("err = %v", err)
	}
	l := r.layout()
	staged, err := os.ReadFile(l.StagedBinary())
	if err != nil || !bytes.Equal(staged, newBinary) {
		t.Fatalf("staged binary: %v", err)
	}
	prev, _ := os.ReadFile(l.PrevBinary())
	self, _ := os.ReadFile(r.self)
	if !bytes.Equal(prev, self) {
		t.Fatal("prev/ is not a copy of the running binary")
	}
	st, err := update.ReadState(l.StatePath())
	if err != nil {
		t.Fatal(err)
	}
	if st.Phase != update.PhaseStaged || st.JobID != jobID || st.FromVersion != running ||
		st.ToVersion != "1.1.0" || st.SHA256 != digest(newBinary) || st.PrevSHA256 != digest(self) ||
		st.TargetPath != r.self || st.HealthWindowS != 600 || st.Signature != update.SignatureVerified {
		t.Fatalf("state = %+v", st)
	}
	if len(r.services.launched) != 1 || r.services.launched[0] != "apply "+l.PrevBinary() {
		t.Fatalf("launched = %v", r.services.launched)
	}
	if runtime.GOOS != "windows" {
		info, _ := os.Stat(l.Dir)
		if info.Mode().Perm() != 0o700 {
			t.Fatalf("update dir mode %v", info.Mode().Perm())
		}
	}
}

func TestAHelperThatCannotLaunchChangesNothing(t *testing.T) {
	r := newRig(t)
	r.services.launchErr = errors.New("systemd-run: denied")
	_, err := r.run(r.params(nil))
	if err == nil || err.Error() != update.ReasonHelperLaunchFailed {
		t.Fatalf("err = %v", err)
	}
	if _, err := os.Stat(r.layout().StatePath()); err == nil {
		t.Fatal("state.json survived a failed launch")
	}
}

func TestATerminalStateFromAnEarlierUpdateDoesNotBlock(t *testing.T) {
	r := newRig(t)
	l := r.layout()
	_ = os.MkdirAll(l.Dir, 0o700)
	_ = update.WriteState(l.StatePath(), &update.State{Phase: update.PhaseRolledBack, Reported: true})
	if _, err := update.TryDecide(l.DecisionLock(), update.DecideRollback); err != nil {
		t.Fatal(err)
	}
	if _, err := r.run(r.params(nil)); !errors.Is(err, jobs.ErrDeferred) {
		t.Fatalf("err = %v", err)
	}
	if update.ReadDecision(l.DecisionLock()) != "" {
		t.Fatal("a stale decision lock would decide the new update")
	}
}

func TestUnboundRefuses(t *testing.T) {
	s := &Service{}
	if _, err := s.update(context.Background(), jobID, json.RawMessage(`{}`)); refusalOf(err) != update.ReasonUnbound {
		t.Fatalf("err = %v", err)
	}
}

func TestResultCarriesNoURL(t *testing.T) {
	r := newRig(t)
	self, _ := os.ReadFile(r.self)
	sha := digest(self)
	res, _ := r.run(r.params(func(p map[string]any) {
		p["target_version"] = running
		p["artifact_sha256"] = sha
		p["artifact_size"] = len(self)
		p["release_signature"] = r.sign(running, sha)
	}))
	b, _ := json.Marshal(res)
	if strings.Contains(string(b), "X-Amz") || strings.Contains(string(b), "https://") {
		t.Fatalf("result echoes the URL: %s", b)
	}
}

func otherOS() string {
	if runtime.GOOS == "linux" {
		return "darwin"
	}
	return "linux"
}

func otherArch() string {
	if runtime.GOARCH == "amd64" {
		return "arm64"
	}
	return "amd64"
}
