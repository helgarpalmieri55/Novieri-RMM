//go:build !windows && !darwin && !linux

package backup

import (
	"context"
	"runtime"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// PlatformSnapshot refuses on a platform with no snapshot provider.
//
// It exists so main.go can wire Options.OpenSnapshot unconditionally: a build
// for an unsupported GOOS refuses at the seam rather than failing to compile.
func PlatformSnapshot(_ context.Context, kind string, _ []string) (Snapshot, error) {
	return nil, jobs.Refuse("backup.run: this build targets %s, which has no volume snapshot "+
		"provider. Backing up the live filesystem instead would produce a backup point that "+
		"restores torn files and reports success", runtime.GOOS)
}
