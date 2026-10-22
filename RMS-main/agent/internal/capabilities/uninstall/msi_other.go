//go:build !windows

package uninstall

import (
	"fmt"
	"os"
)

// MsiMain is Windows-only: an MSI exists for Windows alone.
func MsiMain() int {
	fmt.Fprintln(os.Stderr, "its-agent --msi-uninstall runs on Windows.")
	return 2
}
