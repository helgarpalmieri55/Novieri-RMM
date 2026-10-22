//go:build darwin

package patch

import (
	"context"
	"os"
)

// macOS selects softwareupdate.
//
// The existence check is on the absolute path rather than through PATH: on
// macOS, softwareupdate is always /usr/sbin/softwareupdate, and a binary found
// somewhere else under that name is not the one this engine means.
func osEngine(ctx context.Context) (engine, error) {
	if _, err := os.Stat(softwareUpdate); err != nil {
		return nil, errNoEngine(softwareUpdate + " is not present")
	}
	return newWithApps(newMacEngine(), homebrewSlot()), nil
}
