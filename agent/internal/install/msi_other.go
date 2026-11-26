//go:build !windows

package install

import (
	"fmt"
	"os"
)

// RunMsi is Windows-only: the MSI exists for Windows alone.
func RunMsi(_ string) int {
	fmt.Fprintln(os.Stderr, "novieri-agent --msi-install runs on Windows, from the Novieri RMM MSI.")
	return 2
}
