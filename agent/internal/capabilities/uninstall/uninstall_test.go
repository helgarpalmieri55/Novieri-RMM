package uninstall_test

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"runtime"
	"sync"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/canonical"
	"github.com/toeshee/it-suite/agent/internal/capabilities"
	"github.com/toeshee/it-suite/agent/internal/capabilities/uninstall"
	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

const testDeviceID = "6f1a2c34-5b6d-4e7f-8a9b-0c1d2e3f4a5b"

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

type launchCall struct{ binary, dataDir string }

type fakeLauncher struct {
	mu    sync.Mutex
	calls []launchCall
	err   error
}

func (f *fakeLauncher) launch(_ context.Context, binary, dataDir string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, launchCall{binary, dataDir})
	return f.err
}

func (f *fakeLauncher) seen() []launchCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]launchCall(nil), f.calls...)
}

func bound(f *fakeLauncher) *uninstall.Service {
	return uninstall.New(uninstall.Options{
		Launch:  f.launch,
		Binary:  func() (string, error) { return "/usr/local/bin/its-agent", nil },
		DataDir: "/var/lib/its-agent",
		Logger:  quiet(),
	})
}

func TestTheJobTypeIsTheOneTheServerRegisters(t *testing.T) {
	if uninstall.JobType != "agent.uninstall" {
		t.Fatalf("JobType = %q; the server registers \"agent.uninstall\"", uninstall.JobType)
	}
}

func TestThisBuildAdvertisesIt(t *testing.T) {
	// The control plane sends `agent.uninstall` ONLY to an agent that
	// advertised it (device_review.can_uninstall); an older one is revoked
	// and removed by hand. Advertising it is therefore what makes Reject
	// clean on this OS.
	if !uninstall.Supported {
		t.Skipf("agent.uninstall is not supported on %s", runtime.GOOS)
	}
	if h, ok := capabilities.Lookup(uninstall.JobType); !ok || h == nil {
		t.Fatal("agent.uninstall is not in this build's registry")
	}
}

func TestASignedJobSchedulesTheHelperAndAnswersScheduled(t *testing.T) {
	f := &fakeLauncher{}
	svc := bound(f)
	h := newRunnerHarness(t, map[string]jobs.Handler{uninstall.JobType: svc.Run})
	h.deliver(t, uninstall.JobType, `{}`, "n-ok")
	res := h.awaitResult(t)
	if res["state"] != "succeeded" {
		t.Fatalf("state = %v, want succeeded; frames: %v", res["state"], res)
	}
	body, _ := res["result"].(map[string]any)
	if body["scheduled"] != true {
		t.Fatalf("result = %v, want {scheduled: true}", body)
	}
	if got := f.seen(); len(got) != 1 || got[0] != (launchCall{"/usr/local/bin/its-agent", "/var/lib/its-agent"}) {
		t.Fatalf("launcher calls = %v; the helper is told THIS agent's binary and data directory, "+
			"and nothing from the job", got)
	}
	if svc.Runs() != 1 {
		t.Fatalf("Runs() = %d, want 1", svc.Runs())
	}
}

func TestUnknownParamsAreIgnoredButANonObjectIsRefused(t *testing.T) {
	// The inbound half of the wire-protocol rule: a member a newer server adds
	// is ignored, so a strictly additive change does not break this fleet. A
	// params value that is not an object is not an additive change.
	f := &fakeLauncher{}
	h := newRunnerHarness(t, map[string]jobs.Handler{uninstall.JobType: bound(f).Run})
	h.deliver(t, uninstall.JobType, `{"future":1}`, "n-extra")
	if res := h.awaitResult(t); res["state"] != "succeeded" {
		t.Fatalf("an unknown member was not ignored: %v", res)
	}

	g := &fakeLauncher{}
	h2 := newRunnerHarness(t, map[string]jobs.Handler{uninstall.JobType: bound(g).Run})
	h2.deliver(t, uninstall.JobType, `[]`, "n-array")
	if res := h2.awaitResult(t); res["state"] != "refused" {
		t.Fatalf("state = %v, want refused", res["state"])
	}
	if len(g.seen()) != 0 {
		t.Fatal("the helper was launched for params that were refused")
	}
}

func TestUnboundItRefusesAndNamesWhy(t *testing.T) {
	h := newRunnerHarness(t, map[string]jobs.Handler{uninstall.JobType: uninstall.New(uninstall.Options{}).Run})
	h.deliver(t, uninstall.JobType, `{}`, "n-unbound")
	res := h.awaitResult(t)
	body, _ := res["result"].(map[string]any)
	if res["state"] != "refused" || body["reason"] == nil {
		t.Fatalf("an unbound capability must refuse with a reason, got %v", res)
	}
}

