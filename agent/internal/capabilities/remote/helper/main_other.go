//go:build !windows

package helper

import (
	"fmt"
	"os"
)

// Main is the `remote-helper` subcommand. Only Windows has a helper in this
// build; elsewhere remote.screen.open is not registered, so nothing launches
// one.
func Main() int {
	fmt.Fprintln(os.Stderr, "remote-helper: screen sharing is not available on this platform")
	return 2
}
