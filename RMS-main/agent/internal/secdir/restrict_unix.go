//go:build !windows

package secdir

import (
	"fmt"
	"os"
)

// Restrict makes a job's scratch directory owner-only.
//
// os.MkdirTemp already creates 0700, so this is a re-assertion rather than a
// change — and it is here because a re-assertion is cheap and because the
// Windows half of this pair does real work. A umask cannot widen a mode set
// this way (Chmod is not masked), so the mode after this call is 0700
// regardless of how the service was started.
//
// EXECUTED IN CI, and TestTheScratchDirectoryIsOwnerOnly reads the mode back
// rather than trusting the call.
func Restrict(dir string) error {
	if err := os.Chmod(dir, 0o700); err != nil {
		return fmt.Errorf("scratch directory mode could not be set: %w", err)
	}
	return nil
}