func TestAHelperThatDidNotStartIsAFailureNotAScheduledRemoval(t *testing.T) {
	f := &fakeLauncher{err: errors.New("systemd-run: not found")}
	h := newRunnerHarness(t, map[string]jobs.Handler{uninstall.JobType: bound(f).Run})
	h.deliver(t, uninstall.JobType, `{}`, "n-fail")
	if res := h.awaitResult(t); res["state"] != "failed" {
		t.Fatalf("state = %v, want failed: the server would otherwise read `succeeded` as "+
			"confirmation and revoke a certificate that is still installed", res["state"])
	}
}

func TestBindingStartsNothing(t *testing.T) {
	// Idle budget: registration is a map insert and Bind stores options. No
	// goroutine, no helper, nothing until a verified job arrives.
	before := runtime.NumGoroutine()
	f := &fakeLauncher{}
	svc := uninstall.New(uninstall.Options{})
	svc.Bind(uninstall.Options{Launch: f.launch, Binary: func() (string, error) { return "x", nil }, DataDir: "y"})
	if after := runtime.NumGoroutine(); after > before {
		t.Fatalf("Bind started %d goroutine(s)", after-before)
	}
	if len(f.seen()) != 0 || svc.Runs() != 0 {
		t.Fatal("Bind ran the capability")
	}
}

// ---------------------------------------------------------------- harness
//
// THIS FILE CANNOT FORGE A jobs.VerifiedJob: the only way to hand the
// capability a job is to sign a real envelope and put it through the real
// runner, which is the same path a control plane's Reject takes.

type runnerHarness struct {
	runner *jobs.Runner
	sender *recordingSender
	priv   ed25519.PrivateKey
}

func newRunnerHarness(t *testing.T, registry map[string]jobs.Handler) *runnerHarness {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	sender := &recordingSender{}
	r, err := jobs.New(jobs.Options{
		BaseContext: t.Context(),
		DeviceID:    testDeviceID,
		SigningKey:  pub,
		Nonces:      &permissiveNonces{seen: map[string]bool{}},
		Registry:    mapRegistry(registry),
		Sender:      sender,
		Logger:      quiet(),
	})
	if err != nil {
		t.Fatal(err)
	}
	return &runnerHarness{runner: r, sender: sender, priv: priv}
}

func (h *runnerHarness) deliver(t *testing.T, jobType, params, nonce string) {
	t.Helper()
	now := time.Now().UTC()
	job := fmt.Sprintf(
		`{"id":%q,"type":%q,"device":%q,"params":%s,"issued_at":%q,"expires_at":%q,"nonce":%q}`,
		"9c3f0a11-0000-4000-8000-00000000c0de", jobType, testDeviceID, params,
		now.Add(-time.Second).Format("2006-01-02T15:04:05Z"),
		now.Add(10*time.Minute).Format("2006-01-02T15:04:05Z"),
		nonce,
	)
	msg, err := canonical.BytesFromRaw(json.RawMessage(job))
	if err != nil {
		t.Fatalf("canonicalising the fixture failed: %v", err)
	}
	sig := base64.StdEncoding.EncodeToString(ed25519.Sign(h.priv, msg))
	h.runner.Handle(context.Background(), []byte(fmt.Sprintf(`{"v":1,"kind":"job","job":%s,"sig":%q}`, job, sig)))
}

func (h *runnerHarness) awaitResult(t *testing.T) map[string]any {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		for _, f := range h.sender.frames() {
			if f["kind"] == "job_result" {
				return f
			}
		}
		if time.Now().After(deadline) {
			t.Fatalf("no job_result within 5s; frames: %v", h.sender.frames())
		}
		time.Sleep(2 * time.Millisecond)
	}
}

type mapRegistry map[string]jobs.Handler

func (m mapRegistry) Lookup(jobType string) (jobs.Handler, bool) {
	h, ok := m[jobType]
	return h, ok
}

type permissiveNonces struct {
	mu   sync.Mutex
	seen map[string]bool
}

func (p *permissiveNonces) SeenOrRecord(nonce string, _ time.Time) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.seen[nonce] {
		return true
	}
	p.seen[nonce] = true
	return false
}

type recordingSender struct {
	mu   sync.Mutex
	sent []map[string]any
}

func (r *recordingSender) Send(_ context.Context, msg any) error {
	b, err := wire.Encode(msg)
	if err != nil {
		return err
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		return err
	}
	r.mu.Lock()
	r.sent = append(r.sent, m)
	r.mu.Unlock()
	return nil
}

func (r *recordingSender) frames() []map[string]any {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]map[string]any(nil), r.sent...)
}
