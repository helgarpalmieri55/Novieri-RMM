package capabilities

import (
	"context"
	"encoding/json"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// `hello` -- the kernel's liveness job.
//
// It is registered in app/jobs/registry.py as a Phase 0 kernel job type and
// scripts/demo_e2e_agent.py dispatches it before anything else, asserting
//
//	final["result"].get("greeting") == "hello from the control plane"
//
// The plan's Task 7 listed "the hello job succeeds end-to-end" in its
// definition of done, but Task 7 shipped with an EMPTY registry in main.go, so
// nothing could answer it. It is here, with the registry, because that is the
// first commit in which a capability can exist at all.
//
// It is not a placeholder for a shell: it echoes one signed string and touches
// nothing on the host. Everything a real capability must not do -- read a file,
// start a process, keep state -- it does not do, which is what makes it a
// liveness check rather than a foothold.

// helloParams mirrors app/jobs/registry.py::HelloParams.
//
// Inbound is LOOSE (docs/kernel/wire-protocol/README.md): encoding/json ignores
// members this struct does not declare, so a control plane that adds an
// optional field does not take an old fleet offline. What it does NOT do is
// invent a message -- an absent one falls back to the server's own default.
type helloParams struct {
	Message *string `json:"message"`
}

// helloDefaultMessage is HelloParams.message's server-side default. An old
// server that dispatched `{}` still gets an answer.
const helloDefaultMessage = "ping"

// Hello is the `hello` capability.
func Hello(_ context.Context, job jobs.VerifiedJob) (any, error) {
	return map[string]any{"greeting": greeting(job.Params())}, nil
}

// greeting is the whole of hello's logic, split out because a capability's
// parameter handling can only be tested through the runner -- jobs.VerifiedJob
// has no constructor outside internal/jobs -- and this half deserves a direct
// test as well as the end-to-end one in integration_test.go.
func greeting(params json.RawMessage) string {
	var parsed helloParams
	if err := json.Unmarshal(params, &parsed); err != nil || parsed.Message == nil {
		// Unparseable or absent: the server's default. NOT an error -- a
		// liveness job that fails because the params moved is a liveness job
		// that reports a healthy agent as broken.
		return helloDefaultMessage
	}
	return *parsed.Message
}
