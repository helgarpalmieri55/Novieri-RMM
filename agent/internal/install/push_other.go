//go:build !linux && !darwin

package install

import (
	"fmt"
	"os"
)

// RunPush is the Linux and macOS install mode (push_unix.go). Windows is
// installed by the self-installer (Run) or the WinRM push's fixed PowerShell.
func RunPush(_ []string, _ string) int {
	fmt.Fprintln(os.Stderr, "its-agent install runs on Linux and macOS.")
	return 2
}
