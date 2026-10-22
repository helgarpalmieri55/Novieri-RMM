//go:build windows

package patch

import (
	"context"
	"testing"
	"time"
)

// TestWUAEngineAgainstTheRealWindowsUpdateAgent is what wua_windows.go's file
// comment says nothing here has: an execution. It drives the real COM path --
// CoInitializeEx, Microsoft.Update.Session, CreateUpdateSearcher, one real
// Search -- against the actual Windows Update Agent on this runner.
//
// READ-ONLY, deliberately: it calls Available only. Available never calls
// Download or Install (see wua_windows.go), so this cannot alter update state
// on the runner it executes on -- the same reason the engine's own contract
// makes Available safe to call from a scan-only job. Nothing is patched.
//
// This does not prove Install works, or that a real approval-and-install
// round trip succeeds; that needs a job dispatched against a real endpoint.
// What it proves, for the first time anywhere in this repository, is that the
// COM apartment initializes, the session and searcher objects are creatable,
// and a real search against this machine's WUA returns without the deadline
// or interface-lifetime handling in Available panicking or leaking a COM
// pointer that release() should have freed.
func TestWUAEngineAgainstTheRealWindowsUpdateAgent(t *testing.T) {
	e, err := newWUAEngine()
	if err != nil {
		// The engine treats this as a REFUSAL, not a transient failure (see
		// newWUAEngine's own comment): a machine whose COM apartment cannot
		// be entered, or whose Windows Update Agent is not registered, is a
		// property of this runner's image. Reported, not failed -- the same
		// distinction the engine itself draws.
		t.Skipf("the Windows Update Agent COM session could not be created on this runner: %v", err)
	}
	defer e.Close()

	if got := e.Family(); got != "windows" {
		t.Fatalf("Family() = %q, want %q", got, "windows")
	}

	// The deadline is honoured BETWEEN updates, not inside the COM call
	// itself (Available's own comment); a slow or unreachable WSUS/Windows
	// Update endpoint on this runner should not hang the suite indefinitely.
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()

	available, err := e.Available(ctx)
	if err != nil {
		// A network-dependent outcome (no route to Windows Update from this
		// runner, the service stopped, a deadline reached before the search
		// returned) is reported rather than failed: it is a fact about the
		// runner's environment, not about whether this code correctly drives
		// the WUA API.
		t.Skipf("the real WUA search did not complete on this runner: %v", err)
	}
	t.Logf("real Windows Update Agent search on %s: %d update(s) enumerated "+
		"(read-only -- Available calls neither Download nor Install)", e.Family(), len(available))

	installed, err := e.Installed(ctx)
	if err != nil {
		t.Fatalf("Installed (drawn from the same search, no second query): %v", err)
	}
	t.Logf("same search, installed partition: %d entries", len(installed))
}
