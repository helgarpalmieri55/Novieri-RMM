package backup

import (
	"context"
	"runtime"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// Registration must stay a map insert: Bind wires the seams and must not dial,
// open a file, or start a goroutine.
//
// s.run (not the exported s.Run) is what every other test in this package
// calls: jobs.VerifiedJob has an unexported method, so nothing outside
// internal/jobs can construct one, and that includes this package's own
// tests (see capabilities/integration_test.go's comment). run(ctx, jobID,
// raw) is the internal entry point that takes job params directly, and
// testJobID/runParamsJSON are helpers.go's fixtures for exactly that call.
func TestBindWiresTheSeamsAndStartsNothing(t *testing.T) {
	before := runtime.NumGoroutine()
	s := &Service{}

	// Unbound: both capabilities refuse, and the refusal names what is missing.
	if _, err := s.run(context.Background(), testJobID, runParamsJSON(t, []string{"/no/such/root"}, nil)); err == nil {
		t.Fatal("an unbound service accepted backup.run")
	}

	s.Bind(Options{
		QueryChunks: func(context.Context, wire.ChunkQuery) (wire.ChunkGrant, error) {
			return wire.ChunkGrant{}, nil
		},
		OpenStore: func(context.Context, Repository) (ObjectStore, error) {
			return nil, nil
		},
	})
	runtime.Gosched()
	if got := runtime.NumGoroutine(); got > before {
		t.Errorf("goroutines %d -> %d; Bind started one", before, got)
	}
	if s.Runs() != 0 {
		t.Errorf("runs = %d after Bind, want 0", s.Runs())
	}
}
