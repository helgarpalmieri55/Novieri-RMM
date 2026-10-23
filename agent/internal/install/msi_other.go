//go:build !windows

package install

import (
	"fmt"
	"os"
)

// RunMsi is Windows-only: the MSI exists for Windows alone.
func RunMsi(_ string) int {
	fmt.Fprintln(os.Stderr, "its-agent --msi-install runs on Windows, from the ITMS MSI.")
	return 2
}
