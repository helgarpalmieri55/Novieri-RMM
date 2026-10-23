//go:build !linux && !darwin && !windows

package patch

import (
	"context"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

type noRestarter struct{}

func osRestarter() restarter { return noRestarter{} }

func (noRestarter) Session(context.Context) session { return session{} }

func (noRestarter) Schedule(context.Context, time.Duration, string) error {
	return jobs.Refuse("patch: this agent cannot restart this OS; nothing was restarted")
}

func bootTime(time.Time) (time.Time, bool) { return time.Time{}, false }
