package deploy

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// isRefusal reports whether the runner would report this as `refused` rather
// than `failed`. It uses errors.As against *jobs.Refusal — the same check
// internal/jobs/runner.go makes — so a test asserting "this is a refusal" is
// asserting what the RUNNER will do, not what this package meant.
func isRefusal(err error) bool {
	var r *jobs.Refusal
	return errors.As(err, &r)
}

// Shared apparatus. Two rules for everything in here:
//
//   - A FAKE EXECUTOR RECORDS WHETHER IT RAN. Every security test in this
//     package asserts "the installer did not execute", and the only way to
//     assert that is a spy that counts. "An error was returned" is not the
//     same observation and is the one that lets a hash check be deleted with
//     the suite still green.
//   - THE LOGGER IS CAPTURED, NOT DISCARDED. A discarded logger cannot be
//     searched for a leaked script body.

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// logSpy captures every log record this package emits so a test can search it.
type logSpy struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func newLogSpy() *logSpy { return &logSpy{} }

func (l *logSpy) logger() *slog.Logger {
	return slog.New(slog.NewTextHandler(l, &slog.HandlerOptions{Level: slog.LevelDebug}))
}

func (l *logSpy) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.buf.Write(p)
}

func (l *logSpy) String() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.buf.String()
}

// execSpy is an Executor that records every Command it was asked to run and
// returns a canned Outcome.
type execSpy struct {
	mu   sync.Mutex
	runs []Command
	out  Outcome
	err  error
}

func (e *execSpy) exec(_ context.Context, c Command) (Outcome, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.runs = append(e.runs, c)
	return e.out, e.err
}

func (e *execSpy) calls() []Command {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]Command(nil), e.runs...)
}

// ranNothing is THE assertion the hash tests are built on.
func (e *execSpy) ranNothing(t *testing.T, why string) {
	t.Helper()
	if runs := e.calls(); len(runs) != 0 {
		t.Fatalf("%s, and yet %d process(es) were spawned: %+v. Returning an error is not the "+
			"same observation as not executing", why, len(runs), runs)
	}
}

// fakePaths resolves the named programs and nothing else, so a planner test can
// pretend to be on a Windows box with winget installed while running on Linux.
func fakePaths(names ...string) LookPath {
	set := map[string]bool{}
	for _, n := range names {
		set[n] = true
	}
	return func(name string) (string, error) {
		if set[name] {
			return "/fake/" + name, nil
		}
		return "", errNotFound
	}
}

func nothingInstalled() LookPath {
	return func(string) (string, error) { return "", errNotFound }
}

type notFound struct{}

func (notFound) Error() string { return "executable file not found" }

var errNotFound = notFound{}

// scriptParamsJSON builds a ScriptRunParams document. `extra` overrides or adds
// members so a test can send a document a valid struct could not produce.
func scriptParamsJSON(t *testing.T, body string, extra map[string]any) json.RawMessage {
	t.Helper()
	doc := map[string]any{
		"interpreter":    "bash",
		"body":           body,
		"script_id":      "11111111-2222-3333-4444-555555555555",
		"script_version": 7,
	}
	for k, v := range extra {
		if v == nil {
			delete(doc, k)
			continue
		}
		doc[k] = v
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// deployParamsJSON builds an AppDeployParams document.
func deployParamsJSON(t *testing.T, extra map[string]any) json.RawMessage {
	t.Helper()
	doc := map[string]any{
		"action":         "install",
		"installer_type": "msi",
	}
	for k, v := range extra {
		if v == nil {
			delete(doc, k)
			continue
		}
		doc[k] = v
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// refusalReason returns the Refusal's reason, or fails the test if err is not
// a refusal. The DISTINCTION IS THE SUBJECT of several tests: `refused` takes a
// device out of rotation for some modules and `failed` does not.
func refusalReason(t *testing.T, err error) string {
	t.Helper()
	if err == nil {
		t.Fatal("no error at all; a refusal was expected")
	}
	r, ok := err.(interface{ Error() string })
	if !ok {
		t.Fatalf("%v is not an error", err)
	}
	if !isRefusal(err) {
		t.Fatalf("error %q is a plain failure, not a refusal. `refused` and `failed` are "+
			"different answers to the control plane and at least one module acts on the "+
			"difference", r.Error())
	}
	return r.Error()
}

func assertNotRefusal(t *testing.T, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("no error at all")
	}
	if isRefusal(err) {
		t.Fatalf("error %q is a REFUSAL. A transient condition must be `failed`: a refusal asks "+
			"to be taken out of rotation", err.Error())
	}
}

// assertAbsent is the leak check, factored out so every call site fails with
// the same sentence.
func assertAbsent(t *testing.T, haystack, needle, where string) {
	t.Helper()
	if needle == "" {
		t.Fatal("the needle is empty; this assertion would be vacuous")
	}
	if strings.Contains(haystack, needle) {
		t.Fatalf("%s contains the secret %q. A script body and its arguments carry credentials "+
			"and must never be logged, echoed into an error, or put in a refusal reason.\n"+
			"--- %s ---\n%s", where, needle, where, haystack)
	}
}
