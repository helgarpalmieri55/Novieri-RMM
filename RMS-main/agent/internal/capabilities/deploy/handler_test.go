package deploy

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// THE REGISTRATION SHAPE, AND THE RESULT THE GATEWAY WILL ACTUALLY RECEIVE.
//
// registry.go is owned by another agent in this cycle, so the two lines that
// register these capabilities are not committed here. What IS committed is the
// assertion that those exact lines will compile and behave — a registration
// that does not typecheck, or a handler that returns something the runner
// cannot report, is a job that hangs until it expires with nobody told why.

// The registrations these capabilities are for, verbatim:
//
//	must(r.Register(deploy.JobTypeScript, deploy.Process.Run))
//	must(r.Register(deploy.JobTypeDeploy, deploy.Process.Deploy))
//
// Both method values are taken here, so the compiler checks the shape.
var (
	_ jobs.Handler = Process.Run
	_ jobs.Handler = Process.Deploy
)

func TestTheJobTypesAreTheOnesTheServerRegisters(t *testing.T) {
	// app/modules/deploy/handler.py::register_job_types. A mismatch means the
	// runner refuses `unknown_type` for every dispatch, forever, and no test on
	// either side goes red.
	if JobTypeScript != "script.run" {
		t.Errorf("JobTypeScript = %q", JobTypeScript)
	}
	if JobTypeDeploy != "app.deploy" {
		t.Errorf("JobTypeDeploy = %q", JobTypeDeploy)
	}
}

// A jobs.VerifiedJob CANNOT BE CONSTRUCTED HERE, and that is the point:
// VerifiedJob is an interface with an unexported method, so no type outside
// internal/jobs can satisfy it. Invariant #1, made structural — this package
// cannot be handed anything unverified even by its own tests.
//
// So the two exported handlers are exercised through `handleScript` and
// `handleDeploy`, which are `Run` and `Deploy` with the VerifiedJob already
// unwrapped. Everything the handlers do beyond calling `job.Params()` lives
// there.

func TestBothHandlersReturnAJSONObjectTheRunnerCanReport(t *testing.T) {
	// internal/jobs' resultBody marshals the handler's return value and fails
	// the job if it is not a JSON OBJECT. A capability that returned a slice,
	// or a value with an unmarshalable field, produces "capability returned an
	// unreportable result" — and the operator sees a failed job with no
	// installer output at all.
	//
	// FALSIFYING OBSERVATION: a result that does not round-trip into
	// map[string]any, or one whose members are not the schema's.
	svc := New(Options{
		GOOS: "linux", LookPath: fakePaths("bash"), Logger: quiet(),
		Exec: func(context.Context, Command) (Outcome, error) {
			return Outcome{ExitCode: 0, Output: "hello"}, nil
		},
	})
	sres, err := svc.runScript(context.Background(), scriptParamsJSON(t, "echo x", nil))
	if err != nil {
		t.Fatal(err)
	}
	assertJSONObject(t, "ScriptRunResult", sres, []string{"outcome"})

	a := newArtifact("installer")
	opts, _, _ := msiJob(t, a, a.sha)
	dres, err := New(opts).deploy(context.Background(), msiParams(t, a.sha))
	if err != nil {
		t.Fatal(err)
	}
	assertJSONObject(t, "AppDeployResult", dres, []string{"outcomes"})
}

func assertJSONObject(t *testing.T, name string, v any, wantKeys []string) {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("%s is not JSON-encodable: %v", name, err)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("%s does not decode into a JSON object; JobResultMsg.result must be one: %v",
			name, err)
	}
	if len(m) != len(wantKeys) {
		t.Errorf("%s marshalled to %d members, want %d: %s", name, len(m), len(wantKeys), raw)
	}
	for _, k := range wantKeys {
		if _, ok := m[k]; !ok {
			t.Errorf("%s is missing %q: %s", name, k, raw)
		}
	}
}

func TestTheHandlersIncrementTheirOwnCounterAndOnlyOnSuccess(t *testing.T) {
	// The counters are what a dormancy assertion outside this package reads, so
	// a job that was refused must not move one. Driven through handleScript,
	// which is Run with the VerifiedJob unwrapped (see the note above).
	svc := New(Options{
		GOOS: "linux", LookPath: fakePaths("bash"), Logger: quiet(),
		Exec: func(context.Context, Command) (Outcome, error) { return Outcome{}, nil },
	})
	if _, err := svc.handleScript(context.Background(), scriptParamsJSON(t, "x", nil)); err != nil {
		t.Fatal(err)
	}
	if svc.Scripts() != 1 {
		t.Fatalf("Scripts() = %d after one successful run", svc.Scripts())
	}
	if svc.Deploys() != 0 {
		t.Fatalf("a script run moved the deploy counter")
	}
	if _, err := svc.handleScript(context.Background(), json.RawMessage(`{}`)); err == nil {
		t.Fatal("an empty params document was accepted")
	}
	if svc.Scripts() != 1 {
		t.Fatalf("Scripts() = %d after a refusal", svc.Scripts())
	}
}
