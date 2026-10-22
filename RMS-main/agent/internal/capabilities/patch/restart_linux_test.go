//go:build linux

package patch

import (
	"testing"
	"time"
)

func TestBootTimeIsInThePastAndStable(t *testing.T) {
	now := time.Now()
	a, ok := bootTime(now)
	if !ok || !a.Before(now) {
		t.Fatalf("boot time %v ok=%v", a, ok)
	}
	b, _ := bootTime(now)
	if d := a.Sub(b); d > time.Second || d < -time.Second {
		t.Fatalf("two reads differ by %v", d)
	}
}
