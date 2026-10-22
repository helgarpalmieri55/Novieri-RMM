// Command badclient must not compile.
//
// It stands in for a capability package trying to hand itself a job that was
// never verified. construct_test.go builds it and asserts the build FAILS
// because jobs.VerifiedJob carries an unexported method no outside type can
// supply. The go tool ignores directories named testdata, so this file is
// invisible to `go build ./...`, `go vet ./...` and staticcheck.
//
// If this file ever starts compiling, invariant #1 is no longer enforced by the
// type system on the endpoint: any package in this binary could construct a
// job that claims to have had its signature checked.
//
// HISTORY. docs/modules/agent/plan.md specified VerifiedJob as a struct with an
// unexported zero-size field. This file compiled against that design, because
// Go forbids SPECIFYING an unexported field from another package, not OMITTING
// it -- `jobs.VerifiedJob{ID: "forged"}` was legal. The interface is the fix.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// forged claims to be a verified job without ever having had a signature
// checked. It supplies every exported method; it cannot supply the unexported
// one, so it does not satisfy jobs.VerifiedJob.
type forged struct{}

func (forged) ID() string              { return "forged" }
func (forged) Type() string            { return "script.run" }
func (forged) Device() string          { return "any" }
func (forged) Params() json.RawMessage { return json.RawMessage(`{"cmd":"rm -rf /"}`) }
func (forged) IssuedAt() time.Time     { return time.Now() }
func (forged) ExpiresAt() time.Time    { return time.Now().Add(time.Hour) }
func (forged) Nonce() string           { return "forged" }

// THE FORGE. This assignment must not compile.
var _ jobs.VerifiedJob = forged{}

func main() {
	var h jobs.Handler = func(_ context.Context, j jobs.VerifiedJob) (any, error) {
		return nil, nil
	}
	out, err := h(context.Background(), forged{})
	fmt.Println(out, err)
}
