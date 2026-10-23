package update

import (
	"runtime"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/budget"
)

// countingClock counts timers so the budget test can assert none is armed.
type countingClock struct{ timers int }

func (c *countingClock) Now() time.Time { return time.Time{} }

func (c *countingClock) AfterFunc(time.Duration, func()) Stopper {
	c.timers++
	return time.NewTimer(time.Hour)
}

// Spec §9: with no update pending, Resume is one os.Stat -- no goroutine, no
// timer, under 512 B/op. FALSIFYING OBSERVATION: B/op >= 512, a goroutine
// delta, or a timer armed.
func TestFalsifier_ResumeWithNoStateFileIsIdle(t *testing.T) {
	// A production-length data directory that holds no update. t.TempDir()
	// is far longer on macOS and Windows and would measure the test runner's
	// path, not the agent.
	dir := "/var/lib/its-agent-budget-probe"
	if runtime.GOOS == "windows" {
		dir = `C:\ProgramData\ITSuite\agent-budget-probe`
	}
	clock := &countingClock{}
	r := budget.Measure("resume without state", func() func() {
		return func() {
			if s := Resume(ResumeOptions{Version: "1.0.0", DataDir: dir, Clock: clock}); s != nil {
				panic("a session appeared with no state file")
			}
		}
	})
	budget.AssertNoGoroutineGrowth(t, r, 0)
	if r.BytesPerOp >= 512 {
		t.Fatalf("Resume with no state file costs %d B/op, budget is < 512", r.BytesPerOp)
	}
	if clock.timers != 0 {
		t.Fatalf("Resume armed %d timers with no update pending", clock.timers)
	}
	t.Logf("self-update idle budget: %d allocs/op, %d B/op, goroutine delta %d, timers %d",
		r.AllocsPerOp, r.BytesPerOp, r.Goroutines, clock.timers)
}
